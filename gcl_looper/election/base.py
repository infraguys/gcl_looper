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
"""Leader election abstraction.

A :class:`LeaderElector` is the primary building block of the master
election mechanism: it decides whether *this* process is currently allowed
to perform the guarded work. The service loop drives the elector once per
iteration via :meth:`LeaderElector.try_lead`; the elector is responsible
for acquiring and keeping the leadership (talking to the backing store)
and for reporting the current leadership state.

This is deliberately a *pure election* abstraction: it knows nothing about
service liveness, heartbeats or process supervision.
"""

from __future__ import annotations

import abc
import typing as tp


class LeaderElector(abc.ABC):
    """Abstract leader (master) elector.

    The elector owns the leadership state and the (optional) backing
    resources needed to keep it. The service calls :meth:`try_lead` at the
    start of every iteration; the elector returns ``True`` when this
    process is the master and ``False`` when another node holds the
    leadership.
    """

    @property
    @abc.abstractmethod
    def is_leader(self) -> bool:
        """Whether this process currently holds the leadership.

        Reflects the result of the most recent :meth:`try_lead` (or a
        subsequent loss detected by :meth:`ensure_leadership`). It does
        not contact the backing store by itself.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def try_lead(self) -> bool:
        """Try to become or remain the leader.

        Called once per service iteration. Implementations may throttle
        the backing-store traffic internally (the leadership is kept
        alive by repeated calls).

        Returns:
            ``True`` if this process is the master after the call,
            ``False`` if another node owns the leadership.

        Raises:
            gcl_looper.election.exceptions.BackendError: when the backing
                store is unreachable; the leadership is lost.
        """
        raise NotImplementedError()

    def ensure_leadership(self) -> None:
        """Raise if this process is not the leader.

        Intended for use *inside* a long-running guarded iteration so the
        work can abort promptly when the leadership is lost. The default
        implementation checks :attr:`is_leader`.

        Raises:
            gcl_looper.election.exceptions.LeadershipLostError: when not
                the leader.
        """
        if not self.is_leader:
            from gcl_looper.election import exceptions as exc

            raise exc.LeadershipLostError(lock_key=self.lock_key)

    @property
    def lock_key(self) -> tp.Optional[str]:
        """The logical lock name this elector competes for (if any)."""
        return None

    def close(self) -> None:
        """Release the leadership and any backing resources.

        Best-effort: must not raise. Called on service shutdown so a
        standby node can take over promptly.
        """
        pass
