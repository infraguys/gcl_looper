#    Copyright 2026 George Melikov <mail@gmelikov.ru>
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

from unittest import mock

from gcl_looper.services import hub


def test_worker_exiting_while_peer_starts_rejects_reload():
    service = hub.ReloadableProcessHubService()
    service._enabled = True
    old = mock.Mock()
    service._instances = {0: old}
    first, second = mock.Mock(pid=1, exitcode=1), mock.Mock(pid=2)
    first.ready.wait.return_value = True
    first.is_alive.return_value = True

    def peer_ready(timeout):
        first.is_alive.return_value = False
        return True

    second.ready.wait.side_effect = peer_ready
    service._spawn_generation = mock.Mock(return_value={0: first, 1: second})
    service._drain = mock.Mock()

    service.reload()
    service._iteration()

    assert service._enabled
    assert service._instances == {0: old}
    assert list(service._drain.call_args.args[0]) == [first, second]
    old.terminate.assert_not_called()


def test_stop_during_drain_shares_deadline_between_generations():
    service = hub.ReloadableProcessHubService(drain_timeout=10)
    old, new = mock.Mock(pid=1), mock.Mock(pid=2)
    service._instances = {0: new}
    clock = [0.0]

    def join_old(timeout=None):
        clock[0] += timeout or 0
        if service._stop_deadline is None:
            service.stop()
            assert service._stop_deadline == clock[0] + 10

    def join_new(timeout=None):
        clock[0] += timeout or 0

    old.join.side_effect = join_old
    new.join.side_effect = join_new
    old.kill.side_effect = lambda: setattr(old.is_alive, "return_value", False)
    new.kill.side_effect = lambda: setattr(new.is_alive, "return_value", False)

    with mock.patch.object(hub.time, "monotonic", side_effect=lambda: clock[0]):
        service._drain([old])
        deadline = service._stop_deadline
        service.stop()
        assert service._stop_deadline == deadline
        service._finish()

    assert clock[0] <= deadline
    old.terminate.assert_called_once()
    new.terminate.assert_called_once()
    old.kill.assert_called_once()
    new.kill.assert_called_once()
