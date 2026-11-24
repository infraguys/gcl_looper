#    Copyright 2026 George Melikov <mail@gmelikov.ru>
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

from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
import fcntl
import json
import os
import resource
import sys
import threading
import time
import types

import pytest

from gcl_looper.services import hub


def test_readiness_pipe_closes_on_success_or_eof():
    for message in (b"R", b""):
        read_fd, write_fd = os.pipe()
        ready = hub._Readiness(read_fd)
        os.write(write_fd, message)
        os.close(write_fd)
        assert ready.wait(0.1) == bool(message)
        assert ready.has_failed() == (not message)
        assert ready.fd is None
        assert ready.is_set() == bool(message)


def test_prefork_readiness_pipe_reports_shutdown_after_ready():
    read_fd, write_fd = os.pipe()
    ready = hub._Readiness(read_fd)
    try:
        with os.fdopen(write_fd, "wb", buffering=0) as status:
            status.write(b"P")
            assert ready.wait(0.1)
            assert not ready.has_failed()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with ready._lock:
                check = executor.submit(ready.has_failed)
                with pytest.raises(FutureTimeoutError):
                    check.result(timeout=0.05)
            assert check.result(timeout=5)
        assert ready.is_set()
    finally:
        ready.close()


@pytest.mark.parametrize("direction", ["readiness", "payload"])
def test_pipe_wait_supports_high_fds(direction, monkeypatch):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard <= 1024:
        pytest.skip("A descriptor above FD_SETSIZE requires a higher hard limit")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1025), hard))
    read_fd, write_fd = os.pipe()
    high_fd = None
    ready = None
    create_poll = hub.select.poll

    class IntegerTimeoutPoll:
        def __init__(self):
            self.poller = create_poll()

        def register(self, *args):
            self.poller.register(*args)

        def poll(self, timeout):
            assert isinstance(timeout, int)
            return self.poller.poll(timeout)

    monkeypatch.setattr(hub.select, "poll", IntegerTimeoutPoll)
    try:
        high_fd = fcntl.fcntl(
            read_fd if direction == "readiness" else write_fd, fcntl.F_DUPFD, 1024
        )
        if direction == "readiness":
            ready = hub._Readiness(high_fd)
            os.write(write_fd, b"R")
            assert ready.wait(1)
        else:
            writer = hub._PayloadWriter(high_fd, time.monotonic() + 1, None)
            assert writer.write(b"payload") == 7
            assert os.read(read_fd, 7) == b"payload"
    finally:
        if ready is not None:
            ready.close()
        elif high_fd is not None:
            os.close(high_fd)
        os.close(read_fd)
        os.close(write_fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_readiness_pipe_preserves_signal_for_concurrent_waiters(monkeypatch):
    read_fd, write_fd = os.pipe()
    ready = hub._Readiness(read_fd)
    os.write(write_fd, b"R")
    os.close(write_fd)
    waiters = threading.Barrier(2)
    reading = threading.Barrier(2)
    poll_readiness = hub.select.poll

    class ConcurrentPoll:
        def __init__(self):
            self.poller = poll_readiness()

        def register(self, *args):
            self.poller.register(*args)

        def poll(self, timeout):
            result = self.poller.poll(timeout)
            try:
                reading.wait(timeout=0.1)
            except threading.BrokenBarrierError:
                pass
            return result

    def wait():
        waiters.wait(timeout=5)
        return ready.wait(1)

    monkeypatch.setattr(hub.select, "poll", ConcurrentPoll)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(wait) for _ in range(2)]
            assert [result.result(timeout=5) for result in results] == [True, True]
        assert ready.fd is None
        assert ready.is_set()
    finally:
        ready.close()


@pytest.mark.parametrize("timeout", [0, 0.05])
def test_readiness_timeout_includes_concurrent_wait(timeout, monkeypatch):
    read_fd, write_fd = os.pipe()
    ready = hub._Readiness(read_fd)
    waiting = threading.Event()
    poll_readiness = hub.select.poll

    class NotifyingPoll:
        def __init__(self):
            self.poller = poll_readiness()

        def register(self, *args):
            self.poller.register(*args)

        def poll(self, timeout):
            waiting.set()
            return self.poller.poll(timeout)

    monkeypatch.setattr(hub.select, "poll", NotifyingPoll)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            waiter = executor.submit(ready.wait)
            try:
                assert waiting.wait(5)
                check = executor.submit(
                    ready.is_set if timeout == 0 else ready.wait,
                    *(() if timeout == 0 else (timeout,)),
                )
                assert check.result(timeout=0.5) is False
                assert executor.submit(ready.has_failed).result(timeout=0.5) is False
            finally:
                os.write(write_fd, b"R")
                assert waiter.result(timeout=5)
    finally:
        ready.close()
        os.close(write_fd)


def test_worker_exiting_while_peer_starts_rejects_reload():
    service = hub.ReloadableProcessHubService()
    service._enabled = True
    old = mock.Mock(exitcode=None)
    old.ready.has_failed.return_value = False
    service._instances = {0: old}
    first, second = mock.Mock(pid=1, exitcode=None), mock.Mock(pid=2, exitcode=None)
    first.ready.wait.return_value = True
    first.ready.has_failed.return_value = False
    second.ready.has_failed.return_value = False

    def peer_ready(timeout):
        first.exitcode = 1
        return True

    second.ready.wait.side_effect = peer_ready
    service._spawn_generation = mock.Mock(return_value={0: first, 1: second})
    service._drain = mock.Mock()

    service.reload()
    service._iteration()

    assert service._enabled
    assert service._instances == {0: old}
    assert list(service._drain.call_args.args[0]) == [first, second]
    old.terminate.assert_not_called()


def test_stop_during_drain_shares_deadline_between_generations():
    service = hub.ReloadableProcessHubService(drain_timeout=10)
    old, new = mock.Mock(pid=1), mock.Mock(pid=2)
    service._instances = {0: new}
    clock = [0.0]

    def join_old(timeout=None):
        clock[0] += timeout or 0
        if service._stop_deadline is None:
            service.stop()
            assert service._stop_deadline == clock[0] + 10

    def join_new(timeout=None):
        clock[0] += timeout or 0

    old.join.side_effect = join_old
    new.join.side_effect = join_new
    old.kill.side_effect = lambda: setattr(old.is_alive, "return_value", False)
    new.kill.side_effect = lambda: setattr(new.is_alive, "return_value", False)

    with mock.patch.object(hub.time, "monotonic", side_effect=lambda: clock[0]):
        service._drain([old])
        deadline = service._stop_deadline
        service.stop()
        assert service._stop_deadline == deadline
        service._finish()

    assert clock[0] <= deadline
    old.terminate.assert_called_once()
    new.terminate.assert_called_once()
    old.kill.assert_called_once()
    new.kill.assert_called_once()


def test_payload_transfer_and_readiness_share_startup_deadline():
    service = hub.ReloadableProcessHubService(ready_timeout=10)
    service._enabled = True
    service.add_service_factory(os.getpid)
    service.add_service_factory(os.getuid)
    clock = [0.0]
    instance = mock.Mock(pid=1, exitcode=None)
    instance.ready.has_failed.return_value = False

    def spawn(factory):
        assert service._ready_deadline == 10
        clock[0] += 3
        return instance

    def wait(timeout):
        clock[0] += timeout
        return False

    instance.ready.wait.side_effect = wait
    service._spawn = mock.Mock(side_effect=spawn)
    with mock.patch.object(hub.time, "monotonic", side_effect=lambda: clock[0]):
        generation = service._spawn_generation()
        assert not service._wait_ready(generation)
    assert 10 <= clock[0] < 10.2


def test_source_watch_detects_subsecond_edits_creation_and_deletion(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VERSION = 1\n")
    service = hub.ReloadableProcessHubService(autoreload=True, reload_dirs=[tmp_path])
    service._sources = service._snapshot_sources()
    timestamp = source.stat().st_mtime_ns
    service._check_source_changes()
    assert not service._reload_requested

    cache = tmp_path / "__pycache__" / "service.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"stale bytecode")
    source.write_text("VERSION = 2\n")
    os.utime(source, ns=(timestamp + 1000, timestamp + 1000))
    service._check_source_changes()
    assert service._reload_requested

    service._reload_requested = False
    (tmp_path / "notes.txt").write_text("ignore non-Python files")
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "unrelated.py").write_text("IGNORE = True\n")
    service._check_source_changes()
    assert not service._reload_requested

    extra = tmp_path / "plugin.py"
    extra.write_text("PLUGIN = True\n")
    service._check_source_changes()
    assert service._reload_requested
    service._reload_requested = False
    extra.unlink()
    service._check_source_changes()
    assert service._reload_requested


def test_source_watch_failure_disables_watcher_and_keeps_serving(tmp_path, monkeypatch):
    service = hub.ReloadableProcessHubService(autoreload=True, reload_dirs=[tmp_path])
    service._enabled = True
    error = PermissionError("source directory is unreadable")
    monkeypatch.setattr(hub.os, "scandir", mock.Mock(side_effect=error))
    service._loop_iteration()
    assert service._enabled
    assert not service.autoreload
    service._reload = mock.Mock()
    service.reload()
    service._loop_iteration()
    service._reload.assert_called_once()


def test_initial_source_watch_failure_still_launches_services(tmp_path, monkeypatch):
    service = hub.ReloadableProcessHubService(autoreload=True, reload_dirs=[tmp_path])
    service.should_subscribe_signals = False
    service._discover_source_dirs = mock.Mock(return_value=[tmp_path])
    instance = mock.Mock(exitcode=None)
    instance.ready.wait.return_value = True
    instance.ready.has_failed.return_value = False
    service._spawn_generation = mock.Mock(return_value={0: instance})
    monkeypatch.setattr(
        hub.os, "scandir", mock.Mock(side_effect=PermissionError("unreadable source"))
    )
    service._setup()
    service._spawn_generation.assert_called_once()
    assert service._instances
    assert not service.autoreload
    assert service._load_source
    service._reload = mock.Mock()
    service._enabled = True
    service.reload()
    service._iteration()
    service._reload.assert_called_once()


def test_autoreload_discovers_modules_and_lazy_editable_projects(tmp_path, monkeypatch):
    from importlib import metadata

    sdk = tmp_path / "sdk"
    sdk.mkdir()
    module = types.ModuleType("local_sdk")
    module.__file__ = str(sdk / "__init__.py")
    monkeypatch.setitem(sys.modules, "local_sdk", module)
    plugin = tmp_path / "lazy_plugin"
    plugin.mkdir()
    distribution = mock.Mock()
    distribution.read_text.return_value = json.dumps(
        {"url": plugin.as_uri(), "dir_info": {"editable": True}}
    )
    monkeypatch.setattr(metadata, "distributions", lambda: [distribution])
    service = hub.ReloadableProcessHubService(autoreload=True)
    directories = service._discover_source_dirs()
    assert sdk in directories
    assert plugin in directories
    assert "lazy_plugin" not in sys.modules


@pytest.mark.parametrize(
    "malformed",
    [
        "{",
        "[]",
        '{"url": 3}',
        '{"url": "", "dir_info": null}',
        '{"url": "file:", "dir_info": {"editable": true}}',
    ],
)
def test_autoreload_skips_malformed_package_metadata(
    tmp_path, monkeypatch, caplog, malformed
):
    from importlib import metadata

    packages = tmp_path / "site-packages"
    broken = packages / "broken-1.0.dist-info"
    broken.mkdir(parents=True)
    (broken / "direct_url.json").write_text(malformed)
    editable = packages / "editable-1.0.dist-info"
    editable.mkdir()
    source = tmp_path / "editable"
    source.mkdir()
    (editable / "direct_url.json").write_text(
        json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}})
    )
    distributions = list(metadata.distributions(path=[str(packages)]))
    distributions.sort(key=lambda dist: dist.read_text("direct_url.json") != malformed)
    monkeypatch.setattr(metadata, "distributions", lambda: distributions)
    service = hub.ReloadableProcessHubService(autoreload=True)
    service.should_subscribe_signals = False
    service._spawn_generation = mock.Mock(return_value={})

    service._setup()

    service._spawn_generation.assert_called_once()
    assert service.autoreload
    assert source in service._reload_dirs
    assert "Skipping malformed or unreadable direct_url.json" in caplog.text


def test_production_does_not_discover_or_scan_sources():
    service = hub.ReloadableProcessHubService()
    service.should_subscribe_signals = False
    service._discover_source_dirs = mock.Mock(side_effect=AssertionError)
    service._snapshot_sources = mock.Mock(side_effect=AssertionError)
    service._setup()
    service._check_source_changes()
    service._discover_source_dirs.assert_not_called()
    service._snapshot_sources.assert_not_called()


def test_source_watch_ignores_symlink_loops_and_leaves_external_cache(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VERSION = 1\n")
    protected = tmp_path / "protected"
    protected.mkdir()
    victim = protected / "service.cpython-312.pyc"
    victim.write_bytes(b"protected cache")
    (tmp_path / "__pycache__").symlink_to(protected, target_is_directory=True)
    (tmp_path / "loop.py").symlink_to("loop.py")
    service = hub.ReloadableProcessHubService(autoreload=True, reload_dirs=[tmp_path])
    service._sources = service._snapshot_sources()
    timestamp = source.stat().st_mtime_ns
    source.write_text("VERSION = 2\n")
    os.utime(source, ns=(timestamp + 1000, timestamp + 1000))
    service._check_source_changes()
    assert service._reload_requested
    assert victim.read_bytes() == b"protected cache"
    assert tmp_path / "loop.py" not in service._sources


def test_partial_launch_failure_drains_started_groups():
    service = hub.ReloadableProcessHubService()
    service.add_service_factory(os.getpid)
    service.add_service_factory(os.getuid)
    first = mock.Mock()
    service._spawn = mock.Mock(side_effect=[first, OSError("fork failed")])
    service._drain = mock.Mock()
    with pytest.raises(OSError, match="fork failed"):
        service._spawn_generation()
    assert list(service._drain.call_args.args[0]) == [first]


def test_request_during_cutover_reports_its_own_generation():
    service = hub.ReloadableProcessHubService()
    service._enabled = service._initial_ready = True
    first = service.reload()
    later = []

    def failed_replacement():
        later.append(service.reload())
        return False

    service._reload = mock.Mock(side_effect=failed_replacement)
    service._iteration()
    assert first.result(timeout=0) is False
    assert not later[0].done()
    service._clear_iteration_wake()
    assert service._wake_event.is_set()

    service._reload.side_effect = None
    service._reload.return_value = True
    service._iteration()
    assert later[0].result(timeout=0) is True


def test_shutdown_resolves_pending_and_later_reload_requests():
    service = hub.ReloadableProcessHubService()
    pending = service.reload()
    service._finish()
    assert pending.result(timeout=0) is False
    assert service.reload().result(timeout=0) is False


def test_background_failure_takes_priority_over_clean_worker_exit():
    service = hub.ReloadableProcessHubService()
    service._enabled = service._initial_ready = True
    worker = mock.Mock(pid=1, exitcode=0)
    background = mock.Mock(pid=2, exitcode=7)
    worker.is_alive.return_value = background.is_alive.return_value = False
    service._instances = {0: worker}
    service._background_instances = [background]

    service._iteration()

    assert not service._enabled
    assert service._failure == "Child service 2 exited with code 7"


def test_background_spawn_failure_cleans_up_workers_and_socket(tmp_path):
    path = tmp_path / "reload.sock"
    service = hub.ReloadableProcessHubService(reload_socket=path)
    service.should_subscribe_signals = False
    service.add_background_service_factory(os.getpid)
    worker = mock.Mock()
    service._spawn_generation = mock.Mock(return_value={0: worker})
    service._spawn = mock.Mock(side_effect=OSError("spawn failed"))
    service._drain = mock.Mock()

    with pytest.raises(OSError, match="spawn failed"):
        service.start()

    assert list(service._drain.call_args.args[0]) == [worker]
    assert not path.exists()
    assert service.reload().result(timeout=0) is False


def test_prefork_rejects_mixed_worker_privileges():
    services = [hub.ReloadableProcessHubService(), hub.ReloadableProcessHubService()]
    services[0].__mp_downgrade_user__ = None
    services[1].__mp_downgrade_user__ = "nobody"
    with pytest.raises(ValueError, match="same worker privileges"):
        hub._build_generation([mock.Mock(return_value=service) for service in services])
