import os
import time
import copy
import argparse
import numpy as np
import torch
import torch.nn as nn
from torchvision.utils import save_image
from utils import get_dataset, get_network, get_eval_pool, evaluate_synset, get_daparam, match_loss, get_time, TensorDataset, epoch, DiffAugment, ParamDiffAug, get_dataset_res
from SimCLR.models.resnet_simclr import ResNetSimCLR
from logger.log_setup import setup_logs


def main():
    torch.cuda.empty_cache()

    parser = argparse.ArgumentParser(description='Parameter Processing')
    parser.add_argument('--dataset', type=str, default='CIFAR10', help='dataset')
    parser.add_argument('--model', type=str, default='ConvNet', help='model')
    parser.add_argument('--ipc', type=int, default=50, help='image(s) per class')
    parser.add_argument('--eval_mode', type=str, default='SS', help='eval_mode') # S: the same to training model, M: multi architectures,  W: net width, D: net depth, A: activation function, P: pooling layer, N: normalization layer,
    parser.add_argument('--num_exp', type=int, default=5, help='the number of experiments')
    parser.add_argument('--num_eval', type=int, default=20, help='the number of evaluating randomly initialized models')
    parser.add_argument('--epoch_eval_train', type=int, default=500, help='epochs to train a model with synthetic data') # it can be small for speeding up with little performance drop
    parser.add_argument('--Iteration', type=int, default=4000, help='training iterations')
    parser.add_argument('--lr_img', type=float, default=1.0, help='learning rate for updating synthetic images')
    parser.add_argument('--lr_net', type=float, default=0.01, help='learning rate for updating network parameters')
    parser.add_argument('--batch_real', type=int, default=128, help='batch size for real data')
    parser.add_argument('--batch_train', type=int, default=128, help='batch size for training networks')
    parser.add_argument('--init', type=str, default='real', help='noise/real: initialize synthetic images from random noise or randomly sampled real images.')
    parser.add_argument('--dsa_strategy', type=str, default='color_crop_cutout_flip_scale_rotate', help='differentiable Siamese augmentation strategy')
    parser.add_argument('--data_path', type=str, default='data', help='dataset path')
    parser.add_argument('--save_path', type=str, default='result', help='path to save results')
    parser.add_argument('--dis_metric', type=str, default='ours', help='distance metric')
    parser.add_argument('--partial_condense', type=str, default='F',help='T or F')
    parser.add_argument('--imb_type', type=str, default='exp',help='exp or step')
    parser.add_argument('--imb_factor', type=float, default= 0.01,help='try in (0,1]')
    parser.add_argument('--add_pretrain', type=str, default='F', help='T or F')
    parser.add_argument('--add_aug', type=str, default='F', help='T or F')
    parser.add_argument('--aug_size', type=int, default=100, help='augmentation')


    args = parser.parse_args()
    args.method = 'DM'
    args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
    args.dsa_param = ParamDiffAug()
    args.dsa = False if args.dsa_strategy in ['none', 'None'] else True

    logger = setup_logs('logger', args.dataset +  "_" + str(args.imb_factor) + "_ss_" + args.add_pretrain + "_partial_" + args.partial_condense)

    if not os.path.exists(args.data_path):
        os.mkdir(args.data_path)

    if not os.path.exists(args.save_path):
        os.mkdir(args.save_path)

    eval_it_pool = np.arange(0, args.Iteration+1, 2000).tolist() if args.eval_mode == 'S' or args.eval_mode == 'SS' else [args.Iteration] # The list of iterations when we evaluate models and record results.
    logger.info('eval_it_pool: %s', eval_it_pool)
    channel, im_size, num_classes, class_names, mean, std, dst_train, dst_test, testloader = get_dataset(args.dataset, args.data_path, logger, args.imb_type, args.imb_factor)
    model_eval_pool = get_eval_pool(args.eval_mode, args.model, args.model, logger)
    
    
    if args.partial_condense == 'T':
        dst_train_res = get_dataset_res(args.data_path, imb_factor=args.imb_factor, dataset=args.dataset, logger = logger)
        images_all_res = []
        labels_all_res = []
        
        images_all_res = [torch.unsqueeze(dst_train_res[i][0], dim=0) for i in range(len(dst_train_res))]

        labels_all_res = [dst_train_res[i][1] for i in range(len(dst_train_res))]
        images_all_res = torch.cat(images_all_res, dim=0).to(args.device)
        labels_all_res = torch.tensor(labels_all_res, dtype=torch.long, device=args.device)


    accs_all_exps = dict() # record performances of all experiments
    for key in model_eval_pool:
        accs_all_exps[key] = []

    data_save = []


    for exp in range(args.num_exp):
        logger.info('\n================== Exp %d ==================\n '%exp)
        logger.info('Hyper-parameters: \n %s', args.__dict__)
        logger.info('Evaluation model pool: %s', model_eval_pool)

        ''' organize the real dataset '''
        images_all = []
        labels_all = []
        indices_class = [[] for c in range(num_classes)]

        images_all = [torch.unsqueeze(dst_train[i][0], dim=0) for i in range(len(dst_train))]
        labels_all = [dst_train[i][1] for i in range(len(dst_train))]
        for i, lab in enumerate(labels_all):
            indices_class[lab].append(i)
        images_all = torch.cat(images_all, dim=0).to(args.device)
        labels_all = torch.tensor(labels_all, dtype=torch.long, device=args.device)



        for c in range(num_classes):
            logger.info('class c = %d: %d real images'%(c, len(indices_class[c])))

        def get_images(c, n): # get random n images from class c
            idx_shuffle = np.random.permutation(indices_class[c])[:n]
            return images_all[idx_shuffle]

        for ch in range(channel):
            logger.info('real images channel %d, mean = %.4f, std = %.4f'%(ch, torch.mean(images_all[:, ch]), torch.std(images_all[:, ch])))


        ''' initialize the synthetic data '''
        image_syn = torch.randn(size=(num_classes*args.ipc, channel, im_size[0], im_size[1]), dtype=torch.float, requires_grad=True, device=args.device)
        label_syn = torch.tensor([np.ones(args.ipc)*i for i in range(num_classes)], dtype=torch.long, requires_grad=False, device=args.device).view(-1) # [0,0,0, 1,1,1, ..., 9,9,9]

        if args.init == 'real':
            logger.info('initialize synthetic data from random real images')
            for c in range(num_classes):
                image_syn.data[c*args.ipc:(c+1)*args.ipc] = get_images(c, args.ipc).detach().data
        else:
            logger.info('initialize synthetic data from random noise')


        ''' training '''
        optimizer_img = torch.optim.SGD([image_syn, ], lr=args.lr_img, momentum=0.5) # optimizer_img for synthetic data
        optimizer_img.zero_grad()
        logger.info('%s training begins'%get_time())

        for it in range(args.Iteration+1):

            ''' Evaluate synthetic data '''
            if it in eval_it_pool:
                for model_eval in model_eval_pool:
                    logger.info('-------------------------\nEvaluation\nmodel_train = %s, model_eval = %s, iteration = %d'%(args.model, model_eval, it))

                    logger.info('DSA augmentation strategy: \n %s', args.dsa_strategy)
                    logger.info('DSA augmentation parameters: \n %s', args.dsa_param.__dict__)

                    accs = []
                    for it_eval in range(args.num_eval):
                        # logger.info("num_class: ", num_classes)
                        if "100" in args.dataset:
                            output_channel = 100
                        else:
                            output_channel = 10
                        net_eval = get_network(model_eval, channel, output_channel, im_size).to(args.device) # get a random model
                        if args.add_pretrain == 'T':
                            checkpoint = torch.load('SimCLR/log/checkpoint_' + args.dataset  + '.pth.tar')
                            # print(checkpoint['state_dict'].keys())
                            net_eval.load_state_dict(checkpoint['state_dict'])
                        
                        image_syn_eval, label_syn_eval = copy.deepcopy(image_syn.detach()), copy.deepcopy(label_syn.detach()) # avoid any unaware modification
                        if args.partial_condense == 'T':
                            image_syn_eval = torch.cat((image_syn_eval, images_all_res), dim=0).to(args.device)
                            label_syn_eval = torch.cat((label_syn_eval, labels_all_res), dim = 0).to(args.device)
                        if args.add_aug == 'T' and args.partial_condense == 'F':
                            aug_time = args.aug_size // args.ipc
                            
                            images_train_init = image_syn_eval
                            labels_train_init = label_syn_eval
                            for _ in range(aug_time):
                                for image, label in zip(images_train_init, labels_train_init):
                                    img = image.unsqueeze(0)
                                    img = DiffAugment(img, args.dsa_strategy, param=args.dsa_param)
                                    image_syn_eval = torch.cat((image_syn_eval, img), dim=0)
                                    label = label.unsqueeze(0)
                                    label_syn_eval = torch.cat((label_syn_eval, label), dim=0)

                        if it == args.Iteration and it_eval == args.num_eval - 1:
                            _, acc_train, acc_test = evaluate_synset(it_eval, net_eval, image_syn_eval, label_syn_eval, testloader, args, output_channel, logger, True, "_" + args.dataset + "_" + str(args.imb_factor) + "_final")
                        elif it == 0 and it_eval == 0:
                            _, acc_train, acc_test = evaluate_synset(it_eval, net_eval, image_syn_eval, label_syn_eval, testloader, args, output_channel, logger, True, "_" + args.dataset + "_" + str(args.imb_factor) + "_basline")
                        else:
                            _, acc_train, acc_test = evaluate_synset(it_eval, net_eval, image_syn_eval, label_syn_eval, testloader, args, output_channel, logger, False)
                        accs.append(acc_test)
                    logger.info('Evaluate %d random %s, mean = %.4f std = %.4f\n-------------------------'%(len(accs), model_eval, np.mean(accs), np.std(accs)))

                    if it == args.Iteration: # record the final results
                        accs_all_exps[model_eval] += accs

                ''' visualize and save '''
                save_name = os.path.join(args.save_path, 'vis_%s_%s_%s_%dipc_exp%d_iter%d.png'%(args.method, args.dataset, args.model, args.ipc, exp, it))
                image_syn_vis = copy.deepcopy(image_syn.detach().cpu())
                for ch in range(channel):
                    image_syn_vis[:, ch] = image_syn_vis[:, ch]  * std[ch] + mean[ch]
                image_syn_vis[image_syn_vis<0] = 0.0
                image_syn_vis[image_syn_vis>1] = 1.0
                save_image(image_syn_vis, save_name, nrow=args.ipc) # Trying normalize = True/False may get better visual effects.



            ''' Train synthetic data '''
            net = get_network(args.model, channel, num_classes, im_size).to(args.device) # get a random model
            net.train()
            for param in list(net.parameters()):
                param.requires_grad = False

            # embed = net.module.embed if torch.cuda.device_count() > 1 else net.embed # for GPU parallel
            embed =  net.embed

            loss_avg = 0

            ''' update synthetic data '''
            torch.cuda.empty_cache()
            loss = torch.tensor(0.0).to(args.device)
            for c in range(num_classes):
                img_real = get_images(c, args.batch_real)
                img_syn = image_syn[c*args.ipc:(c+1)*args.ipc].reshape((args.ipc, channel, im_size[0], im_size[1]))

                if args.dsa:
                    seed = int(time.time() * 1000) % 100000
                    img_real = DiffAugment(img_real, args.dsa_strategy, seed=seed, param=args.dsa_param)
                    img_syn = DiffAugment(img_syn, args.dsa_strategy, seed=seed, param=args.dsa_param)
                
                output_real = embed(img_real).detach()
                output_syn = embed(img_syn)

                # loss += torch.sum((torch.mean(output_real, dim=0) - torch.mean(output_syn, dim=0))**2)
                loss = torch.sum((torch.mean(output_real, dim=0) - torch.mean(output_syn, dim=0))**2)
                
                optimizer_img.zero_grad()
                loss.backward()
                optimizer_img.step()
                loss_avg += loss.item()

            # optimizer_img.zero_grad()
            # loss.backward()
            # optimizer_img.step()
            # loss_avg += loss.item()


            loss_avg /= (num_classes)

            if it%10 == 0:
                logger.info('%s iter = %05d, loss = %.4f' % (get_time(), it, loss_avg))

            if it == args.Iteration: # only record the final results
                data_save.append([copy.deepcopy(image_syn.detach().cpu()), copy.deepcopy(label_syn.detach().cpu())])
                torch.save({'data': data_save, 'accs_all_exps': accs_all_exps, }, os.path.join(args.save_path, 'res_%s_%s_%s_%dipc.pt'%(args.method, args.dataset, args.model, args.ipc)))


    logger.info('\n==================== Final Results ====================\n')
    for key in model_eval_pool:
        accs = accs_all_exps[key]
        logger.info('Run %d experiments, train on %s, evaluate %d random %s, mean  = %.2f%%  std = %.2f%%'%(args.num_exp, args.model, len(accs), key, np.mean(accs)*100, np.std(accs)*100))



if __name__ == '__main__':
    main()


