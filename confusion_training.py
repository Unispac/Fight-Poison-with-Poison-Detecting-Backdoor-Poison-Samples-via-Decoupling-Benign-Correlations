import os
import torch
from torch import nn
import numpy as np
from tqdm import tqdm
from torch.optim.lr_scheduler import MultiStepLR
from utils import tools


# extract features
def get_features(data_loader, model):
    label_list = []
    preds_list = []
    feats = []
    gt_confidence = []
    loss_vals = []
    criterion_no_reduction = nn.CrossEntropyLoss(reduction='none')
    model.eval()
    with torch.no_grad():
        for i, (ins_data, ins_target) in enumerate(tqdm(data_loader)):
            ins_data, ins_target = ins_data.cuda(), ins_target.cuda()
            output, x_features = model(ins_data, return_hidden=True)

            loss = criterion_no_reduction(output, ins_target).cpu().numpy()

            preds = torch.argmax(output, dim=1).cpu().numpy()
            prob = torch.softmax(output, dim=1).cpu().numpy()
            this_batch_size = len(ins_target)
            for bid in range(this_batch_size):
                gt = ins_target[bid].cpu().item()
                feats.append(x_features[bid].cpu().numpy())
                label_list.append(gt)
                preds_list.append(preds[bid])
                gt_confidence.append(prob[bid][gt])
                loss_vals.append(loss[bid])
    return feats, label_list, preds_list, gt_confidence, loss_vals


def identify_poison_samples_simplified(inspection_set, clean_indices, model, num_classes):

    from scipy.stats import multivariate_normal

    kwargs = {'num_workers': 4, 'pin_memory': True}
    num_samples = len(inspection_set)

    # main dataset we aim to cleanse
    inspection_split_loader = torch.utils.data.DataLoader(
        inspection_set,
        batch_size=128, shuffle=False, worker_init_fn=tools.worker_init, **kwargs)

    model.eval()
    feats_inspection, class_labels_inspection, preds_inspection, \
    gt_confidence_inspection, loss_vals = get_features(inspection_split_loader, model)

    feats_inspection = np.array(feats_inspection)
    class_labels_inspection = np.array(class_labels_inspection)

    class_indices = [[] for _ in range(num_classes)]
    class_indices_in_clean_chunklet = [[] for _ in range(num_classes)]

    for i in range(num_samples):
        gt = int(class_labels_inspection[i])
        class_indices[gt].append(i)

    for i in clean_indices:
        gt = int(class_labels_inspection[i])
        class_indices_in_clean_chunklet[gt].append(i)

    for i in range(num_classes):
        class_indices[i].sort()
        class_indices_in_clean_chunklet[i].sort()

        if len(class_indices[i]) < 2:
            raise Exception('dataset is too small for class %d' % i)

        if len(class_indices_in_clean_chunklet[i]) < 2:
            raise Exception('clean chunklet is too small for class %d' % i)

    # apply cleanser, if the likelihood of two-clusters-model is twice of the likelihood of single-cluster-model
    threshold = 2
    suspicious_indices = []
    class_likelihood_ratio = []

    for target_class in range(num_classes):

        num_samples_within_class = len(class_indices[target_class])
        print('class-%d : ' % target_class, num_samples_within_class)
        clean_chunklet_size = len(class_indices_in_clean_chunklet[target_class])
        clean_chunklet_indices_within_class = []
        pt = 0
        for i in range(num_samples_within_class):
            if pt == clean_chunklet_size:
                break
            if class_indices[target_class][i] < class_indices_in_clean_chunklet[target_class][pt]:
                continue
            else:
                clean_chunklet_indices_within_class.append(i)
                pt += 1

        print('start_pca..')

        temp_feats = torch.FloatTensor(
            feats_inspection[class_indices[target_class]]).cuda()


        # reduce dimensionality
        U, S, V = torch.pca_lowrank(temp_feats, q=2)
        projected_feats = torch.matmul(temp_feats, V[:, :2]).cpu()

        # isolate samples via the confused inference model
        isolated_indices_global = []
        isolated_indices_local = []
        other_indices_local = []
        labels = []
        for pt, i in enumerate(class_indices[target_class]):
            if preds_inspection[i] == target_class:
                isolated_indices_global.append(i)
                isolated_indices_local.append(pt)
                labels.append(1) # suspected as positive
            else:
                other_indices_local.append(pt)
                labels.append(0)

        projected_feats_isolated = projected_feats[isolated_indices_local]
        projected_feats_other = projected_feats[other_indices_local]

        print('========')
        print('num_isolated:', projected_feats_isolated.shape)
        print('num_other:', projected_feats_other.shape)

        num_isolated = projected_feats_isolated.shape[0]

        print('num_isolated : ', num_isolated)

        if (num_isolated >= 2) and (num_isolated <= num_samples_within_class - 2):

            mu = np.zeros((2,2))
            covariance = np.zeros((2,2,2))

            mu[0] = projected_feats_other.mean(axis=0)
            covariance[0] = np.cov(projected_feats_other.T)
            mu[1] = projected_feats_isolated.mean(axis=0)
            covariance[1] = np.cov(projected_feats_isolated.T)

            # avoid singularity
            covariance += 0.001

            # likelihood ratio test
            single_cluster_likelihood = 0
            two_clusters_likelihood = 0
            for i in range(num_samples_within_class):
                single_cluster_likelihood += multivariate_normal.logpdf(x=projected_feats[i:i + 1], mean=mu[0],
                                                                        cov=covariance[0],
                                                                        allow_singular=True).sum()
                two_clusters_likelihood += multivariate_normal.logpdf(x=projected_feats[i:i + 1], mean=mu[labels[i]],
                                                                      cov=covariance[labels[i]], allow_singular=True).sum()

            likelihood_ratio = np.exp( (two_clusters_likelihood - single_cluster_likelihood) / num_samples_within_class )

        else:

            likelihood_ratio = 1

        class_likelihood_ratio.append(likelihood_ratio)

        print('likelihood_ratio = ', likelihood_ratio)

    max_ratio = np.array(class_likelihood_ratio).max()

    for target_class in range(num_classes):
        likelihood_ratio = class_likelihood_ratio[target_class]

        if likelihood_ratio == max_ratio and likelihood_ratio > 1.5:  # a lower conservative threshold for maximum ratio

            print('[class-%d] class with maximal ratio %f!. Apply Cleanser!' % (target_class, max_ratio))

            for i in class_indices[target_class]:
                if preds_inspection[i] == target_class: #gt_confidence_inspection[i] > 0.5:
                    suspicious_indices.append(i)

        elif likelihood_ratio > threshold:
            print('[class-%d] likelihood_ratio = %f > threshold = %f. Apply Cleanser!' % (
                target_class, likelihood_ratio, threshold))

            for i in class_indices[target_class]:
                if preds_inspection[i] == target_class:
                #if gt_confidence_inspection[i] > 0.5:
                    suspicious_indices.append(i)

        else:
            print('[class-%d] likelihood_ratio = %f <= threshold = %f. Pass!' % (
                target_class, likelihood_ratio, threshold))

    return suspicious_indices



# pretraining on the poisoned datast to learn a prior of the backdoor
def pretrain(args, debug_packet, arch, num_classes, weight_decay, pretrain_epochs, distilled_set_loader, criterion,
             inspection_set_dir, confusion_iter, lr, load = True, dataset_name=None):


    all_to_all = False
    if args.poison_type == 'badnet_all_to_all':
        all_to_all = True

    ######### Pretrain Base Model ##############
    model = arch(num_classes = num_classes)

    if confusion_iter != 0 and load:
        ckpt_path = os.path.join(inspection_set_dir, 'base_%d_seed=%d.pt' % (confusion_iter-1, args.seed))
        model.load_state_dict( torch.load(ckpt_path) )

    model = nn.DataParallel(model)
    model = model.cuda()
    optimizer = torch.optim.SGD(model.parameters(), lr,  momentum=0.9, weight_decay=weight_decay)

    for epoch in range(1, pretrain_epochs + 1):  # pretrain backdoored base model with the distilled set
        model.train()

        for batch_idx, (data, target) in enumerate( tqdm(distilled_set_loader) ):
            optimizer.zero_grad()
            data, target = data.cuda(), target.cuda()  # train set batch
            output = model(data)
            loss = criterion(output, target)
            loss.backward()
            optimizer.step()

        if epoch % 10 == 0:
            print('<Round-{} : Pretrain> Train Epoch: {}/{} \tLoss: {:.6f}'.format(confusion_iter, epoch, pretrain_epochs, loss.item()))
            if args.debug_info:
                model.eval()

                if dataset_name != 'ember' and dataset_name != 'imagenet':
                    tools.test(model=model, test_loader=debug_packet['test_set_loader'], poison_test=True,
                           poison_transform=debug_packet['poison_transform'], num_classes=num_classes,
                           source_classes=debug_packet['source_classes'], all_to_all = all_to_all)
                elif dataset_name == 'imagenet':
                    tools.test_imagenet(model=model, test_loader=debug_packet['test_set_loader'],
                                        poison_transform=debug_packet['poison_transform'])
                else:
                    tools.test_ember(model=model, test_loader=debug_packet['test_set_loader'],
                                     backdoor_test_loader=debug_packet['backdoor_test_set_loader'])

    base_ckpt = model.module.state_dict()
    torch.save(base_ckpt, os.path.join(inspection_set_dir, 'base_%d_seed=%d.pt' % (confusion_iter, args.seed)))
    print('save : ', os.path.join(inspection_set_dir, 'base_%d_seed=%d.pt' % (confusion_iter, args.seed)))

    return model


# confusion training : joint training on the poisoned dataset and a randomly labeled small clean set (i.e. confusion set)
def confusion_train(args, params, inspection_set, debug_packet, distilled_set_loader, clean_set_loader, confusion_iter, arch,
                    num_classes, inspection_set_dir, weight_decay, criterion_no_reduction,
                    momentum, lamb, freq, lr, batch_factor, distillation_iters, dataset_name = None):

    all_to_all = False
    if args.poison_type == 'badnet_all_to_all':
        all_to_all = True


    base_model = arch(num_classes = num_classes)
    base_model.load_state_dict(
        torch.load(os.path.join(inspection_set_dir, 'full_base_aug_seed=%d.pt' % (args.seed)))
    )
    base_model = nn.DataParallel(base_model)
    base_model = base_model.cuda()
    base_model.eval()


    ######### Distillation Step ################

    model = arch(num_classes = num_classes)
    model.load_state_dict(
                torch.load(os.path.join(inspection_set_dir, 'base_%d_seed=%d.pt' % (confusion_iter, args.seed)))
    )
    model = nn.DataParallel(model)
    model = model.cuda()

    optimizer = torch.optim.SGD(model.parameters(), lr=lr, weight_decay=weight_decay,
                                momentum=momentum)

    distilled_set_iters = iter(distilled_set_loader)
    clean_set_iters = iter(clean_set_loader)


    rounder = 0

    for batch_idx in tqdm(range(distillation_iters)):

        try:
            data_shift, target_shift = next(clean_set_iters)
        except Exception as e:
            clean_set_iters = iter(clean_set_loader)
            data_shift, target_shift = next(clean_set_iters)
        data_shift, target_shift = data_shift.cuda(), target_shift.cuda()

        if dataset_name != 'ember':
            target_clean = (target_shift + num_classes - 1) % num_classes
            s = len(target_clean)
            with torch.no_grad():
                preds = torch.argmax(base_model(data_shift), dim=1).detach()
                if (rounder + batch_idx) % num_classes == 0:
                    rounder += 1
                next_target = (preds + rounder + batch_idx) % num_classes
                target_confusion = next_target
        else:
           target_confusion = target_shift

        model.train()

        if batch_idx % batch_factor == 0:

            try:
                data, target = next(distilled_set_iters)
            except Exception as e:
                distilled_set_iters = iter(distilled_set_loader)
                data, target = next(distilled_set_iters)

            data, target = data.cuda(), target.cuda()
            data_mix = torch.cat([data_shift, data], dim=0)
            target_mix = torch.cat([target_confusion, target], dim=0)
            boundary = data_shift.shape[0]

            output_mix = model(data_mix)
            loss_mix = criterion_no_reduction(output_mix, target_mix)

            loss_inspection_batch_all = loss_mix[boundary:]
            loss_confusion_batch_all = loss_mix[:boundary]
            loss_confusion_batch = loss_confusion_batch_all.mean()
            target_inspection_batch_all = target_mix[boundary:]
            inspection_batch_size = len(loss_inspection_batch_all)
            loss_inspection_batch = 0
            normalizer = 0
            for i in range(inspection_batch_size):
                gt = int(target_inspection_batch_all[i].item())
                loss_inspection_batch += (loss_inspection_batch_all[i] / freq[gt])
                normalizer += (1 / freq[gt])
            loss_inspection_batch = loss_inspection_batch / normalizer

            weighted_loss = (loss_confusion_batch * (lamb-1) + loss_inspection_batch) / lamb

            loss_confusion_batch = loss_confusion_batch.item()
            loss_inspection_batch = loss_inspection_batch.item()
        else:
            output = model(data_shift)
            weighted_loss = loss_confusion_batch = criterion_no_reduction(output, target_confusion).mean()
            loss_confusion_batch = loss_confusion_batch.item()

        optimizer.zero_grad()
        weighted_loss.backward()
        optimizer.step()

        if (batch_idx + 1) % 500 == 0:

            print('<Round-{} : Distillation Step> Batch_idx: {}, batch_factor: {}, lr: {}, lamb : {}, moment : {}, Loss: {:.6f}'.format(
                confusion_iter, batch_idx + 1, batch_factor, optimizer.param_groups[0]['lr'], lamb, momentum,
                weighted_loss.item()))
            print('inspection_batch_loss = %f, confusion_batch_loss = %f' %
                  (loss_inspection_batch, loss_confusion_batch))

            if args.debug_info:
                model.eval()

                if dataset_name != 'ember' and dataset_name != 'imagenet':
                    tools.test(model=model, test_loader=debug_packet['test_set_loader'], poison_test=True,
                           poison_transform=debug_packet['poison_transform'], num_classes=num_classes,
                           source_classes=debug_packet['source_classes'], all_to_all = all_to_all)
                elif dataset_name == 'imagenet':
                    tools.test_imagenet(model=model, test_loader=debug_packet['test_set_loader'],
                                        poison_transform=debug_packet['poison_transform'])
                else:
                    tools.test_ember(model=model, test_loader=debug_packet['test_set_loader'],
                                     backdoor_test_loader=debug_packet['backdoor_test_set_loader'])

    torch.save( model.module.state_dict(),
               os.path.join(inspection_set_dir, 'confused_%d_seed=%d.pt' % (confusion_iter, args.seed)) )
    print('save : ', os.path.join(inspection_set_dir, 'confused_%d_seed=%d.pt' % (confusion_iter, args.seed)))

    return model


# restore from a certain iteration step
def distill(args, params, inspection_set, n_iter, criterion_no_reduction,
            dataset_name = None, final_budget = None, class_wise = False, custom_arch=None):

    kwargs = params['kwargs']
    inspection_set_dir = params['inspection_set_dir']
    num_classes = params['num_classes']
    num_samples = len(inspection_set)
    arch = params['arch']
    distillation_ratio = params['distillation_ratio']
    num_confusion_iter = len(distillation_ratio) + 1

    if custom_arch is not None:
        arch = custom_arch

    model = arch(num_classes=num_classes)
    ckpt = torch.load(os.path.join(inspection_set_dir, 'confused_%d_seed=%d.pt' % (n_iter, args.seed)))
    model.load_state_dict(ckpt)
    model = nn.DataParallel(model)
    model = model.cuda()
    inspection_set_loader = torch.utils.data.DataLoader(inspection_set, batch_size=256,
                                                            shuffle=False, worker_init_fn=tools.worker_init, **kwargs)

    """
        Collect loss values for inspected samples.
    """
    loss_array = []
    confidence_array = []
    correct_instances = []
    gts = []
    model.eval()
    st = 0
    with torch.no_grad():

        for data, target in tqdm(inspection_set_loader):
            data, target = data.cuda(), target.cuda()
            output = model(data)

            if dataset_name != 'ember':
                preds = torch.argmax(output, dim=1)
            else:
                preds = (output >= 0.5).float()

            prob = torch.softmax(output, dim=1).cpu().numpy()

            batch_loss = criterion_no_reduction(output, target)

            this_batch_size = len(target)

            for i in range(this_batch_size):
                loss_array.append(batch_loss[i].item())
                confidence_array.append(prob[i][target[i].item()])
                gts.append(int(target[i].item()))
                if dataset_name != 'ember':
                    if preds[i] == target[i]:
                        correct_instances.append(st + i)
                else:
                    if preds[i] == target[i]:
                        correct_instances.append(st + i)

            st += this_batch_size

    loss_array = np.array(loss_array)
    sorted_indices = np.argsort(loss_array)


    top_indices_each_class = [[] for _ in range(num_classes)]
    for t in sorted_indices:
        gt = gts[t]
        top_indices_each_class[gt].append(t)

    """
        Distill samples with low loss values from the inspected set.
    """

    if n_iter < num_confusion_iter - 1:

        if distillation_ratio[n_iter] is None:
            distilled_samples_indices = head = correct_instances
        else:
            num_expected = int(distillation_ratio[n_iter] * num_samples)
            head = sorted_indices[:num_expected]
            head = list(head)
            distilled_samples_indices = head

        if n_iter < num_confusion_iter - 2: rate_factor = 50
        else: rate_factor = 100

        if True: #n_iter < num_confusion_iter - 2:

            class_dist = np.zeros(num_classes, dtype=int)
            for i in distilled_samples_indices:
                gt = gts[i]
                class_dist[gt] += 1

            for i in range(num_classes):
                minimal_sample_num = len(top_indices_each_class[i]) // rate_factor
                print('class-%d, collected=%d, minimal_to_collect=%d' % (i, class_dist[i], minimal_sample_num) )
                if class_dist[i] < minimal_sample_num:
                    for k in range(class_dist[i], minimal_sample_num):
                        distilled_samples_indices.append(top_indices_each_class[i][k])

    else:
        if final_budget is not None:
            head = sorted_indices[:final_budget]
            head = list(head)
            distilled_samples_indices = head
        else:
            distilled_samples_indices = head = correct_instances

    distilled_samples_indices.sort()


    median_sample_rate = params['median_sample_rate']
    median_sample_indices = []
    sorted_indices_each_class = [[] for _ in range(num_classes)]
    for temp_id in sorted_indices:
        gt = gts[temp_id]
        sorted_indices_each_class[gt].append(temp_id)



    for i in range(num_classes):
        num_class_i = len(sorted_indices_each_class[i])
        st = int(num_class_i / 2 - num_class_i * median_sample_rate / 2)
        ed = int(num_class_i / 2 + num_class_i * median_sample_rate / 2)
        for temp_id in range(st, ed):
            median_sample_indices.append(sorted_indices_each_class[i][temp_id])

    """Report statistics of the distillation results...
    """
    if args.debug_info:

        print('num_correct : ', len(correct_instances))

        if args.poison_type == 'TaCT' or args.poison_type == 'adaptive_blend' or args.poison_type == 'adaptive_patch':
            cover_indices = torch.load(os.path.join(inspection_set_dir, 'cover_indices'))

        poison_indices = torch.load(os.path.join(inspection_set_dir, 'poison_indices'))

        #for pid in poison_indices:
        #    print('poison confidence : ', confidence_array[pid])


        cnt = 0
        for s, cid in enumerate(head):  # enumerate the head part
            original_id = cid
            if original_id in poison_indices:
                cnt += 1
        print('How Many Poison Samples are Concentrated in the Head? --- %d/%d' % (cnt, len(poison_indices)))

        cover_dist = []
        poison_dist = []

        for temp_id in range(num_samples):

            if sorted_indices[temp_id] in poison_indices:
                poison_dist.append(temp_id)

            if args.poison_type == 'TaCT' or args.poison_type == 'adaptive_blend':
                if sorted_indices[temp_id] in cover_indices:
                    cover_dist.append(temp_id)

        print('poison distribution : ', poison_dist)

        if args.poison_type == 'TaCT' or args.poison_type == 'adaptive_blend' or args.poison_type == 'adaptive_patch':
            print('cover distribution : ', cover_dist)

        num_poison = len(poison_indices)
        num_collected = len(correct_instances)
        pt = 0

        recall = 0
        for idx in correct_instances:
            if pt >= num_poison:
                break
            while (idx > poison_indices[pt] and pt + 1 < num_poison): pt += 1
            if pt < num_poison and poison_indices[pt] == idx:
                recall += 1

        fpr = num_collected - recall
        print('recall = %d/%d = %f, fpr = %d/%d = %f' % (recall, num_poison, recall/num_poison if num_poison!=0 else 0,
                                                             fpr, num_samples - num_poison,
                                                             fpr / (num_samples - num_poison) if (num_samples-num_poison)!=0 else 0))

    if class_wise:
        return distilled_samples_indices, median_sample_indices, top_indices_each_class
    else:
        return distilled_samples_indices, median_sample_indices


