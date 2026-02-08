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
from __future__ import annotations

import enum
import logging
import typing as tp

from oslo_config import cfg

from gcl_looper import utils
from gcl_looper import version
from gcl_looper import constants as c
from gcl_looper.services import basic
from gcl_looper.services.oslo import base as oslo_base

LOG = logging.getLogger(__name__)
DOMAIN = "launchpad"


class ServiceType(enum.Enum):
    OPS = "ops"
    CONFIG = "config"


def load_config(
    args: list[str], conf: cfg.ConfigOpts | None = None
) -> cfg.ConfigOpts:
    if conf is None:
        conf = cfg.ConfigOpts()

    conf(
        args=args,
        project=c.GLOBAL_SERVICE_NAME,
        version=f"{c.GLOBAL_SERVICE_NAME.capitalize()} {version.version_info}",
    )
    if not conf.config_file:
        raise FileNotFoundError("Configuration file is not set")

    return conf


class LaunchpadService(basic.BasicService, oslo_base.OsloConfigurableService):

    def __init__(
        self,
        services: tp.Collection[basic.BasicService],
        iter_min_period: float = 1,
        iter_pause: float = 0.1,
    ):
        super().__init__(iter_min_period, iter_pause)
        self._services = services

    def _setup(self):
        LOG.info("Setup all services")
        for service in self._services:
            service._setup()

    def _iteration(self):
        # Iterate all services
        for service in self._services:
            service._loop_iteration()

    @classmethod
    def _parse_svc_str(cls, svc_str: str) -> tuple[str, int]:
        """Parse the service string to get the service class and count.

        Valid formats:
        MyService                   # Registered through entry point
        MyService::2                # Registered through entry point, count=2
        package.module:MyService    # Path to service class
        package.module:MyService::2 # Path to service class, count=2

        Returns:
            tuple[str, int]: Service class and count.
        """

        if "::" in svc_str:
            try:
                svc, count_str = svc_str.split("::")
                count = int(count_str)
            except ValueError:
                LOG.error("Invalid service format: %s", svc_str)
                raise
        else:
            svc = svc_str
            count = 1

        return svc, count

    @classmethod
    def svc_get_config_opts(cls) -> tp.Collection[cfg.Opt]:
        """Return Oslo config options for the service.

        Example:

        def svc_get_config_opts(cls):
            return [
                cfg.StrOpt(
                    "name",
                    default="set_agent",
                    help="Name for the service",
                ),
            ]
        """
        return [
            cfg.ListOpt(
                "services",
                default=tuple(),
                help=(
                    "List of services to run. The service can be in two formats: "
                    "1. <module>:<class>[::count] "
                    "2. <class>[::count], available loaded from the entry point. "
                    "The count is optional and defaults to 1. "
                    "If the count is specified, the corresponding configuration "
                    "section is expected to be repeated count times. "
                    "Example: "
                    "services = \n"
                    "    my_project.my_module:MyService::2\n"
                    "\n"
                    "[my_project.my_module:MyService::0]\n"
                    "option1 = value1\n"
                    "option2 = value2\n"
                    "\n"
                    "[my_project.my_module:MyService::1]\n"
                    "option1 = value1\n"
                    "option2 = value2\n"
                ),
            ),
            cfg.StrOpt(
                "common_registrator_opts",
                default=None,
                help=(
                    "Registration handler for common options. "
                    "It's a path to any callable object that is loaded and called "
                    "before any service is started. For instance, registration "
                    "of the database engine options. Example: "
                    "my_project.my_module:db_engine_reg "
                    "The registration handler has single input parameter: "
                    "cfg.CONF (oslo config options)."
                ),
            ),
            cfg.StrOpt(
                "common_initializer",
                default=None,
                help=(
                    "Common initialization handler for all services. "
                    "It's a path to any callable object that is loaded and called "
                    "before any service is started. For instance, initialization "
                    "of the database engine. Example: "
                    "my_project.my_module:db_engine_init "
                    "The initializer has single input parameter: "
                    "cfg.CONF (oslo config options)."
                ),
            ),
            cfg.FloatOpt(
                "iter_min_period",
                default=3,
                help="Minimum period between iterations",
            ),
            cfg.FloatOpt(
                "iter_pause",
                default=0.1,
                help="Pause between iterations",
            ),
        ]

    @classmethod
    def from_cmd_line(cls, args: list[str]) -> "LaunchpadService":
        """Instantiate a LaunchpadService instance from command line arguments.

        Args:
            args: List of command line arguments.

        Returns:
            LaunchpadService instance.
        """
        launchpad_cfg_opts = cfg.ConfigOpts()
        launchpad_cfg_opts.register_cli_opts(cls.svc_get_config_opts(), DOMAIN)

        launchpad_cfg = load_config(args, launchpad_cfg_opts)

        # Load service classes
        services = []
        services_classes = []

        for svc in launchpad_cfg[DOMAIN].services:
            svc, count = cls._parse_svc_str(svc)

            try:
                svc_class: oslo_base.OsloConfigurableService = (
                    utils.cfg_load_module_attr(svc)
                )
            except ValueError:
                svc_class: oslo_base.OsloConfigurableService = (
                    utils.load_from_entry_point(c.EP_GCL_LOOPER_SERVICES, svc)
                )

            opts = svc_class.svc_get_config_opts()

            # Register service options
            if opts is not None:
                # Register options for each service instance
                for i in range(count):
                    section_name = f"{svc}::{i}" if count > 1 else svc
                    cfg.CONF.register_cli_opts(opts, section_name)

                services_classes.append(
                    (svc, svc_class, ServiceType.OPS, count)
                )
            # Allow to load configuration manually for the service
            else:
                services_classes.append(
                    (svc, svc_class, ServiceType.CONFIG, count)
                )

        # Use common initializer if specified
        if launchpad_cfg[DOMAIN].common_registrator_opts:
            common_registrator_opts = utils.cfg_load_module_attr(
                launchpad_cfg[DOMAIN].common_registrator_opts
            )
            common_registrator_opts(cfg.CONF)

        # Parse config for services
        load_config(args, cfg.CONF)

        # Use common initializer if specified
        if launchpad_cfg[DOMAIN].common_initializer:
            common_initializer = utils.cfg_load_module_attr(
                launchpad_cfg[DOMAIN].common_initializer
            )
            common_initializer(cfg.CONF)

        # Load services
        for svc_name, svc_class, svc_type, count in services_classes:
            # Loading services in according to the count
            for i in range(count):
                if svc_type == ServiceType.CONFIG:
                    svc = svc_class.svc_from_config(launchpad_cfg.config_file)
                elif svc_type == ServiceType.OPS:
                    section_name = (
                        f"{svc_name}::{i}" if count > 1 else svc_name
                    )

                    # Check if configuration section exists
                    if section_name not in cfg.CONF.list_all_sections():
                        LOG.error(
                            "Section %s not found in config", section_name
                        )
                        raise ValueError(
                            f"Section `{section_name}` for service "
                            f"`{svc_name}` not found in config"
                        )

                    svc = svc_class(**cfg.CONF[section_name])
                else:
                    raise ValueError(f"Unknown service type: {svc_type}")
                services.append(svc)
            LOG.info("Service %s loaded, %d instance(s)", svc_name, count)

        # Create global service
        return cls(
            services=services,
            iter_min_period=launchpad_cfg[DOMAIN].iter_min_period,
            iter_pause=launchpad_cfg[DOMAIN].iter_pause,
        )
