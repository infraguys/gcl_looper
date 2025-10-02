import pytest

from gcl_looper.services.oslo import launchpad


class TestLaunchpadServiceParseAndOpts:
    def test_parse_svc_str_variants(self):
        assert launchpad.LaunchpadService._parse_svc_str("MyService") == (
            "MyService",
            1,
        )
        assert launchpad.LaunchpadService._parse_svc_str("MyService::2") == (
            "MyService",
            2,
        )
        assert launchpad.LaunchpadService._parse_svc_str(
            "pkg.mod:MyService"
        ) == ("pkg.mod:MyService", 1)

    def test_parse_svc_str_invalid(self):
        with pytest.raises(ValueError):
            # split will raise ValueError when more than one '::'
            launchpad.LaunchpadService._parse_svc_str("A::B::C")

    def test_svc_get_config_opts_contains_expected(self):
        opts = launchpad.LaunchpadService.svc_get_config_opts()
        names = {o.name for o in opts}
        # Expected option names
        assert {
            "services",
            "common_registrator_opts",
            "common_initializer",
            "iter_min_period",
            "iter_pause",
        }.issubset(names)

    def test_load_config_ok_with_config_file(self, tmp_path):
        cfg_file = tmp_path / "conf.ini"
        cfg_file.write_text("")

        conf = launchpad.load_config(args=["--config-file", str(cfg_file)])
        # oslo.config populates .config_file list
        assert conf.config_file
