"""Who is driving.

The requirement is that a human takes over *the same live session* the
automation was using -- not a fresh one, with the cookies, the form state and
the navigation history intact. That is a constraint on session lifetime, which
makes it an architectural property rather than a feature that can be added
later, and it is why the browser context is owned here rather than created per
step by whatever happens to need one.

Control is an explicit value rather than an assumption. Every action asserts
that its caller owns it, so an automation action taken while a human is in the
middle of typing is a loud error at the point it happens, instead of a race that
produces a confusing screenshot ten seconds later.

    AUTOMATION --- intervention raised ---> BLOCKED
                                              |
                                       operator takes control
                                              v
    AUTOMATION <--- checkpoint re-asserted --- HANDING_BACK <--- operator resumes --- HUMAN

The return path goes through HANDING_BACK rather than straight to AUTOMATION on
purpose: the automation re-observes and re-checks its expectations before it
starts acting again, because it has no idea what the human did and should not
assume they left the screen where it was.
"""

from __future__ import annotations

import asyncio
from enum import Enum


class ControlOwner(str, Enum):
    AUTOMATION = "automation"
    #: Automation has stopped and is waiting; nobody is acting yet.
    BLOCKED = "blocked"
    HUMAN = "human"
    #: The human is finished; automation is re-orienting before it resumes.
    HANDING_BACK = "handing_back"


class ControlViolation(RuntimeError):
    """Something tried to act while it did not own control.

    Always a bug in this system rather than a condition to recover from, so it
    is raised loudly instead of being handled.
    """


_ALLOWED: dict[ControlOwner, set[ControlOwner]] = {
    ControlOwner.AUTOMATION: {ControlOwner.BLOCKED},
    ControlOwner.BLOCKED: {ControlOwner.HUMAN, ControlOwner.AUTOMATION},
    ControlOwner.HUMAN: {ControlOwner.HANDING_BACK},
    ControlOwner.HANDING_BACK: {ControlOwner.AUTOMATION},
}


class ControlToken:
    """The single source of truth for who may act on the session."""

    def __init__(self) -> None:
        self._owner = ControlOwner.AUTOMATION
        self._resumed = asyncio.Event()
        self._resumed.set()
        self.human_action_count = 0

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def automation_may_act(self) -> bool:
        return self._owner is ControlOwner.AUTOMATION

    def _transition(self, to: ControlOwner) -> None:
        if to not in _ALLOWED[self._owner]:
            raise ControlViolation(f"cannot move from {self._owner.value} to {to.value}")
        self._owner = to

    # -- transitions -----------------------------------------------------

    def block(self) -> None:
        """Automation stops and waits. Nobody is acting."""
        self._transition(ControlOwner.BLOCKED)
        self._resumed.clear()

    def grant_to_human(self) -> None:
        self._transition(ControlOwner.HUMAN)

    def begin_handback(self) -> None:
        self._transition(ControlOwner.HANDING_BACK)

    def restore_to_automation(self) -> None:
        if self._owner is ControlOwner.BLOCKED:
            # The intervention was resolved without anyone taking over -- an
            # approval, say. Nothing to hand back.
            self._transition(ControlOwner.AUTOMATION)
        else:
            if self._owner is ControlOwner.HUMAN:
                self.begin_handback()
            self._transition(ControlOwner.AUTOMATION)
        self._resumed.set()

    def record_human_action(self) -> None:
        self.human_action_count += 1

    # -- waiting ---------------------------------------------------------

    async def wait_until_resumed(self, timeout: float | None = None) -> None:
        """Park the automation while a person deals with it."""
        if timeout is None:
            await self._resumed.wait()
        else:
            await asyncio.wait_for(self._resumed.wait(), timeout=timeout)

    def assert_automation(self, action: str) -> None:
        if self._owner is not ControlOwner.AUTOMATION:
            raise ControlViolation(
                f"automation tried to {action} while control belongs to "
                f"{self._owner.value}"
            )
