from tqdm import trange
import torch.nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
from tools.utils import *
from tools.ops import compute_grad_gp, update_average, copy_norm_params, queue_data, dequeue_data, \
    average_gradients, calc_adv_loss, calc_contrastive_loss, calc_recon_loss


def trainGAN(data_loader, networks, opts, epoch, args, additional):
    # avg meter
    d_losses = AverageMeter()
    d_advs = AverageMeter()
    d_gps = AverageMeter()

    g_losses = AverageMeter()
    g_advs = AverageMeter()
    g_imgrecs = AverageMeter()
    g_rec = AverageMeter()

    moco_losses = AverageMeter()

    # set nets
    D = networks['D']
    G = networks['G'] if not args.distributed else networks['G'].module
    C = networks['C'] if not args.distributed else networks['C'].module
    G_EMA = networks['G_EMA'] if not args.distributed else networks['G_EMA'].module
    C_EMA = networks['C_EMA'] if not args.distributed else networks['C_EMA'].module
    # set opts
    d_opt = opts['D']
    g_opt = opts['G']
    c_opt = opts['C']
    # switch to train mode
    D.train()
    G.train()
    C.train()
    C_EMA.train()
    G_EMA.train()

    logger = additional['logger']


    # summary writer
    train_it = iter(data_loader)

    t_train = trange(0, args.iters, initial=0, total=args.iters)

    for i in t_train:
        try:
            imgs, y_org = next(train_it)
            	
            
        except:
            train_it = iter(data_loader)
            imgs, y_org = next(train_it)

        #print("##############################################################################")
	   #print(imgs)
	   #print(y_org)
        x_org = imgs

        #print(imgs.shape)


        x_ref_idx = torch.randperm(x_org.size(0))
        #print('1111111')
        #print(x_ref_idx)

        x_org = x_org.cuda(args.gpu)

        y_org = y_org.cuda(args.gpu)
        x_ref_idx = x_ref_idx.cuda(args.gpu)

        x_ref = x_org.clone()
	   
        #print(x_ref.shape)
        #print('1111111111111111111111111111111111111111111111111111111111')
        x_ref = x_ref[x_ref_idx]

        #print(x_ref_idx)
        

        #print(x_ref.shape)

        training_mode = 'GAN'

        ####################
        # BEGIN Train GANs #
        ####################
        with torch.no_grad():
            y_ref = y_org.clone()
            y_ref = y_ref[x_ref_idx]
            s_ref = C.moco(x_ref)
            c_src, skip1, skip2 = G.cnt_encoder(x_org)
            x_fake, _ = G.decode(c_src, s_ref, skip1, skip2)

        x_ref.requires_grad_()

        d_real_logit, _ = D(x_ref, y_ref)
        d_fake_logit, _ = D(x_fake.detach(), y_ref)
        #print('xref')
        #print(x_ref)
        #print('yref')
        #print(y_ref)
        #print('x_fake.detach()')
        #print(x_fake.detach())
        
        

        d_adv_real = calc_adv_loss(d_real_logit, 'd_real')
        d_adv_fake = calc_adv_loss(d_fake_logit, 'd_fake')

        d_adv = d_adv_real + d_adv_fake

        d_gp = args.w_gp * compute_grad_gp(d_real_logit, x_ref, is_patch=False)

        d_loss = d_adv + d_gp

        d_opt.zero_grad()
        d_adv_real.backward(retain_graph=True)
        d_gp.backward()
        d_adv_fake.backward()
        if args.distributed:
            average_gradients(D)
        d_opt.step()

        # Train G
        s_src = C.moco(x_org)
        s_ref = C.moco(x_ref)

        c_src, skip1, skip2 = G.cnt_encoder(x_org)
        x_fake, offset_loss = G.decode(c_src, s_ref, skip1, skip2)
        x_rec, _ = G.decode(c_src, s_src, skip1, skip2)

        g_fake_logit, _ = D(x_fake, y_ref)
        g_rec_logit, _ = D(x_rec, y_org)

        g_adv_fake = calc_adv_loss(g_fake_logit, 'g')
        g_adv_rec = calc_adv_loss(g_rec_logit, 'g')

        g_adv = g_adv_fake + g_adv_rec

        g_imgrec = calc_recon_loss(x_rec, x_org)

        c_x_fake, _, _ = G.cnt_encoder(x_fake)
        g_conrec = calc_recon_loss(c_x_fake, c_src)

        g_loss = args.w_adv * g_adv + args.w_rec * g_imgrec +args.w_rec * g_conrec + args.w_off * offset_loss
 
        g_opt.zero_grad()
        c_opt.zero_grad()
        g_loss.backward()
        if args.distributed:
            average_gradients(G)
            average_gradients(C)
        c_opt.step()
        g_opt.step()

        ##################
        # END Train GANs #
        ##################


        if epoch >= args.ema_start:
            training_mode = training_mode + "_EMA"
            update_average(G_EMA, G)
        update_average(C_EMA, C)

        torch.cuda.synchronize()

        with torch.no_grad():
            if epoch >= args.separated:
                d_losses.update(d_loss.item(), x_org.size(0))
                d_advs.update(d_adv.item(), x_org.size(0))
                d_gps.update(d_gp.item(), x_org.size(0))

                g_losses.update(g_loss.item(), x_org.size(0))
                g_advs.update(g_adv.item(), x_org.size(0))
                g_imgrecs.update(g_imgrec.item(), x_org.size(0))
                g_rec.update(g_conrec.item(), x_org.size(0))

                moco_losses.update(offset_loss.item(), x_org.size(0))

            if (i + 1) % args.log_step == 0 and (args.gpu == 0 or args.gpu == '0'):
                summary_step = epoch * args.iters + i
                add_logs(args, logger, 'D/LOSS', d_losses.avg, summary_step)
                add_logs(args, logger, 'D/ADV', d_advs.avg, summary_step)
                add_logs(args, logger, 'D/GP', d_gps.avg, summary_step)

                add_logs(args, logger, 'G/LOSS', g_losses.avg, summary_step)
                add_logs(args, logger, 'G/ADV', g_advs.avg, summary_step)
                add_logs(args, logger, 'G/IMGREC', g_imgrecs.avg, summary_step)
                add_logs(args, logger, 'G/conrec', g_rec.avg, summary_step)

                add_logs(args, logger, 'C/OFFSET', moco_losses.avg, summary_step)

                print('Epoch: [{}/{}] [{}/{}] MODE[{}] Avg Loss: D[{d_losses.avg:.2f}] G[{g_losses.avg:.2f}] '.format(epoch + 1, args.epochs, i+1, args.iters,
                                                        training_mode, d_losses=d_losses, g_losses=g_losses))

    copy_norm_params(G_EMA, G)
    copy_norm_params(C_EMA, C)

