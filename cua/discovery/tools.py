"""The agent's action vocabulary.

These are the only things the discovery agent can do. The set is deliberately
small and maps one-to-one onto ``ActionRequest``, so there is no interpretation
layer between what the model asked for and what the executor performs -- the
model either names a legal action on a real element, or it has said nothing the
system will act on.

Every tool takes a ``why``. It is not decoration: it becomes the recorded step's
``intent``, which is what lets a human read a capability and understand what it
does without replaying it, and what gives a failure report something meaningful
to name. Requiring it per call also costs nothing and reliably produces better
rationales than asking for them afterwards.
"""

from __future__ import annotations

from typing import Any

_WHY = {
    "type": "string",
    "description": (
        "One sentence: why this action moves you toward the goal. This is "
        "recorded permanently as the step's intent and read by human reviewers."
    ),
}

_REF = {
    "type": "string",
    "description": "The ref of the target element, exactly as shown in the observation.",
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "navigate",
            "description": (
                "Load a URL. Only URLs inside the allowlist are permitted; anything "
                "else is refused by the runtime."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "why": _WHY},
                "required": ["url", "why"],
            },
        },
        {
            "name": "click",
            "description": "Click a control: a button, a link, or a checkbox.",
            "input_schema": {
                "type": "object",
                "properties": {"ref": _REF, "why": _WHY},
                "required": ["ref", "why"],
            },
        },
        {
            "name": "type_text",
            "description": (
                "Type into a text field, replacing what is there. If the value is one "
                "of the declared inputs, use that input's exact value -- it will be "
                "recorded as a parameter rather than a constant."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": _REF,
                    "text": {"type": "string"},
                    "why": _WHY,
                },
                "required": ["ref", "text", "why"],
            },
        },
        {
            "name": "select_option",
            "description": "Choose an option in a dropdown by its visible label.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": _REF,
                    "option": {"type": "string"},
                    "why": _WHY,
                },
                "required": ["ref", "option", "why"],
            },
        },
        {
            "name": "press_key",
            "description": "Press a key while an element is focused, e.g. Enter or Tab.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": _REF,
                    "key": {"type": "string"},
                    "why": _WHY,
                },
                "required": ["ref", "key", "why"],
            },
        },
        {
            "name": "extract",
            "description": (
                "Read a value off the screen and bind it to one of the goal's declared "
                "outputs. Target the element that holds the value itself, not its label."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": _REF,
                    "output": {
                        "type": "string",
                        "description": "Which declared output this value fills.",
                    },
                    "why": _WHY,
                },
                "required": ["ref", "output", "why"],
            },
        },
        {
            "name": "finish",
            "description": (
                "Declare the goal met. Only call this once every declared output has "
                "been extracted and you can see the expected end state on screen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was accomplished and how you know.",
                    },
                    "why": _WHY,
                },
                "required": ["summary", "why"],
            },
        },
        {
            "name": "give_up",
            "description": (
                "Stop and hand off to a human. Use this when you are stuck, when the "
                "application is in a state you cannot safely act on, or when going "
                "further would need a decision that is not yours to make. Giving up "
                "clearly is a better outcome than guessing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "why": _WHY,
                },
                "required": ["reason", "why"],
            },
        },
    ]


#: Tools that end the loop rather than acting on the surface.
TERMINAL_TOOLS = {"finish", "give_up"}

#: Maps a tool name onto the surface action it performs.
ACTION_TOOLS = {
    "navigate": "navigate",
    "click": "click",
    "type_text": "type_text",
    "select_option": "select_option",
    "press_key": "press_key",
    "extract": "extract",
}
