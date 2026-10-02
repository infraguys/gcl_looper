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
"""Exceptions raised by the leader election.

The semantics are inherited from the original ``rooster``/``node-manager``
watchdog stack, with the "minor" outcome moved to the ``bool`` return value
of :meth:`~gcl_looper.election.base.LeaderElector.try_lead`:

* not being the master is *not* an exception: ``try_lead()`` simply
  returns ``False`` and the service skips the iteration;
* :class:`BackendError` is *fatal* for the current iteration (e.g. the
  database connection is lost) and is surfaced to the service loop;
* :class:`LeadershipLostError` is raised by
  :meth:`~gcl_looper.election.base.LeaderElector.ensure_leadership` when
  long-running guarded work must be aborted because the leadership is gone.
"""

from __future__ import annotations


class ElectionError(Exception):
    """Base leader-election exception.

    Subclasses should define ``msg_template`` which is interpolated with
    the keyword arguments passed to the constructor, e.g.::

        raise LeadershipLostError(lock_key="my_lock")
    """

    msg_template = "An unknown election exception occurred."

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        super(ElectionError, self).__init__()

    def __repr__(self) -> str:
        qual_name = "%s.%s" % (self.__module__, type(self).__name__)
        kwargs = ", ".join("%s=%r" % (k, v) for k, v in self._kwargs.items())
        return "%s(%s)" % (qual_name, kwargs)

    def __str__(self) -> str:
        try:
            return self.msg_template % self._kwargs
        except (TypeError, KeyError):
            return "%s: %r" % (self.msg_template, self._kwargs)

    @property
    def message(self) -> str:
        return str(self)

    @property
    def kwargs(self) -> dict:
        return dict(self._kwargs)


class BackendError(ElectionError):
    """The backing store of the election is lost or unusable.

    The leadership can not be trusted anymore when this is raised; the
    elector resets its state and the service treats the iteration as
    failed.
    """

    msg_template = "Election backend error: %(reason)r"


class LeadershipLostError(ElectionError):
    """The lock was lost while guarded work was still running."""

    msg_template = "Ownership of lock %(lock_key)r was lost during the iteration."


class ElectorNotFound(ElectionError):
    """Requested election backend is not available/registered."""

    msg_template = (
        "Unknown election backend %(backend)r. Allowed variants: %(allowed)s."
    )
