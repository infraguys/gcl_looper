import multiprocessing
import os
import signal
import time

import pytest


from gcl_looper.services import basic
from gcl_looper.services import hub


class OneTimeProcessHub(hub.ProcessHubService):
    def _iteration(self):
        self._enabled = False
        return super()._iteration()


class OneTimeThreadHub(hub.ThreadHubService):
    def _iteration(self):
        self._enabled = False
        return super()._iteration()


class ConcreteService(basic.BasicService):
    def __init__(self, value, iter_min_period=0.1, iter_pause=0.05):
        self._value = value
        super(ConcreteService, self).__init__(iter_min_period, iter_pause)

    def _iteration(self):
        self._value.value = self._value.value + 1

    def stop(self):
        super(ConcreteService, self).stop()
        self._value.value = -1


@pytest.fixture
def prepared_service():
    value = multiprocessing.Value("i", 0)
    return ConcreteService(value)


def test_process_hub_service_initialization(prepared_service):
    h = hub.ProcessHubService()
    h.add_service(prepared_service)

    assert len(h._services) == 1
    assert len(h._instances) == 0


def test_mp_start_stop_services(prepared_service):
    h = OneTimeProcessHub()
    h.add_service(prepared_service)

    h.start()

    # Allow some iterations to run
    time.sleep(0.2)
    instance = h._instances[prepared_service]

    assert instance.is_alive()

    assert prepared_service._value.value > 2

    h.stop()

    assert not instance.is_alive(), "Service did not stop gracefully"
    assert prepared_service._value.value == -1


def test_mp_service_died(prepared_service):
    h = OneTimeProcessHub()
    h.add_service(prepared_service)

    h.start()
    instance = h._instances[prepared_service]

    os.kill(instance.pid, signal.SIGKILL)

    # Continue hub's loop to check if it handles the service death
    h._enabled = True
    h._loop()

    assert not instance.is_alive(), "Service did not stop gracefully"
    # Check that service's stop() method wasn't called
    assert prepared_service._value.value != -1
    assert h._enabled == False


def test_mt_start_stop_services(prepared_service):
    h = OneTimeThreadHub()
    h.add_service(prepared_service)

    h.start()
    instance = h._instances[prepared_service]

    # Allow some iterations to run
    time.sleep(0.2)
    assert instance.is_alive()

    assert prepared_service._value.value > 2

    h.stop()

    assert not instance.is_alive(), "Service did not stop gracefully"
    assert prepared_service._value.value == -1
