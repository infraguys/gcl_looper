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

from gcl_looper.election import always


class TestAlwaysLeader:
    def test_always_leader(self):
        elector = always.AlwaysLeader()
        assert elector.is_leader is True
        assert elector.try_lead() is True
        assert elector.is_leader is True

    def test_ensure_leadership_never_raises(self):
        elector = always.AlwaysLeader()
        elector.ensure_leadership()  # must not raise

    def test_lock_key_is_none(self):
        assert always.AlwaysLeader().lock_key is None

    def test_close_is_noop(self):
        elector = always.AlwaysLeader()
        elector.close()  # must not raise
        assert elector.is_leader is True
