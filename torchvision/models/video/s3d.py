from functools import partial
from typing import Any, Callable, List, Optional

import torch
from torch import nn
from torchvision.ops.misc import Conv3dNormActivation

from .._api import register_model, Weights, WeightsEnum
from .._meta import _KINETICS400_CATEGORIES
from .._utils import _ovewrite_named_param


__all__ = [
    "S3D",
    "S3D_Weights",
    "s3d",
]


class TemporalSeparableConv(nn.Sequential):
    def __init__(self, in_planes: int, out_planes: int, kernel_size: int, stride: int, padding: int = 0):
        super().__init__(
            Conv3dNormActivation(
                in_planes,
                out_planes,
                kernel_size=(1, kernel_size, kernel_size),
                stride=(1, stride, stride),
                padding=(0, padding, padding),
                bias=False,
                norm_layer=partial(nn.BatchNorm3d, eps=1e-3, momentum=1e-3, affine=True),
            ),
            Conv3dNormActivation(
                out_planes,
                out_planes,
                kernel_size=(kernel_size, 1, 1),
                stride=(stride, 1, 1),
                padding=(padding, 0, 0),
                bias=False,
                norm_layer=partial(nn.BatchNorm3d, eps=1e-3, momentum=1e-3, affine=True),
            ),
        )


class SepInceptionBlock3D(nn.Module):
    """Separable Inception block for S3D model.

    Args:
        in_planes (int): dimension of input
        b0_out (int): output dimension of 0th branch.
        b1_mid (int): middle layer dimension of 1st branch.
        b1_out (int) output dimension of 1st branch.
        b2_mid (int): middle layer dimension of 2nd branch.
        b2_out (int): output dimension of 2nd branch.
        b3_out (int): output dimension of 3rd branch.
        norm_layer (Optional[Callable]): Module specifying the normalization layer to use.
    """

    def __init__(self, in_planes: int, branch_layers: List[List[int]], norm_layer: Optional[Callable] = None):
        super().__init__()
        [b0_out], [b1_mid, b1_out], [b2_mid, b2_out], [b3_out] = branch_layers
        if norm_layer is None: 
            norm_layer = partial(nn.BatchNorm3d, eps=0.001, momentum=0.001) 

        self.branch0 = Conv3dNormActivation(
            in_planes, b0_out, kernel_size=1, stride=1, norm_layer=norm_layer
        )
        self.branch1 = nn.Sequential(
            Conv3dNormActivation(
                in_planes, b1_mid, kernel_size=1, stride=1, norm_layer=norm_layer
            ),
            TemporalSeparableConv(b1_mid, b1_out, kernel_size=3, stride=1, padding=1),
        )
        self.branch2 = nn.Sequential(
            Conv3dNormActivation(
                in_planes, b2_mid, kernel_size=1, stride=1, norm_layer=norm_layer
            ),
            TemporalSeparableConv(b2_mid, b2_out, kernel_size=3, stride=1, padding=1),
        )
        self.branch3 = nn.Sequential(
            nn.MaxPool3d(kernel_size=(3, 3, 3), stride=1, padding=1),
            Conv3dNormActivation(
                in_planes, b3_out, kernel_size=1, stride=1, norm_layer=norm_layer
            ),
        )

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        x3 = self.branch3(x)
        out = torch.cat((x0, x1, x2, x3), 1)

        return out


class S3D(nn.Module):
    """S3D main class.

    Args:
        norm_layer (Optional[Callable]): Module specifying the normalization layer to use.
        num_class (int): number of classes for the classification task.
        dropout (float): dropout probability.

    Inputs:
        x (Tensor): batch of videos with dimensions (batch, channel, time, height, width)
    """

    def __init__(
        self,
        norm_layer: Optional[Callable] = None,
        num_classes: int = 400,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        if norm_layer is None: 
            norm_layer = partial(nn.BatchNorm3d, eps=0.001, momentum=0.001) 

        self.features = nn.Sequential(
            TemporalSeparableConv(3, 64, kernel_size=7, stride=2, padding=3),
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 2, 2), padding=(0, 1, 1)),
            Conv3dNormActivation(
                64, 64, kernel_size=1, stride=1, norm_layer=norm_layer,
            ),
            TemporalSeparableConv(64, 192, kernel_size=3, stride=1, padding=1),
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 2, 2), padding=(0, 1, 1)),
            SepInceptionBlock3D(192, 64, 96, 128, 16, 32, 32),
            SepInceptionBlock3D(256, 128, 128, 192, 32, 96, 64),
            nn.MaxPool3d(kernel_size=(3, 3, 3), stride=(2, 2, 2), padding=(1, 1, 1)),
            SepInceptionBlock3D(480, 192, 96, 208, 16, 48, 64),
            SepInceptionBlock3D(512, 160, 112, 224, 24, 64, 64),
            SepInceptionBlock3D(512, 128, 128, 256, 24, 64, 64),
            SepInceptionBlock3D(512, 112, 144, 288, 32, 64, 64),
            SepInceptionBlock3D(528, 256, 160, 320, 32, 128, 128),
            nn.MaxPool3d(kernel_size=(2, 2, 2), stride=(2, 2, 2), padding=(0, 0, 0)),
            SepInceptionBlock3D(832, 256, 160, 320, 32, 128, 128),
            SepInceptionBlock3D(832, 384, 192, 384, 48, 128, 128),
        )
        self.avgpool = nn.AvgPool3d(kernel_size=(2, 7, 7), stride=1)
        self.classifier = nn.Sequential(
            nn.Dropout(p=dropout),
            nn.Conv3d(1024, num_classes, kernel_size=1, stride=1, bias=True),
        )

    def forward(self, x):
        x = self.features(x)
        x = self.avgpool(x)
        x = self.classifier(x)
        x = torch.mean(x, dim=(2, 3, 4))
        return x


class S3D_Weights(WeightsEnum):
    KINETICS400_V1 = Weights(
        url="https://download.pytorch.org/models/s3d.pt",
        transforms=partial(
            crop_size=(224, 224),
            resize_size=(256, 256),
            mean=(0.5, 0.5, 0.5),
            std=(0.5 ,0.5, 0.5),
        ),
        meta={
            "min_size": (224, 224),
            "min_temporal_size": 64,
            "categories": _KINETICS400_CATEGORIES,
            "_docs": "The weights are ported from Min and Corso (2019).",
            "num_params": -1,
            "_metrics": {
                "Kinetics-400": {
                    "acc@1": -1,
                    "acc@5": -1,
                }
            },
        },
    )
    DEFAULT = KINETICS400_V1


@register_model()
def s3d(*, weights: Optional[S3D_Weights] = None, progress: bool = True, **kwargs: Any) -> S3D:
    """Construct Separable 3D CNN model.

    Reference: `Rethinking Spatiotemporal Feature Learning <https://arxiv.org/abs/1712.04851>`__.

    Args:
        weights (:class:`~torchvision.models.video.S3D_Weights`, optional): The
            pretrained weights to use. See
            :class:`~torchvision.models.video.S3D_Weights`
            below for more details, and possible values. By default, no
            pre-trained weights are used.
        progress (bool): If True, displays a progress bar of the download to stderr. Default is True.
        **kwargs: parameters passed to the ``torchvision.models.video.S3D`` base class.
            Please refer to the `source code
            <https://github.com/pytorch/vision/blob/main/torchvision/models/video/s3d.py>`_
            for more details about this class.

    """
    weights = S3D_Weights.verify(weights)

    if weights is not None:
        _ovewrite_named_param(kwargs, "num_classes", len(weights.meta["categories"]))

    model = S3D(**kwargs)

    if weights is not None:
        model.load_state_dict(weights.get_state_dict(progress=progress))

    return model
