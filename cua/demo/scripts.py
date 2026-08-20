"""Scripted discovery runs for the offline demo path.

These are the tool calls a model produces for the reference goals, captured so
the loop can be exercised without a key or a network. They are used by the test
suite and by ``--provider scripted``.

They are **not** a substitute for a discovery run. A scripted run proves that the
loop, the policy gate, the recorder and the artifact emission work; it proves
nothing about whether an agent can work the application out. The committed
evidence contains a genuine model-driven run for that.

Refs are resolved at runtime rather than hard-coded, because a ref is only
meaningful within the observation that produced it -- the same reason the
capability schema does not record selectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cua.llm.base import ToolCall
from cua.surfaces.base import Element, Observation


@dataclass
class ScriptedAction:
    """One step, with its target described the way a human would describe it."""

    tool: str
    why: str
    match: Callable[[Element], bool] | None = None
    extra: dict[str, str] | None = None


def _textbox(label: str) -> Callable[[Element], bool]:
    return lambda e: e.role == "textbox" and e.label_hint == label


def _button(name: str) -> Callable[[Element], bool]:
    return lambda e: e.role == "button" and e.name == name


def _link(name: str) -> Callable[[Element], bool]:
    return (
        lambda e: e.role == "link"
        and e.name == name
        and (not e.frame_path or e.frame_path[-1] != "navframe")
    )


def _cell_labelled(label: str) -> Callable[[Element], bool]:
    return lambda e: e.role == "cell" and e.label_hint == label


READ_SAVINGS_BALANCE: list[ScriptedAction] = [
    ScriptedAction(
        tool="type_text",
        why="The operator id is required before the servicing console will open.",
        match=_textbox("Operator ID"),
        extra={"text": "svc01"},
    ),
    ScriptedAction(
        tool="click",
        why="Sign in to reach the member search screen.",
        match=_button("Sign In"),
    ),
    ScriptedAction(
        tool="type_text",
        why="Enter the member number the caller supplied so the lookup targets them.",
        match=_textbox("Member Number"),
        extra={"text": "{{member_id}}"},
    ),
    ScriptedAction(
        tool="click",
        why="Run the search to reach the member's detail screen.",
        match=_button("Search"),
    ),
    ScriptedAction(
        tool="extract",
        why="The balance summary panel carries the current savings balance the caller asked for.",
        match=_cell_labelled("Current Savings Balance"),
        extra={"output": "savings_balance"},
    ),
    ScriptedAction(
        tool="finish",
        why="The savings balance has been read from the member detail screen.",
        extra={"summary": "Read the savings balance from the Balance Summary panel."},
    ),
]


OPEN_SUBACCOUNT: list[ScriptedAction] = [
    ScriptedAction(
        tool="type_text",
        why="Sign in before servicing functions become available.",
        match=_textbox("Operator ID"),
        extra={"text": "svc01"},
    ),
    ScriptedAction(tool="click", why="Sign in.", match=_button("Sign In")),
    ScriptedAction(
        tool="type_text",
        why="Look up the member the sub-account is being opened for.",
        match=_textbox("Member Number"),
        extra={"text": "{{member_id}}"},
    ),
    ScriptedAction(tool="click", why="Run the search.", match=_button("Search")),
    ScriptedAction(
        tool="click",
        why="Open the sub-account form from the member detail screen.",
        match=_link("Open Sub-Account"),
    ),
    ScriptedAction(
        tool="select_option",
        why="Select the account type the caller asked for.",
        match=lambda e: e.role == "combobox",
        extra={"option": "{{account_type}}"},
    ),
    ScriptedAction(
        tool="type_text",
        why="Name the account so it is identifiable on the member's profile.",
        match=_textbox("Account Nickname"),
        extra={"text": "{{nickname}}"},
    ),
    ScriptedAction(
        tool="type_text",
        why="Fund the account with the opening deposit.",
        match=_textbox("Initial Deposit"),
        extra={"text": "{{initial_deposit}}"},
    ),
    ScriptedAction(
        tool="click",
        why="Submit the form to open the sub-account and reach the confirmation screen.",
        match=_button("Open Sub-Account"),
    ),
    ScriptedAction(
        tool="extract",
        why="The confirmation screen carries the reference number the caller needs.",
        match=_cell_labelled("Reference Number"),
        extra={"output": "reference_number"},
    ),
    ScriptedAction(
        tool="finish",
        why="The sub-account was opened and the confirmation reference captured.",
        extra={"summary": "Opened the sub-account and read the confirmation reference."},
    ),
]


SCRIPTS: dict[str, list[ScriptedAction]] = {
    "read_savings_balance": READ_SAVINGS_BALANCE,
    "open_subaccount": OPEN_SUBACCOUNT,
}


class ScriptRunner:
    """Resolves a script's element matchers against the live observation.

    The orchestrator asks the provider for a decision given an observation; this
    turns "the textbox labelled Member Number" into whatever ref that control has
    right now.
    """

    def __init__(self, actions: list[ScriptedAction], inputs: dict[str, str]) -> None:
        self._actions = list(actions)
        self._inputs = dict(inputs)
        self._index = 0

    def _fill(self, text: str) -> str:
        for key, value in self._inputs.items():
            text = text.replace(f"{{{{{key}}}}}", str(value))
        return text

    def next_call(self, observation: Observation) -> ToolCall:
        if self._index >= len(self._actions):
            raise RuntimeError("script exhausted")
        action = self._actions[self._index]
        self._index += 1

        arguments: dict[str, str] = {"why": action.why}
        for key, value in (action.extra or {}).items():
            arguments[key] = self._fill(value)

        if action.match is not None:
            candidates = [e for e in observation.actionable() if action.match(e)]
            if not candidates:
                raise RuntimeError(
                    f"scripted step {self._index} ({action.tool}) found no matching "
                    f"control on {observation.url}"
                )
            arguments["ref"] = candidates[0].ref

        return ToolCall(name=action.tool, arguments=arguments, reasoning=action.why)
