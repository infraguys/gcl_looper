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

import threading
import time

from gcl_looper.services import basic


class CountingService(basic.BasicService):
    def __init__(self, *args, **kwargs):
        super(CountingService, self).__init__(*args, **kwargs)
        self.iterations = 0

    def _iteration(self):
        self.iterations += 1


def test_boost_mode_speeds_up_iterations():
    service = CountingService(iter_min_period=10, iter_pause=0)
    loop_thread = threading.Thread(target=service._loop)
    loop_thread.start()

    # Only the first iteration is expected within the slow pace
    time.sleep(0.2)
    assert service.iterations == 1

    # Boost mode speeds the iterations up
    service.boost(0.05)
    time.sleep(0.5)
    boosted_iterations = service.iterations
    assert boosted_iterations >= 5

    # Resetting the boost returns the service to the slow pace
    service.reset_boost()
    time.sleep(0.5)
    assert service.iterations <= boosted_iterations + 1

    service.stop()
    loop_thread.join(timeout=2)
    assert not loop_thread.is_alive()
