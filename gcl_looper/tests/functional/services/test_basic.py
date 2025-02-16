#    Copyright 2025 George Melikov <mail@gmelikov.ru>
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

import multiprocessing
import pytest
import time
import signal
import logging

from gcl_looper.services.basic import BasicService


LOG = logging.getLogger(__name__)


class ConcreteService(BasicService):
    def __init__(self, value, iter_min_period=0.1, iter_pause=0.05):
        self._value = value
        super(ConcreteService, self).__init__(iter_min_period, iter_pause)

    def _iteration(self):
        self._value.value = self._value.value + 1

    def stop(self):
        super(ConcreteService, self).stop()
        self._value.value = -1


def run_service(value):
    service = ConcreteService(value=value)
    service.start()


def test_basic_service_iterations_and_stop():
    value = multiprocessing.Value("i", 0)
    process = multiprocessing.Process(target=run_service, args=(value,))

    process.start()
    # Allow some iterations to run
    time.sleep(0.2)

    assert process.is_alive()

    assert value.value == 2

    process.terminate()
    process.join(timeout=2)

    assert not process.is_alive(), "Service did not stop gracefully"
    assert value.value == -1
