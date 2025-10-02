#    Copyright 2025 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import time

from gcl_looper.services import basic
from gcl_looper.services.oslo import launchpad


class ConcreteServiceFoo(basic.BasicService):
    def __init__(self, value, iter_min_period=0.01, iter_pause=0.005):
        self._value = value
        super().__init__(iter_min_period, iter_pause)

    def _iteration(self):
        self._value = self._value + 1


class ConcreteServiceBar(basic.BasicService):
    def __init__(self, value, iter_min_period=0.01, iter_pause=0.005):
        self._value = value
        super().__init__(iter_min_period, iter_pause)

    def _iteration(self):
        self._value = self._value + 1


class OneTimeLaunchpad(launchpad.LaunchpadService):
    def _iteration(self):
        # run one cycle over services, then stop
        super()._iteration()
        self._enabled = False


def test_launchpad_iteration_over_services():
    svc_foo = ConcreteServiceFoo(0)
    svc_bar = ConcreteServiceBar(10)
    lp = OneTimeLaunchpad(
        services=[svc_foo, svc_bar], iter_min_period=0.01, iter_pause=0.005
    )

    lp.start()

    # allow a tiny bit to ensure loop ran
    time.sleep(0.05)

    assert svc_foo._value == 1
    assert svc_bar._value == 11
