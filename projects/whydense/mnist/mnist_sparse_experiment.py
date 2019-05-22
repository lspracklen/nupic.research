# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2018, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------
import logging
import os
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from nupic.torch.modules import (
  SparseWeights2d, KWinners2d, Flatten, SparseWeights, KWinners,
  updateBoostStrength, rezeroWeights
)
from torchvision import datasets, transforms

from nupic.research.frameworks.pytorch.dataset_utils import createValidationDataSampler
from nupic.research.frameworks.pytorch.image_transforms import RandomNoise
from nupic.research.frameworks.pytorch.model_utils import (
  trainModel, evaluateModel, setRandomSeed
)


def getLogger(name, verbose):
  """
  Configure Logger based on verbose level (0: ERROR, 1: INFO, 2: DEBUG)
  """
  logger = logging.getLogger(name)
  if verbose == 0:
    logger.setLevel(logging.ERROR)
  elif verbose == 1:
    logger.setLevel(logging.INFO)
  else:
    logger.setLevel(logging.DEBUG)

  return logger


def add_cnn_layer(network, suffix, in_channels, out_channels,
                  use_batch_norm, weight_sparsity, percent_on,
                  k_inference_factor, boost_strength, boost_strength_factor):
  """

  Add sparse cnn layer to network

  :param network: The network to add the sparse layer to
  :param suffix: Layer suffix. Used to name its components
  :param in_channels: input channels
  :param out_channels: output channels
  :param use_batch_norm: whether or not to use batch norm
  :param weight_sparsity: Pct of weights that are allowed to be non-zero
  :param percent_on: Pct of ON (non-zero) units
  :param k_inference_factor: During inference we increase percent_on by this factor
  :param boost_strength: boost strength (0.0 implies no boosting)
  :param boost_strength_factor: boost strength is multiplied by this factor after each epoch
  """
  cnn = nn.Conv2d(in_channels=in_channels,
                  out_channels=out_channels,
                  kernel_size=5,
                  padding=0,
                  stride=1)
  if 0 < weight_sparsity < 1.:
    sparseCNN = SparseWeights2d(cnn, weight_sparsity)
    network.add_module("cnnSdr{}_cnn".format(suffix), sparseCNN)
  else:
    network.add_module("cnnSdr{}_cnn".format(suffix), cnn)

  if use_batch_norm:
    bn = nn.BatchNorm2d(out_channels, affine=False)
    network.add_module("cnnSdr{}_bn".format(suffix), bn)

  # Max pool
  maxpool = nn.MaxPool2d(kernel_size=2)
  network.add_module("cnnSdr{}_maxpool".format(suffix), maxpool)

  if 0 < percent_on < 1.0:
    kwinner = KWinners2d(channels=out_channels,
                         percent_on=percent_on,
                         kInferenceFactor=k_inference_factor,
                         boostStrength=boost_strength,
                         boostStrengthFactor=boost_strength_factor)
    network.add_module("cnnSdr{}_kwinner".format(suffix), kwinner)
  else:
    network.add_module("cnnSdr{}_relu".format(suffix), nn.ReLU())


def add_linear_layer(network, suffix, input_size, linear_n, weight_sparsity,
                     percent_on, k_inference_factor, boost_strength,
                     boost_strength_factor):
  """
  Add sparse linear layer to network
  :param network: The network to add the sparse layer to
  :param suffix: Layer suffix. Used to name its components
  :param input_size: Input size
  :param linear_n: Number of units
  :param weight_sparsity: Pct of weights that are allowed to be non-zero
  :param percent_on: Pct of ON (non-zero) units
  :param k_inference_factor: During inference we increase percent_on by this factor
  :param boost_strength: boost strength (0.0 implies no boosting)
  :param boost_strength_factor: boost strength is multiplied by this factor after each epoch
  """
  linear = nn.Linear(input_size, linear_n)
  if 0 < weight_sparsity < 1.0:
    network.add_module("linear{}".format(suffix),
                       SparseWeights(linear, weight_sparsity))
  else:
    network.add_module("linear{}".format(suffix), linear)

  if 0 < percent_on < 1.0:
    network.add_module("linear{}_kwinners".format(suffix),
                       KWinners(n=linear_n,
                                percent_on=percent_on,
                                kInferenceFactor=k_inference_factor,
                                boostStrength=boost_strength,
                                boostStrengthFactor=boost_strength_factor))


  else:
    network.add_module("linear{}_relu".format(suffix), nn.ReLU())


class MNISTSparseExperiment(object):
  """
  Allows running multiple sparse MNIST experiments in parallel
  """


  def __init__(self, config):
    """
    Called once at the beginning of each experiment.
    """
    super(MNISTSparseExperiment, self).__init__()
    self.startTime = time.time()
    self.logger = getLogger(config["name"], config.get("verbose", 2))
    self.logger.debug("Config: %s", config)

    # Setup random seed
    seed = config["seed"]
    setRandomSeed(seed)

    self.data_dir = config["data_dir"]
    self.batch_size = config["batch_size"]
    self.test_batch_size = config["test_batch_size"]
    self.first_epoch_batch_size = config["first_epoch_batch_size"]
    self.validation = config.get("validation", 50000.0 / 60000.0)
    self.learning_rate_factor = config["learning_rate_factor"]
    self.lr_scheduler_params = config.get("lr_scheduler_params", None)

    self._configureDataloaders()

    # Configure Model
    cnn_input_shape = config.get("cnn_input_shape", (1, 28, 28))
    linear_n = config["linear_n"]
    linear_percent_on = config["linear_percent_on"]
    cnn_out_channels = config["cnn_out_channels"]
    cnn_percent_on = config["cnn_percent_on"]
    boost_strength = config["boost_strength"]
    weight_sparsity = config["weight_sparsity"]
    cnn_weight_sparsity = config["cnn_weight_sparsity"]
    boost_strength_factor = config["boost_strength_factor"]
    k_inference_factor = config["k_inference_factor"]
    use_batch_norm = config["use_batch_norm"]

    model = nn.Sequential()

    # Add CNN Layers
    input_shape = cnn_input_shape
    cnn_layers = len(cnn_out_channels)
    if cnn_layers > 0:
      for i in range(cnn_layers):
        in_channels, height, width = input_shape
        add_cnn_layer(network=model, suffix=i + 1,
                      in_channels=in_channels,
                      out_channels=cnn_out_channels[i],
                      use_batch_norm=use_batch_norm,
                      weight_sparsity=cnn_weight_sparsity,
                      percent_on=cnn_percent_on[i],
                      k_inference_factor=k_inference_factor,
                      boost_strength=boost_strength,
                      boost_strength_factor=boost_strength_factor)

        # Feed this layer output into next layer input
        in_channels = cnn_out_channels[i]

        # Compute next layer input shape
        wout = (width - 5) + 1
        maxpoolWidth = wout // 2
        input_shape = (in_channels, maxpoolWidth, maxpoolWidth)

    # Flatten CNN output before passing to linear layer
    model.add_module("flatten", Flatten())

    # Add Linear layers
    input_size = np.prod(input_shape)
    for i in range(len(linear_n)):
      add_linear_layer(network=model, suffix=i + 1, input_size=input_size,
                       linear_n=linear_n[i],
                       weight_sparsity=weight_sparsity,
                       percent_on=linear_percent_on[i],
                       k_inference_factor=k_inference_factor,
                       boost_strength=boost_strength,
                       boost_strength_factor=boost_strength_factor)
      input_size = linear_n[i]

    # Output layer
    model.add_module("output", nn.Linear(input_size, 10))
    model.add_module("softmax", nn.LogSoftmax(dim=1))

    if torch.cuda.is_available():
      self.device = torch.device("cuda")
      model = model.cuda()
    else:
      self.device = torch.device("cpu")

    if torch.cuda.device_count() > 1:
      self.logger.debug("Using", torch.cuda.device_count(), "GPUs")
      model = torch.nn.DataParallel(model)

    self.model = model.to(self.device)
    self.logger.debug("Model: %s", self.model)
    self.learning_rate = config["learning_rate"]
    self.momentum = config["momentum"]

    self.batches_in_epoch = config["batches_in_epoch"]
    self.batches_in_first_epoch = config["batches_in_first_epoch"]
    self.config = config

    self.optimizer = self._createOptimizer(
      name=config["optimizer"], model=self.model)
    self.lr_scheduler = self._createLearningRateScheduler(
      name=config.get("lr_scheduler", None), optimizer=self.optimizer)


  def train(self, epoch):
    """
    Train one epoch of this model by iterating through mini batches. An epoch
    ends after one pass through the training set, or if the number of mini
    batches exceeds the parameter "batches_in_epoch".
    """

    if epoch == 0:
      loader = self.first_loader
      batches_in_epoch = self.batches_in_first_epoch
    else:
      loader = self.train_loader
      batches_in_epoch = self.batches_in_epoch

    self.logger.info("epoch: %s", epoch)
    t0 = time.time()
    self.preEpoch()
    self.logger.info("learning rate: %s", self.lr_scheduler.get_lr())
    trainModel(model=self.model, loader=loader,
               optimizer=self.optimizer, device=self.device,
               batches_in_epoch=batches_in_epoch)
    self.postEpoch()
    self.logger.info("training duration: %s", time.time() - t0)


  def validate(self):
    if self.validation_loader:
      return self.test(self.validation_loader)
    return None


  def test(self, loader=None):
    """
    Test the model using the given loader and return test metrics
    """
    if loader is None:
      loader = self.test_loader

    t0 = time.time()
    results = evaluateModel(model=self.model, device=self.device,
                            loader=loader)
    results.update({
      "entropy": float(self.entropy())})

    self.logger.info("testing duration: %s", time.time() - t0)
    self.logger.info("mean_accuracy: %s", results["mean_accuracy"])
    self.logger.info("mean_loss: %s", results["mean_loss"])
    self.logger.info("entropy: %s", results["entropy"])

    return results


  def entropy(self):
    """
    Returns the current entropy
    """
    entropy = 0
    for module in self.model.modules():
      if module == self.model:
        continue
      if hasattr(module, "entropy"):
        entropy += module.entropy()

    return entropy


  def save(self, checkpoint_path):
    checkpoint_path = os.path.join(checkpoint_path, "model.pt")
    torch.save(self.model.state_dict(), checkpoint_path)
    return checkpoint_path


  def restore(self, checkpoint_path):
    checkpoint_path = os.path.join(checkpoint_path, "model.pt")
    self.model.load_state_dict(torch.load(checkpoint_path, map_location=self.device))


  def preEpoch(self):
    pass


  def postEpoch(self):
    self.model.apply(updateBoostStrength)
    self.model.apply(rezeroWeights)
    self.lr_scheduler.step()


  def runNoiseTests(self):
    """
    Test the model with different noise values and return test metrics.
    """
    ret = {}

    # Test with noise
    total_correct = 0
    for noise in [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]:
      self.logger.info("Noise: %s", noise)
      transform = transforms.Compose([
        transforms.ToTensor(),
        RandomNoise(noise, highValue=0.1307 + 2 * 0.3081),
        transforms.Normalize((0.1307,), (0.3081,))
      ])
      test_loader = torch.utils.data.DataLoader(
        datasets.MNIST(self.data_dir, train=False, transform=transform),
        batch_size=self.test_batch_size, shuffle=True)

      testResult = self.test(test_loader)
      total_correct += testResult["total_correct"]
      ret[noise] = testResult

    return ret


  def _configureDataloaders(self):

    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize((0.1307,), (0.3081,))])

    train_dataset = datasets.MNIST(self.data_dir, train=True, download=True,
                                   transform=transform)

    # Create training and validation sampler from MNIST dataset by training on
    # random X% of the training set and validating on the remaining (1-X)%,
    # where X can be tuned via the "validation" parameter
    if self.validation < 1.0:
      self.train_sampler, self.validation_sampler = createValidationDataSampler(
        train_dataset, self.validation)

      self.train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=self.batch_size,
        sampler=self.train_sampler)

      self.validation_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=self.batch_size,
        sampler=self.validation_sampler)
    else:
      # No validation. Normal training dataset
      self.validation_loader = None
      self.validation_sampler = None
      self.train_sampler = None
      self.train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=self.batch_size,
        shuffle=True)

    self.test_loader = torch.utils.data.DataLoader(
      datasets.MNIST(self.data_dir, train=False, transform=transform),
      batch_size=self.test_batch_size, shuffle=True)

    if self.first_epoch_batch_size != self.batch_size:
      self.first_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=self.first_epoch_batch_size,
        shuffle=True)
    else:
      self.first_loader = self.train_loader


  def _createLearningRateScheduler(self, name, optimizer):
    """
    Creates the learning rate scheduler and attach the optimizer
    """
    if name is None:
      return None

    lr_scheduler_params = self.lr_scheduler_params
    if name == "StepLR":
      lr_scheduler_params = "{'step_size': 1, 'gamma':" + str(self.learning_rate_factor) + "}"
    else:
      if lr_scheduler_params is None:
        raise ValueError("Missing 'lr_scheduler_params' for {}".format(name))

    # Get lr_scheduler class by name
    clazz = eval("torch.optim.lr_scheduler.{}".format(name))

    # Parse scheduler parameters from config
    lr_scheduler_params = eval(lr_scheduler_params)

    return clazz(optimizer, **lr_scheduler_params)


  def _createOptimizer(self, name, model):
    """
    Create a new instance of the optimizer
    """
    if name == "SGD":
      optimizer = optim.SGD(model.parameters(),
                            lr=self.learning_rate,
                            momentum=self.momentum)
    elif name == "Adam":
      optimizer = optim.Adam(model.parameters(), lr=self.learning_rate)
    else:
      raise LookupError("Incorrect optimizer value")

    return optimizer