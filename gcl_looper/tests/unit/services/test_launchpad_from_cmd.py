import textwrap
from unittest import mock

import pytest
from oslo_config import cfg

from gcl_looper.services.oslo import launchpad


class OpsSvc:
    created = []

    @classmethod
    def svc_get_config_opts(cls):
        return [
            cfg.StrOpt("param", default="x"),
            cfg.IntOpt("num", default=1),
        ]

    def __init__(self, param: str = "x", num: int = 1):
        self.param = param
        self.num = num
        OpsSvc.created.append((param, num))


class ConfigSvc:
    created = 0

    @classmethod
    def svc_get_config_opts(cls):
        return None

    @classmethod
    def svc_from_config(cls, config_file: str):
        inst = cls()
        ConfigSvc.created += 1
        return inst


@pytest.fixture(autouse=True)
def fresh_conf(monkeypatch):
    # isolate global CONF per test
    monkeypatch.setattr(cfg, "CONF", cfg.ConfigOpts())


def make_cfg(tmp_path, content):
    p = tmp_path / "l.ini"
    p.write_text(textwrap.dedent(content))
    return str(p)


def test_from_cmd_line_ops_single(monkeypatch, tmp_path):
    # mock loader: module attr path -> OpsSvc
    monkeypatch.setattr(
        "gcl_looper.utils.cfg_load_module_attr",
        lambda path: OpsSvc,
    )

    ini = make_cfg(
        tmp_path,
        f"""
        [launchpad]
        services = mock.module:OpsSvc
        iter_min_period = 0
        iter_pause = 0
        
        [mock.module:OpsSvc]
        param = abc
        num = 7
        """,
    )

    svc = launchpad.LaunchpadService.from_cmd_line(
        [
            "--config-file",
            ini,
        ]
    )

    assert isinstance(svc, launchpad.LaunchpadService)
    # iter options applied to BasicService
    assert svc._iter_min_period == 0
    assert svc._iter_pause == 0
    # one OpsSvc instance constructed with config values
    assert len(svc._services) == 1
    assert OpsSvc.created[-1] == ("abc", 7)


def test_from_cmd_line_ops_count_2(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gcl_looper.utils.cfg_load_module_attr",
        lambda path: OpsSvc,
    )

    ini = make_cfg(
        tmp_path,
        f"""
        [launchpad]
        services = mock.module:OpsSvc::2
        
        [mock.module:OpsSvc::0]
        param = first
        num = 1
        
        [mock.module:OpsSvc::1]
        param = second
        num = 2
        """,
    )

    svc = launchpad.LaunchpadService.from_cmd_line(
        [
            "--config-file",
            ini,
        ]
    )

    assert len(svc._services) == 2
    assert OpsSvc.created[-2:] == [("first", 1), ("second", 2)]


def test_from_cmd_line_config_service(monkeypatch, tmp_path):
    # First try cfg_load_module_attr; make it raise ValueError
    # so code uses entry point path
    def fake_cfg_load(path):
        raise ValueError("not a module path")

    monkeypatch.setattr(
        "gcl_looper.utils.cfg_load_module_attr",
        fake_cfg_load,
    )

    monkeypatch.setattr(
        "gcl_looper.utils.load_from_entry_point",
        lambda group, name: ConfigSvc,
    )

    ini = make_cfg(
        tmp_path,
        f"""
        [launchpad]
        services = EntryConfig
        """,
    )

    svc = launchpad.LaunchpadService.from_cmd_line(
        [
            "--config-file",
            ini,
        ]
    )

    assert len(svc._services) == 1
    assert isinstance(svc._services[0], ConfigSvc)
    assert ConfigSvc.created == 1


def test_missing_section_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gcl_looper.utils.cfg_load_module_attr",
        lambda path: OpsSvc,
    )

    ini = make_cfg(
        tmp_path,
        f"""
        [launchpad]
        services = mock.module:OpsSvc
        """,
    )

    with pytest.raises(ValueError):
        launchpad.LaunchpadService.from_cmd_line(
            [
                "--config-file",
                ini,
            ]
        )


def test_common_handlers_called(monkeypatch, tmp_path):
    calls = {"reg": 0, "init": 0}

    def reg(conf):
        calls["reg"] += 1

    def init(conf):
        calls["init"] += 1

    def cfg_loader(path):
        if path.endswith(":reg"):
            return reg
        if path.endswith(":init"):
            return init
        return OpsSvc

    monkeypatch.setattr(
        "gcl_looper.utils.cfg_load_module_attr",
        cfg_loader,
    )

    ini = make_cfg(
        tmp_path,
        f"""
        [launchpad]
        services = mock.module:OpsSvc
        common_registrator_opts = mock.module:reg
        common_initializer = mock.module:init
        
        [mock.module:OpsSvc]
        param = abc
        num = 1
        """,
    )

    launchpad.LaunchpadService.from_cmd_line(
        [
            "--config-file",
            ini,
        ]
    )

    assert calls == {"reg": 1, "init": 1}
