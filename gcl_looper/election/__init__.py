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
"""Leader (master) election for gcl_looper services.

When several instances of a service run on different nodes, only the
instance that owns the election lock (the *master*) performs the guarded
work; the others stay on standby and skip their iterations until the lock
becomes free.

The primary abstraction is :class:`~gcl_looper.election.base.LeaderElector`:
a per-iteration leadership guard driven by the service loop. The backend is
chosen through a database-agnostic lock driver interface and a name
registry, so supporting a new database is a matter of implementing a
driver class and selecting it in the configuration.

Quick start::

    from gcl_looper.services import basic
    from gcl_looper.election.drivers.pg_table import PostgresTableLockDriver
    from gcl_looper.election.db import DbLeaderElector

    elector = DbLeaderElector(
        PostgresTableLockDriver(
            connection_url="postgresql://user:pass@host:5432/db",
            lock_key="my_service_master",
        ),
    )

    class MyService(basic.BasicService):
        def _iteration(self):
            print("I am the master now")
        # optional hooks:
        # def _on_become_master(self): ...
        # def _on_lose_master(self): ...

    MyService(elector=elector).start()
"""

from __future__ import annotations

from gcl_looper.election import exceptions
from gcl_looper.election.always import AlwaysLeader
from gcl_looper.election.base import LeaderElector
from gcl_looper.election.config import build_elector, build_elector_from_kwargs
from gcl_looper.election.config import get_config_opts as get_election_config_opts
from gcl_looper.election.db import DbLeaderElector
from gcl_looper.election.factory import get_elector

__all__ = (
    "AlwaysLeader",
    "DbLeaderElector",
    "LeaderElector",
    "build_elector",
    "build_elector_from_kwargs",
    "exceptions",
    "get_election_config_opts",
    "get_elector",
)
