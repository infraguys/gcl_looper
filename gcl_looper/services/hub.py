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
import logging
import os
import signal
import threading
import time

from gcl_looper.services import base
from gcl_looper.services import basic
from gcl_looper import utils

LOG = logging.getLogger(__name__)


class BasicHubService(basic.BasicService):
    __log_iteration__ = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._services = []
        self._instances = {}

    def add_service(self, service):
        """Add a service to the list of services to start."""
        if isinstance(service, base.AbstractService):
            self._services.append(service)
        else:
            raise ValueError("Service must implement the AbstractService interface.")

    def _iteration(self):
        raise NotImplementedError()

    def _setup(self):
        raise NotImplementedError()

    def _stop_instance(self, service, instance):
        raise NotImplementedError()


class ProcessHubService(BasicHubService):
    _instance_class = multiprocessing.get_context("fork").Process

    def add_service(self, service):
        if service.__mp_downgrade_user__:
            service.add_setup(
                lambda: utils.downgrade_user_group_privileges(
                    service.__mp_downgrade_user__
                )
            )
        super().add_service(service)

    def _iteration(self):
        for instance in self._instances.values():
            if not instance.is_alive():
                LOG.error(
                    "Child service(pid:%i) is not running, exit code %r, let's stop",
                    instance.pid,
                    instance.exitcode,
                )
                self.stop()
                return

    def _setup(self):
        for service in self._services:
            instance = self._instance_class(target=service.start)
            self._instances[service] = instance
            instance.start()

    def _stop_instance(self, service, instance):
        LOG.info("Stop child service(pid:%i)", instance.pid)
        try:
            instance.terminate()
        except OSError:  # Process doesn't exist
            LOG.exception("Failed to terminate child service, pid:%i", instance.pid)

    def stop(self):
        super().stop()
        # Stop all managed services
        for service, instance in self._instances.items():
            self._stop_instance(service, instance)
        for instance in self._instances.values():
            instance.join()


class ThreadHubService(ProcessHubService):
    _instance_class = threading.Thread

    def add_service(self, service):
        super(ProcessHubService, self).add_service(service)

    def _iteration(self):
        for instance in self._instances.values():
            if not instance.is_alive():
                LOG.error(
                    "Child service(tid:%i) is not running, let's stop",
                    instance.native_id,
                )
                self.stop()
                return

    def _setup(self):
        # Threads can't hangle signals so we need to disable them
        for service in self._services:
            service.should_subscribe_signals = False
        super()._setup()

    def _stop_instance(self, service, instance):
        LOG.info("Stop child service(native_id:%i)", instance.native_id)
        service.stop()


def _run_service_factory(factory, ready):
    """Entry point of a ReloadableProcessHubService worker process."""
    # SIGHUP is the hub's reload signal; a worker must not die from a stray one.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    started_as_root = os.getuid() == 0
    service = factory()
    if service.__mp_downgrade_user__:
        service.add_setup(
            lambda: utils.downgrade_user_group_privileges(service.__mp_downgrade_user__)
        )
    setup = service._setup

    def setup_and_publish_ready():
        setup()
        if started_as_root and os.getuid() != 0:
            from multiprocessing import resource_tracker

            tracker = resource_tracker._resource_tracker
            if tracker._fd is not None:
                os.close(tracker._fd)
                tracker._fd = None
                tracker._pid = None
        ready.set()

    service._setup = setup_and_publish_ready
    service.start()


class ReloadableProcessHubService(ProcessHubService):
    """Process hub that replaces its workers on SIGHUP without downtime.

    Workers are built by factories inside fresh interpreters (the "spawn"
    start method), so a reload picks up code that changed on disk since the
    hub started, e.g. an upgraded package. A factory is a picklable callable
    (a module-level function or a ``functools.partial`` of one) that returns
    an ``AbstractService``; it runs in the worker and is responsible for
    everything the worker needs, logging and config parsing included.

    On SIGHUP the hub starts a new generation of workers and waits until all
    of them are ready. Only then the old generation gets SIGTERM and time to
    drain; stragglers are killed after ``drain_timeout``. If any new worker
    dies or is not ready within ``ready_timeout``, the new generation is
    stopped and the old one keeps serving. The hub loop is busy for the whole
    reload, drain included.

    Serving both generations at once requires the workers to share their
    listening address, e.g. ``BjoernService`` with ``reuse_port=True``. Set
    ``net.ipv4.tcp_migrate_req=1`` (Linux 5.14+) so connections queued on a
    closed listener move to a live one instead of being reset.

    A draining bjoern worker keeps serving its open keep-alive connections
    (bjoern can't close them on its own), so behind a keep-alive client such
    as an nginx upstream it lives until ``drain_timeout`` and is killed, which
    may reset a request in flight on such a connection.
    """

    _mp_context = multiprocessing.get_context("spawn")

    def __init__(self, *args, ready_timeout=60, drain_timeout=30, **kwargs):
        super().__init__(*args, **kwargs)
        self._ready_timeout = ready_timeout
        self._drain_timeout = drain_timeout
        self._factories = []
        self._reload_requested = False
        self._stop_deadline = None

    def add_service(self, service):
        raise TypeError(
            "Workers of a reloadable hub are built in fresh interpreters, "
            "use add_service_factory() instead."
        )

    def add_service_factory(self, factory):
        """Add a factory building one worker service."""
        self._factories.append(factory)

    def reload(self):
        """Request a reload; it runs on the next hub iteration."""
        LOG.info("Reload requested")
        self._reload_requested = True
        self._wake_event.set()

    def _spawn(self, factory):
        ready = self._mp_context.Event()
        instance = self._mp_context.Process(
            target=_run_service_factory,
            args=(factory, ready),
        )
        # Keep the event alive with the process: once the parent drops it, its
        # semaphore is unlinked and a worker that has not attached yet fails.
        instance.ready = ready
        instance.start()
        return instance

    def _spawn_generation(self):
        generation = {}
        try:
            for idx, factory in enumerate(self._factories):
                generation[idx] = self._spawn(factory)
        except Exception:
            self._drain(generation.values())
            raise
        return generation

    def _setup(self):
        # Installed here rather than with the base signal handlers, which come
        # after the setup: spawning takes a while, and until then SIGHUP would
        # kill the hub instead of reloading it.
        if self.should_subscribe_signals:
            signal.signal(signal.SIGHUP, lambda s, frame: self.reload())
        self._instances = self._spawn_generation()

    def _wait_ready(self, generation):
        deadline = time.monotonic() + self._ready_timeout
        for instance in generation.values():
            while not instance.ready.wait(timeout=0.1):
                if not instance.is_alive():
                    LOG.error(
                        "New worker(pid:%i) exited with code %r before becoming ready",
                        instance.pid,
                        instance.exitcode,
                    )
                    return False
                if not self._enabled:
                    return False
                if time.monotonic() > deadline:
                    LOG.error(
                        "New worker(pid:%i) is not ready in %ss",
                        instance.pid,
                        self._ready_timeout,
                    )
                    return False
        for instance in generation.values():
            if not instance.is_alive():
                LOG.error(
                    "New worker(pid:%i) exited with code %r before the generation was ready",
                    instance.pid,
                    instance.exitcode,
                )
                return False
        return self._enabled

    def _drain(self, instances):
        """SIGTERM the workers, kill the ones still alive after drain_timeout."""
        instances = list(instances)
        deadline = time.monotonic() + self._drain_timeout
        for instance in instances:
            try:
                instance.terminate()
            except OSError:  # Process doesn't exist
                pass
        for instance in instances:
            while instance.is_alive():
                if self._stop_deadline is not None:
                    deadline = min(deadline, self._stop_deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                instance.join(timeout=min(remaining, 0.1))
            if instance.is_alive():
                LOG.warning(
                    "Worker(pid:%i) did not stop in %ss, killing it",
                    instance.pid,
                    self._drain_timeout,
                )
                instance.kill()
            instance.join()

    def _reload(self):
        self._reload_requested = False
        LOG.info("Reload: starting %i new worker(s)", len(self._factories))
        generation = self._spawn_generation()

        if not self._wait_ready(generation):
            self._drain(generation.values())
            if self._enabled:
                LOG.error("Reload failed, the current workers keep serving")
            return

        old = self._instances
        self._instances = generation
        LOG.info(
            "Reload: new workers (pids: %s) are serving, draining the old ones",
            ", ".join(str(i.pid) for i in generation.values()),
        )
        self._drain(old.values())

    def _iteration(self):
        if self._reload_requested and self._enabled:
            self._reload()
        super()._iteration()

    def stop(self):
        # Only flag the loop to exit: stop() may come from a signal handler or
        # another thread in the middle of a reload, when a new generation is
        # not in _instances yet. The loop's own thread stops the workers in
        # _finish().
        if self._stop_deadline is None:
            self._stop_deadline = time.monotonic() + self._drain_timeout
        basic.BasicService.stop(self)

    def _finish(self):
        # Unlike the base hub, don't wait for the workers forever: a keep-alive
        # client holds a draining bjoern worker until it disconnects.
        self._drain(self._instances.values())
        super()._finish()
