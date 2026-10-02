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
import functools
import pickle
import select
import subprocess
import sys
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


def _detach_privileged_tracker(initial_uid):
    if os.getuid() != initial_uid:
        from multiprocessing import resource_tracker

        tracker = resource_tracker._resource_tracker
        if tracker._fd is not None:
            os.close(tracker._fd)
            tracker._fd = tracker._pid = None


def _run_service_factory(factory, ready):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    initial_uid = os.getuid()
    service = factory()
    service._ready_fd = getattr(ready, "fd", None)
    if service.__mp_downgrade_user__:
        service.add_setup(
            lambda: utils.downgrade_user_group_privileges(service.__mp_downgrade_user__)
        )
    setup = service._setup

    def setup_and_publish_ready():
        setup()
        _detach_privileged_tracker(initial_uid)
        ready.set()

    service._setup = setup_and_publish_ready
    service.start()


class _Readiness:
    def __init__(self, fd):
        self.fd = fd
        self._ready = False

    def wait(self, timeout=None):
        if not self._ready and self.fd is not None:
            if select.select([self.fd], [], [], timeout)[0]:
                self._ready = os.read(self.fd, 1) == b"R"
                self.close()
        return self._ready

    def is_set(self):
        return self.wait(0)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _ServiceProcess(subprocess.Popen):
    def __init__(self, factory, autoreload=False):
        self._group_closed = False
        # Restore trusted paths before importing the launcher, without adding CWD.
        bootstrap = "import sys\nsys.path = " + repr(sys.path) + "\n"
        if autoreload:
            bootstrap += (
                "from importlib.machinery import SourceFileLoader\n"
                "def get_code(loader, fullname):\n"
                "    path = loader.get_filename(fullname)\n"
                "    return loader.source_to_code(loader.get_data(path), path)\n"
                "SourceFileLoader.get_code = get_code\n"
            )
        bootstrap += "from gcl_looper.services._generation import main\nmain()\n"
        main = sys.modules["__main__"]
        payload = (
            (
                getattr(getattr(main, "__spec__", None), "name", None),
                getattr(main, "__file__", None),
                sys.argv,
            ),
            pickle.dumps(factory),
        )
        read_fd, write_fd = os.pipe()
        try:
            super().__init__(
                [
                    sys.executable,
                    *subprocess._args_from_interpreter_flags(),
                    "-c",
                    bootstrap,
                    str(write_fd),
                ],
                stdin=subprocess.PIPE,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self.ready = _Readiness(read_fd)
        try:
            # The child reads concurrently, so payloads can exceed pipe capacity.
            with self.stdin:
                pickle.dump(payload, self.stdin)
        except BaseException:
            self.close()
            raise

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self._group_closed = True  # Another reaper took ownership of the PID.
            return super().poll()
        if status is None:
            return None
        # Keep the zombie until group cleanup, so its numeric PGID cannot be reused.
        return (
            status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
        )

    @property
    def exitcode(self):
        return self.poll()

    def is_alive(self):
        return self.poll() is None

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.is_alive():
            if deadline is not None and time.monotonic() >= deadline:
                return
            time.sleep(0.01)

    def terminate(self):
        if self.is_alive():
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def kill(self):
        self.poll()
        if not self._group_closed:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._group_closed = True

    def close(self):
        self.kill()
        super().wait()
        self.ready.close()


def _run_prefork_worker(service, ready, master_ready_fd):
    if master_ready_fd is not None:
        os.close(master_ready_fd)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _run_service_factory(lambda: service, ready)


def _build_generation(factories, drain_timeout=30):
    if len(factories) == 1:
        return factories[0]()
    initial_uid = os.getuid()
    generation = _PreforkGeneration(iter_min_period=0.1, drain_timeout=drain_timeout)
    for factory in factories:
        BasicHubService.add_service(generation, factory())
    if len({service.__mp_downgrade_user__ for service in generation._services}) != 1:
        raise ValueError("Prefork replicas must use the same worker privileges")
    _detach_privileged_tracker(initial_uid)
    return generation


class ReloadableProcessHubService(ProcessHubService):
    """Supervisor replacing prefork service generations on SIGHUP.

    A fresh interpreter preloads factory-built services and forks workers.
    With one worker it runs the service directly. Factories must be picklable,
    single-threaded and fork-safe; open connections in worker setup callbacks.
    Readiness travels over a pipe, without multiprocessing spawn or a resource
    tracker. TCP workers need reuse_port=True to overlap generations.

    New workers must all finish setup before the old master receives SIGTERM.
    A failed or timed-out replacement leaves the old generation serving.
    drain_timeout bounds shutdown, including open bjoern keep-alive connections;
    remaining processes in the generation's group are killed after drain.
    """

    def __init__(
        self,
        *args,
        ready_timeout=60,
        drain_timeout=30,
        autoreload=False,
        reload_dirs=(),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._ready_timeout = ready_timeout
        self._drain_timeout = drain_timeout
        self._factories = []
        self._reload_requested = False
        self._stop_deadline = None
        self.autoreload = autoreload
        self._reload_dirs = []
        self._sources = {}
        self._initial_ready = False
        if autoreload and reload_dirs:
            from pathlib import Path

            self._reload_dirs = [Path(directory).resolve() for directory in reload_dirs]
            for directory in self._reload_dirs:
                if not directory.is_dir():
                    raise ValueError(f"Reload directory does not exist: {directory}")

    def _discover_source_dirs(self):
        from importlib import metadata
        import json
        from pathlib import Path
        from urllib.parse import unquote, urlsplit

        directories = set(self._reload_dirs)
        system_dirs = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
        for module in tuple(sys.modules.values()):
            filename = getattr(module, "__file__", None)
            if filename and filename.endswith(".py"):
                directory = Path(filename).resolve().parent
                if not any(
                    root == directory or root in directory.parents
                    for root in system_dirs
                ):
                    directories.add(directory)
        # Editable packages include SDKs and plugins not imported until a request.
        for distribution in metadata.distributions():
            direct_url = distribution.read_text("direct_url.json")
            if direct_url:
                info = json.loads(direct_url)
                url = urlsplit(info["url"])
                if info.get("dir_info", {}).get("editable") and url.scheme == "file":
                    directory = Path(unquote(url.path)).resolve()
                    if directory.is_dir():
                        directories.add(directory)
        # Keep only outer roots, avoiding repeated scans of nested packages.
        return [
            directory
            for directory in sorted(directories)
            if not any(parent in directories for parent in directory.parents)
        ]

    def _snapshot_sources(self):
        from pathlib import Path
        from stat import S_ISREG

        def scan_error(error):
            if not isinstance(error, FileNotFoundError):
                raise error

        sources = {}
        for directory in self._reload_dirs:
            for root, dirs, files in os.walk(directory, onerror=scan_error):
                dirs[:] = [
                    name
                    for name in dirs
                    if not name.startswith(".")
                    and name
                    not in {"__pycache__", "venv", "node_modules", "build", "dist"}
                ]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    source = Path(root) / name
                    try:
                        stat = source.lstat()
                    except FileNotFoundError:  # Files can disappear during a scan.
                        continue
                    if S_ISREG(stat.st_mode):
                        sources[source] = (stat.st_mtime_ns, stat.st_size)
        return sources

    def _check_source_changes(self):
        if not self.autoreload:
            return
        sources = self._snapshot_sources()
        if sources != self._sources:
            self._sources = sources
            LOG.info("Source changes detected, reloading services")
            self.reload()

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
        return _ServiceProcess(factory, autoreload=self.autoreload)

    def _spawn_generation(self):
        if not self._factories:
            return {}
        groups = {}
        for factory in self._factories:
            groups.setdefault(pickle.dumps(factory), []).append(factory)
        generation = {}
        try:
            for index, factories in enumerate(groups.values()):
                generation[index] = self._spawn(
                    functools.partial(_build_generation, factories, self._drain_timeout)
                )
        except BaseException:
            self._drain(generation.values())
            raise
        return generation

    def _setup(self):
        # Installed here rather than with the base signal handlers, which come
        # after the setup: spawning takes a while, and until then SIGHUP would
        # kill the hub instead of reloading it.
        if self.should_subscribe_signals:
            signal.signal(signal.SIGHUP, lambda s, frame: self.reload())
        if self.autoreload:
            self._reload_dirs = self._discover_source_dirs()
            LOG.info("Watching Python sources in %s", self._reload_dirs)
            self._sources = self._snapshot_sources()
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
            if isinstance(instance, _ServiceProcess):
                instance.close()

    def _reload(self):
        self._reload_requested = False
        LOG.info("Reload: starting %i new worker(s)", len(self._factories))
        try:
            generation = self._spawn_generation()
        except Exception:
            LOG.exception("Reload failed to launch, the current workers keep serving")
            return

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
        if not self._initial_ready:
            self._initial_ready = True
            if not self._wait_ready(self._instances):
                self.stop()
                return
        try:
            self._check_source_changes()
        except OSError:
            LOG.exception(
                "Autoreload disabled after a watcher error; services keep running"
            )
            self.autoreload = False
            self._reload_dirs = []
            self._sources = {}
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


class _PreforkGeneration(ReloadableProcessHubService):
    def _setup(self):
        self._subscribe_signals(self._get_sig_handlers())
        context = multiprocessing.get_context("fork")
        for service in self._services:
            ready = context.Event()
            instance = context.Process(
                target=_run_prefork_worker,
                args=(service, ready, self._ready_fd),
            )
            instance.ready = ready
            self._instances[service] = instance
            instance.start()
        while not all(i.ready.is_set() for i in self._instances.values()):
            if self._stop_event.is_set():
                raise InterruptedError("Prefork generation stopped during setup")
            if any(not i.is_alive() for i in self._instances.values()):
                raise RuntimeError("Prefork worker exited before readiness")
            time.sleep(0.05)
        if any(not i.is_alive() for i in self._instances.values()):
            raise RuntimeError("Prefork worker exited before readiness")
