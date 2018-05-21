import re
import os
import shutil
import time
import logging
import parser
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler
import torchvision.datasets
import torch.cuda

from mean_teacher import architectures, datasets, data, losses, ramps, cli
from mean_teacher.run_context import RunContext
from mean_teacher.data import NO_LABEL
from mean_teacher.utils import *
import contextlib
import random


LOG = logging.getLogger('main')

################
# NOTE: To enable logging on IPythonConsole output or IPyNoteBook
# LOG = logging.getLogger()
# LOG.setLevel(logging.DEBUG)
# LOG.debug("test")

# NOTE: To init args to Mean Teacher :
# parser = cli.create_parser()
# parser.set_defaults(dataset='cifar10') # OR any other param
# args = parser.parse_known_args()[0]
################

args = None
best_prec1 = 0
global_step = 0
NA_label = -1
student_pred_match_noNA = 0.0
student_pred_noNA = 0.0
student_true_noNA = 0.0
teacher_pred_match_noNA = 0.0
teacher_pred_noNA = 0.0
teacher_true_noNA = 0.0
###########
# NOTE: To change to a new NEC dataset .. currently some params are hardcoded
# 1. Change args.dataset in the command line
###########


def main(context):
    global global_step
    global best_prec1
    global student_pred_match_noNA
    global student_pred_noNA
    global student_true_noNA
    global teacher_pred_match_noNA
    global teacher_pred_noNA
    global teacher_true_noNA

    time_start = time.time()

    checkpoint_path = context.transient_dir
    training_log = context.create_train_log("training")
    validation_log = context.create_train_log("validation")
    ema_validation_log = context.create_train_log("ema_validation")

    dataset_config = datasets.__dict__[args.dataset]()
    num_classes = dataset_config.pop('num_classes')

    if args.dataset in ['conll', 'ontonotes', 'riedel', 'gids']:
        train_loader, eval_loader, dataset, dataset_test = create_data_loaders(**dataset_config, args=args)
        word_vocab_embed = dataset.word_vocab_embed
        word_vocab_size = dataset.word_vocab.size()
    else:
        train_loader, eval_loader = create_data_loaders(**dataset_config, args=args)

    def create_model(ema=False):
        LOG.info("=> creating {pretrained}{ema}model '{arch}'".format(
            pretrained='pre-trained ' if args.pretrained else '',
            ema='EMA ' if ema else '',
            arch=args.arch))

        model_factory = architectures.__dict__[args.arch]
        model_params = dict(pretrained=args.pretrained, num_classes=num_classes)

        if args.dataset in ['conll', 'ontonotes', 'riedel', 'gids']:
            model_params['word_vocab_embed'] = word_vocab_embed
            model_params['word_vocab_size'] = word_vocab_size
            model_params['wordemb_size'] = args.wordemb_size
            model_params['hidden_size'] = args.hidden_size
            model_params['update_pretrained_wordemb'] = args.update_pretrained_wordemb

        model = model_factory(**model_params)
        LOG.info("--------------------IMPORTANT: REMOVING nn.DataParallel for the moment --------------------")
        if torch.cuda.is_available():
            model = model.cuda()    # Note: Disabling data parallelism for now
        else:
            model = model.cpu()

        if ema:
            for param in model.parameters():
                param.detach_() ##NOTE: Detaches the variable from the gradient computation, making it a leaf .. needed from EMA model

        return model

    model = create_model()
    ema_model = create_model(ema=True)

    LOG.info(parameters_string(model))

    evaldir = os.path.join(dataset_config['datadir'], args.eval_subdir)
    student_pred_file = evaldir + '/' + args.run_name + '_pred_student.tsv'
    teacher_pred_file = evaldir + '/' + args.run_name + '_pred_teacher.tsv'
    with contextlib.suppress(FileNotFoundError):
        os.remove(student_pred_file)
        os.remove(teacher_pred_file)

    if args.dataset in ['conll', 'ontonotes', 'riedel', 'gids'] and args.update_pretrained_wordemb is False:
        ## Note: removing the parameters of embeddings as they are not updated
        # https://discuss.pytorch.org/t/freeze-the-learnable-parameters-of-resnet-and-attach-it-to-a-new-network/949/9
        filtered_parameters = list(filter(lambda p: p.requires_grad, model.parameters()))
        optimizer = torch.optim.SGD(filtered_parameters, args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    nesterov=args.nesterov)
    else:
        optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,
                                nesterov=args.nesterov)

    # optionally resume from a checkpoint
    if args.resume:
        assert os.path.isfile(args.resume), "=> no checkpoint found at '{}'".format(args.resume)
        LOG.info("=> loading checkpoint '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)
        args.start_epoch = checkpoint['epoch']
        global_step = checkpoint['global_step']
        best_prec1 = checkpoint['best_prec1']
        model.load_state_dict(checkpoint['state_dict'])
        ema_model.load_state_dict(checkpoint['ema_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        LOG.info("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))

    cudnn.benchmark = True

    if args.evaluate:
        if args.dataset in ['conll', 'ontonotes']:
            LOG.info("Evaluating the primary model:")
            validate(eval_loader, model, validation_log, global_step, args.start_epoch, dataset, context.result_dir, "student")
            LOG.info("Evaluating the EMA model:")
            validate(eval_loader, ema_model, ema_validation_log, global_step, args.start_epoch, dataset, context.result_dir, "teacher")
        elif args.dataset in ['riedel', 'gids']:
            LOG.info("Evaluating the primary model:")
            validate(eval_loader, model, validation_log, global_step, args.start_epoch, dataset_test, context.result_dir, "student")
            LOG.info("Evaluating the EMA model:")
            validate(eval_loader, ema_model, ema_validation_log, global_step, args.start_epoch, dataset_test, context.result_dir, "teacher")
        return

    for epoch in range(args.start_epoch, args.epochs):
        start_time = time.time()
        # train for one epoch
        train(train_loader, model, ema_model, optimizer, epoch, training_log, dataset)
        LOG.info("--- training epoch in %s seconds ---" % (time.time() - start_time))

        if args.evaluation_epochs and (epoch + 1) % args.evaluation_epochs == 0:
            start_time = time.time()
            LOG.info("Evaluating the primary model:")
            prec1 = validate(eval_loader, model, validation_log, global_step, epoch + 1, dataset_test, context.result_dir, "student")
            LOG.info("Evaluating the EMA model:")
            ema_prec1 = validate(eval_loader, ema_model, ema_validation_log, global_step, epoch + 1, dataset_test, context.result_dir, "teacher")
            LOG.info("--- validation in %s seconds ---" % (time.time() - start_time))
            is_best = ema_prec1 > best_prec1
            best_prec1 = max(ema_prec1, best_prec1)
        else:
            is_best = False

        if args.checkpoint_epochs and (epoch + 1) % args.checkpoint_epochs == 0:
            save_checkpoint({
                'epoch': epoch + 1,
                'global_step': global_step,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'ema_state_dict': ema_model.state_dict(),
                'best_prec1': best_prec1,
                'optimizer' : optimizer.state_dict(),
                'dataset' : args.dataset,
            }, is_best, checkpoint_path, epoch + 1)

    # for testing only .. commented
    # LOG.info("For testing only; Comment the following line of code--------------------------------")
    # validate(eval_loader, model, validation_log, global_step, 0, dataset, context.result_dir, "student")
    LOG.info("--------Total end to end time %s seconds ----------- " % (time.time() - time_start))


def parse_dict_args(**kwargs):
    global args

    def to_cmdline_kwarg(key, value):
        if len(key) == 1:
            key = "-{}".format(key)
        else:
            key = "--{}".format(re.sub(r"_", "-", key))
        value = str(value)
        return key, value

    kwargs_pairs = (to_cmdline_kwarg(key, value)
                    for key, value in kwargs.items())
    cmdline_args = list(sum(kwargs_pairs, ()))
    args = parser.parse_args(cmdline_args)

def create_data_loaders(train_transformation,
                        eval_transformation,
                        datadir,
                        args):

    global NA_label
    traindir = os.path.join(datadir, args.train_subdir)
    evaldir = os.path.join(datadir, args.eval_subdir)

    assert_exactly_one([args.exclude_unlabeled, args.labeled_batch_size])

    if torch.cuda.is_available():
        pin_memory = True
    else:
        pin_memory = False

    if args.dataset in ['conll', 'ontonotes']:

        LOG.info("traindir : " + traindir)
        LOG.info("evaldir : " + evaldir)
        dataset = datasets.NECDataset(traindir, args, train_transformation)
        LOG.info("Type of Noise : "+ dataset.WORD_NOISE_TYPE)
        LOG.info("Size of Noise : "+ str(dataset.NUM_WORDS_TO_REPLACE))

        if args.labels:
            labeled_idxs, unlabeled_idxs = data.relabel_dataset_nlp(dataset, args)

        if args.exclude_unlabeled:
            sampler = SubsetRandomSampler(labeled_idxs)
            batch_sampler = BatchSampler(sampler, args.batch_size, drop_last=True)
        elif args.labeled_batch_size:
            batch_sampler = data.TwoStreamBatchSampler(
                unlabeled_idxs, labeled_idxs, args.batch_size, args.labeled_batch_size)
        else:
            assert False, "labeled batch size {}".format(args.labeled_batch_size)

        train_loader = torch.utils.data.DataLoader(dataset,
                                                   pin_memory,
                                                   batch_sampler=batch_sampler,
                                                   num_workers=args.workers
                                                  )
                                                  # drop_last=False)
                                                  # batch_size=args.batch_size,
                                                  # shuffle=False)

        ################## Using torchtext .. not using this currently ####################################
        # train_loader, _ = BucketIterator.splits(
        #    (dataset, dataset), # we pass in the datasets we want the iterator to draw data from
        #     batch_sizes=(64, 64),
        #     device=-1, # if you want to use the GPU, specify the GPU number here
        #     sort_key=lambda x: len(x.patterns), # the BucketIterator needs to be told what function it should use to group the data.
        #     sort_within_batch=False,
        #     repeat=False # we pass repeat=False because we want to wrap this Iterator layer.
        #     )
        ############################################################################################################

        dataset_test = datasets.NECDataset(evaldir, args, eval_transformation) ## NOTE: test data is the same as train data

        eval_loader = torch.utils.data.DataLoader(dataset_test,
                                                  pin_memory,
                                                  batch_size=args.batch_size,
                                                  shuffle=False,
                                                  num_workers=2 * args.workers,
                                                  drop_last=False)

    elif args.dataset in ['riedel', 'gids']:

        LOG.info("traindir : " + traindir)
        LOG.info("evaldir : " + evaldir)
        dataset = datasets.REDataset(traindir, args, train_transformation, 'train')
        LOG.info("Type of Noise : "+ dataset.WORD_NOISE_TYPE)
        LOG.info("Size of Noise : "+ str(dataset.NUM_WORDS_TO_REPLACE))

        if args.labels:
            labeled_idxs, unlabeled_idxs = data.relabel_dataset_RE(dataset, args)
        if args.exclude_unlabeled or len(unlabeled_idxs) == 0:
            sampler = SubsetRandomSampler(labeled_idxs)
            batch_sampler = BatchSampler(sampler, args.batch_size, drop_last=True)
        elif args.labeled_batch_size:
            batch_sampler = data.TwoStreamBatchSampler(
                unlabeled_idxs, labeled_idxs, args.batch_size, args.labeled_batch_size)
        else:
            assert False, "labeled batch size {}".format(args.labeled_batch_size)

        train_loader = torch.utils.data.DataLoader(dataset,
                                                   pin_memory=pin_memory,
                                                   batch_sampler=batch_sampler,
                                                   num_workers=args.workers
                                                   )
                                                  # drop_last=False)
                                                  # batch_size=args.batch_size,
                                                  # shuffle=False)

        dataset_test = datasets.REDataset(evaldir, args, eval_transformation, args.eval_subdir)

        eval_loader = torch.utils.data.DataLoader(dataset_test,
                                                  pin_memory=pin_memory,
                                                  batch_size=args.batch_size,
                                                  shuffle=False,
                                                  num_workers=2 * args.workers,
                                                  drop_last=False)


        NA_label = dataset.categories.index('NA')

    # https://stackoverflow.com/questions/44429199/how-to-load-a-list-of-numpy-arrays-to-pytorch-dataset-loader
    ## Used for loading the riedel10 arrays into pytorch
    elif args.dataset in ['riedel10']:

        dataset = datasets.RiedelDataset(traindir, train_transformation)

        if args.labels:
            labeled_idxs, unlabeled_idxs = data.relabel_dataset_nlp(dataset, args)
        if args.exclude_unlabeled:
            sampler = SubsetRandomSampler(labeled_idxs)
            batch_sampler = BatchSampler(sampler, args.batch_size, drop_last=True)
        elif args.labeled_batch_size:
            batch_sampler = data.TwoStreamBatchSampler(
                unlabeled_idxs, labeled_idxs, args.batch_size, args.labeled_batch_size)
        else:
            assert False, "labeled batch size {}".format(args.labeled_batch_size)

        train_loader = torch.utils.data.DataLoader(dataset,
                                                   pin_memory,
                                                   batch_sampler=batch_sampler,
                                                   num_workers=args.workers,
                                                   )
                                                   # drop_last=False)
                                                   # batch_size=args.batch_size)
                                                   # shuffle=True)

        dataset_test = datasets.RiedelDataset(evaldir, eval_transformation)

        eval_loader = torch.utils.data.DataLoader(dataset_test,
                                                  pin_memory,
                                                  batch_size=args.batch_size,
                                                  shuffle=False,
                                                  num_workers=2 * args.workers,
                                                  drop_last=False)

    else:

        dataset = torchvision.datasets.ImageFolder(traindir, train_transformation)

        if args.labels:
            with open(args.labels) as f:
                labels = dict(line.split(' ') for line in f.read().splitlines())
            labeled_idxs, unlabeled_idxs = data.relabel_dataset(dataset, labels)

        if args.exclude_unlabeled:
            sampler = SubsetRandomSampler(labeled_idxs)
            batch_sampler = BatchSampler(sampler, args.batch_size, drop_last=True)
        elif args.labeled_batch_size:
            batch_sampler = data.TwoStreamBatchSampler(
                unlabeled_idxs, labeled_idxs, args.batch_size, args.labeled_batch_size)
        else:
            assert False, "labeled batch size {}".format(args.labeled_batch_size)

        train_loader = torch.utils.data.DataLoader(dataset,
                                                   pin_memory,
                                                   batch_sampler=batch_sampler,
                                                   num_workers=args.workers,
                                                   )

        eval_loader = torch.utils.data.DataLoader(
            torchvision.datasets.ImageFolder(evaldir, eval_transformation),
            pin_memory,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=2 * args.workers,  # Needs images twice as fast
            drop_last=False)

    if args.dataset in ['conll', 'ontonotes', 'riedel', 'gids']:
        return train_loader, eval_loader, dataset, dataset_test
    else:
        return train_loader, eval_loader


def update_ema_variables(model, ema_model, alpha, global_step):
    # Use the true average until the exponential average is more correct
    alpha = min(1 - 1 / (global_step + 1), alpha)
    for ema_param, param in zip(ema_model.parameters(), model.parameters()):
        ema_param.data.mul_(alpha).add_(1 - alpha, param.data)


def train(train_loader, model, ema_model, optimizer, epoch, log, dataset):
    global global_step
    global NA_label

    if torch.cuda.is_available():
        class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cuda()
    else:
        class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cpu()

    if args.consistency_type == 'mse':
        consistency_criterion = losses.softmax_mse_loss
    elif args.consistency_type == 'kl':
        consistency_criterion = losses.softmax_kl_loss
    else:
        assert False, args.consistency_type
    residual_logit_criterion = losses.symmetric_mse_loss

    meters = AverageMeterSet()

    # switch to train mode
    model.train() ### From the documentation (nn.module,py) : i) Sets the module in training mode. (ii) This has any effect only on modules such as Dropout or BatchNorm. (iii) Returns: Module: self
    ema_model.train()

    end = time.time()
    #for i, datapoint in enumerate(train_loader):
    for i, datapoint in enumerate(dataset):

        # measure data loading time
        meters.update('data_time', time.time() - end)

        adjust_learning_rate(optimizer, epoch, i, len(train_loader))
        meters.update('lr', optimizer.param_groups[0]['lr'])

        if args.dataset in ['conll', 'ontonotes']:

            input = datapoint[0]
            ema_input = datapoint[1]
            target = datapoint[2]

            ## Input consists of tuple (entity_id, pattern_ids)
            input_entity = input[0]
            input_patterns = input[1]

            ema_input_entity = ema_input[0]
            ema_input_patterns = ema_input[1]

            if torch.cuda.is_available():
                entity_var = torch.autograd.Variable(input_entity).cuda()
                patterns_var = torch.autograd.Variable(input_patterns).cuda()
                ema_entity_var = torch.autograd.Variable(ema_input_entity, volatile=True).cuda()
                ema_patterns_var = torch.autograd.Variable(ema_input_patterns, volatile=True).cuda()

            else:
                entity_var = torch.autograd.Variable(input_entity).cpu()
                patterns_var = torch.autograd.Variable(input_patterns).cpu()
                ema_entity_var = torch.autograd.Variable(ema_input_entity, volatile=True).cpu()
                ema_patterns_var = torch.autograd.Variable(ema_input_patterns, volatile=True).cpu()

        elif args.dataset in ['riedel', 'gids']:
            input = datapoint[0]
            ema_input = datapoint[1]
            target = torch.LongTensor([datapoint[2]])
            print ("input "+ str(input))
            print ("ema_input " + str(ema_input))
            input_entity1 = input[0]
            input_entity2 = input[1]
            input_inbetween_chunk = input[2]
            ema_input_entity1 = ema_input[0]
            ema_input_entity2 = ema_input[1]
            ema_input_inbetween_chunk = ema_input[2]

            if torch.cuda.is_available():
                entity1_var = torch.autograd.Variable(input_entity1).cuda()
                entity2_var = torch.autograd.Variable(input_entity2).cuda()
                inbetween_chunk_var = torch.autograd.Variable(input_inbetween_chunk).cuda()

                ema_entity1_var = torch.autograd.Variable(ema_input_entity1, volatile=True).cuda()
                ema_entity2_var = torch.autograd.Variable(ema_input_entity2, volatile=True).cuda()
                ema_inbetween_chunk_var = torch.autograd.Variable(ema_input_inbetween_chunk, volatile=True).cuda()

            else:
                entity1_var = torch.autograd.Variable(input_entity1).cpu()
                entity2_var = torch.autograd.Variable(input_entity2).cpu()
                inbetween_chunk_var = torch.autograd.Variable(input_inbetween_chunk).cpu()

                ema_entity1_var = torch.autograd.Variable(ema_input_entity1, volatile=True).cpu()
                ema_entity2_var = torch.autograd.Variable(ema_input_entity2, volatile=True).cpu()
                ema_inbetween_chunk_var = torch.autograd.Variable(ema_input_inbetween_chunk, volatile=True).cpu()

        else:
            ((input, ema_input), target) = datapoint

            if torch.cuda.is_available():
                input_var = torch.autograd.Variable(input).cuda()
                ema_input_var = torch.autograd.Variable(ema_input, volatile=True).cuda() ## NOTE: AJAY - volatile: Boolean indicating that the Variable should be used in inference mode,
            else:
                input_var = torch.autograd.Variable(input).cpu()
                ema_input_var = torch.autograd.Variable(ema_input, volatile=True).cpu()

        if torch.cuda.is_available():
            target_var = torch.autograd.Variable(target.cuda())
        else:
            target_var = torch.autograd.Variable(target.cpu())  # todo: not passing the async=True (as above) .. going along with it now .. to check if this is a problem

        minibatch_size = len(target_var)
        labeled_minibatch_size = target_var.data.ne(NO_LABEL).sum()
        assert labeled_minibatch_size > 0
        meters.update('labeled_minibatch_size', labeled_minibatch_size)

        # some debug stmt ... to be removed later
        # num_unlabeled = sum([1 for lbl in datapoint[2].numpy().flatten() if lbl == -1])
        # num_labeled = minibatch_size - num_unlabeled
        # LOG.info("[Batch " + str(i) + "] NumLabeled="+str(num_labeled)+ "; NumUnlabeled="+str(num_unlabeled))

        if args.dataset in ['conll', 'ontonotes'] and args.arch == 'custom_embed':
            # print("entity_var = " + str(entity_var.size()))
            # print("patterns_var = " + str(patterns_var.size()))
            ema_model_out, _, _ = ema_model(ema_entity_var, ema_patterns_var)
            model_out, _, _ = model(entity_var, patterns_var)
        elif args.dataset in ['conll', 'ontonotes'] and args.arch == 'simple_MLP_embed':
            ema_model_out = ema_model(ema_entity_var, ema_patterns_var)
            model_out = model(entity_var, patterns_var)

        elif args.dataset in ['riedel', 'gids'] and args.arch == 'lstm_RE':
            ema_model_out = ema_model(ema_entity1_var, ema_entity2_var, ema_inbetween_chunk_var)
            model_out = model(entity1_var, entity2_var, inbetween_chunk_var)

        elif args.dataset in ['riedel', 'gids'] and args.arch == 'simple_MLP_embed_RE':
            ema_model_out = ema_model(ema_entity1_var, ema_entity2_var, ema_inbetween_chunk_var)
            model_out = model(entity1_var, entity2_var, inbetween_chunk_var)
            # model_out: size of one batch(256) * score of each label (torch.FloatTensor of size 56)
        else:
            ema_model_out = ema_model(ema_input_var)
            model_out = model(input_var)

        ## DONE: AJAY - WHAT IS THIS CODE BLK ACHIEVING ? Ans: THIS IS RELATED TO --logit-distance-cost .. (fc1 and fc2 in model) ...
        if isinstance(model_out, Variable):       # this is default
            assert args.logit_distance_cost < 0
            logit1 = model_out
            ema_logit = ema_model_out
        else:
            assert len(model_out) == 2
            assert len(ema_model_out) == 2
            logit1, logit2 = model_out
            ema_logit, _ = ema_model_out

        ema_logit = Variable(ema_logit.detach().data, requires_grad=False) ## DO NOT UPDATE THE GRADIENTS THORUGH THE TEACHER (EMA) MODEL

        if args.logit_distance_cost >= 0:
            class_logit, cons_logit = logit1, logit2
            res_loss = args.logit_distance_cost * residual_logit_criterion(class_logit, cons_logit) / minibatch_size
            meters.update('res_loss', res_loss.data[0])
        else:                                 # this is the default
            class_logit, cons_logit = logit1, logit1    # class_logit.data.size(): torch.Size([256, 56])
            res_loss = 0


        class_loss = class_criterion(class_logit, target_var) / minibatch_size  ## DONE: AJAY - WHAT IF target_var NOT PRESENT (UNLABELED DATAPOINT) ? Ans: See  ignore index in  `class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cpu()`
        meters.update('class_loss', class_loss.data[0])

        ema_class_loss = class_criterion(ema_logit, target_var) / minibatch_size ## DONE: AJAY - WHAT IF target_var NOT PRESENT (UNLABELED DATAPOINT) ? Ans: See  ignore index in  `class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cpu()`
        meters.update('ema_class_loss', ema_class_loss.data[0])    # Do we need this?

        if args.consistency:     # if pass --consistency in running script
            consistency_weight = get_current_consistency_weight(epoch)
            meters.update('cons_weight', consistency_weight)
            consistency_loss = consistency_weight * consistency_criterion(cons_logit, ema_logit) / minibatch_size
            meters.update('cons_loss', consistency_loss.data[0])
        else:
            consistency_loss = 0
            meters.update('cons_loss', 0)

        loss = class_loss + consistency_loss + res_loss # NOTE: AJAY - loss is a combination of classification loss and consistency loss (+ residual loss from the 2 outputs of student model fc1 and fc2, see args.logit_distance_cost)
        assert not (np.isnan(loss.data[0]) or loss.data[0] > 1e5), 'Loss explosion: {}'.format(loss.data[0])
        meters.update('loss', loss.data[0])

        if args.dataset in ['riedel', 'gids']:
            #student
            correct, num_target_notNA, num_pred_notNA = prec_rec(class_logit.data, target_var.data, NA_label, topk=(1,))
            if num_pred_notNA > 0:
                prec = float(correct) / float(num_pred_notNA)
            else:
                prec = 0.0

            if num_target_notNA > 0:
                rec = float(correct) / float(num_target_notNA)
            else:
                rec = 0.0

            if prec + rec == 0:
                f1 = 0
            else:
                f1 = 2 * prec * rec / (prec + rec)

            meters.update('correct', correct, 1)
            meters.update('target_notNA', num_target_notNA, 1)
            meters.update('pred_notNA', num_pred_notNA, 1)

            if float(meters['pred_notNA'].sum) == 0:
                accum_prec = 0
            else:
                accum_prec = float(meters['correct'].sum) / float(meters['pred_notNA'].sum)

            if float(meters['target_notNA'].sum) == 0:
                accum_rec = 0
            else:
                accum_rec = float(meters['correct'].sum) / float(meters['target_notNA'].sum)

            if accum_prec + accum_rec == 0:
                accum_f1 = 0
            else:
                accum_f1 = 2 * accum_prec * accum_rec / (accum_prec + accum_rec)

            #teacher
            ema_correct, ema_num_target_notNA, ema_num_pred_notNA  = prec_rec(ema_logit.data, target_var.data, NA_label, topk=(1,))
            if ema_num_pred_notNA > 0:
                ema_prec = float(ema_correct) / float(ema_num_pred_notNA)
            else:
                ema_prec = 0.0

            if num_target_notNA > 0:
                ema_rec = float(ema_correct) / float(ema_num_target_notNA)
            else:
                ema_rec = 0.0

            if ema_prec + ema_rec == 0:
                ema_f1 = 0
            else:
                ema_f1 = 2 * ema_prec * ema_rec / (ema_prec + ema_rec)

            meters.update('ema_correct', ema_correct, 1)
            meters.update('ema_target_notNA', ema_num_target_notNA, 1)
            meters.update('ema_pred_notNA', ema_num_pred_notNA, 1)


            if float(meters['ema_pred_notNA'].sum) == 0:
                accum_ema_prec = 0
            else:
                accum_ema_prec = float(meters['ema_correct'].sum) / float(meters['ema_pred_notNA'].sum)

            if float(meters['ema_target_notNA'].sum) == 0:
                accum_ema_rec = 0
            else:
                accum_ema_rec = float(meters['ema_correct'].sum) / float(meters['ema_target_notNA'].sum)

            if accum_ema_prec + accum_ema_rec == 0:
                accum_ema_f1 = 0
            else:
                accum_ema_f1 = 2 * accum_ema_prec * accum_ema_rec / (accum_ema_prec + accum_ema_rec)

        else:
            prec1, prec5 = accuracy(class_logit.data, target_var.data, topk=(1, 2)) #Note: Ajay changing this to 2 .. since there are only 4 labels in CoNLL dataset
            meters.update('top1', prec1[0], labeled_minibatch_size)
            meters.update('error1', 100. - prec1[0], labeled_minibatch_size)
            meters.update('top5', prec5[0], labeled_minibatch_size)
            meters.update('error5', 100. - prec5[0], labeled_minibatch_size)

            ema_prec1, ema_prec5 = accuracy(ema_logit.data, target_var.data, topk=(1, 2)) #Note: Ajay changing this to 2 .. since there are only 4 labels in CoNLL dataset
            meters.update('ema_top1', ema_prec1[0], labeled_minibatch_size)
            meters.update('ema_error1', 100. - ema_prec1[0], labeled_minibatch_size)
            meters.update('ema_top5', ema_prec5[0], labeled_minibatch_size)
            meters.update('ema_error5', 100. - ema_prec5[0], labeled_minibatch_size)


        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        global_step += 1
        update_ema_variables(model, ema_model, args.ema_decay, global_step)

        # measure elapsed time
        meters.update('batch_time', time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            if args.dataset in ['riedel', 'gids']:
                LOG.info(
                    'Epoch: [{0}][{1}/{2}]  '
                    'Prec {prec:.3f}({accum_prec:.3f})  '
                    'Rec {rec:.3f}({accum_rec:.3f})  '
                    'F1 {f1:.3f}({accum_f1:.3f})  '
                    'EMA_Prec {ema_prec:.3f}({accum_ema_prec:.3f})  '
                    'EMA_Rec {ema_rec:.3f}({accum_ema_rec:.3f})  '
                    'EMA_F1 {ema_f1:.3f}({accum_ema_f1:.3f})'.format(
                        epoch, i, len(train_loader), prec=prec, accum_prec=accum_prec, rec=rec, accum_rec=accum_rec, f1=f1, accum_f1=accum_f1, ema_prec=ema_prec, accum_ema_prec=accum_ema_prec, ema_rec=ema_rec, accum_ema_rec=accum_ema_rec, ema_f1=ema_f1, accum_ema_f1=accum_ema_f1))

            else:
                LOG.info(
                    'Epoch: [{0}][{1}/{2}]\t'
                    'ClassLoss {meters[class_loss]:.4f}\t'
                    'Prec@1 {meters[top1]:.3f}\t'
                    'Prec@2 {meters[top5]:.3f}'.format(
                        epoch, i, len(train_loader), meters=meters))

            log.record(epoch + i / len(train_loader), {
                'step': global_step,
                **meters.values(),
                **meters.averages(),
                **meters.sums()
            })


def validate(eval_loader, model, log, global_step, epoch, dataset, result_dir, model_type):
    global NA_label

    if torch.cuda.is_available():
        class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cuda()
    else:
        class_criterion = nn.CrossEntropyLoss(size_average=False, ignore_index=NO_LABEL).cpu()

    meters = AverageMeterSet()

    # switch to evaluate mode
    model.eval() ### From the documentation (nn.module,py) : i) Sets the module in evaluation mode. (ii) This has any effect only on modules such as Dropout or BatchNorm. (iii) Returns: Module: self

    end = time.time()

    save_custom_embed_condition = args.arch == 'custom_embed' \
                                  and args.save_custom_embedding \
                                  and epoch == args.epochs  # todo: only in the final epoch or best_epoch ?

    if save_custom_embed_condition:
        # Note: contains a tuple: (custom_entity_embed, custom_patterns_embed, min-batch-size)
        # enumerating the list of tuples gives the minibatch_id
        custom_embeddings_minibatch = list()
        # eval_loader.batch_size = 1
        # LOG.info("NOTE: Setting the eval_loader's batch_size=1 .. to dump all the entity and pattern embeddings ....")

    #for i, datapoint in enumerate(eval_loader):
    for i, datapoint in enumerate(dataset):
        meters.update('data_time', time.time() - end)

        if args.dataset in ['conll', 'ontonotes']:
            entity = datapoint[0][0]
            patterns = datapoint[0][1]
            target = datapoint[1]

            if torch.cuda.is_available():
                entity_var = torch.autograd.Variable(entity, volatile=True).cuda()
                patterns_var = torch.autograd.Variable(patterns, volatile=True).cuda()
            else:
                entity_var = torch.autograd.Variable(entity, volatile=True).cpu()
                patterns_var = torch.autograd.Variable(patterns, volatile=True).cpu()

        elif args.dataset in ['riedel', 'gids']:
            input = datapoint[0]
            target = datapoint[1]

            input_entity1 = input[0]
            input_entity2 = input[1]
            input_inbetween_chunk = input[2]

            if torch.cuda.is_available():
                entity1_var = torch.autograd.Variable(input_entity1, volatile=True).cuda()
                entity2_var = torch.autograd.Variable(input_entity2, volatile=True).cuda()
                inbetween_chunk_var = torch.autograd.Variable(input_inbetween_chunk, volatile=True).cuda()
            else:
                entity1_var = torch.autograd.Variable(input_entity1, volatile=True).cpu()
                entity2_var = torch.autograd.Variable(input_entity2, volatile=True).cpu()
                inbetween_chunk_var = torch.autograd.Variable(input_inbetween_chunk, volatile=True).cpu()

        else:
            (input, target) = datapoint
            if torch.cuda.is_available():
                input_var = torch.autograd.Variable(input, volatile=True).cuda()
            else:
                input_var = torch.autograd.Variable(input, volatile=True).cpu() ## NOTE: AJAY - volatile: Boolean indicating that the Variable should be used in inference mode,

        if torch.cuda.is_available():
            target_var = torch.autograd.Variable(target.cuda(), volatile=True)
        else:
            target_var = torch.autograd.Variable(target.cpu(), volatile=True) ## NOTE: AJAY - volatile: Boolean indicating that the Variable should be used in inference mode,

        minibatch_size = len(target_var)
        labeled_minibatch_size = target_var.data.ne(NO_LABEL).sum()
        ############## NOTE: AJAY -- changing this piece of code to make sure evaluation does not
        ############## thrown exception when the minibatch consists of only NAs. Skip the batch
        ############## TODO: AJAY -- To remove this later
        # assert labeled_minibatch_size > 0
        if labeled_minibatch_size == 0:
            print ("%%%%%%%%%%%%%%%%%%%%%%%%%%%%%AJAY: Labeled_minibatch_size == 0 ....%%%%%%%%%%%%%%%%%%%%%%%")
            continue
        ###################################################
        meters.update('labeled_minibatch_size', labeled_minibatch_size)

        # compute output
        if args.dataset in ['conll', 'ontonotes'] and args.arch == 'custom_embed':
            output1, entity_custom_embed, pattern_custom_embed = model(entity_var, patterns_var)
            if save_custom_embed_condition:
                custom_embeddings_minibatch.append((entity_custom_embed, pattern_custom_embed))  # , minibatch_size))
        elif args.dataset in ['conll', 'ontonotes'] and args.arch == 'simple_MLP_embed':
            output1 = model(entity_var, patterns_var)
        elif args.dataset in ['riedel', 'gids']and args.arch == 'lstm_RE':
            output1 = model(entity1_var, entity2_var, inbetween_chunk_var)
        elif args.dataset in ['riedel', 'gids'] and args.arch == 'simple_MLP_embed_RE':
            output1 = model(entity1_var, entity2_var, inbetween_chunk_var)
        else:
            output1 = model(input_var) ##, output2 = model(input_var)
        #softmax1, softmax2 = F.softmax(output1, dim=1), F.softmax(output2, dim=1)
        class_loss = class_criterion(output1, target_var) / minibatch_size

        if args.dataset in ['riedel', 'gids']:
            correct, num_target_notNA, num_pred_notNA = prec_rec(output1.data, target_var.data, NA_label, topk=(1,))
            if num_pred_notNA > 0:
                prec = float(correct) / float(num_pred_notNA)
            else:
                prec = 0.0

            if num_target_notNA > 0:
                rec = float(correct) / float(num_target_notNA)
            else:
                rec = 0.0

            if prec + rec == 0:
                f1 = 0
            else:
                f1 = 2 * prec * rec / (prec + rec)

            meters.update('correct', correct, 1)
            meters.update('target_notNA', num_target_notNA, 1)
            meters.update('pred_notNA', num_pred_notNA, 1)

            if float(meters['pred_notNA'].sum) == 0:
                accum_prec = 0
            else:
                accum_prec = float(meters['correct'].sum) / float(meters['pred_notNA'].sum)

            if float(meters['target_notNA'].sum) == 0:
                accum_rec = 0
            else:
                accum_rec = float(meters['correct'].sum) / float(meters['target_notNA'].sum)

            if accum_prec + accum_rec == 0:
                accum_f1 = 0
            else:
                accum_f1 = 2 * accum_prec * accum_rec / (accum_prec + accum_rec)

            lbl_categories = dataset.categories
            if epoch == args.epochs:
                dump_result(i, args, output1.data, lbl_categories, model_type, topk=(1,))

        else:
            # measure accuracy and record loss
            prec1, prec5 = accuracy(output1.data, target_var.data, topk=(1, 2)) #Note: Ajay changing this to 2 .. since there are only 4 labels in CoNLL dataset
            meters.update('class_loss', class_loss.data[0], labeled_minibatch_size)
            meters.update('top1', prec1[0], labeled_minibatch_size)
            meters.update('error1', 100.0 - prec1[0], labeled_minibatch_size)
            meters.update('top5', prec5[0], labeled_minibatch_size)
            meters.update('error5', 100.0 - prec5[0], labeled_minibatch_size)

        # measure elapsed time
        meters.update('batch_time', time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            if args.dataset in ['riedel', 'gids']:
                LOG.info(
                    'Test: [{0}/{1}]  '
                    'Precision {prec:.3f} ({accum_prec:.3f})  '
                    'Recall {rec:.3f} ({accum_rec:.3f})  '
                    'F1 {f1:.3f} ({accum_f1:.3f})'.format(
                        i, len(eval_loader), prec=prec, accum_prec=accum_prec,rec=rec, accum_rec=accum_rec, f1=f1, accum_f1=accum_f1))

            else:
                LOG.info(
                    'Test: [{0}/{1}]\t'
                    'ClassLoss {meters[class_loss]:.4f}\t'
                    'Prec@1 {meters[top1]:.3f}'.format(
                        i, len(eval_loader), meters=meters))

    if args.dataset in ['riedel', 'gids']:
        if epoch == args.epochs:

            if model_type == 'student':
                if student_pred_noNA == 0.0:
                    student_precision = 0.0
                else:
                    student_precision = student_pred_match_noNA / student_pred_noNA
                if student_true_noNA == 0.0:
                    student_recall = 0.0
                else:
                    student_recall = student_pred_match_noNA / student_true_noNA
                if student_precision + student_recall == 0.0:
                    student_f1 = 0.0
                else:
                    student_f1 = 2 * student_precision * student_recall / (student_precision + student_recall)

                print('******* Student : Overall Precision ' + str(student_precision) + '\tRecall ' + str(student_recall) + '\tF1 ' + str(student_f1) + '\t********')

            else:
                if teacher_pred_noNA == 0.0:
                    teacher_precision = 0.0
                else:
                    teacher_precision = teacher_pred_match_noNA / teacher_pred_noNA
                if teacher_true_noNA == 0.0:
                    teacher_recall = 0.0
                else:
                    teacher_recall = teacher_pred_match_noNA / teacher_true_noNA
                if teacher_precision + teacher_recall == 0.0:
                    teacher_f1 = 0.0
                else:
                    teacher_f1 = 2 * teacher_precision * teacher_recall / (teacher_precision + teacher_recall)

                print('******* Teacher : Overall Precision ' + str(teacher_precision) + '\tRecall ' + str(teacher_recall) + '\tF1 ' + str(teacher_f1) + '\t********')

    else:
        LOG.info(' * Prec@1 {top1.avg:.3f}\tClassLoss {class_loss.avg:.3f}'
              .format(top1=meters['top1'], class_loss=meters['class_loss']))


    log.record(epoch, {
        'step': global_step,
        **meters.values(),
        **meters.averages(),
        **meters.sums()
    })

    if save_custom_embed_condition:
        save_custom_embeddings(custom_embeddings_minibatch, dataset, result_dir, model_type)

    if args.dataset in ['riedel', 'gids']:
        return accum_f1
    else:
        return meters['top1'].avg

#todo: do we need to save custom_embeddings?  - mihai
def save_custom_embeddings(custom_embeddings_minibatch, dataset, result_dir, model_type):

    start_time = time.time()
    mention_embeddings = dict()
    pattern_embeddings = dict()

    # dataset_id_list = list()

    for min_batch_id, datapoint in enumerate(custom_embeddings_minibatch):
        if torch.cuda.is_available():
            mention_embeddings_batch = datapoint[0].cuda().data.numpy()
            patterns_embeddings_batch = datapoint[1].permute(1, 0, 2).cuda().data.numpy()
        else:
            mention_embeddings_batch = datapoint[0].cpu().data.numpy()
            patterns_embeddings_batch = datapoint[1].permute(1, 0, 2).cpu().data.numpy()  # Note the permute .. to get the min-batches in the 1st dim
        # min_batch_sz = datapoint[2]

        # compute the custom entity embeddings
        for idx, embed in enumerate(mention_embeddings_batch):
            dataset_id = (min_batch_id * args.batch_size) + idx  # NOTE: `mini_batch_sz` here is a bug (in last batch)!! So changing to args.batch_size
            # dataset_id_list.append("ID="+str(min_batch_id)+"*"+str(min_batch_sz)+"+"+str(idx)+"="+str(dataset_id))
            mention_str = dataset.entity_vocab.get_word(dataset.mentions[dataset_id])
            if mention_str in mention_embeddings:
                prev_embed = mention_embeddings[mention_str]
                np.mean([prev_embed, embed], axis=0)
            else:
                mention_embeddings[mention_str] = embed

        # compute the custom pattern embeddings
        # print("==========================")
        # print("datapoint[0] (sz) = " + str(datapoint[0].size()))
        # print("datapoint[1] (sz) = " + str(datapoint[1].size()))
        # print("patterns_embeddings_batch (sz) = " + str(patterns_embeddings_batch.shape))
        # print("==========================")
        for idx, embed_arr in enumerate(patterns_embeddings_batch):
            dataset_id = (min_batch_id * args.batch_size) + idx  # NOTE: `mini_batch_sz` here is a bug (in last batch)!! So changing to args.batch_size
            patterns_arr = [dataset.context_vocab.get_word(ctxId) for ctxId in dataset.contexts[dataset_id]]
            num_patterns = len(patterns_arr)

            # print("-------------------------------------------------")
            # print("Patterns Arr : " + str(patterns_arr))
            # print("Num Patterns : " + str(num_patterns))
            # print("dataset_id : " + str(dataset_id))
            # print("embed_arr (sz) : " + str(embed_arr.shape))
            # print("-------------------------------------------------")
            for i in range(num_patterns):
                embed = embed_arr[i]
                pattern_str = patterns_arr[i]
                if pattern_str in pattern_embeddings:
                    prev_embed = pattern_embeddings[pattern_str]
                    np.mean([prev_embed, embed], axis=0)
                else:
                    pattern_embeddings[pattern_str] = embed

    entity_embed_file = os.path.join(result_dir, model_type + "_entity_embed.txt")
    with open(entity_embed_file, 'w') as ef:
        for string, embed in mention_embeddings.items():
            ef.write(string + "\t" + " ".join([str(i) for i in embed]) + "\n")
        # for string in dataset_id_list:
        #     ef.write(string + "\n")
    ef.close()

    pattern_embed_file = os.path.join(result_dir, model_type + "_pattern_embed.txt")
    with open(pattern_embed_file, 'w') as pf:
        for string, embed in pattern_embeddings.items():
            pf.write(string + "\t" + " ".join([str(i) for i in embed]) + "\n")
    pf.close()

    LOG.info("Saving the customs entity and pattern embeddings in dir :=> " + str(result_dir))
    LOG.info("Size of entity embeddings :=> " + str(len(mention_embeddings)))
    LOG.info("Size of pattern embeddings :=> " + str(len(pattern_embeddings)))
    LOG.info("COMPLETED writing the files in " + str(time.time() - start_time) + "s.")
    # LOG.info("Size of dataset_id : " + str(len(dataset_id_list)))
    # LOG.info("Size of dataset : " + str(len(dataset.mentions)))


def save_checkpoint(state, is_best, dirpath, epoch):
    filename = 'checkpoint.{}.ckpt'.format(epoch)
    checkpoint_path = os.path.join(dirpath, filename)
    best_path = os.path.join(dirpath, 'best.ckpt')
    torch.save(state, checkpoint_path)
    LOG.info("--- checkpoint saved to %s ---" % checkpoint_path)
    if is_best:
        shutil.copyfile(checkpoint_path, best_path)
        LOG.info("--- checkpoint copied to %s ---" % best_path)
        if args.epochs != epoch: # Note: Save the last checkpoint
            os.remove(checkpoint_path)
            LOG.info("--- removing original checkpoint %s ---" % checkpoint_path) # Note: I can as well not save the original file and only save the best config. But not changing the functionality too much, if need to revert later


def adjust_learning_rate(optimizer, epoch, step_in_epoch, total_steps_in_epoch):
    lr = args.lr
    epoch = epoch + step_in_epoch / total_steps_in_epoch

    # LR warm-up to handle large minibatch sizes from https://arxiv.org/abs/1706.02677
    lr = ramps.linear_rampup(epoch, args.lr_rampup) * (args.lr - args.initial_lr) + args.initial_lr

    # Cosine LR rampdown from https://arxiv.org/abs/1608.03983 (but one cycle only)
    if args.lr_rampdown_epochs:
        assert args.lr_rampdown_epochs >= args.epochs
        lr *= ramps.cosine_rampdown(epoch, args.lr_rampdown_epochs)

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def get_current_consistency_weight(epoch):
    # Consistency ramp-up from https://arxiv.org/abs/1610.02242
    return args.consistency * ramps.sigmoid_rampup(epoch, args.consistency_rampup)

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    labeled_minibatch_size = max(target.ne(NO_LABEL).sum(), 1e-8)

    _, pred = output.topk(maxk, 1, True, True)   # pred: torch.LongTensor of size 256x2, which 2 labels (labels' id) have highest score
    pred = pred.t()    # 2 * 256
    correct = pred.eq(target.view(1, -1).expand_as(pred))
    # target.size(): torch.Size([256])
    # target.view(1, -1): 1 * 256
    # expand_as(pred): copy the first row to be the second row to get 2*256
    # correct: 2*256

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / labeled_minibatch_size))
    return res


def prec_rec(output, target, NA_label, topk=(1,)):
    maxk = max(topk)
    assert maxk == 1, "Right now only computing P/R/F for topk=1"
    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()

    # index() takes same size of pred with idx value 0 and 1, and only return pred[idx] where idx is 1
    tp = pred.index(pred.eq(target.view(1, -1).expand_as(pred))).ne(
        NA_label).sum()  # number of cases where pred matches with target and that is not NA label
    tp_fp = pred.ne(NA_label).sum()  # number of non NA labels in pred
    tp_fn = target.ne(NA_label).sum()  # number of non NA labels in target

    return tp, tp_fn, tp_fp


def dump_result(batch_id, args, output, lbl_categories, model_type='teacher', topk=(1,)):
    global student_pred_match_noNA
    global student_pred_noNA
    global student_true_noNA
    global teacher_pred_match_noNA
    global teacher_pred_noNA
    global teacher_true_noNA

    maxk = max(topk)
    assert maxk == 1, "Right now only computing for topk=1"
    score, prediction = output.topk(maxk, 1, True, True)

    dataset_config = datasets.__dict__[args.dataset]()
    evaldir = os.path.join(dataset_config['datadir'], args.eval_subdir)
    dataset_file = evaldir + '/' + args.eval_subdir + '.txt'

    student_pred_file = evaldir + '/' + args.run_name + '_pred_student.tsv'
    teacher_pred_file = evaldir + '/' + args.run_name + '_pred_teacher.tsv'
    f = open(dataset_file)
    lines = f.readlines()

    if model_type == 'teacher':
        with open(teacher_pred_file, "a") as fo:
            for p, pre in enumerate(prediction):
                line_id = int(batch_id * args.batch_size + p)
                line = lines[line_id].strip()
                lbl_id = int(pre)
                pred_label = lbl_categories[lbl_id].strip()

                vals = line.split('\t')
                true_label = vals[4].strip()
                if pred_label == true_label and true_label != 'NA':
                    teacher_pred_match_noNA += 1.0
                if pred_label != 'NA':
                    teacher_pred_noNA += 1.0
                if true_label != 'NA':
                    teacher_true_noNA += 1.0

                line = line + '\t' + pred_label + '\t' + str(float(score[p])) + '\n'
                fo.write(line)
    else:
        with open(student_pred_file, "a") as fo:
            for p, pre in enumerate(prediction):
                line_id = int(batch_id * args.batch_size + p)
                line = lines[line_id].strip()
                lbl_id = int(pre)
                pred_label = lbl_categories[lbl_id].strip()

                vals = line.split('\t')
                true_label = vals[4].strip()
                if pred_label == true_label and true_label != 'NA':
                    student_pred_match_noNA += 1.0
                if pred_label != 'NA':
                    student_pred_noNA += 1.0
                if true_label != 'NA':
                    student_true_noNA += 1.0

                line = line + '\t' + pred_label + '\t' + str(float(score[p])) + '\n'
                fo.write(line)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    args = cli.parse_commandline_args()
    random_seed = args.random_seed
    random.seed(random_seed)
    np.random.seed(random_seed)

    torch.backends.cudnn.deterministic = True
    if torch.cuda.is_available():
        torch.manual_seed(args.random_seed)
        torch.cuda.manual_seed(args.random_seed)
        # torch.cuda.manual_seed_all(args.random_seed)
    else:
        torch.manual_seed(args.random_seed)

    print('----------------')
    print("Running mean teacher experiment with args:")
    print('----------------')
    print(args)
    print('----------------')
    main(RunContext(__file__, 0, args.run_name))
