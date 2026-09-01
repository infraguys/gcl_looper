import multiprocessing
import os
import signal
import threading
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

    # Allow some iterations to run. The hub exits immediately after
    # its single iteration (OneTimeProcessHub sets _enabled = False),
    # so the child process's entire run time comes from this sleep.
    time.sleep(0.5)
    instance = h._instances[prepared_service]

    assert instance.is_alive()

    assert prepared_service._value.value > 2

    h.stop()
    # The child process receives SIGTERM from h.stop() and sets
    # _value to -1 in its signal handler. h.stop() already calls
    # instance.join(), but the child may not have written the value
    # to shared memory by the time join() returns. Poll until the
    # value is visible.
    deadline = time.monotonic() + 5
    while prepared_service._value.value != -1 and time.monotonic() < deadline:
        instance.join(timeout=0.1)

    assert not instance.is_alive(), "Service did not stop gracefully"
    assert prepared_service._value.value == -1, (
        "Service stop() did not set value to -1"
    )


def test_mp_service_died(prepared_service):
    h = OneTimeProcessHub()
    h.add_service(prepared_service)

    h.start()
    instance = h._instances[prepared_service]

    os.kill(instance.pid, signal.SIGKILL)
    # Wait for the OS to reap the killed process so that
    # instance.is_alive() reflects the real state by the time the
    # hub's _iteration checks it.
    instance.join()

    # Continue hub's loop to check if it handles the service death
    h._enabled = True
    h._loop()

    assert not instance.is_alive(), "Service did not stop gracefully"
    # Check that service's stop() method wasn't called
    assert prepared_service._value.value != -1
    assert not h._enabled


def test_mp_stop_by_signal(prepared_service):
    h = hub.ProcessHubService()
    h.should_subscribe_signals = False
    h.add_service(prepared_service)

    # Subscribe signal handler in main thread manually
    original_handler = signal.signal(
        signal.SIGTERM,
        lambda s, f: h.stop(),
    )

    hub_thread = threading.Thread(target=h.start)
    hub_thread.start()

    try:
        # Allow some iterations to run
        time.sleep(0.3)
        instance = h._instances[prepared_service]

        assert instance.is_alive()
        assert prepared_service._value.value >= 2

        # Send SIGTERM to the hub process (current process)
        os.kill(os.getpid(), signal.SIGTERM)

        # Hub should stop quickly (not wait for full sleep period)
        hub_thread.join(timeout=0.3)

        assert not hub_thread.is_alive(), "Hub did not stop after SIGTERM"
        assert not h._enabled
        assert not instance.is_alive(), "Service did not stop after SIGTERM"
        assert prepared_service._value.value == -1
    finally:
        signal.signal(signal.SIGTERM, original_handler)


def test_mt_start_stop_services(prepared_service):
    h = OneTimeThreadHub()
    h.add_service(prepared_service)

    h.start()
    instance = h._instances[prepared_service]

    # Allow some iterations to run. The hub exits immediately after
    # its single iteration (OneTimeThreadHub sets _enabled = False),
    # so the child thread's entire run time comes from this sleep.
    time.sleep(0.5)
    assert instance.is_alive()

    assert prepared_service._value.value > 2

    h.stop()

    assert not instance.is_alive(), "Service did not stop gracefully"
    assert prepared_service._value.value == -1
