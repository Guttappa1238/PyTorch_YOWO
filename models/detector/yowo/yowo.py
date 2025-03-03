# This is a frame-level model which is set as the Baseline
import numpy as np
import torch
import torch.nn as nn

from ...backbone import build_backbone_2d
from ...backbone import build_backbone_3d
from .encoder import ChannelEncoder
from .loss import Criterion



class YOWO(nn.Module):
    def __init__(self, 
                 cfg,
                 device,
                 img_size,
                 anchor_size,
                 len_clip = 16,
                 num_classes = 20, 
                 conf_thresh = 0.05,
                 nms_thresh = 0.6,
                 topk = 1000,
                 trainable = False,
                 multi_hot = False):
        super(YOWO, self).__init__()
        self.cfg = cfg
        self.device = device
        self.img_size = img_size
        self.stride = cfg['stride']
        self.len_clip = len_clip
        self.num_classes = num_classes
        self.trainable = trainable
        self.multi_hot = multi_hot
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.topk = topk
        self.num_anchors = len(anchor_size)
        self.anchor_size = torch.as_tensor(anchor_size)

        # ------------------ Anchor Box --------------------
        # [M, 4]
        self.anchor_boxes = self.generate_anchors(img_size)

        # ------------------ Network ---------------------
        # 2D backbone
        self.backbone_2d, bk_dim_2d = build_backbone_2d(
            model_name=cfg['backbone_2d'], 
            pretrained=cfg['pretrained_2d'] and trainable
            )
            
        # 3D backbone
        self.backbone_3d, bk_dim_3d = build_backbone_3d(
            model_name=cfg['backbone_3d'],
            pretrained=cfg['pretrained_2d'] and trainable
        )

        # channel encoder
        self.channel_encoder = ChannelEncoder(
            in_dim=bk_dim_2d + bk_dim_3d,
            out_dim=cfg['head_dim'],
            act_type=cfg['head_act'],
            norm_type=cfg['head_norm']
        )

        # output
        self.pred = nn.Conv2d(cfg['head_dim'], self.num_anchors * (1 + num_classes + 4), kernel_size=1)


        if trainable:
            # init bias
            self._init_bias()

        # ------------------ Criterion ---------------------
        if self.trainable:
            self.criterion = Criterion(
                cfg=cfg,
                device=device,
                anchor_size=self.anchor_size,
                num_anchors=self.num_anchors,
                num_classes=self.num_classes,
                multi_hot=self.multi_hot,
                loss_obj_weight=cfg['loss_obj_weight'],
                loss_noobj_weight=cfg['loss_noobj_weight'],
                loss_cls_weight=cfg['loss_cls_weight'],
                loss_reg_weight=cfg['loss_reg_weight'],
                )


    def _init_bias(self):  
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        # init bias
        nn.init.constant_(self.pred.bias[..., :self.num_anchors], bias_value)
        nn.init.constant_(self.pred.bias[..., self.num_anchors:self.num_anchors*(1 + self.num_classes)], bias_value)


    def generate_anchors(self, img_size):
        """fmp_size: list -> [H, W] \n
           stride: int -> output stride
        """
        # generate grid cells
        img_h = img_w = img_size
        fmp_h, fmp_w = img_h // self.stride, img_w // self.stride
        anchor_y, anchor_x = torch.meshgrid([torch.arange(fmp_h), torch.arange(fmp_w)])
        # [H, W, 2] -> [HW, 2]
        anchor_xy = torch.stack([anchor_x, anchor_y], dim=-1).float().view(-1, 2)
        # [HW, 2] -> [HW, 1, 2] -> [HW, KA, 2] 
        anchor_xy = anchor_xy[:, None, :].repeat(1, self.num_anchors, 1)
        anchor_xy *= self.stride

        # [KA, 2] -> [1, KA, 2] -> [HW, KA, 2]
        anchor_wh = self.anchor_size[None, :, :].repeat(fmp_h*fmp_w, 1, 1)

        # [HW, KA, 4] -> [M, 4]
        anchor_boxes = torch.cat([anchor_xy, anchor_wh], dim=-1)
        anchor_boxes = anchor_boxes.view(-1, 4).to(self.device)

        return anchor_boxes
        

    def decode_bbox(self, anchors, reg_pred):
        """
        Input:
            anchors:  [B, M, 4] or [M, 4]
            reg_pred: [B, M, 4] or [M, 4]
        Output:
            box_pred: [B, M, 4] or [M, 4]
        """
        # txty -> cxcy
        xy_pred = reg_pred[..., :2].sigmoid() * self.stride + anchors[..., :2]
        # twth -> wh
        wh_pred = reg_pred[..., 2:].exp() * anchors[..., 2:]

        # xywh -> x1y1x2y2
        x1y1_pred = xy_pred - wh_pred * 0.5
        x2y2_pred = xy_pred + wh_pred * 0.5
        box_pred = torch.cat([x1y1_pred, x2y2_pred], dim=-1)

        return box_pred


    def nms(self, dets, scores):
        """"Pure Python NMS."""
        x1 = dets[:, 0]  #xmin
        y1 = dets[:, 1]  #ymin
        x2 = dets[:, 2]  #xmax
        y2 = dets[:, 3]  #ymax

        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]

        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            # compute iou
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-28, xx2 - xx1)
            h = np.maximum(1e-28, yy2 - yy1)
            inter = w * h

            ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-14)
            #reserve all the boundingbox whose ovr less than thresh
            inds = np.where(ovr <= self.nms_thresh)[0]
            order = order[inds + 1]

        return keep


    def bbox_iou(self, box1, box2):
        mx = min(box1[0], box2[0])
        Mx = max(box1[2], box2[2])
        my = min(box1[1], box2[1])
        My = max(box1[3], box2[3])
        w1 = box1[2] - box1[0]
        h1 = box1[3] - box1[1]
        w2 = box2[2] - box2[0]
        h2 = box2[3] - box2[1]
        uw = Mx - mx
        uh = My - my
        cw = w1 + w2 - uw
        ch = h1 + h2 - uh
        carea = 0
        if cw <= 0 or ch <= 0:
            return 0.0

        area1 = w1 * h1
        area2 = w2 * h2
        carea = cw * ch
        uarea = area1 + area2 - carea

        return carea / uarea


    def post_process_one_hot(self, conf_pred, cls_pred, reg_pred):
        # scores
        scores, labels = torch.max(torch.sigmoid(conf_pred) *\
                                    torch.softmax(cls_pred, dim=-1), dim=-1)

        # topk
        anchor_boxes = self.anchor_boxes
        if scores.shape[0] > self.topk:
            scores, indices = torch.topk(scores, self.topk)
            labels = labels[indices]
            reg_pred = reg_pred[indices]
            anchor_boxes = anchor_boxes[indices]

        # decode box
        bboxes = self.decode_bbox(anchor_boxes, reg_pred) # [N, 4]
        # normalize box
        bboxes = torch.clamp(bboxes / self.img_size, 0., 1.)
        
        # to cpu
        scores = scores.cpu().numpy()
        labels = labels.cpu().numpy()
        bboxes = bboxes.cpu().numpy()
        
        # threshold
        keep = np.where(scores >= self.conf_thresh)
        scores = scores[keep]
        labels = labels[keep]
        bboxes = bboxes[keep]

        # nms
        keep = np.zeros(len(bboxes), dtype=np.int)
        for i in range(self.num_classes):
            inds = np.where(labels == i)[0]
            if len(inds) == 0:
                continue
            c_bboxes = bboxes[inds]
            c_scores = scores[inds]
            c_keep = self.nms(c_bboxes, c_scores)
            keep[inds[c_keep]] = 1

        keep = np.where(keep > 0)
        scores = scores[keep]
        labels = labels[keep]
        bboxes = bboxes[keep]

        return scores, labels, bboxes
    

    def post_process_multi_hot(self, conf_pred, cls_pred, reg_pred):
        # decode box
        bboxes = self.decode_bbox(self.anchor_boxes, reg_pred)       # [M, 4]
        # normalize box
        bboxes = torch.clamp(bboxes / self.img_size, 0., 1.)
        
        # conf pred & cls pred (for AVA dataset)
        conf_pred = torch.sigmoid(conf_pred)                         # [M, 1]
        pose_cls_pred = torch.softmax(cls_pred[..., :14], dim=-1)
        act_cls_pred = torch.sigmoid(cls_pred[..., 14:])
        cls_pred = torch.cat([pose_cls_pred, act_cls_pred], dim=-1)  # [M, C]

        all_bboxes = []
        for i in range(conf_pred.shape[0]):
            pred_box_conf =  conf_pred[i].item()
            pred_box = bboxes[i]
            pred_cls_conf = cls_pred[i]

            if pred_box_conf > self.conf_thresh:
                x1, y1, x2, y2 = pred_box
                box = [x1, y1, x2, y2, pred_box_conf, pred_cls_conf]
                all_bboxes.append(box)

        # nms
        if len(all_bboxes) > 0:
            det_confs = torch.zeros(len(all_bboxes))
            for i in range(len(all_bboxes)):
                det_confs[i] = 1.0 - all_bboxes[i][4]                

            _, sortIds = torch.sort(det_confs)

            out_boxes = []
            for i in range(len(all_bboxes)):
                box_i = all_bboxes[sortIds[i]]
                if box_i[4] > 0:
                    out_boxes.append(box_i)
                    for j in range(i+1, len(all_bboxes)):
                        box_j = all_bboxes[sortIds[j]]
                        if self.bbox_iou(box_i, box_j) > self.nms_thresh:
                            box_j[4] = 0

            return out_boxes

        else:
            return all_bboxes
    

    @torch.no_grad()
    def inference(self, video_clips):
        """
        Input:
            video_clips: (Tensor) -> [B, 3, T, H, W].
        """                        
        key_frame = video_clips[:, :, -1, :, :]
        # backbone
        feat_2d = self.backbone_2d(key_frame)               # [B, C1, H, W]
        feat_3d = self.backbone_3d(video_clips).squeeze(2)  # [B, C2, H, W]

        # channel encoder
        feat = self.channel_encoder(torch.cat([feat_2d, feat_3d], dim=1))

        # pred
        pred = self.pred(feat)

        B, K, C = pred.size(0), self.num_anchors, self.num_classes
        # [B, K*C, H, W] -> [B, H, W, K*C] -> [B, M, C], M = HWK
        conf_pred = pred[:, :K, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, 1)
        cls_pred = pred[:, K:K*(1 + C), :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, C)
        reg_pred = pred[:, K*(1 + C):, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, 4)

        if self.multi_hot:
            batch_bboxes = []
            for batch_idx in range(conf_pred.size(0)):
                # [B, M, C] -> [M, C]
                cur_conf_pred = conf_pred[batch_idx]
                cur_cls_pred = cls_pred[batch_idx]
                cur_reg_pred = reg_pred[batch_idx]

                out_boxes = self.post_process_multi_hot(cur_conf_pred, cur_cls_pred, cur_reg_pred)

                batch_bboxes.append(out_boxes)

            return batch_bboxes

        else:
                        
            batch_scores = []
            batch_labels = []
            batch_bboxes = []
            for batch_idx in range(conf_pred.size(0)):
                # [B, M, C] -> [M, C]
                cur_conf_pred = conf_pred[batch_idx]
                cur_cls_pred = cls_pred[batch_idx]
                cur_reg_pred = reg_pred[batch_idx]
                # post-process
                scores, labels, bboxes = self.post_process_one_hot(cur_conf_pred, cur_cls_pred, cur_reg_pred)

                batch_scores.append(scores)
                batch_labels.append(labels)
                batch_bboxes.append(bboxes)

            return batch_scores, batch_labels, batch_bboxes


    def forward(self, video_clips, targets=None):
        """
        Input:
            video_clips: (Tensor) -> [B, 3, T, H, W].
            targets: (List) -> [[x1, y1, x2, y2, label, frame_id], ...]
        """                        
        if not self.trainable:
            return self.inference(video_clips)
        else:
            key_frame = video_clips[:, :, -1, :, :]
            # backbone
            feat_2d = self.backbone_2d(key_frame)               # [B, C1, H, W]
            feat_3d = self.backbone_3d(video_clips).squeeze(2)  # [B, C2, H, W]

            # channel encoder
            feat = self.channel_encoder(torch.cat([feat_2d, feat_3d], dim=1))

            # pred
            pred = self.pred(feat)

            B, K, C = pred.size(0), self.num_anchors, self.num_classes
            # [B, K*C, H, W] -> [B, H, W, K*C] -> [B, M, C], M = HWK
            conf_pred = pred[:, :K, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, 1)
            cls_pred = pred[:, K:K*(1 + C), :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, C)
            reg_pred = pred[:, K*(1 + C):, :, :].permute(0, 2, 3, 1).contiguous().view(B, -1, 4)

            # decode box
            box_pred = self.decode_bbox(self.anchor_boxes[None], reg_pred)

            outputs = {"conf_pred": conf_pred,
                       "cls_pred": cls_pred,
                       "box_pred": box_pred,
                       "anchor_size": self.anchor_size,
                       "img_size": self.img_size,
                       "stride": self.stride}

            # loss
            loss_dict = self.criterion(
                outputs=outputs, 
                targets=targets, 
                video_clips=video_clips
                )

            return loss_dict
