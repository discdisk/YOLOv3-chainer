import argparse
import os
import random

import numpy as np
import chainer
import chainercv
from chainer import serializers, optimizer_hooks
from chainer.backends import cuda
from chainer import training
from chainer.training import extensions

from lib.links.darknet53 import Darknet53
from lib.links.yolov3 import YOLOv3
from lib.links.loss import YOLOv3Loss
from lib.links.predict import YOLOv3Predictor
from lib.data import load_list
from lib.datasets.yolo_dataset import YOLODataset
from lib.extensions.darknet_shift import DarknetShift
from lib.extensions.crop_size_updater import CropSizeUpdater
from lib.extensions.yolo_detection import YOLODetection
from lib.convert import concat_yolo

def main():
    parser = argparse.ArgumentParser(description='Chainer YOLOv3 Train')
    parser.add_argument('--names')
    parser.add_argument('--train')
    parser.add_argument('--valid', default='')
    parser.add_argument('--detection', default='')
    
    parser.add_argument('--batchsize', '-b', type=int, default=8)
    parser.add_argument('--iteration', '-i', type=int, default=50200)
    parser.add_argument('--gpus', '-g', type=int, nargs='*', default=[])
    parser.add_argument('--out', '-o', default='yolov3-result')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--display_interval', type=int, default=100)
    parser.add_argument('--snapshot_interval', type=int, default=100)
    parser.add_argument('--ignore_thresh', type=float, default=0.5)
    parser.add_argument('--thresh', type=float, default=0.5)
    parser.add_argument('--darknet', default='')
    parser.add_argument('--darknet_class', type=int, default=-1)
    parser.add_argument('--steps', type=int, nargs='*', default=[-10200, -5200])
    parser.add_argument('--scales', type=float, nargs='*', default=[0.1, 0.1])
    args = parser.parse_args()
    
    print('GPUs: {}'.format(args.gpus))
    print('# Minibatch-size: {}'.format(args.batchsize))
    print('# iteration: {}'.format(args.iteration))
    
    class_names = load_list(args.names)
    
    random.seed(args.seed)
    np.random.seed(args.seed)
    
    base = None
    if len(args.darknet) > 0:
        darknet_class = args.darknet_class if args.darknet_class > 0 else len(class_names)
        darknet53 = Darknet53(darknet_class)
        serializers.load_npz(args.darknet, darknet53)
        base = darknet53.base
    yolov3 = YOLOv3(len(class_names), base, ignore_thresh=args.ignore_thresh)
    model = YOLOv3Loss(yolov3)
    device = -1
    if len(args.gpus) > 0:
        device = args.gpus[0]
        cuda.cupy.random.seed(args.seed)
        cuda.get_device_from_id(args.gpus[0]).use()
    if len(args.gpus) == 1:
        model.to_gpu()
    
    optimizer = chainer.optimizers.MomentumSGD(lr=0.001)
    optimizer.setup(model)
    optimizer.add_hook(optimizer_hooks.WeightDecay(0.0005), 'hook_decay')
    optimizer.add_hook(optimizer_hooks.GradientClipping(10.0), 'hook_grad_clip')
    
    train = YOLODataset(args.train, train=True, classifier=False, 
                        jitter=0.3, hue=0.1, sat=1.5, val=1.5)
    #train_iter = chainer.iterators.SerialIterator(train, args.batchsize)
    
    
    train_iter = chainer.iterators.MultiprocessIterator(train, args.batchsize,
                                                        shared_mem=(448**2*3+(1+4)*100)*4)
    
    if len(args.gpus) <= 1:
        updater = training.StandardUpdater(
            train_iter, optimizer, converter=concat_yolo, device=device)
    else:
        devices = {'main': args.gpus[0]}
        for gpu in args.gpus[1:]:
            devices['gpu{}'.format(gpu)] = gpu
        updater = training.ParallelUpdater(
            train_iter, optimizer, converter=concat_yolo, devices=devices)
    trainer = training.Trainer(
        updater, (args.iteration, 'iteration'), out=args.out)
    
    display_interval = (args.display_interval, 'iteration')
    snapshot_interval = (args.snapshot_interval, 'iteration')
    
    print_entries = ['epoch', 'iteration', 'main/loss', 'elapsed_time']
    plot_keys = ['main/loss']
    snapshot_key = 'main/loss'
    
    if len(args.valid) > 0:
        print_entries = ['epoch', 'iteration', 
             'main/loss', 'validation/main/loss', 'elapsed_time']
        plot_keys = ['main/loss', 'validation/main/loss']
        snapshot_key = 'validation/main/loss'
        
        test = YOLODataset(args.valid, train=False, classifier=False)
        test_iter = chainer.iterators.SerialIterator(test, args.batchsize,
                                                     repeat=False, shuffle=False)
        trainer.extend(extensions.Evaluator(
            test_iter, model, converter=concat_yolo, 
            device=device), trigger=display_interval)
    
    trainer.extend(extensions.dump_graph('main/loss'))
    trainer.extend(extensions.LogReport(trigger=display_interval))
    if extensions.PlotReport.available():
        trainer.extend(
            extensions.PlotReport(
                plot_keys, 'iteration',
                display_interval, file_name='loss.png'))
    
    trainer.extend(extensions.PrintReport(print_entries),
                  trigger=display_interval)
    trainer.extend(extensions.ProgressBar(update_interval=1))
    
    trainer.extend(extensions.snapshot_object(
        yolov3, 'yolov3_snapshot.npz'), 
        trigger=training.triggers.MinValueTrigger(
            snapshot_key, snapshot_interval))
    trainer.extend(extensions.snapshot_object(
        yolov3, 'yolov3_backup.npz'), 
        trigger=snapshot_interval)
    trainer.extend(extensions.snapshot_object(
        yolov3, 'yolov3_final.npz'), 
        trigger=(args.iteration, 'iteration'))
    
    steps = args.steps
    for i in range(len(steps)):
        if steps[i] < 0:
            steps[i] = args.iteration + steps[i]
    scales = args.scales
    print('# steps: {}'.format(steps))
    print('# scales: {}'.format(scales))
        
    trainer.extend(DarknetShift(
        optimizer, 'steps', args.iteration, burn_in=1000,
        steps=steps, scales=scales
    ))
    trainer.extend(CropSizeUpdater(train, 
                                   [(10+i)*32 for i in range(0,5)],
                                   args.iteration - 200))
    
    if len(args.detection):
        detector = YOLOv3Predictor(yolov3, thresh=args.thresh)
        trainer.extend(YOLODetection(
            detector, 
            load_list(args.detection),
            class_names, (416, 416),args.thresh,
            trigger=display_interval, device=device
        ))
    
    print('')
    print('RUN')
    print('')
    trainer.run()

if __name__ == '__main__':
    main()