from typing import Optional, Callable
import itertools

import torch
from torch import nn
from torch.nn.functional import cross_entropy
import pytorch_lightning as pl
from pytorch_lightning.metrics.functional import accuracy
from torch.optim import Optimizer
from transformers import Wav2Vec2Model, Wav2Vec2Config
from scripts.models.wav2vec2_modified import Wav2VecModelOverridden


class Wav2VecBase(pl.LightningModule):
    def __init__(self, num_classes):
        super(Wav2VecBase, self).__init__()

    def training_step(self, batch, batch_idx, optimizer_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('train_loss', loss, on_step=True)
        y_hat = torch.argmax(y_hat, dim=1)
        acc = accuracy(y_hat, y)
        self.log('val_acc', acc, on_step=True)
        return loss

    def validation_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('val_loss', loss, on_epoch=True)
        y_hat = torch.argmax(y_hat, dim=1)
        acc = accuracy(y_hat, y)
        self.log('val_acc', acc, on_epoch=True)
        return loss

    def test_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('test_loss', loss, on_epoch=True)
        y_hat = torch.argmax(y_hat, dim=1)
        acc = accuracy(y_hat, y)
        self.log('test_acc', acc, on_epoch=True)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=2e-5)
        return optimizer


class Wav2VecCLSPaperFinetuning(Wav2VecBase):

    def __init__(self, num_classes, learning_rate, num_epochs):
        super(Wav2VecCLSPaperFinetuning, self).__init__(num_classes)

        self.lr = learning_rate
        self.num_epochs = num_epochs

        # We replace the pretrained model with the one with the CLS token
        self.pretrained_model = Wav2VecModelOverridden.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        # freezing the feature extractor (we are not going to finetune it)
        for name, param in self.pretrained_model.feature_extractor.named_parameters():
            param.requires_grad = False

        # then we add on top the classification layer to be trained
        self.linear_layer = torch.nn.Linear(self.pretrained_model.config.hidden_size, num_classes)

    def forward(self, x):
        cls_token, _ = self.pretrained_model(x)

        y_pred = self.linear_layer(cls_token)
        return y_pred

    # here we must define the optimizer and the different learning rate
    def configure_optimizers(self):
        optimizer_linear_layer = torch.optim.Adam(params=self.linear_layer.parameters(), lr=self.lr)

        params = [self.pretrained_model.feature_projection.parameters(),
                  self.pretrained_model.encoder.parameters(),
                  self.linear_layer.parameters()]
        optimizer_linear_and_encoder = torch.optim.Adam(
            # params=itertools.chain(*params),
            params=itertools.chain(*params),
            lr=self.lr)
        return optimizer_linear_layer, optimizer_linear_and_encoder

    def optimizer_step(
            self,
            epoch: int = None,
            batch_idx: int = None,
            optimizer: Optimizer = None,
            optimizer_idx: int = None,
            optimizer_closure: Optional[Callable] = None,
            on_tpu: bool = None,
            using_native_amp: bool = None,
            using_lbfgs: bool = None,
    ) -> None:

        # for the first 30% of updates we train only the linear layer
        # for the rest of the updates the encoder gets finetuned as well
        if (0.3 >= epoch / self.num_epochs and optimizer_idx == 0) or \
                (0.3 < epoch / self.num_epochs and optimizer_idx == 1):

            # warm-up for the first 10%
            if epoch < self.num_epochs // 10:
                lr_scale = min(1., float(epoch + 1) / float(self.num_epochs // 10))
                for pg in optimizer.param_groups:
                    pg['lr'] = lr_scale * optimizer.defaults["lr"]
            # constant learning rate for the next 40%
            # linearly decaying for the final 50%
            elif epoch >= self.num_epochs // 2:
                lr_scale = min(1.,
                               1 - (float(epoch - self.num_epochs // 2) / float(
                                   self.num_epochs - self.num_epochs // 2)))
                for pg in optimizer.param_groups:
                    pg['lr'] = lr_scale * optimizer.defaults["lr"]

            # update params
            optimizer.step(closure=optimizer_closure)


class Wav2VecFeatureExtractor(Wav2VecBase):
    def __init__(self, num_classes, pretrained_out_dim=(512, 226), finetune_pretrained=True):
        super(Wav2VecFeatureExtractor, self).__init__(num_classes=num_classes)
        self.finetune_pretrained = finetune_pretrained

        # First we take the pretrained xlsr model
        complete_pretrained_model = Wav2Vec2Model.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        self.pretrained_model = complete_pretrained_model.feature_extractor

        # setting require grad = true only if we want to fine tune the pretrained model
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = self.finetune_pretrained

        # then we add on top the classification layers to be trained
        self.linear_projector = nn.Sequential(
            nn.Linear(torch.prod(torch.tensor(pretrained_out_dim)).item(), num_classes)
        )

    def forward(self, x):
        with torch.enable_grad() if self.finetune_pretrained else torch.no_grad():
            # the features are like a spectrogram, an image with one channel
            features = self.pretrained_model(x)

        # first we flatten everything
        features = torch.flatten(features, start_dim=1)
        # then we use the linear projection for prediction
        y_pred = self.linear_projector(features)
        return y_pred


class Wav2VecFeatureExtractorGAP(Wav2VecBase):
    def __init__(self, num_classes, finetune_pretrained=True):
        super(Wav2VecFeatureExtractorGAP, self).__init__(num_classes=num_classes)
        self.finetune_pretrained = finetune_pretrained

        # First we take the pretrained xlsr model
        complete_pretrained_model = Wav2Vec2Model.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        self.pretrained_model = complete_pretrained_model.feature_extractor

        # setting require grad = true only if we want to fine tune the pretrained model
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = self.finetune_pretrained

        self.cls_net = nn.Sequential(
            nn.Conv2d(in_channels=1, out_channels=64, kernel_size=3),
            nn.Sigmoid(),
            nn.Conv2d(in_channels=64, out_channels=num_classes, kernel_size=3),
            nn.AdaptiveAvgPool2d(output_size=(1, 1))
        )

    def forward(self, x):
        with torch.enable_grad() if self.finetune_pretrained else torch.no_grad():
            # the features are like a one channel image
            features = self.pretrained_model(x)

        # we need to add the first channel to the "image"
        features = self.cls_net(torch.unsqueeze(features, dim=1))
        # we feed this image in the cls_net that gives the classification tensor
        y_pred = torch.reshape(features, shape=(features.shape[0], features.shape[1]))
        return y_pred


class Wav2VecCLSToken(Wav2VecBase):

    def __init__(self, num_classes):
        super(Wav2VecCLSToken, self).__init__(num_classes)

        # We replace the pretrained model with the one with the CLS token
        self.pretrained_model = Wav2VecModelOverridden.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        # we don't want to get the masks
        # self.pretrained_model.config.mask_time_prob = 0

        # require grad for all the model:
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = True
        """
        # then freezing the encoder only, except for the normalization layers that we want to fine-tune:
        for name, param in self.pretrained_model.encoder.named_parameters():
            if "layer_norm" not in name:
                param.requires_grad = False
        """

        pretrained_out_dim = self.pretrained_model.config.hidden_size
        # then we add on top the classification layer to be trained
        self.linear_layer = torch.nn.Linear(pretrained_out_dim, num_classes)

    def forward(self, x):
        cls_token, _ = self.pretrained_model(x)

        y_pred = self.linear_layer(cls_token)
        return y_pred


class Wav2VecCLSTokenNotPretrained(Wav2VecBase):

    def __init__(self, num_classes):
        super(Wav2VecCLSTokenNotPretrained, self).__init__(num_classes)

        # getting the config for constructing the model randomly initialized
        model_config = Wav2Vec2Config("facebook/wav2vec2-large-xlsr-53")

        # we don't want to get the masks
        model_config.mask_time_prob = 0

        # We replace the pretrained model with a non pretrained architecture with CLS token
        self.pretrained_model = Wav2VecModelOverridden(model_config)

        # require grad for all the model:
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = True
        """
        # then freezing the encoder only, except for the normalization layers that we want to fine-tune:
        for name, param in self.pretrained_model.encoder.named_parameters():
            if "layer_norm" not in name:
                param.requires_grad = False
        """

        pretrained_out_dim = self.pretrained_model.config.hidden_size
        # then we add on top the classification layer to be trained
        self.linear_layer = torch.nn.Linear(pretrained_out_dim, num_classes)

    def forward(self, x):
        cls_token, _ = self.pretrained_model(x)

        y_pred = self.softmax_activation(self.linear_layer(cls_token))
        return y_pred


class Wav2VecComplete(pl.LightningModule):
    def __init__(self, num_classes, pretrained_out_dim=1024, finetune_pretrained=False):

        super(Wav2VecComplete, self).__init__()
        self.finetune_pretrained = finetune_pretrained

        # First we take the pretrained xlsr model
        self.pretrained_model = Wav2Vec2Model.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        # setting require grad = true only if we want to fine tune the pretrained model
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = self.finetune_pretrained

        # then we add on top the classification layers to be trained
        self.linear_layer = torch.nn.Linear(pretrained_out_dim, num_classes)
        self.softmax_activation = torch.nn.Softmax(dim=0)

    def forward(self, x):
        with torch.enable_grad() if self.finetune_pretrained else torch.no_grad():
            # the audio is divided in chunks depending of it's length, 
            # so we do the mean of all the chunks embeddings to get the final embedding
            embedding = self.pretrained_model(x).last_hidden_state.mean(dim=1)

        y_pred = self.softmax_activation(self.linear_layer(embedding))
        return y_pred

    def training_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('val_loss', loss)
        return loss

    def test_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('test_loss', loss)
        return loss

    def train(self):
        # we train the pretrained architecture only if specified
        if self.finetune_pretrained:
            self.pretrained_model.train()
        else:
            self.pretrained_model.eval()

        self.linear_layer.train()

    def eval(self):
        self.pretrained_model.eval()
        self.linear_layer.eval()

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer


class Wav2VecFeezingEncoderOnly(pl.LightningModule):
    def __init__(self, num_classes, pretrained_out_dim=1024):

        super(Wav2VecFeezingEncoderOnly, self).__init__()

        # First we take the pretrained xlsr model        
        self.pretrained_model = Wav2Vec2Model.from_pretrained("facebook/wav2vec2-large-xlsr-53")

        # require grad for all the model:
        for name, param in self.pretrained_model.named_parameters():
            param.requires_grad = True
        # then freezing the encoder only, except for the normalization layers that we want to fine-tune:
        for name, param in self.pretrained_model.encoder.named_parameters():
            if "layer_norm" not in name:
                param.requires_grad = False

        # then we add on top the classification layers to be trained
        self.linear_layer = torch.nn.Linear(pretrained_out_dim, num_classes)
        self.softmax_activation = torch.nn.Softmax(dim=0)

    def forward(self, x):

        embedding = self.pretrained_model(x).last_hidden_state.mean(dim=1)

        y_pred = self.softmax_activation(self.linear_layer(embedding))
        return y_pred

    def training_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('val_loss', loss)
        return loss

    def test_step(self, batch, batch_idx):
        # training_step defined the train loop. It is independent of forward
        x, y = batch
        y_hat = self(x)
        loss = cross_entropy(y_hat, y)
        self.log('test_loss', loss)
        return loss

    def train(self):
        # we don't want to train the encoder as well
        self.pretrained_model.encoder.eval()

        self.pretrained_model.feature_extractor.train()
        self.pretrained_model.feature_projection.train()

        self.linear_layer.train()

    def eval(self):
        self.pretrained_model.eval()
        self.linear_layer.eval()

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer
