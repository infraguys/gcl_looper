#    Copyright 2026 George Melikov <mail@gmelikov.ru>
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import functools
import importlib
import json
import os
import signal
import socket
import sys
import subprocess
import threading
import time
from unittest import mock

import pytest
import requests
import psutil

from gcl_looper.services import bjoern_service
from gcl_looper.services import hub


def _factory(directory, port):
    sys.path.insert(0, str(directory))
    module = importlib.import_module("prefork_test_application")

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [f"{module.VERSION} {os.getpid()} {os.getppid()}".encode()]

    service = bjoern_service.BjoernService(
        app, "127.0.0.1", port, bjoern_kwargs={"reuse_port": True}
    )
    service.__mp_downgrade_user__ = None
    if module.VERSION == "stalled":
        service.add_setup(lambda: time.sleep(60))
    if module.VERSION == "failed_setup":
        service.add_setup(lambda: (_ for _ in ()).throw(RuntimeError("Setup failed")))
    return service


def _wait(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out")


@pytest.fixture(params=[1, 2])
def api(tmp_path, request):
    source = tmp_path / "prefork_test_application.py"
    source.write_text("VERSION = 'original'\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service = hub.ReloadableProcessHubService(
        ready_timeout=10, drain_timeout=0.5, iter_min_period=0.05
    )
    service.should_subscribe_signals = False
    for _ in range(request.param):
        service.add_service_factory(functools.partial(_factory, tmp_path, port))
    thread = threading.Thread(target=service.start)
    thread.start()
    try:
        _wait(
            lambda: (
                bool(service._instances)
                and all(i.ready.is_set() for i in service._instances.values())
            )
        )
        yield service, source, port
    finally:
        service.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert all(not i.is_alive() for i in service._instances.values())


def _get(port):
    return requests.get(
        f"http://127.0.0.1:{port}",
        headers={"Connection": "close"},
        timeout=2,
    ).text.split()


def test_fork_workers_and_reload_changed_module(api):
    service, source, port = api
    master = service._instances[0]
    responses = [_get(port) for _ in range(40)]
    assert {r[0] for r in responses} == {"original"}
    single_worker = {int(r[1]) for r in responses} == {master.pid}
    if not single_worker:
        assert {int(r[2]) for r in responses} == {master.pid}
        assert len({r[1] for r in responses}) == 2

    source.write_text("VERSION = 'updated_application'\n")
    service.reload()
    _wait(lambda: service._instances[0] is not master)
    _wait(lambda: not master.is_alive())
    responses = [_get(port) for _ in range(40)]
    assert {r[0] for r in responses} == {"updated_application"}
    pid_column = 1 if single_worker else 2
    assert {int(r[pid_column]) for r in responses} == {service._instances[0].pid}


@pytest.mark.parametrize(
    "code",
    ["invalid python!", "VERSION = 'stalled'\n", "VERSION = 'failed_setup'\n"],
)
def test_failed_generation_keeps_old_workers(api, code):
    service, source, port = api
    master = service._instances[0]
    finished = threading.Event()
    reload_generation = service._reload

    def reload_and_notify():
        reload_generation()
        finished.set()

    service._reload = reload_and_notify
    source.write_text(code)
    if "stalled" in code:
        service._ready_timeout = 1
    service.reload()
    assert finished.wait(10)
    assert service._instances[0] is master
    assert master.is_alive()
    assert _get(port)[0] == "original"


def test_dead_master_cleans_up_workers(api):
    service, _, port = api
    workers = {int(_get(port)[1]) for _ in range(40)}
    os.kill(service._instances[0].pid, signal.SIGKILL)
    _wait(lambda: not service._enabled)

    def stopped(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().split()[2] == "Z"
        except FileNotFoundError:
            return True

    _wait(lambda: all(stopped(pid) for pid in workers))


def test_stop_during_reload(api):
    service, source, _ = api
    spawning = threading.Event()
    spawn = service._spawn_generation

    def spawn_and_notify():
        generation = spawn()
        spawning.set()
        return generation

    service._spawn_generation = spawn_and_notify
    source.write_text("VERSION = 'stalled'\n")
    service.reload()
    assert spawning.wait(5)
    started = time.monotonic()
    service.stop()
    _wait(lambda: not service._instances[0].is_alive())
    assert time.monotonic() - started < 3


def test_no_resource_tracker_or_extra_master(api):
    service, _, port = api
    master = service._instances[0]
    children = psutil.Process(master.pid).children(recursive=True)
    single_worker = int(_get(port)[1]) == master.pid
    assert len(children) == (0 if single_worker else 2)
    assert all("resource_tracker" not in " ".join(p.cmdline()) for p in children)


def test_reload_closes_readiness_descriptors(api):
    service, source, port = api
    before = len(os.listdir("/proc/self/fd"))
    for version in ("another_application", "third_application_generation"):
        old = service._instances[0]
        source.write_text(f"VERSION = {version!r}\n")
        service.reload()
        _wait(lambda: service._instances[0] is not old and not old.is_alive())
        _wait(lambda: old.ready.fd is None)
        assert _get(port)[0] == version
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("module_entrypoint", [True, False])
def test_entrypoint_preserves_relative_imports_and_cli(tmp_path, module_entrypoint):
    package = tmp_path / "service_entrypoint"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "value.py").write_text("VALUE = 'module factory loaded'\n")
    (package / "runner.py").write_text(
        (
            "from .value import VALUE\n"
            if module_entrypoint
            else "VALUE = 'module factory loaded'\n"
        )
        + "from pathlib import Path\n"
        "import argparse, sys\n"
        "from gcl_looper.services import base, hub\n"
        "class Probe(base.AbstractService):\n"
        "    def __init__(self, output):\n"
        "        super().__init__()\n"
        "        self.output = output\n"
        "    def _loop(self):\n"
        "        Path(self.output).write_text(VALUE)\n"
        "    def stop(self):\n"
        "        pass\n"
        "def factory():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--output', required=True)\n"
        "    parser.add_argument('--config-file', required=True)\n"
        "    args = parser.parse_args()\n"
        "    assert args.config_file == 'service.conf'\n"
        "    return Probe(args.output)\n"
        "if __name__ == '__main__':\n"
        "    service = hub.ReloadableProcessHubService(iter_min_period=0.05)\n"
        "    service.add_service_factory(factory)\n"
        "    service.start()\n"
    )
    output = tmp_path / "result"
    subprocess.run(
        [sys.executable]
        + (
            ["-m", "service_entrypoint.runner"]
            if module_entrypoint
            else [str(package / "runner.py")]
        )
        + ["--output", str(output), "--config-file", "service.conf"],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path)] + sys.path)),
        check=True,
        timeout=10,
    )
    assert output.read_text() == "module factory loaded"


def test_isolated_interpreter_preserves_flags_and_closes_payload(tmp_path):
    poison = tmp_path / "gcl_looper"
    poison.mkdir()
    marker = tmp_path / "poisoned"
    (poison / "__init__.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    )
    output = tmp_path / "result"
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import os, pickle, sys\n"
        "from pathlib import Path\n"
        "from gcl_looper.services import base, hub\n"
        "class Probe(base.AbstractService):\n"
        "    def _loop(self):\n"
        "        assert sys.flags.isolated and sys.flags.no_user_site\n"
        "        assert os.read(0, 1) == b''\n"
        "        assert pickle.loads(pickle.dumps(Probe())).__class__ is Probe\n"
        f"        Path({str(output)!r}).write_text('ok')\n"
        "    def stop(self): pass\n"
        "def factory(): return Probe()\n"
        "if __name__ == '__main__':\n"
        "    service = hub.ReloadableProcessHubService(iter_min_period=0.05)\n"
        "    service.add_service_factory(factory)\n"
        "    service.start()\n"
    )
    subprocess.run(
        [sys.executable, "-I", str(runner)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(tmp_path)),
        check=True,
        timeout=15,
    )
    assert output.read_text() == "ok"
    assert not marker.exists()


def test_package_main_is_not_replayed(tmp_path):
    package = tmp_path / "unguarded_entrypoint"
    package.mkdir()
    (package / "__init__.py").touch()
    output = tmp_path / "result"
    (package / "worker.py").write_text(
        "from pathlib import Path\n"
        "from gcl_looper.services import base, hub\n"
        "class Probe(base.AbstractService):\n"
        f"    def _loop(self): Path({str(output)!r}).write_text('ok')\n"
        "    def stop(self): pass\n"
        "def factory(): return Probe()\n"
        "def main():\n"
        "    service = hub.ReloadableProcessHubService(iter_min_period=0.05)\n"
        "    service.add_service_factory(factory)\n"
        "    service.start()\n"
    )
    (package / "__main__.py").write_text("from .worker import main\nmain()\n")
    subprocess.run(
        [sys.executable, "-m", "unguarded_entrypoint"],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path)] + sys.path)),
        check=True,
        timeout=15,
    )
    assert output.read_text() == "ok"


@pytest.mark.parametrize("relocated_cache", [False, True])
@pytest.mark.parametrize("watcher_failed", [False, True])
def test_dev_generation_bypasses_same_second_bytecode(
    tmp_path, relocated_cache, watcher_failed
):
    source = tmp_path / "cached_service.py"
    output = tmp_path / "result"
    source.write_text("VERSION = 'old'\n")
    timestamp = source.stat().st_mtime_ns // 1_000_000_000 * 1_000_000_000
    os.utime(source, ns=(timestamp, timestamp))
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from pathlib import Path\n"
        "import os, py_compile, time\n"
        "from gcl_looper.services import base, hub\n"
        "class Probe(base.AbstractService):\n"
        "    def _loop(self):\n"
        "        import cached_service\n"
        f"        Path({str(output)!r}).write_text(cached_service.VERSION)\n"
        "    def stop(self): pass\n"
        "def factory(): return Probe()\n"
        "if __name__ == '__main__':\n"
        f"    py_compile.compile({str(source)!r}, doraise=True)\n"
        f"    Path({str(source)!r}).write_text(\"VERSION = 'new'\\n\")\n"
        f"    os.utime({str(source)!r}, ns=({timestamp + 1000}, {timestamp + 1000}))\n"
        "    service = hub.ReloadableProcessHubService(autoreload=True, iter_min_period=0.05)\n"
        "    service.add_service_factory(factory)\n"
        + (
            "    def fail_scan(): raise PermissionError('scan failed')\n"
            "    service._snapshot_sources = fail_scan\n"
            "    service._initial_ready = service._enabled = True\n"
            "    service._iteration()\n"
            "    assert not service.autoreload\n"
            "    service.reload()\n"
            "    service._iteration()\n"
            "    try:\n"
            "        deadline = time.monotonic() + 5\n"
            f"        while not Path({str(output)!r}).exists() and time.monotonic() < deadline:\n"
            "            time.sleep(0.01)\n"
            f"        assert Path({str(output)!r}).exists()\n"
            "    finally: service._finish()\n"
            if watcher_failed
            else "    service.start()\n"
        )
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path)] + sys.path))
    if relocated_cache:
        env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "cache")
    subprocess.run([sys.executable, str(runner)], env=env, check=True, timeout=15)
    assert output.read_text() == "new"


def test_failed_initial_prefork_generation_has_bounded_cleanup(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import time\n"
        "from gcl_looper.services import basic, hub\n"
        "count = 0\n"
        "class Probe(basic.BasicService):\n"
        "    def _iteration(self): pass\n"
        "def factory():\n"
        "    global count\n"
        "    count += 1\n"
        "    service = Probe()\n"
        "    if count == 1:\n"
        "        def fail(): raise RuntimeError('setup failed')\n"
        "        service.add_setup(fail)\n"
        "    else: service.add_setup(lambda: time.sleep(60))\n"
        "    return service\n"
        "if __name__ == '__main__':\n"
        "    service = hub.ReloadableProcessHubService(ready_timeout=5, drain_timeout=0.2)\n"
        "    service.add_service_factory(factory)\n"
        "    service.add_service_factory(factory)\n"
        "    service.start()\n"
    )
    subprocess.run(
        [sys.executable, str(runner)],
        check=True,
        timeout=10,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
    )


_BUILT_FACTORIES = []


class _SnapshotProbe(bjoern_service.base.AbstractService):
    def __init__(self, output):
        super().__init__()
        self.output = output

    def _loop(self):
        from pathlib import Path

        (Path(self.output) / str(os.getpid())).write_text(json.dumps(_BUILT_FACTORIES))
        time.sleep(60)

    def stop(self):
        pass


def _snapshot_factory(output, identity):
    _BUILT_FACTORIES.append(identity)
    return _SnapshotProbe(output)


def test_different_factories_have_isolated_prefork_state(tmp_path):
    service = hub.ReloadableProcessHubService(drain_timeout=0.1)
    for identity in ("first", "first", "second"):
        service.add_service_factory(
            functools.partial(_snapshot_factory, tmp_path, identity)
        )
    instances = service._spawn_generation()
    try:
        assert len(instances) == 2
        assert all(instance.ready.wait(10) for instance in instances.values())
        _wait(lambda: len(list(tmp_path.iterdir())) == 3)
        snapshots = [json.loads(path.read_text()) for path in tmp_path.iterdir()]
        assert snapshots.count(["first", "first"]) == 2
        assert snapshots.count(["second"]) == 1
    finally:
        service._drain(instances.values())


def test_process_group_stays_owned_until_cleanup(tmp_path):
    process = hub._ServiceProcess(
        functools.partial(_snapshot_factory, tmp_path, "probe")
    )
    try:
        assert process.ready.wait(10)
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=5)
        assert process.exitcode == -signal.SIGKILL
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert status.si_pid == process.pid
        process.close()
        with mock.patch.object(hub.os, "killpg") as kill_group:
            process.close()
            kill_group.assert_not_called()
    finally:
        process.close()


def test_external_reap_prevents_signalling_stale_group(tmp_path):
    process = hub._ServiceProcess(
        functools.partial(_snapshot_factory, tmp_path, "probe")
    )
    try:
        assert process.ready.wait(10)
        os.kill(process.pid, signal.SIGKILL)
        os.waitpid(process.pid, 0)
        with mock.patch.object(hub.os, "killpg") as kill_group:
            process.close()
            kill_group.assert_not_called()
    finally:
        process.close()


def test_factory_payload_can_exceed_pipe_capacity(tmp_path):
    identity = "large payload " * 100000
    process = hub._ServiceProcess(
        functools.partial(_snapshot_factory, tmp_path, identity)
    )
    try:
        assert process.ready.wait(10)
        output = tmp_path / str(process.pid)
        _wait(lambda: output.exists() and output.stat().st_size > len(identity))
        assert json.loads(output.read_text()) == [identity]
    finally:
        process.close()


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "stop"])
def test_blocked_payload_write_is_bounded_and_cleans_up(
    api, tmp_path, monkeypatch, cancel
):
    service, _, port = api
    old = service._instances[0]
    service._ready_timeout = 60 if cancel else 0.3
    service._factories = [
        functools.partial(_snapshot_factory, tmp_path, "large payload " * 100000)
    ]
    launched = threading.Event()
    finished = threading.Event()
    processes = []
    launch = subprocess.Popen.__init__

    def stalled_launch(process, args, **kwargs):
        args = list(args)
        args[args.index("-c") + 1] = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(60)\n"
        )
        launch(process, args, **kwargs)
        processes.append(process)
        launched.set()

    monkeypatch.setattr(subprocess.Popen, "__init__", stalled_launch)
    reload_generation = service._reload

    def reload_and_notify():
        try:
            reload_generation()
        finally:
            finished.set()

    service._reload = reload_and_notify
    service.reload()
    try:
        assert launched.wait(5)
        if cancel:
            service.stop()
        assert finished.wait(3)
        assert service._instances[0] is old
        assert processes[0].returncode is not None
        assert processes[0].stdin.closed
        assert processes[0].ready.fd is None
        if not cancel:
            assert old.is_alive()
            assert _get(port)[0] == "original"
    finally:
        for process in processes:
            process.kill()
