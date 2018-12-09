import torch
from torch.autograd import Variable
from torch.autograd import Function
from torchvision import models
from torchvision import utils
import cv2
import sys
import numpy as np
import argparse
from datahelper import imgnet_classes
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns


class FeatureExtractor():
    """ Class for extracting activations and 
    registering gradients from targetted intermediate layers """

    def __init__(self, model, target_layers):
        self.model = model
        self.target_layers = target_layers
        self.gradients = []

    def save_gradient(self, grad):
        self.gradients.append(grad)

    def __call__(self, x):
        outputs = []
        self.gradients = []
        for name, module in self.model._modules.items():
            x = module(x)
            if name in self.target_layers:
                x.register_hook(self.save_gradient)
                outputs += [x]
        return outputs, x


class ModelOutputs():
    """ Class for making a forward pass, and getting:
    1. The network output.
    2. Activations from intermeddiate targetted layers.
    3. Gradients from intermeddiate targetted layers. """

    def __init__(self, model, target_layers):
        self.model = model
        # target_layer_names=["35"]
        self.feature_extractor = FeatureExtractor(self.model.features, target_layers)

    def get_gradients(self):
        return self.feature_extractor.gradients

    def __call__(self, x):
        target_activations, output = self.feature_extractor(x)
        output = output.view(output.size(0), -1)
        output = self.model.classifier(output)
        return target_activations, output


def preprocess_image(img):
    means = [0.485, 0.456, 0.406]
    stds = [0.229, 0.224, 0.225]

    preprocessed_img = img.copy()[:, :, ::-1]
    for i in range(3):
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] - means[i]
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] / stds[i]
    preprocessed_img = \
        np.ascontiguousarray(np.transpose(preprocessed_img, (2, 0, 1)))
    preprocessed_img = torch.from_numpy(preprocessed_img)
    preprocessed_img.unsqueeze_(0)
    input = Variable(preprocessed_img, requires_grad=True)
    return input


def show_cam_on_image(img, mask, save_fname="results/CAM.jpg"):
    heatmap = cv2.applyColorMap(np.uint8(255 * cv2.resize(mask, (224, 224))), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.float32(img)
    cam = cam / np.max(cam)
    cv2.imwrite(save_fname, np.uint8(255 * cam))

    df = pd.DataFrame(mask)
    sns.heatmap(df)
    plt.savefig(save_fname.replace('CAM', 'Heatmap'))
    plt.close()


class GradCam:
    def __init__(self, model, target_layer_names, use_cuda):
        self.model = model  # vgg19
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        self.extractor = ModelOutputs(self.model, target_layer_names)

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        # features [1,512,14,14], output: [1,1k]
        if self.cuda:
            features, output = self.extractor(input.cuda())
        else:
            features, output = self.extractor(input)

        if index == None:
            noutput = output.cpu().data.numpy()[0]
            topk_idxs = noutput.argsort()[-5:]
            index = np.argmax(output.cpu().data.numpy())
            d = 1
        # one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        # one_hot[0][index] = 1
        # one_hot = Variable(torch.from_numpy(one_hot), requires_grad=True)  # [1,1k]
        # # output[0][index]=11.8605
        # # one_hot.item()=11.8605
        # if self.cuda:
        #     one_hot = torch.sum(one_hot.cuda() * output)
        # else:
        #     one_hot = torch.sum(one_hot * output)
        one_hot = output[0][index]
        self.model.features.zero_grad()
        self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        grads_val = self.extractor.get_gradients()[-1].cpu().data.numpy()

        target = features[-1]
        target = target.cpu().data.numpy()[0, :]

        weights = np.mean(grads_val, axis=(2, 3))[0, :]
        cam = np.zeros(target.shape[1:], dtype=np.float32)

        for i, w in enumerate(weights):
            cam += w * target[i, :, :]

        # cam = np.maximum(cam, 0)
        # cam = cv2.resize(cam, (224, 224))
        cam = cam - np.min(cam)
        cam = cam / np.max(cam)
        return cam


class GuidedBackpropReLU(Function):

    def forward(self, input):
        positive_mask = (input > 0).type_as(input)
        output = torch.addcmul(torch.zeros(input.size()).type_as(input), input, positive_mask)
        self.save_for_backward(input, output)
        return output

    def backward(self, grad_output):
        input, output = self.saved_tensors
        grad_input = None

        positive_mask_1 = (input > 0).type_as(grad_output)
        positive_mask_2 = (grad_output > 0).type_as(grad_output)
        grad_input = torch.addcmul(torch.zeros(input.size()).type_as(input),
                                   torch.addcmul(torch.zeros(input.size()).type_as(input), grad_output,
                                                 positive_mask_1), positive_mask_2)

        return grad_input


class GuidedBackpropReLUModel:
    def __init__(self, model, use_cuda):
        self.model = model
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        # replace ReLU with GuidedBackpropReLU
        for idx, module in self.model.features._modules.items():
            if module.__class__.__name__ == 'ReLU':
                self.model.features._modules[idx] = GuidedBackpropReLU()

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        if self.cuda:
            output = self.forward(input.cuda())
        else:
            output = self.forward(input)

        if index == None:
            index = np.argmax(output.cpu().data.numpy())

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0][index] = 1
        one_hot = Variable(torch.from_numpy(one_hot), requires_grad=True)
        if self.cuda:
            one_hot = torch.sum(one_hot.cuda() * output)
        else:
            one_hot = torch.sum(one_hot * output)

        # self.model.features.zero_grad()
        # self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        output = input.grad.cpu().data.numpy()
        output = output[0, :, :, :]
        return output


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--use-cuda', action='store_true', default=False)
    parser.add_argument('--image-path', type=str, default='./examples/000004.jpg')
    args = parser.parse_args()
    args.use_cuda = args.use_cuda and torch.cuda.is_available()
    return args


if __name__ == '__main__':
    args = get_args()
    grad_cam = GradCam(model=models.vgg19(pretrained=True), \
                       target_layer_names=["35"], use_cuda=args.use_cuda)
    img_name = args.image_path.split('/')[-1].split('.')[0]
    img = cv2.imread(args.image_path, 1)
    img = np.float32(cv2.resize(img, (224, 224))) / 255
    input = preprocess_image(img)

    # Find tok and plot
    noutput = grad_cam.forward(input).cpu().data.numpy()[0]
    topk_idxs = noutput.argsort()[-5:][::-1]
    print(noutput[topk_idxs])
    # If None, returns the map for the highest scoring category.
    # Otherwise, targets the requested index.
    # target_index = None
    for topl, target_index in enumerate(topk_idxs):
        print(f'{topl}:{imgnet_classes[target_index]}')
        mask = grad_cam(input, target_index)
        show_cam_on_image(img, mask, save_fname=f'results/{img_name}@{topl}@Class{target_index:03}@CAM.jpg')

        gb_model = GuidedBackpropReLUModel(model=models.vgg19(pretrained=True), use_cuda=args.use_cuda)
        gb = gb_model(input, index=target_index)
        utils.save_image(torch.from_numpy(gb), f'results/{img_name}@{topl}@Class{target_index:03}@GBP.jpg')

        cam_mask = np.zeros(gb.shape)
        mask_resize = cv2.resize(mask, (224, 224))
        for i in range(0, gb.shape[0]):
            cam_mask[i, :, :] = mask_resize

        cam_gb = np.multiply(cam_mask, gb)
        utils.save_image(torch.from_numpy(cam_gb), f'results/{img_name}@{topl}@Class{target_index:03}@RES.jpg')
