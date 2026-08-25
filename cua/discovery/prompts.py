"""System prompt and observation rendering.

The interesting decision here is what the agent is *not* sent. A twenty-five step
loop that re-sends a full element graph every turn grows quadratically, and the
cost is the smaller half of the problem: a context stuffed with fifteen stale
page snapshots measurably degrades the agent's choices, because the relevant
observation is always the current one and the older ones invite it to reason
about a screen that is no longer there.

So history is trimmed: the current observation in full, and everything before it
collapsed to one line per step recording what was tried and what happened. That
is all the agent needs -- what it has already done, and what is on the screen
now.
"""

from __future__ import annotations

from cua.surfaces.base import Element, Observation

SYSTEM_PROMPT = """\
You operate back-office business applications the way a human operator would, by
reading the screen and clicking and typing. You are working inside a bank or
credit union's servicing software. There is no API; the interface is the only way
in.

You will be given a goal, a set of declared inputs, and a set of declared outputs
to collect. Work toward the goal one action at a time. Before each action you get
a fresh observation of the screen.

How to read an observation:

- Each line is one control: a ref, a role, its name, and where it sits.
- `ref` is how you address a control. Use it exactly. Refs change every
  observation -- never reuse one from an earlier turn.
- `label:` is the text of the cell next to a control. These applications label
  their fields by position rather than markup, so for many inputs the label is
  the only name the field has.
- `in:` is the panel the control sits in. When several controls share a name,
  the panel is what tells them apart.
- `frame:` is which frame of the page it is in. This application uses framesets;
  the navigation frame and the content frame often contain similar links.

Rules:

- Act only on refs present in the current observation.
- Prefer the control inside the content frame over a similarly named one in the
  navigation frame, unless you specifically intend to navigate.
- When you type a value that was given to you as a declared input, type it
  exactly as supplied. It is recorded as a parameter, not as a constant, which is
  what makes the recording reusable and keeps regulated data out of it.
- To read a value, target the element containing the value itself, not the label
  beside it.
- Some actions cannot be undone -- opening an account, transferring money,
  submitting a form that commits a change. The runtime will stop you and ask a
  human before any of those. Do not try to work around it.
- If you are stuck, or the screen is in a state you do not understand, call
  give_up and say why. That routes to a human operator, which is a good outcome.
  Guessing is not.
- Call finish only when every declared output has been extracted and you can see
  the expected end state.

You must respond with exactly one tool call every turn.
"""


def render_element(element: Element) -> str:
    bits = [f"[{element.ref}]", element.role]
    if element.name:
        bits.append(f'"{element.name}"')
    if element.label_hint and element.label_hint != element.name:
        bits.append(f"label:{element.label_hint!r}")
    if element.value and element.role in {"textbox", "combobox"}:
        bits.append(f"value:{element.value!r}")
    if element.section:
        bits.append(f"in:{element.section!r}")
    if element.frame_path:
        bits.append(f"frame:{'/'.join(element.frame_path)}")
    if not element.enabled:
        bits.append("(disabled)")
    return " ".join(bits)


def render_observation(observation: Observation, *, limit: int = 120) -> str:
    lines = [
        f"URL: {observation.url}",
        f"Title: {observation.title}",
        "",
        "Controls on screen:",
    ]
    for element in observation.actionable()[:limit]:
        lines.append("  " + render_element(element))

    page_text = observation.text.strip()
    if page_text:
        # Enough page text to recognise a state -- an error banner, a "no results"
        # message -- without pasting the entire screen twice.
        excerpt = page_text[:1200]
        lines += ["", "Visible text:", excerpt]
    return "\n".join(lines)


def render_goal(goal: str, inputs: dict[str, str], outputs: list[str]) -> str:
    lines = [f"GOAL: {goal}", ""]
    if inputs:
        lines.append("Declared inputs (type these exactly where the flow calls for them):")
        for name, value in inputs.items():
            lines.append(f"  {name} = {value}")
        lines.append("")
    if outputs:
        lines.append("Declared outputs to collect before finishing:")
        for name in outputs:
            lines.append(f"  {name}")
        lines.append("")
    return "\n".join(lines)


def summarize_step(index: int, tool: str, arguments: dict, outcome: str) -> str:
    """One line standing in for a turn that has scrolled out of the context."""
    detail = ""
    if tool == "navigate":
        detail = str(arguments.get("url", ""))
    elif tool in {"click", "press_key"}:
        detail = str(arguments.get("target_description", arguments.get("ref", "")))
    elif tool == "type_text":
        # The value is deliberately not echoed: it may be caller data, and the
        # agent only needs to know the field was filled.
        detail = f"{arguments.get('target_description', arguments.get('ref', ''))} <- (value)"
    elif tool == "select_option":
        detail = f"{arguments.get('target_description', '')} = {arguments.get('option', '')}"
    elif tool == "extract":
        detail = f"-> {arguments.get('output', '')}"
    return f"  {index}. {tool} {detail} :: {outcome}"
