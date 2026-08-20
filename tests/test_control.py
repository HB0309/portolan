"""Tests for the control-ownership state machine.

What matters is that automation cannot act while a human holds the session, and
that the way back always goes through the re-orientation state rather than
straight to acting again.
"""

from __future__ import annotations

import asyncio

import pytest

from cua.session.control import ControlOwner, ControlToken, ControlViolation


def test_starts_with_automation_in_control():
    token = ControlToken()
    assert token.owner is ControlOwner.AUTOMATION
    assert token.automation_may_act
    token.assert_automation("click")


def test_automation_may_not_act_once_blocked():
    token = ControlToken()
    token.block()
    assert not token.automation_may_act
    with pytest.raises(ControlViolation, match="belongs to blocked"):
        token.assert_automation("click")


def test_automation_may_not_act_while_a_human_holds_the_session():
    token = ControlToken()
    token.block()
    token.grant_to_human()
    with pytest.raises(ControlViolation, match="belongs to human"):
        token.assert_automation("type_text")


def test_handback_passes_through_the_reorientation_state():
    """Automation never goes straight from HUMAN back to acting."""
    token = ControlToken()
    token.block()
    token.grant_to_human()
    token.begin_handback()
    assert token.owner is ControlOwner.HANDING_BACK
    with pytest.raises(ControlViolation):
        token.assert_automation("click")
    token.restore_to_automation()
    assert token.automation_may_act


def test_restore_from_human_is_allowed_and_reorients_implicitly():
    token = ControlToken()
    token.block()
    token.grant_to_human()
    token.restore_to_automation()
    assert token.owner is ControlOwner.AUTOMATION


def test_approval_without_takeover_returns_control_directly():
    """An operator can approve without ever touching the session."""
    token = ControlToken()
    token.block()
    token.restore_to_automation()
    assert token.automation_may_act


def test_illegal_transitions_are_rejected():
    token = ControlToken()
    with pytest.raises(ControlViolation):
        token.grant_to_human()  # must be blocked first


def test_human_actions_are_counted():
    token = ControlToken()
    token.block()
    token.grant_to_human()
    token.record_human_action()
    token.record_human_action()
    assert token.human_action_count == 2


@pytest.mark.asyncio
async def test_automation_parks_until_resumed():
    token = ControlToken()
    token.block()

    resumed = []

    async def waiter():
        await token.wait_until_resumed()
        resumed.append(token.owner)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)
    assert not resumed, "automation resumed while still blocked"

    token.grant_to_human()
    token.restore_to_automation()
    await asyncio.wait_for(task, timeout=1)
    assert resumed == [ControlOwner.AUTOMATION]
