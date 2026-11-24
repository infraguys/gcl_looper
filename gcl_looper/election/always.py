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
"""A no-op elector that always grants the leadership.

Used when the master election is disabled: the service always acts as the
master without any coordination.
"""

from __future__ import annotations

from gcl_looper.election import base


class AlwaysLeader(base.LeaderElector):
    """An elector that always reports itself as the leader."""

    @property
    def is_leader(self) -> bool:
        return True

    def try_lead(self) -> bool:
        return True

    def ensure_leadership(self) -> None:
        # Always the leader: nothing to check.
        return None
