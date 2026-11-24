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
"""Leader elector factory.

The elector backend is a plain string so it can come straight from the
configuration file::

    [my_service:MyService]
    election_backend = postgres_advisory
    election_connection_url = postgresql://user:pass@dbhost:5432/mydb
    election_lock_key = my_master_service

    [my_service:MyService]        # table-based flavor
    election_backend = postgres_table
    election_lock_timeout = 30
"""

from __future__ import annotations

import inspect
import logging
import typing as tp

from gcl_looper.election import always as always_elector
from gcl_looper.election import base as elector_base
from gcl_looper.election import db as db_elector
from gcl_looper.election.drivers import base as drivers_base
from gcl_looper.election.drivers import registry as drivers_registry

LOG = logging.getLogger(__name__)

ALWAYS_TYPE = "always"


def create_always_elector(**kwargs: tp.Any) -> always_elector.AlwaysLeader:
    """Create an elector that always grants the leadership (no election)."""
    return always_elector.AlwaysLeader()


def _driver_kwargs(
    driver_class: tp.Type[drivers_base.BaseLockDriver],
    params: tp.Dict[str, tp.Any],
) -> tp.Dict[str, tp.Any]:
    """Keep only the parameters accepted by the driver constructor."""
    signature = inspect.signature(driver_class.__init__)
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return dict(params)
    accepted = set(signature.parameters) - {"self"}
    return {k: v for k, v in params.items() if k in accepted}


def create_db_elector(
    refresh_interval: float = db_elector.DEFAULT_REFRESH_INTERVAL,
    **params: tp.Any,
) -> db_elector.DbLeaderElector:
    """Create a database-backed leader elector.

    The ``lock_driver`` parameter selects the registered driver by name
    (see ``gcl_looper.election.drivers.registry``); the rest of the
    parameters are passed to the driver constructor (unknown ones are
    dropped, so it is safe to pass the full configuration set to any
    driver).
    """
    try:
        driver_name = params.pop("lock_driver")
    except KeyError:
        raise ValueError(
            "`lock_driver` must be specified for the database elector; "
            "available drivers: %s" % ", ".join(drivers_registry.available_drivers())
        ) from None

    if not driver_name:
        raise ValueError("`lock_driver` must not be empty")

    driver_class = drivers_registry.get_driver_class(driver_name)
    driver = driver_class(**_driver_kwargs(driver_class, params))

    LOG.info(
        "Created database leader elector: driver=%s lock_key=%r",
        driver_name,
        driver.lock_key,
    )
    return db_elector.DbLeaderElector(
        driver=driver,
        refresh_interval=refresh_interval,
    )


def get_elector(
    backend: tp.Optional[str], **kwargs: tp.Any
) -> elector_base.LeaderElector:
    """Return a leader elector instance depending on the requested backend.

    ``backend`` is either the special ``always`` type (no election) or the
    name of a registered database lock driver (``postgres_advisory``,
    ``postgres_table``, ...).
    """
    if backend is None or backend == ALWAYS_TYPE:
        return create_always_elector(**kwargs)

    # Anything else must be a registered lock driver.
    return create_db_elector(lock_driver=backend, **kwargs)
