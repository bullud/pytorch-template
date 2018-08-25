import click
import json
import numpy as np
import pandas as pd
from pathlib import Path
import lockfile
from logzero import logger
from tqdm import tqdm
import os
import subprocess
from sklearn.model_selection import train_test_split
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

from src import utils, data_utils, models

ROOT = '/opt/airbus-ship-detection/'

params = {
    'ex_name': __file__[:-3],  # remove '.py'
    'seed': 123456789,
    'lr': 1e-4,
    'batch_size': 8,
    'test_batch_size': 8,
    'optimizer': 'momentum',
    'epochs': 10,
    'workers': 8,
    'dropout': 0.3,
    'wd': 1e-5,
}


@click.group()
def cli():
    if not Path(ROOT + f'experiments/{params["ex_name"]}/train').exists():
        Path(ROOT + f'experiments/{params["ex_name"]}/train').mkdir(parents=True)
    if not Path(ROOT + f'experiments/{params["ex_name"]}/tuning').exists():
        Path(ROOT + f'experiments/{params["ex_name"]}/tuning').mkdir(parents=True)
    if not Path(ROOT + f'experiments/{params["ex_name"]}/tmp').exists():
        Path(ROOT + f'experiments/{params["ex_name"]}/tmp').mkdir(parents=True)

    np.random.seed(params['seed'])
    torch.manual_seed(params['seed'])
    torch.cuda.manual_seed_all(params['seed'])
    torch.backends.cudnn.benchmark = True


@cli.command()
@click.option('--tuning', is_flag=True)
@click.option('--params-path', type=click.Path(), default=None, help='json file path for setting parameters')
@click.option('--devices', '-d', type=str, help='comma delimited gpu device list (e.g. "0,1")')
@click.option('--resume', type=str, default=None, help='checkpoint path')
def job(tuning, params_path, devices, resume):

    exp_path = ROOT + f'experiments/{params["ex_name"]}/'
    os.environ['CUDA_VISIBLE_DEVICES'] = devices

    global params
    if tuning:
        with open(params_path, 'r') as f:
            params = json.load(f)
        mode_str = 'tuning'
        setting = '_'.join(f'{tp}-{params[tp]}' for tp in params['tuning_params'])
    else:
        mode_str = 'train'
        setting = ''

    writer = utils.set_logger(log_dir=exp_path + f'{mode_str}/log/',
                              tf_board_dir=exp_path + f'{mode_str}/tf_board/')

    train_df = pd.read_csv(ROOT + 'data/train.csv')
    train_df, val_df = train_test_split(train_df, test_size=1024, random_state=114514)

    model = models.UNet(in_channels=3, n_classes=2, depth=4, ch_first=32, padding=True,
                        batch_norm=False, up_mode='upconv').cuda()

    optimizer = utils.get_optim(model, params)

    if resume is not None:
        model, optimizer = utils.load_checkpoint(model, resume, optimizer=optimizer)

    if len(devices.split(',')) > 1:
        model = nn.DataParallel(model)

    data_transforms = {
        'train': transforms.Compose([
            transforms.ToPILImage(),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        ]),
        'val': transforms.Compose([
            transforms.ToPILImage(),
            transforms.ToTensor(),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        ]),
    }
    image_datasets = {'train': data_utils.CSVDataset(train_df, data_transforms['train']),
                      'val': data_utils.CSVDataset(val_df, data_transforms['val'])}
    data_loaders = {'train': DataLoader(image_datasets['train'],
                                        batch_size=params['batch_size'], pin_memory=True,
                                        shuffle=True, drop_last=True, num_workers=params['workers']),
                    'val': DataLoader(image_datasets['val'], batch_size=params['test_batch_size'],
                                      pin_memory=True, shuffle=False, num_workers=params['workers'])}

    criterion = nn.CrossEntropyLoss()
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[7, 9], gamma=0.1)

    for epoch in range(params['epochs']):
        logger.info(f'Epoch {epoch}/{params["epochs"]} | lr: {optimizer.param_groups[0]["lr"]}')

        # ============================== train ============================== #
        model.train(True)

        losses = utils.AverageMeter()
        prec1 = utils.AverageMeter()

        for i, (x, y) in tqdm(enumerate(data_loaders['train']),
                              total=len(data_loaders['train']), miniters=50):
            x = x.cuda()
            y = y.cuda(non_blocking=True)

            outputs = model(x)
            loss = criterion(outputs, y)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            acc = utils.accuracy(outputs, y)
            losses.update(loss.item(), x.size(0))
            prec1.update(acc.item(), x.size(0))

        train_loss = losses.avg
        train_acc = prec1.avg

        # ============================== validation ============================== #
        model.train(False)
        losses.reset()
        prec1.reset()

        for i, (x, y) in tqdm(enumerate(data_loaders['val']), total=len(data_loaders['val'])):
            x = x.cuda()
            y = y.cuda(non_blocking=True)

            with torch.no_grad():
                outputs = model(x)
                loss = criterion(outputs, y)

            acc = utils.accuracy(outputs, y)
            losses.update(loss.item(), x.size(0))
            prec1.update(acc.item(), x.size(0))

        val_loss = losses.avg
        val_acc = prec1.avg

        writer.add_scalars('Loss', {'train': train_loss}, epoch)
        writer.add_scalars('Acc', {'train': train_acc}, epoch)
        writer.add_scalars('Loss', {'val': val_loss}, epoch)
        writer.add_scalars('Acc', {'val': val_acc}, epoch)
        writer.add_scalar('LR', optimizer.param_groups[0]['lr'], epoch)

        scheduler.step()

        if not tuning:
            utils.save_checkpoint(model, epoch, exp_path + 'model_optim.pth', optimizer)

    if tuning:
        row = pd.DataFrame()
        for key, val in params.items():
            if key in params['tuning_params']:
                row[key] = [val]

        row['train_loss'] = [train_loss]

        df_path = exp_path + f'{mode_str}/results.csv'
        with lockfile.FileLock(df_path):
            df_results = pd.read_csv(df_path)
            df_results = pd.concat([df_results, row], sort=False).reset_index(drop=True)
            df_results.to_csv(df_path, index=None)


@cli.command()
@click.option('--mode', type=str, default='grid', help='Search method (tuning)')
@click.option('--n-iter', type=int, default=8, help='n of iteration for random parameter search (tuning)')
@click.option('--n-gpu', type=int, default=-1, help='n of used gpu at once')
@click.option('--devices', '-d', type=str, help='comma delimited gpu device list (e.g. "0,1")')
def tuning(mode, n_iter, n_gpu, devices):
    if n_gpu == -1:
        n_gpu = len(devices.split(','))
    space = {
        'lr': [1e-5, 1e-4, 1e-3],
        'batch_size': [16, 8],
    }
    utils.launch_tuning(mode, n_iter, n_gpu, devices, params, space, ROOT, metrics=('train_loss', 'acc'))


@cli.command()
@click.option('--model-path', '-m', type=str)
@click.option('--devices', '-d', type=str, help='comma delimited gpu device list (e.g. "0,1")')
@click.option('--compression', '-c', is_flag=True)
@click.option('--message', '-m', default=None, type=str)
def predict(model_path, devices, compression, message):

    os.environ['CUDA_VISIBLE_DEVICES'] = devices

    test_img_paths = list(map(str, Path(ROOT + 'data/test/').glob('*.jpg')))
    submission = pd.read_csv(ROOT + 'data/sample_submission.csv')

    model = models.UNet(in_channels=3, n_classes=2, depth=4, ch_first=32, padding=True,
                        batch_norm=False, up_mode='upconv').cuda()
    model = utils.load_checkpoint(model, model_path)

    sub_path = ROOT + f'submit/{params["ex_name"]}.csv'
    if compression:
        sub_path += '.gz'
        submission.to_csv(sub_path, index=False, compression='gzip')
    else:
        submission.to_csv(sub_path, index=False)

    if message is None:
        message = params['ex_name']

    cmd = f'kaggle c submit -c airbus-ship-detection -f {sub_path} -m "{message}"'
    subprocess.run(cmd, shell=True)


if __name__ == '__main__':
    cli()
