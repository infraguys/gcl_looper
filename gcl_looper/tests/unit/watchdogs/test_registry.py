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

import pytest

from gcl_looper.watchdogs import exceptions as exc
from gcl_looper.watchdogs.locks import registry
from gcl_looper.watchdogs.locks.base import BaseLockDriver


def test_builtin_drivers_registered():
    names = registry.available_drivers()
    assert "postgres_advisory" in names
    assert "postgres_table" in names


def test_get_driver_class_resolves_postgres_drivers():
    advisory = registry.get_driver_class("postgres_advisory")
    table = registry.get_driver_class("postgres_table")
    assert issubclass(advisory, BaseLockDriver)
    assert issubclass(table, BaseLockDriver)


def test_unknown_driver_raises():
    with pytest.raises(exc.LockDriverNotFound):
        registry.get_driver_class("does_not_exist")


def test_register_custom_driver():
    # Use a builtin path to avoid depending on a test module import path.
    registry.register_driver(
        "custom",
        "gcl_looper.watchdogs.locks.pg_table:PostgresTableLockDriver",
    )
    try:
        assert "custom" in registry.available_drivers()
        assert registry.get_driver_class("custom") is (
            registry.get_driver_class("postgres_table")
        )
    finally:
        registry._driver_registry.pop("custom", None)


def test_register_non_subclass_rejected():
    registry.register_driver("not_a_driver", "gcl_looper.constants:GLOBAL_SERVICE_NAME")
    try:
        with pytest.raises(TypeError):
            registry.get_driver_class("not_a_driver")
    finally:
        registry._driver_registry.pop("not_a_driver", None)
