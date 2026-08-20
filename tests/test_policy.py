"""Tests for the runtime guardrails.

The allowlist and the reversibility classifier are the two places where a
mistake is expensive rather than annoying, so both are tested for what they
*refuse* as much as for what they permit.
"""

from __future__ import annotations

import pytest

from cua.policy import Disposition, Mode, PolicyEngine, Redactor
from cua.policy.redaction import MASK
from cua.schema.capability import ElementDescriptor, RiskClass


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.load()


def desc(role: str, name: str) -> ElementDescriptor:
    return ElementDescriptor(role=role, accessible_name=name, robustness_note="test")


class TestAllowlist:
    def test_permits_an_allowlisted_route(self, policy):
        allowed, _ = policy.url_allowed("http://127.0.0.1:8080/member?id=1")
        assert allowed

    def test_refuses_an_unknown_origin(self, policy):
        allowed, why = policy.url_allowed("https://evil.example.com/member")
        assert not allowed
        assert "origin" in why

    def test_refuses_an_unlisted_path_on_an_allowed_origin(self, policy):
        allowed, why = policy.url_allowed("http://127.0.0.1:8080/admin/purge")
        assert not allowed
        assert "path" in why

    def test_refuses_a_relative_url(self, policy):
        allowed, _ = policy.url_allowed("/member?id=1")
        assert not allowed

    def test_refuses_an_action_type_that_is_not_permitted(self, policy):
        verdict = policy.check(mode=Mode.REPLAY, action_type="upload_file")
        assert verdict.disposition is Disposition.DENY
        assert verdict.rule == "allowlist.actions"


class TestReversibility:
    def test_a_committing_button_is_irreversible(self, policy):
        risk, why = policy.classify_risk("click", desc("button", "Transfer Funds"))
        assert risk is RiskClass.IRREVERSIBLE
        assert "transfer" in why

    def test_a_link_with_a_committing_name_is_not(self, policy):
        """A link named "Open Sub-Account" opens the form; it does not submit it."""
        risk, _ = policy.classify_risk("click", desc("link", "Open Sub-Account"))
        assert risk is RiskClass.SAFE

    def test_typing_is_never_irreversible(self, policy):
        risk, _ = policy.classify_risk("type_text", desc("textbox", "Transfer Amount"))
        assert risk is RiskClass.SAFE

    def test_reading_is_never_irreversible(self, policy):
        risk, _ = policy.classify_risk("extract", desc("cell", "Delete"))
        assert risk is RiskClass.SAFE

    def test_an_ordinary_button_is_safe(self, policy):
        risk, _ = policy.classify_risk("click", desc("button", "Search"))
        assert risk is RiskClass.SAFE


class TestDispositions:
    def test_discovery_always_stops_for_a_human_on_an_irreversible_action(self, policy):
        verdict = policy.check(
            mode=Mode.DISCOVERY, action_type="click", target=desc("button", "Submit")
        )
        assert verdict.disposition is Disposition.ESCALATE

    def test_replay_refuses_an_unapproved_irreversible_action(self, policy):
        verdict = policy.check(
            mode=Mode.REPLAY, action_type="click", target=desc("button", "Submit")
        )
        assert verdict.disposition is Disposition.ESCALATE

    def test_replay_needs_both_the_step_and_the_capability_approved(self, policy):
        target = desc("button", "Submit")
        step_only = policy.check(
            mode=Mode.REPLAY,
            action_type="click",
            target=target,
            step_approved_risky=True,
            capability_approved=False,
        )
        cap_only = policy.check(
            mode=Mode.REPLAY,
            action_type="click",
            target=target,
            step_approved_risky=False,
            capability_approved=True,
        )
        both = policy.check(
            mode=Mode.REPLAY,
            action_type="click",
            target=target,
            step_approved_risky=True,
            capability_approved=True,
        )
        assert step_only.disposition is Disposition.ESCALATE
        assert cap_only.disposition is Disposition.ESCALATE
        assert both.disposition is Disposition.ALLOW

    def test_safe_actions_pass_without_ceremony(self, policy):
        verdict = policy.check(
            mode=Mode.REPLAY, action_type="type_text", target=desc("textbox", "Member ID")
        )
        assert verdict.allowed

    def test_bbox_targeting_is_off_by_default(self, policy):
        """Coordinates cannot survive a tenant re-theming the same product."""
        assert policy.allow_bbox_fallback is False


class TestRedaction:
    def test_masks_recognisable_secrets(self, policy):
        text = "ssn 123-45-6789 token: hunter2 key sk-ant-abcdefghijklmnopqrstuv"
        scrubbed = policy.redactor.scrub(text)
        assert "123-45-6789" not in scrubbed
        assert "sk-ant-abcdefghijklmnopqrstuv" not in scrubbed
        assert MASK in scrubbed

    def test_masks_registered_values_wherever_they_appear(self):
        """A page echoing a sensitive value back must not put it in a log."""
        redactor = Redactor()
        redactor.register_values(["A1B2C3D4"])
        assert "A1B2C3D4" not in redactor.scrub("error: account A1B2C3D4 is closed")

    def test_ignores_values_too_short_to_be_distinctive(self):
        redactor = Redactor()
        redactor.register_values(["12"])
        assert redactor.scrub("there are 12 accounts") == "there are 12 accounts"

    def test_masks_values_under_sensitive_keys(self, policy):
        scrubbed = policy.redactor.scrub_obj({"password": "hunter2", "member": "10042"})
        assert scrubbed["password"] == MASK
        assert scrubbed["member"] == "10042"

    def test_scrubs_nested_structures(self, policy):
        payload = {"steps": [{"note": "ssn 123-45-6789"}]}
        assert "123-45-6789" not in str(policy.redactor.scrub_obj(payload))
