# imports
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os
import glob
import random
import numpy as np
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms  # noqa

import imgaug
from imgaug import augmenters as iaa
from PIL import Image
from tqdm import tqdm
import matplotlib.pyplot as plt

import sklearn.metrics
import io
import itertools
from six.moves import range

import sys
from ..helpers.utils import *
import time
import cv2
from skimage.color import rgb2hsv
from skimage.filters import threshold_otsu

# DataLoader Implementation
class WSIStridedPatchDataset(Dataset):
    """
    Data producer that generate all the square grids, e.g. 3x3, of patches,
    from a WSI and its tissue mask, and their corresponding indices with
    respect to the tissue mask
    """
    def __init__(self, wsi_path, mask_path, label_path=None, image_size=256,
                 normalize=True, flip='NONE', rotate='NONE',                
                 sampling_stride=16, roi_masking=True):
        """
        Initialize the data producer.

        Arguments:
            wsi_path: string, path to WSI file
            mask_path: string, path to mask file in numpy format OR None
            label_mask_path: string, path to ground-truth label mask path in tif file or
                            None (incase of Normal WSI or test-time)
            image_size: int, size of the image before splitting into grid, e.g. 768
            patch_size: int, size of the patch, e.g. 256
            crop_size: int, size of the final crop that is feed into a CNN,
                e.g. 224 for ResNet
            normalize: bool, if normalize the [0, 255] pixel values to [-1, 1],
                mostly False for debuging purpose
            flip: string, 'NONE' or 'FLIP_LEFT_RIGHT' indicating the flip type
            rotate: string, 'NONE' or 'ROTATE_90' or 'ROTATE_180' or
                'ROTATE_270', indicating the rotate type
            roi_masking: True: Multiplies the strided WSI with tissue mask to eliminate white spaces,
                                False: Ensures inference is done on the entire WSI   
            sampling_stride: Number of pixels to skip in the tissue mask, basically it's the overlap
                            fraction when patches are extracted from WSI during inference.
                            stride=1 -> consecutive pixels are utilized
                            stride= image_size/pow(2, level) -> non-overalaping patches 
        """
        self._wsi_path = wsi_path
        self._mask_path = mask_path
        self._label_path = label_path
        self._image_size = image_size
        self._normalize = normalize
        self._flip = flip
        self._rotate = rotate
        self._sampling_stride = sampling_stride
        self._roi_masking = roi_masking
        self._preprocess()

    def _preprocess(self):
        self._slide = np.array(ReadWholeSlideImage(self._wsi_path).convert('RGB'))
        factor = self._sampling_stride
        X_slide, Y_slide, _ = self._slide.shape

        if self._label_path is not None:
            self._label_slide = np.array(ReadWholeSlideImage(self._label_path).convert('L'))
        
        if self._mask_path is not None:
            self._mask = np.array(ReadWholeSlideImage(self._mask_path).convert('L'))
        else:
            # Generate tissue mask on the fly    
            self._mask = TissueMaskGeneration(self._slide)
           
        # morphological operations ensure the holes are filled in tissue mask
        # and minor points are aggregated to form a larger chunk         

        self._mask = BinMorphoProcessMask(np.uint8(self._mask))
        # self._all_bbox_mask = get_all_bbox_masks(self._mask, factor)
        # self._largest_bbox_mask = find_largest_bbox(self._mask, factor)
        # self._all_strided_bbox_mask = get_all_bbox_masks_with_stride(self._mask, factor)

        X_mask, Y_mask = self._mask.shape
#         imshow(self._slide, self._mask)
             
        # all the idces for tissue region from the tissue mask  
        self._strided_mask =  np.ones_like(self._mask)
        ones_mask = np.zeros_like(self._mask)
        ones_mask[::factor, ::factor] = 1 #self._strided_mask[::factor, ::factor]
        if self._roi_masking:
            self._strided_mask = ones_mask*self._mask   
            # self._strided_mask = ones_mask*self._largest_bbox_mask   
            # self._strided_mask = ones_mask*self._all_bbox_mask 
            # self._strided_mask = self._all_strided_bbox_mask  
        else:
            self._strided_mask = ones_mask 
            
        # print (np.count_nonzero(self._strided_mask), np.count_nonzero(self._mask[::factor, ::factor]))
        # imshow(self._strided_mask.T, self._mask[::factor, ::factor].T)
        # imshow(self._mask.T, self._strided_mask.T)
 
        self._X_idcs, self._Y_idcs = [], []
        tempX, tempY = np.where(self._strided_mask)
        print (tempX.max(), tempX.min(), tempY.max(), tempY.min(), len(np.unique(tempX)), len(np.unique(tempY)))
        for x_, y_ in zip(tempX, tempY):
            case1 = (x_ - self._image_size//2) > 0
            case2 = (x_ + self._image_size//2) < X_mask
            case3 = (y_ - self._image_size//2) > 0
            case4 = (y_ + self._image_size//2) < Y_mask 
            if (case1 and case2 and case3 and case4):
                self._X_idcs.append(x_)
                self._Y_idcs.append(y_)
                
        self._X_idcs = np.array(self._X_idcs)
        self._Y_idcs = np.array(self._Y_idcs)
        self._idcs_num = len(self._X_idcs)

    def __len__(self):        
        return self._idcs_num 

    def save_get_mask(self, save_path):
        np.save(save_path, self._mask)

    def get_mask(self):
        return self._mask
    
    def get_label_mask(self):
        return self._label_slide
    
    def get_image(self):
        return self._slide

    def get_strided_mask(self):
        return self._strided_mask
    
    def __getitem__(self, idx):
        x_coord, y_coord = self._X_idcs[idx], self._Y_idcs[idx]   

        x = int(x_coord)
        y = int(y_coord)    
        img = getImagePatch(self._slide, (x, y), self._image_size)
        
        if self._label_path is not None:
            label_img = getImagePatch(self._label_slide, (x, y), self._image_size)
        else:
            label_img = Image.fromarray(np.zeros((self._image_size, self._image_size), dtype=np.uint8))
        
        if self._flip == 'FLIP_LEFT_RIGHT':
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            label_img = label_img.transpose(Image.FLIP_LEFT_RIGHT)
            
        if self._rotate == 'ROTATE_90':
            img = img.transpose(Image.ROTATE_90)
            label_img = label_img.transpose(Image.ROTATE_90)
            
        if self._rotate == 'ROTATE_180':
            img = img.transpose(Image.ROTATE_180)
            label_img = label_img.transpose(Image.ROTATE_180)

        if self._rotate == 'ROTATE_270':
            img = img.transpose(Image.ROTATE_270)
            label_img = label_img.transpose(Image.ROTATE_270)

        # PIL image:   H x W x C
        img = np.array(img, dtype=np.float32)
        label_img = np.array(label_img, dtype=np.uint8)

        if self._normalize:
            img = (img - 128.0)/128.0
        
        return (img, x_coord, y_coord, label_img)


