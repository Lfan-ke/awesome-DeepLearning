import pycocotools.mask as mask_util
import paddle.nn as nn
import paddle.nn.functional as F

from . initializer import linear_init_

class MLP(nn.Layer):
    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.LayerList(
            nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

        self._reset_parameters()
        
    def _reset_parameters(self):
        for l in self.layers:
            linear_init_(l)

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


class MultiHeadAttentionMap(nn.Layer):
    """This is a 2D attention module, which only returns the attention softmax (no multiplication by value)"""

    def __init__(self, query_dim, hidden_dim, num_heads, dropout=0.0,
                 bias=True):
        super().__init__()
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.dropout = nn.Dropout(dropout)

        weight_attr = paddle.ParamAttr(
            initializer=paddle.nn.initializer.XavierUniform())
        bias_attr = paddle.framework.ParamAttr(
            initializer=paddle.nn.initializer.Constant()) if bias else False

        self.q_proj = nn.Linear(query_dim, hidden_dim, weight_attr, bias_attr)
        self.k_proj = nn.Conv2D(
            query_dim,
            hidden_dim,
            1,
            weight_attr=weight_attr,
            bias_attr=bias_attr)

        self.normalize_fact = float(hidden_dim / self.num_heads)**-0.5

    def forward(self, q, k, mask=None):
        q = self.q_proj(q)
        k = self.k_proj(k)
        bs, num_queries, n, c, h, w = q.shape[0], q.shape[1], self.num_heads,\
                                      self.hidden_dim // self.num_heads, k.shape[-2], k.shape[-1]
        qh = q.reshape([bs, num_queries, n, c])
        kh = k.reshape([bs, n, c, h, w])
        # weights = paddle.einsum("bqnc,bnchw->bqnhw", qh * self.normalize_fact, kh)
        qh = qh.transpose([0, 2, 1, 3]).reshape([-1, num_queries, c])
        kh = kh.reshape([-1, c, h * w])
        weights = paddle.bmm(qh * self.normalize_fact, kh).reshape(
            [bs, n, num_queries, h, w]).transpose([0, 2, 1, 3, 4])

        if mask is not None:
            weights += mask
        # fix a potenial bug: https://github.com/facebookresearch/detr/issues/247
        weights = F.softmax(weights.flatten(3), axis=-1).reshape(weights.shape)
        weights = self.dropout(weights)
        return weights


class MaskHeadFPNConv(nn.Layer):
    """
    Simple convolutional head, using group norm.
    Upsampling is done using a FPN approach
    """

    def __init__(self, input_dim, fpn_dims, context_dim, num_groups=8):
        super().__init__()

        inter_dims = [input_dim,
                      ] + [context_dim // (2**i) for i in range(1, 5)]
        weight_attr = paddle.ParamAttr(
            initializer=paddle.nn.initializer.KaimingUniform())
        bias_attr = paddle.framework.ParamAttr(
            initializer=paddle.nn.initializer.Constant())

        self.conv0 = self._make_layers(input_dim, input_dim, 3, num_groups,
                                       weight_attr, bias_attr)
        self.conv_inter = nn.LayerList()
        for in_dims, out_dims in zip(inter_dims[:-1], inter_dims[1:]):
            self.conv_inter.append(
                self._make_layers(in_dims, out_dims, 3, num_groups, weight_attr,
                                  bias_attr))

        self.conv_out = nn.Conv2D(
            inter_dims[-1],
            1,
            3,
            padding=1,
            weight_attr=weight_attr,
            bias_attr=bias_attr)

        self.adapter = nn.LayerList()
        for i in range(len(fpn_dims)):
            self.adapter.append(
                nn.Conv2D(
                    fpn_dims[i],
                    inter_dims[i + 1],
                    1,
                    weight_attr=weight_attr,
                    bias_attr=bias_attr))

    def _make_layers(self,
                     in_dims,
                     out_dims,
                     kernel_size,
                     num_groups,
                     weight_attr=None,
                     bias_attr=None):
        return nn.Sequential(
            nn.Conv2D(
                in_dims,
                out_dims,
                kernel_size,
                padding=kernel_size // 2,
                weight_attr=weight_attr,
                bias_attr=bias_attr),
            nn.GroupNorm(num_groups, out_dims),
            nn.ReLU())

    def forward(self, x, bbox_attention_map, fpns):
        x = paddle.concat([
            x.tile([bbox_attention_map.shape[1], 1, 1, 1]),
            bbox_attention_map.flatten(0, 1)
        ], 1)
        x = self.conv0(x)
        for inter_layer, adapter_layer, feat in zip(self.conv_inter[:-1],
                                                    self.adapter, fpns):
            feat = adapter_layer(feat).tile(
                [bbox_attention_map.shape[1], 1, 1, 1])
            x = inter_layer(x)
            x = feat + F.interpolate(x, size=feat.shape[-2:])

        x = self.conv_inter[-1](x)
        x = self.conv_out(x)
        return x


class DETRHead(nn.Layer):
    def __init__(self,
                 num_classes=80,
                 hidden_dim=256,
                 nhead=8,
                 num_mlp_layers=3,
                 loss='DETRLoss',
                 fpn_dims=[1024, 512, 256],
                 with_mask_head=False,
                 use_focal_loss=False):
        super(DETRHead, self).__init__()
        # add background class
        self.num_classes = num_classes if use_focal_loss else num_classes + 1
        self.hidden_dim = hidden_dim
        self.loss = loss
        self.with_mask_head = with_mask_head
        self.use_focal_loss = use_focal_loss

        self.score_head = nn.Linear(hidden_dim, self.num_classes)
        self.bbox_head = MLP(hidden_dim,
                             hidden_dim,
                             output_dim=4,
                             num_layers=num_mlp_layers)
        if self.with_mask_head:
            self.bbox_attention = MultiHeadAttentionMap(hidden_dim, hidden_dim,
                                                        nhead)
            self.mask_head = MaskHeadFPNConv(hidden_dim + nhead, fpn_dims,
                                             hidden_dim)
        self._reset_parameters()
    def _reset_parameters(self):
        linear_init_(self.score_head)

    @staticmethod
    def get_gt_mask_from_polygons(gt_poly, pad_mask):
        out_gt_mask = []
        for polygons, padding in zip(gt_poly, pad_mask):
            height, width = int(padding[:, 0].sum()), int(padding[0, :].sum())
            masks = []
            for obj_poly in polygons:
                rles = mask_util.frPyObjects(obj_poly, height, width)
                rle = mask_util.merge(rles)
                masks.append(
                    paddle.to_tensor(mask_util.decode(rle)).astype('float32'))
            masks = paddle.stack(masks)
            masks_pad = paddle.zeros(
                [masks.shape[0], pad_mask.shape[1], pad_mask.shape[2]])
            masks_pad[:, :height, :width] = masks
            out_gt_mask.append(masks_pad)
        return out_gt_mask

    def forward(self, out_transformer, body_feats, inputs=None):
        r"""
        Args:
            out_transformer (Tuple): (feats: [num_levels, batch_size,
                                                num_queries, hidden_dim],
                            memory: [batch_size, hidden_dim, h, w],
                            src_proj: [batch_size, h*w, hidden_dim],
                            src_mask: [batch_size, 1, 1, h, w])
            body_feats (List(Tensor)): list[[B, C, H, W]]
            inputs (dict): dict(inputs)
        """
        feats, memory, src_proj, src_mask = out_transformer
        outputs_logit = self.score_head(feats)
        outputs_bbox = F.sigmoid(self.bbox_head(feats))
        outputs_seg = None
        if self.with_mask_head:
            bbox_attention_map = self.bbox_attention(feats[-1], memory,
                                                     src_mask)
            fpn_feats = [a for a in body_feats[::-1]][1:]
            outputs_seg = self.mask_head(src_proj, bbox_attention_map,
                                         fpn_feats)
            outputs_seg = outputs_seg.reshape([
                feats.shape[1], feats.shape[2], outputs_seg.shape[-2],
                outputs_seg.shape[-1]
            ])

        if self.training:
            assert inputs is not None
            assert 'gt_bbox' in inputs and 'gt_class' in inputs
            gt_mask = self.get_gt_mask_from_polygons(
                inputs['gt_poly'],
                inputs['pad_mask']) if 'gt_poly' in inputs else None
            return self.loss(
                outputs_bbox,
                outputs_logit,
                inputs['gt_bbox'],
                inputs['gt_class'],
                masks=outputs_seg,
                gt_mask=gt_mask)
        else:
            return (outputs_bbox[-1], outputs_logit[-1], outputs_seg)