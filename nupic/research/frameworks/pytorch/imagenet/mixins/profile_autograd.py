# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2020, Numenta, Inc.  Unless you have an agreement
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

import torch.autograd


class ProfileAutograd:
    def setup_experiment(self, config):
        super().setup_experiment(config)
        # Only profile from rank 0
        self.profile_autograd = self.rank == 0

    def train_epoch(self, epoch):
        with torch.autograd.profiler.profile(use_cuda=torch.cuda.is_available(),
                                             enabled=self.profile) as prof:
            super().train_epoch(epoch)

        if self.profile and prof is not None:
            self.logger.info(prof.key_averages().table(
                sort_by="self_cpu_time_total"))
