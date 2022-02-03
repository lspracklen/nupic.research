# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2021, Numenta, Inc.  Unless you have an agreement
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
#
# This work was based on the original Greedy InfoMax codebase from Sindy Lowe:
# https://github.com/loeweX/Greedy_InfoMax
# The Greedy InfoMax paper can be found here:
# https://arxiv.org/abs/1905.11786
# ----------------------------------------------------------------------

import torch
import torch.nn.functional as F


def multiple_cross_entropy(log_f_module_list, targets, reduction="mean"):
    """
    Calculates the cross entropy for the output of each BilinearInfo module and returns
    the sum.
    """
    device = log_f_module_list[0][0].device
    total_loss = torch.tensor(0.0, requires_grad=True, device=device)
    # Sum losses from each module
    for log_f_list in log_f_module_list:
        # Sum losses for each k prediction
        for log_fk in log_f_list:
            # Positive samples are at index 0
            true_fk = torch.zeros(
                (log_fk.shape[0], log_fk.shape[-2], log_fk.shape[-1]),
                dtype=torch.long,
                device=log_fk.device,
                requires_grad=False,
            )  # b, y, x
            total_loss = total_loss + F.cross_entropy(
                log_fk, true_fk, reduction=reduction
            )
    return total_loss


def all_module_multiple_log_softmax(log_f_module_list, targets, reduction="mean"):
    """
    Used when training BlockModels using GIM. Returns a tensor of losses, each entry
    representing the cross entropy loss of a specific BilinearInfo module.

    Use this loss function when training with a DistributedDataParallel.
    """
    device = log_f_module_list[0][0].device
    module_losses = torch.empty(0, requires_grad=True, device=device)
    # Sum losses from each module
    for log_f_list in log_f_module_list:
        # Sum losses for each k prediction
        module_loss = torch.tensor(0.0, requires_grad=True, device=device)
        for log_fk in log_f_list:
            # Positive samples are at index 0
            true_fk = torch.zeros(
                (log_fk.shape[0], log_fk.shape[-2], log_fk.shape[-1]),
                dtype=torch.long,
                device=device,
                requires_grad=False,
            )
            log_fk = log_fk.to(device)
            softmax_fk = torch.softmax(log_fk, dim=1)
            log_softmax_fk = torch.log(softmax_fk + 1e-11)
            module_loss = module_loss + F.nll_loss(
                log_softmax_fk, true_fk, reduction=reduction
            )
        module_loss = module_loss / len(log_f_list)
        module_losses = torch.cat([module_losses, module_loss.view(1)])
    return module_losses


def all_module_losses(module_losses, targets, reduction="mean"):
    """
    Functionally, this loss function is used in the same way that the other loss
    functions in this file are used. However, this was created to accommodate the fact
    that the true InfoNCE loss has been pushed into the forward pass of the BilinearInfo
    estimators, which was done in order to make the BlockModel work when wrapped under
    DataParallel.
    """
    print(module_losses)
    module_losses = torch.stack(module_losses, 1).view(-1, len(module_losses))  # g, n
    module_losses = torch.mean(module_losses, 0)  # n
    return module_losses


def all_module_losses_2(module_losses, targets, reduction="mean"):
    """
    This is used when training with a DataParallel BlockModel with multiple GPUs.
    """
    module_losses = torch.stack(module_losses, -1).view(-1, len(module_losses))  # g, n
    module_losses = torch.mean(module_losses, 0)  # n
    return module_losses


def multiple_cross_entropy_supervised(outputs, targets, reduction="sum"):
    """
    Used for supervised training of a BlockModel with GIM. This outputs a tensor of
    losses, each of which is the cross entropy classification loss according to a
    specific EmitEncoding module paired with a classification head.
    """
    device = outputs[0].device
    module_losses = torch.empty(0, requires_grad=True, device=device)
    for i in range(len(outputs)):
        module_losses = torch.cat(
            [
                module_losses,
                F.cross_entropy(outputs[i], targets, reduction=reduction).view(1),
            ]
        )
    return module_losses


def multiple_log_softmax_nll_loss(data_lists, targets, reduction="mean"):
    """
    Computes the log softmax of multiple BilinearInfo module outputs and then takes the
    sum of their negative log-likelihood losses.
    """
    return module_specific_log_softmax_nll_loss(data_lists, targets).sum()


def module_specific_log_softmax_nll_loss(data_lists, targets, reduction="mean"):
    """
    Instead of taking the sum of losses of all modules, this returns a tensor with size
    equal to the number of modules, each entry being the loss contribution of a single
    BilinearInfo module.
    """
    log_f_module_list, true_f_module_list = data_lists
    device = log_f_module_list[0][0].device
    total_loss = torch.zeros(len(log_f_module_list), requires_grad=False, device=device)
    # Sum losses from each module
    for i, (log_f_list, true_f_list) in enumerate(
        zip(log_f_module_list, true_f_module_list)
    ):
        # Sum losses for each k prediction
        for log_fk, true_fk in zip(log_f_list, true_f_list):
            softmax_fk = torch.softmax(log_fk, dim=1)
            log_softmax_fk = torch.log(softmax_fk + 1e-11)
            total_loss[i] = total_loss[i] + F.nll_loss(
                log_softmax_fk, true_fk, reduction=reduction
            )
        total_loss[i] /= len(log_f_list)

    return total_loss


def true_gim_loss(data_lists, targets, reduction="mean"):
    """
    Calculates the "true" GreedyInfoMax loss function defined in the paper for each
    module, then takes the sum. The difference lies in the fact that the numerator
    should not be included in the denominator, whereas the softmax function includes the
    numerator in the denominator.
    """
    log_f_module_list, true_f_module_list = data_lists
    device = log_f_module_list[0][0].device
    total_loss = torch.tensor(0.0, requires_grad=True, device=device)
    # Sum losses from each module
    for log_f_list, true_f_list in zip(log_f_module_list, true_f_module_list):
        # Sum losses for each k prediction
        for log_fk, _ in zip(log_f_list, true_f_list):
            numerator = log_fk[:, 0, :, :]
            denominator = torch.logsumexp(log_fk[:, 1:, :, :], dim=1).mean()
            total_loss = total_loss + (numerator - denominator).mean()
    return total_loss


def module_specific_cross_entropy(data_lists, targets, reduction="mean", module=-1):
    """
    Calculates the cross entropy loss for a single module out of the multiple
    BilinearInfo module outputs provided in data_lists.
    """
    log_f_module_list, true_f_module_list = data_lists
    device = log_f_module_list[0][0].device
    total_loss = torch.tensor(0.0, requires_grad=True, device=device)
    # Sum losses from each module
    log_f_list, true_f_list = log_f_module_list[module], true_f_module_list[module]
    for log_fk, true_fk in zip(log_f_list, true_f_list):
        total_loss = total_loss + F.cross_entropy(log_fk, true_fk, reduction=reduction)
    return total_loss