import logging

import numpy as np
import torch
import torch.utils.data as data
import torchvision.transforms as transforms
from PIL import Image
from torch.utils.data import RandomSampler, SequentialSampler, DataLoader
from torchvision import datasets
from torchvision.datasets.samplers import DistributedSampler

from fedml_api.data_preprocessing.cifar10.datasets import CIFAR10_truncated

logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.INFO)


# generate the non-IID distribution for all methods
def read_data_distribution(filename='./data_preprocessing/non-iid-distribution/CIFAR10/distribution.txt'):
    distribution = {}
    with open(filename, 'r') as data:
        for x in data.readlines():
            if '{' != x[0] and '}' != x[0]:
                tmp = x.split(':')
                if '{' == tmp[1].strip():
                    first_level_key = int(tmp[0])
                    distribution[first_level_key] = {}
                else:
                    second_level_key = int(tmp[0])
                    distribution[first_level_key][second_level_key] = int(tmp[1].strip().replace(',', ''))
    return distribution


def read_net_dataidx_map(filename='./data_preprocessing/non-iid-distribution/CIFAR10/net_dataidx_map.txt'):
    net_dataidx_map = {}
    with open(filename, 'r') as data:
        for x in data.readlines():
            if '{' != x[0] and '}' != x[0] and ']' != x[0]:
                tmp = x.split(':')
                if '[' == tmp[-1].strip():
                    key = int(tmp[0])
                    net_dataidx_map[key] = []
                else:
                    tmp_array = x.split(',')
                    net_dataidx_map[key] = [int(i.strip()) for i in tmp_array]
    return net_dataidx_map


def record_net_data_stats(y_train, net_dataidx_map):
    net_cls_counts = {}

    for net_i, dataidx in net_dataidx_map.items():
        unq, unq_cnt = np.unique(y_train[dataidx], return_counts=True)
        tmp = {unq[i]: unq_cnt[i] for i in range(len(unq))}
        net_cls_counts[net_i] = tmp
    logging.debug('Data statistics: %s' % str(net_cls_counts))
    return net_cls_counts


class Cutout(object):
    def __init__(self, length):
        self.length = length

    def __call__(self, img):
        h, w = img.size(1), img.size(2)
        mask = np.ones((h, w), np.float32)
        y = np.random.randint(h)
        x = np.random.randint(w)

        y1 = np.clip(y - self.length // 2, 0, h)
        y2 = np.clip(y + self.length // 2, 0, h)
        x1 = np.clip(x - self.length // 2, 0, w)
        x2 = np.clip(x + self.length // 2, 0, w)

        mask[y1: y2, x1: x2] = 0.
        mask = torch.from_numpy(mask)
        mask = mask.expand_as(img)
        img *= mask
        return img


def _data_transforms_cifar10(args=None):
    CIFAR_MEAN = [0.49139968, 0.48215827, 0.44653124]
    CIFAR_STD = [0.24703233, 0.24348505, 0.26158768]

    if args is not None:
        train_transform = transforms.Compose([
            # transforms.ToPILImage(),
            transforms.RandomResizedCrop((args.img_size, args.img_size), scale=(0.05, 1.0)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
        ])

        valid_transform = transforms.Compose([
            # transforms.ToPILImage(),
            transforms.Resize((args.img_size, args.img_size)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
        ])
    else:
        train_transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ])

        train_transform.transforms.append(Cutout(16))

        valid_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ])

    return train_transform, valid_transform


def load_cifar10_data(datadir, args):
    train_transform, test_transform = _data_transforms_cifar10(args)

    cifar10_train_ds = CIFAR10_truncated(datadir, train=True, download=True, transform=train_transform)
    cifar10_test_ds = CIFAR10_truncated(datadir, train=False, download=True, transform=test_transform)

    X_train, y_train = cifar10_train_ds.data, cifar10_train_ds.target
    X_test, y_test = cifar10_test_ds.data, cifar10_test_ds.target

    return (X_train, y_train, X_test, y_test)


def partition_data(dataset, datadir, partition, n_nets, alpha, args=None):
    logging.info("*********partition data***************")
    X_train, y_train, X_test, y_test = load_cifar10_data(datadir, args)
    n_train = X_train.shape[0]
    # n_test = X_test.shape[0]

    if partition == "homo":
        total_num = n_train
        idxs = np.random.permutation(total_num)
        batch_idxs = np.array_split(idxs, n_nets)
        net_dataidx_map = {i: batch_idxs[i] for i in range(n_nets)}

    elif partition == "hetero":
        min_size = 0
        K = 10
        N = y_train.shape[0]
        logging.info("N = " + str(N))
        net_dataidx_map = {}

        while min_size < 10:
            idx_batch = [[] for _ in range(n_nets)]
            # for each class in the dataset
            for k in range(K):
                idx_k = np.where(y_train == k)[0]
                np.random.shuffle(idx_k)
                proportions = np.random.dirichlet(np.repeat(alpha, n_nets))
                ## Balance
                proportions = np.array([p * (len(idx_j) < N / n_nets) for p, idx_j in zip(proportions, idx_batch)])
                proportions = proportions / proportions.sum()
                proportions = (np.cumsum(proportions) * len(idx_k)).astype(int)[:-1]
                idx_batch = [idx_j + idx.tolist() for idx_j, idx in zip(idx_batch, np.split(idx_k, proportions))]
                min_size = min([len(idx_j) for idx_j in idx_batch])

        for j in range(n_nets):
            np.random.shuffle(idx_batch[j])
            net_dataidx_map[j] = idx_batch[j]

    elif partition == "hetero-fix":
        dataidx_map_file_path = './data_preprocessing/non-iid-distribution/CIFAR10/net_dataidx_map.txt'
        net_dataidx_map = read_net_dataidx_map(dataidx_map_file_path)

    if partition == "hetero-fix":
        distribution_file_path = './data_preprocessing/non-iid-distribution/CIFAR10/distribution.txt'
        traindata_cls_counts = read_data_distribution(distribution_file_path)
    else:
        traindata_cls_counts = record_net_data_stats(y_train, net_dataidx_map)

    return X_train, y_train, X_test, y_test, net_dataidx_map, traindata_cls_counts


# for centralized training
def get_dataloader(dataset, datadir, train_bs, test_bs, args=None, dataidxs=None):
    return get_dataloader_CIFAR10(datadir, train_bs, test_bs, args=args, dataidxs=dataidxs)


# for local devices
def get_dataloader_test(dataset, datadir, train_bs, test_bs, dataidxs_train, dataidxs_test, args=None):
    return get_dataloader_test_CIFAR10(datadir, train_bs, test_bs, dataidxs_train, dataidxs_test, args)


def get_dataloader_CIFAR10(datadir, train_bs, test_bs, args=None, dataidxs=None):
    dl_obj = CIFAR10_truncated

    transform_train, transform_test = _data_transforms_cifar10(args=args)

    train_ds = dl_obj(datadir, dataidxs=dataidxs, train=True, transform=transform_train, download=True)
    test_ds = dl_obj(datadir, train=False, transform=transform_test, download=True)

    train_dl = data.DataLoader(dataset=train_ds, batch_size=train_bs, shuffle=True, drop_last=True)
    test_dl = data.DataLoader(dataset=test_ds, batch_size=test_bs, shuffle=False, drop_last=True)

    return train_dl, test_dl


def get_dataloader_test_CIFAR10(datadir, train_bs, test_bs, dataidxs_train=None, dataidxs_test=None, args=None):
    dl_obj = CIFAR10_truncated

    transform_train, transform_test = _data_transforms_cifar10(args)

    train_ds = dl_obj(datadir, dataidxs=dataidxs_train, train=True, transform=transform_train, download=True)
    test_ds = dl_obj(datadir, dataidxs=dataidxs_test, train=False, transform=transform_test, download=True)

    train_dl = data.DataLoader(dataset=train_ds, batch_size=train_bs, shuffle=True, drop_last=True)
    test_dl = data.DataLoader(dataset=test_ds, batch_size=test_bs, shuffle=False, drop_last=True)

    return train_dl, test_dl


def load_partition_data_distributed_cifar10(process_id, dataset, data_dir, partition_method, partition_alpha,
                                            client_number, batch_size, args=None):
    X_train, y_train, X_test, y_test, net_dataidx_map, traindata_cls_counts = partition_data(dataset,
                                                                                             data_dir,
                                                                                             partition_method,
                                                                                             client_number,
                                                                                             partition_alpha, args=args)
    class_num = len(np.unique(y_train))
    logging.info("traindata_cls_counts = " + str(traindata_cls_counts))
    train_data_num = sum([len(net_dataidx_map[r]) for r in range(client_number)])

    # get global test data
    if process_id == 0:
        train_data_global, test_data_global = get_dataloader(dataset, data_dir, batch_size, batch_size, args=args)
        logging.info("train_dl_global number = " + str(len(train_data_global)))
        logging.info("test_dl_global number = " + str(len(test_data_global)))
        train_data_local = None
        test_data_local = None
        local_data_num = 0
    else:
        # get local dataset
        dataidxs = net_dataidx_map[process_id - 1]
        local_data_num = len(dataidxs)
        logging.info("rank = %d, local_sample_number = %d" % (process_id, local_data_num))
        # training batch size = 64; algorithms batch size = 32
        train_data_local, test_data_local = get_dataloader(dataset, data_dir, batch_size, batch_size,
                                                           args=args, dataidxs=dataidxs)
        logging.info("process_id = %d, batch_num_train_local = %d, batch_num_test_local = %d" % (
            process_id, len(train_data_local), len(test_data_local)))
        train_data_global = None
        test_data_global = None
    return train_data_num, train_data_global, test_data_global, local_data_num, train_data_local, test_data_local, class_num


def load_partition_data_cifar10(dataset, data_dir, partition_method, partition_alpha, client_number, batch_size,
                                args=None):
    X_train, y_train, X_test, y_test, net_dataidx_map, traindata_cls_counts = partition_data(dataset,
                                                                                             data_dir,
                                                                                             partition_method,
                                                                                             client_number,
                                                                                             partition_alpha, args=args)
    class_num = len(np.unique(y_train))
    logging.info("traindata_cls_counts = " + str(traindata_cls_counts))
    train_data_num = sum([len(net_dataidx_map[r]) for r in range(client_number)])

    train_data_global, test_data_global = get_dataloader(dataset, data_dir, batch_size, batch_size, args=args)
    logging.info("train_dl_global number = " + str(len(train_data_global)))
    logging.info("test_dl_global number = " + str(len(test_data_global)))
    test_data_num = len(test_data_global)

    # get local dataset
    data_local_num_dict = dict()
    train_data_local_dict = dict()
    test_data_local_dict = dict()

    for client_idx in range(client_number):
        dataidxs = net_dataidx_map[client_idx]
        local_data_num = len(dataidxs)
        data_local_num_dict[client_idx] = local_data_num
        logging.info("client_idx = %d, local_sample_number = %d" % (client_idx, local_data_num))

        # training batch size = 64; algorithms batch size = 32
        train_data_local, test_data_local = get_dataloader(dataset, data_dir, batch_size, batch_size,
                                                           args=args, dataidxs=dataidxs)
        logging.info("client_idx = %d, batch_num_train_local = %d, batch_num_test_local = %d" % (
            client_idx, len(train_data_local), len(test_data_local)))
        train_data_local_dict[client_idx] = train_data_local
        test_data_local_dict[client_idx] = test_data_local
    return train_data_num, test_data_num, train_data_global, test_data_global, \
           data_local_num_dict, train_data_local_dict, test_data_local_dict, class_num


def load_cifar10_centralized_training_for_vit(args):
    if args.is_distributed == 1:
        torch.distributed.barrier()

    transform_train = transforms.Compose([
        transforms.RandomResizedCrop((args.img_size, args.img_size), scale=(0.05, 1.0)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ])
    transform_test = transforms.Compose([
        transforms.Resize((args.img_size, args.img_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ])

    if args.dataset == "cifar10":
        trainset = datasets.CIFAR10(root=args.data_dir,
                                    train=True,
                                    download=True,
                                    transform=transform_train)
        testset = datasets.CIFAR10(root=args.data_dir,
                                   train=False,
                                   download=True,
                                   transform=transform_test) if args.is_distributed == 0 else None

    else:
        trainset = datasets.CIFAR100(root=args.data_dir,
                                     train=True,
                                     download=True,
                                     transform=transform_train)
        testset = datasets.CIFAR100(root=args.data_dir,
                                    train=False,
                                    download=True,
                                    transform=transform_test) if args.is_distributed == 0 else None
    if args.is_distributed == 1:
        torch.distributed.barrier()

    train_sampler = RandomSampler(trainset) if args.is_distributed == 0 else DistributedSampler(trainset)
    test_sampler = SequentialSampler(testset)
    train_loader = DataLoader(trainset,
                              sampler=train_sampler,
                              batch_size=args.batch_size,
                              num_workers=4,
                              pin_memory=True)
    test_loader = DataLoader(testset,
                             sampler=test_sampler,
                             batch_size=args.batch_size,
                             num_workers=4,
                             pin_memory=True) if testset is not None else None

    return train_loader, test_loader