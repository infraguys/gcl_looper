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

import sys
import importlib
import typing as tp
import configparser
import importlib_metadata


def cfg_load_module_attr(attr_path: str) -> tp.Any:
    """Load attribute from config file.

    Model path format: <module>:<attr>
    Example: gcl_sdk.infra.dm.models:Node
    """
    if ":" not in attr_path:
        raise ValueError(f"Invalid model path: {attr_path}")

    module_path, attr_name = attr_path.split(":", 1)

    # Import the module if it's not already loaded
    if module_path not in sys.modules:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            raise ValueError(f"Module {module_path} not found")
    else:
        module = sys.modules[module_path]

    try:
        attr = getattr(module, attr_name)
    except AttributeError:
        raise ValueError(
            f"Attribute {attr_name} not found in module {module_path}"
        )

    return attr


def cfg_load_section_map(config_file: str, section: str) -> dict[str, str]:
    """Load section map from config file

    Example:
    [section]
    option1 = value1
    option2 = value2

    Returns: {"option1": "value1", "option2": "value2"}
    """
    params = {}
    parser = configparser.ConfigParser()
    parser.read(config_file)

    if not parser.has_section(section):
        return params

    for option in parser.options(section):
        if option in parser.defaults():
            continue

        params[option] = parser.get(section, option)

    return params


def load_from_entry_point(group: str, name: str) -> tp.Any:
    """Load class from entry points."""
    for ep in importlib_metadata.entry_points(group=group):
        if ep.name == name:
            return ep.load()

    raise RuntimeError(f"No class '{name}' found in entry points {group}")
