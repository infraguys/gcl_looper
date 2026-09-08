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
"""Watchdogs for gcl_looper services.

The package provides the master election mechanism (originally implemented
in the ``rooster``/``node-manager`` stack on top of MySQL locks) for the
gcl_looper services: when several instances of a service run on different
nodes, only the instance holding the database lock (the *master*) performs
the iterations; the others stay on standby and skip their iterations until
the lock becomes free.

Quick start::

    from gcl_looper.services import basic
    from gcl_looper.watchdogs.locks.pg_table import PostgresTableLockDriver
    from gcl_looper.watchdogs.database import DbWatchDog

    watchdog = DbWatchDog(
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

    MyService(watchdog=watchdog).start()
"""

from gcl_looper.watchdogs import exceptions
from gcl_looper.watchdogs.base import TimedWatchDog, WatchDogBase
from gcl_looper.watchdogs.config import build_watchdog, build_watchdog_from_kwargs
from gcl_looper.watchdogs.config import get_config_opts as get_watchdog_config_opts
from gcl_looper.watchdogs.database import DbWatchDog
from gcl_looper.watchdogs.factory import get_watchdog

__all__ = (
    "DbWatchDog",
    "TimedWatchDog",
    "WatchDogBase",
    "build_watchdog",
    "build_watchdog_from_kwargs",
    "exceptions",
    "get_watchdog",
    "get_watchdog_config_opts",
)
