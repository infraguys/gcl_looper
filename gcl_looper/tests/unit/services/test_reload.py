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
import json
import os
import sys
import threading
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
        assert ready.fd is None
        assert ready.is_set() == bool(message)


def test_readiness_pipe_preserves_signal_for_concurrent_waiters(monkeypatch):
    read_fd, write_fd = os.pipe()
    ready = hub._Readiness(read_fd)
    os.write(write_fd, b"R")
    os.close(write_fd)
    waiters = threading.Barrier(2)
    reading = threading.Barrier(2)
    select_readiness = hub.select.select

    def simultaneous_select(*args):
        result = select_readiness(*args)
        try:
            reading.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return result

    def wait():
        waiters.wait(timeout=5)
        return ready.wait(1)

    monkeypatch.setattr(hub.select, "select", simultaneous_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(wait) for _ in range(2)]
            assert [result.result(timeout=5) for result in results] == [True, True]
        assert ready.fd is None
        assert ready.is_set()
    finally:
        ready.close()


def test_worker_exiting_while_peer_starts_rejects_reload():
    service = hub.ReloadableProcessHubService()
    service._enabled = True
    old = mock.Mock()
    service._instances = {0: old}
    first, second = mock.Mock(pid=1, exitcode=1), mock.Mock(pid=2)
    first.ready.wait.return_value = True
    first.is_alive.return_value = True

    def peer_ready(timeout):
        first.is_alive.return_value = False
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
    instance = mock.Mock(pid=1)

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


def test_prefork_rejects_mixed_worker_privileges():
    services = [hub.ReloadableProcessHubService(), hub.ReloadableProcessHubService()]
    services[0].__mp_downgrade_user__ = None
    services[1].__mp_downgrade_user__ = "nobody"
    with pytest.raises(ValueError, match="same worker privileges"):
        hub._build_generation([mock.Mock(return_value=service) for service in services])
