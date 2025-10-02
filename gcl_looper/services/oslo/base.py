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

import typing as tp

from oslo_config import cfg


class OsloConfigurableService:
    @classmethod
    def svc_get_config_opts(cls) -> tp.Collection[cfg.Opt] | None:
        """Return Oslo config options for the service.

        If None is returned, the service will attempt to load from
        the config file directly via `svc_from_config`.

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
        return None

    @classmethod
    def svc_from_config(cls, config_file: str) -> "OsloConfigurableService":
        """Create service instance from config file."""
        return cls()
