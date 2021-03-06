#---------------------------------------------
# Pytorch YOLOv2 - A simplified yolov2 version
# @Author: Noi Truong <noitq.hust@gmail.com>
#---------------------------------------------

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import args
from torch.utils.data import DataLoader
from darknet import Darknet19, conv_bn_leaky
from utils import bbox_ious, BestAnchorFinder
from dataset import VOCDataset

# VOC 2012 dataset
ANCHORS             = [1.3221, 1.73145, 3.19275, 4.00944, 5.05587, 8.09892, 9.47112, 4.84053, 11.2364, 10.0071]
LABELS              = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow',
                        'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
                        'tvmonitor']
IMAGE_W, IMAGE_H    = 416, 416
GRID_W, GRID_H      = 13, 13
OBJ_THRESHOLD       = 0.6
NMS_THRESHOLD       = 0.4

# hyper parameters for loss function
LAMBDA_OBJECT       = 5.0
LAMBDA_NO_OBJECT    = 1.0
LAMBDA_COORD        = 1.0
LAMBDA_CLASS        = 1.0

class ReorgLayer(nn.Module):
    def __init__(self, stride=2):
        super(ReorgLayer, self).__init__()
        self.stride = stride

    def forward(self, x):
        B, C, H, W = x.data.size()
        ws = self.stride
        hs = self.stride
        x = x.view(B, C, int(H / hs), hs, int(W / ws), ws).transpose(3, 4).contiguous()
        x = x.view(B, C, int(H / hs * W / ws), hs * ws).transpose(2, 3).contiguous()
        x = x.view(B, C, hs * ws, int(H / hs), int(W / ws)).transpose(1, 2).contiguous()
        x = x.view(B, hs * ws * C, int(H / hs), int(W / ws))
        return x

class YOLOv2(nn.Module):
    def __init__(self, args):
        super(YOLOv2, self).__init__()

        ### YOLOv2 Config from VOC
        self.ANCHORS            = ANCHORS
        self.BOX                = len(ANCHORS) // 2
        self.LABELS             = LABELS
        self.CLASS              = len(LABELS)
        self.IMAGE_H            = IMAGE_H
        self.IMAGE_W            = IMAGE_W
        self.GRID_H             = GRID_H
        self.GRID_W             = GRID_W
        
        self.LAMBDA_OBJECT      = LAMBDA_OBJECT
        self.LAMBDA_NO_OBJECT   = LAMBDA_NO_OBJECT
        self.LAMBDA_COORD       = LAMBDA_COORD
        self.LAMBDA_CLASS       = LAMBDA_CLASS

        self.OBJ_THRESHOLD      = OBJ_THRESHOLD
        self.NMS_THRESHOLD      = NMS_THRESHOLD

        ### training config
        self.BATCH_SIZE         = int(args.batch_size)
        self.DARKNET19_WEIGHTS  = args.darknet19_weights

        ### create YOLO network components
        # helper
        self.best_anchor_finder = BestAnchorFinder(self.ANCHORS)
        self.darknet19 = Darknet19()

        # take some blocks from darknet 19
        self.conv1 = nn.Sequential(self.darknet19.layer0, self.darknet19.layer1,
                                   self.darknet19.layer2, self.darknet19.layer3, self.darknet19.layer4)
        self.conv2 = self.darknet19.layer5
        # detection layers
        self.conv3 = nn.Sequential(conv_bn_leaky(1024, 1024, kernel_size=3, return_module=True),
                                   conv_bn_leaky(1024, 1024, kernel_size=3, return_module=True))
        self.downsampler = conv_bn_leaky(512, 64, kernel_size=1, return_module=True)
        self.conv4 = nn.Sequential(conv_bn_leaky(1280, 1024, kernel_size=3, return_module=True),
                                   nn.Conv2d(1024, (5 + self.CLASS) * self.BOX, kernel_size=1))
        # reorg 
        self.reorg = ReorgLayer()

        return

    def forward(self, x):
        """
        Only output an feature map that didn't transform.
        """
        x = self.conv1(x)
        shortcut = self.reorg(self.downsampler(x))
        x = self.conv2(x)
        x = self.conv3(x)
        x = torch.cat([shortcut, x], dim=1)
        out = self.conv4(x)

        return out

    def loss(self, y_pred, true_boxes):
        """
        return YOLOv2 loss
        input:
            - Y_pred: YOLOv2 predicted feature map, the output feature map of forward() function
              shape of [N, B*(5+C), Grid, Grid], t_x, t_y, t_w, t_h, t_c, and (class1_score, class2_score, ...)
            - true_boxes: all ground truth boxes, maximum 50 objects
        output:
            YOLOv2 loss includes coordinate loss, confidence score loss, and class loss.
        """

        # true boxes has boxes with size nomalized by input size
        # we need to scale to grid size
        true_boxes = true_boxes.float()
        true_boxes[...,0].mul_(self.GRID_W / self.IMAGE_W)    # x
        true_boxes[...,1].mul_(self.GRID_H / self.IMAGE_H)    # y
        true_boxes[...,2].mul_(self.GRID_W / self.IMAGE_W)    # w
        true_boxes[...,3].mul_(self.GRID_H / self.IMAGE_H)    # h

        # build y_true from ground truth
        y_true      = self.build_target(true_boxes) #  shape of [N, S*S*B, 5 + n_class]

        # prepare grid
        lin_x = torch.arange(0, self.GRID_W).repeat(self.GRID_H, 1).t().contiguous().view(self.GRID_W * self.GRID_H)
        lin_y = torch.arange(0, self.GRID_H).repeat(self.GRID_W, 1).view(self.GRID_W * self.GRID_H)
        
        t_anchors   = torch.Tensor(self.ANCHORS).view(-1, 2) #[BOX, 2]
        anchor_w = t_anchors[:, 0]
        anchor_h = t_anchors[:, 1]

        if torch.cuda.is_available():
            y_pred = y_pred.cuda()
            y_true = y_true.cuda()
            true_boxes = true_boxes.cuda()

            lin_x = lin_x.cuda()
            lin_y = lin_y.cuda()
            anchor_w = anchor_w.cuda()
            anchor_h = anchor_h.cuda()
            

        coord_mask  = y_true.new_zeros([self.BATCH_SIZE, self.GRID_H * self.GRID_W * self.BOX])
        conf_mask   = y_true.new_zeros([self.BATCH_SIZE, self.GRID_H * self.GRID_W * self.BOX])
        class_mask  = y_true.new_zeros([self.BATCH_SIZE, self.GRID_H * self.GRID_W * self.BOX]).byte()

        '''
        Adjust prediction
        '''
        ### y_pred has shape of [N, B*(5+CLASS), S, S], we need it transfromated 
        ### to [N, W, H, B * (5 + CLASS)]
        y_pred = y_pred.permute(0, 2, 3, 1).contiguous()

        ### adjust x, y, w, h
        y_pred          = y_pred.view(self.BATCH_SIZE, self.GRID_H * self.GRID_W, self.BOX, 5 + self.CLASS)     #[N, W*H, B, (5 + CLASS)]
        pred_box_x      = y_pred[..., 0].sigmoid() + lin_x.view(-1, 1)       # [N, W*H, B] + [W*H, 1]      =>   #[N, W*H, B]
        pred_box_y      = y_pred[..., 1].sigmoid() + lin_y.view(-1, 1)       # [N, W*H, B] + [W*H, 1]      =>   #[N, W*H, B]
        pred_box_w      = y_pred[..., 2].exp() * anchor_w.view(-1)           # [N, W*H, B] * [B]           =>   #[N, W*H, B]
        pred_box_h      = y_pred[..., 3].exp() * anchor_h.view(-1)           # [N, W*H, B] * [B]           =>   #[N, W*H, B]

        y_pred          = y_pred.view(self.BATCH_SIZE, self.GRID_H * self.GRID_W * self.BOX, 5 + self.CLASS)
        pred_box_xywh   = torch.cat([pred_box_x.view(self.BATCH_SIZE, -1, 1), pred_box_y.view(self.BATCH_SIZE, -1, 1), \
            pred_box_w.view(self.BATCH_SIZE, -1, 1), pred_box_h.view(self.BATCH_SIZE, -1, 1)], -1)
       
        # adjust confidence score
        pred_box_conf = (y_pred[..., 4]).sigmoid()

        # adjust class propabilities: 
        # - at train time: we do not Softmax cuz we call nn.CrossEntropyLoss
        #   this loss function takes care call to nn.Softmax
        # - at test time, we adjust by calling Softmax.
        pred_box_class = y_pred[...,5:]

        '''
        Adjust ground truth
        '''
        #  get true xy and wh
        true_box_xy = y_true[..., 0:2]
        true_box_wh = y_true[..., 2:4]

        #### adjust true confidence score
        iou_scores = bbox_ious(pred_box_xywh, y_true[..., :4])      # [N, W*H*B]

        true_box_conf = iou_scores.detach() * y_true[..., 4]        # [N, W*H*B]
        
        # adjust class probabilities
        true_box_class = y_true[...,5].long()                       # [N, W*H*B]
        '''
        Determine the mask
        '''
        ### coordinate mask, simply is all predictors
        coord_mask = y_true[..., 4] * self.LAMBDA_COORD             # [N, W*H*B]
        coord_mask = coord_mask.unsqueeze(-1)                       # [N, W*H*B, 1]

        ### confidence mask: penalize predictors and boxes with low IoU
        # first, penalize boxes, which has IoU with any ground truth box < 0.6
        iou_scores = bbox_ious(pred_box_xywh.unsqueeze(2),          # [N, W*H*B, 1, 4]
            true_boxes[..., :4].unsqueeze(1))                       # [N, 1, 50, 4]       
                                                                    # => [N, W*H*B, 50]
        assert iou_scores.shape[1] == self.GRID_H * self.GRID_W * self.BOX and iou_scores.shape[2] == 50    

        best_ious, _ = torch.max(iou_scores, dim=2, keepdim=False)  #[N, W*H*B]

        conf_mask = conf_mask + (best_ious < 0.6).float() * (1 - y_true[..., 4]) * self.LAMBDA_NO_OBJECT

        # second, penalized predictors
        conf_mask = conf_mask + y_true[..., 4] * self.LAMBDA_OBJECT

        ### class mask: simply the positions containing true boxes
        class_mask = y_true[..., 4].bool().view(-1)                 # [N * W * H * B]

        '''
        Finalize the loss
        '''
        # Compute losses
        mse = nn.MSELoss(reduction='sum')
        ce = nn.CrossEntropyLoss(reduction='sum')

        # count number
        nb_coord_box = (coord_mask > 0.0).float().sum()
        nb_conf_box = (conf_mask > 0.0).float().sum()
        nb_class_box = (class_mask > 0.0).float().sum()

        # loss_xywh
        # print("pred_box_xyhw at x:6, y:5,: ", pred_box_xywh.view(self.BATCH_SIZE, self.GRID_W, self.GRID_H, self.BOX, 4)[0, 6, 5, :, :])
        # print("pred_box_xyhw at x:4, y:10,: ", pred_box_xywh.view(self.BATCH_SIZE, self.GRID_W, self.GRID_H, self.BOX, 4)[0, 4, 10, :, :])
        loss_xy = mse(pred_box_xywh[..., 0:2] * coord_mask, true_box_xy * coord_mask) / (nb_coord_box + 1e-6)

        loss_wh = mse(pred_box_xywh[..., 2:4] * coord_mask, true_box_wh * coord_mask) / (nb_coord_box + 1e-6)

        # loss_class
        loss_class = ce(pred_box_class.view(-1, self.CLASS)[class_mask], true_box_class.view(-1)[class_mask]) / (nb_class_box + 1e-6) * self.LAMBDA_CLASS

        # loss_confidence
        loss_conf = mse(pred_box_conf * conf_mask, true_box_conf * conf_mask) / (nb_conf_box + 1e-6)
        loss = loss_xy + loss_wh + loss_class + loss_conf

        return loss_xy, loss_wh, loss_conf, loss_class

    def build_target(self, ground_truth):
        """
        Build target output y_true with shape of [N, S*S*B, 5+1]
        """

        y_true = np.zeros([self.BATCH_SIZE, self.GRID_W, self.GRID_H, self.BOX, 4 + 1 + 1], dtype=np.float)

        for iframe in range(self.BATCH_SIZE):
            for obj in ground_truth[iframe]:
                if obj[2] == 0 and obj[3] == 0: 
                    # both w and h are zero
                    break
                center_x, center_y, w, h, class_index = obj

                grid_x = int(np.floor(center_x))
                grid_y = int(np.floor(center_y))

                assert grid_x < self.GRID_W and grid_y < self.GRID_H and class_index < self.CLASS

                box = [center_x, center_y, w, h]
                best_anchor, best_iou = self.best_anchor_finder.find(w, h)

                y_true[iframe, grid_x, grid_y, best_anchor, :4] = box
                y_true[iframe, grid_x, grid_y, best_anchor, 4] = 1.
                y_true[iframe, grid_x, grid_y, best_anchor, 5] = int(class_index)

        y_true = y_true.reshape([self.BATCH_SIZE, -1, 6])

        return torch.from_numpy(y_true).float()

    def load_weight(self):
        self.darknet19.load_weight(self.DARKNET19_WEIGHTS)

if __name__ == "__main__":
    args_ = args.arg_parse()
    net = YOLOv2(args_)
    net.load_weight("./darknet19_448.conv.23")

    training_set = VOCDataset("D:/dataset/VOC/VOCdevkit/", "2012", "train", image_size=net.IMAGE_W)
    dataloader = DataLoader(training_set, batch_size=net.BATCH_SIZE)

    print("Training len: ", len(dataloader))
    for iter, batch in enumerate(dataloader):
        input, label = batch

        output = net.forward(input)
        print("Input shape: ", input.shape)
        print("Output shape: ", output.shape)

        loss = net.loss(output, label)
        
        break


    # input = torch.randn(4, 3, IMAGE_H, IMAGE_W)
    
    