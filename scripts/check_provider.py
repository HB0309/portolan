"""Does this model actually drive the loop? Ask it, don't read the docs.

    python scripts/check_provider.py --provider anthropic
    python scripts/check_provider.py --provider openai --model meta/llama-3.3-70b-instruct

Model catalogues advertise "function calling" with a single checkbox, and the
checkbox does not distinguish a model that reliably emits one well-formed call
per turn from one that emits prose, two calls, or a call with the arguments
missing. This loop breaks on all three.

So this sends the real system prompt, a real rendered observation, and the real
tool definitions, and checks the four things the orchestrator depends on:

    1. a tool call comes back at all
    2. it names a tool that exists
    3. the required arguments are present
    4. the ref it chose is one that was actually on the screen

A provider that passes is worth spending a discovery run on. One that fails here
will fail there too, only slower and with a browser open.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from cua.config import DEFAULT_PROVIDER  # noqa: E402
from cua.discovery.prompts import SYSTEM_PROMPT, render_goal  # noqa: E402
from cua.discovery.tools import tool_definitions  # noqa: E402
from cua.llm import LLMError, Message, build_provider  # noqa: E402

load_dotenv(ROOT / ".env")

#: A real screen from the target application, captured after sign-in. The trap
#: is deliberate and is the whole point of the check: the toolbar carries a
#: "Find Member" link and the form carries a "Search" button, so a model that
#: pattern-matches on the word rather than reading the roles picks the wrong one.
OBSERVATION = """\
URL: http://127.0.0.1:8080/
Title: CoreServ Member Servicing

Controls on screen:
  [1:e1] link "Member Search" frame:navframe
  [1:e2] link "Member Detail" frame:navframe
  [1:e3] link "Sign Out" frame:navframe
  [2:e2] link "Open" in:'Member Search' frame:contentframe
  [2:e3] link "Save" in:'Member Search' frame:contentframe
  [2:e4] link "Find Member" in:'Member Search' frame:contentframe
  [2:e5] link "Refresh" in:'Member Search' frame:contentframe
  [2:e8] cell "Member Number" in:'Search By' frame:contentframe
  [2:e9] textbox label:'Member Number' in:'Search By' frame:contentframe
  [2:e10] button "Search" in:'Search By' frame:contentframe
  [2:e11] button "Clear" in:'Search By' frame:contentframe
  [2:e13] textbox label:'Last Name' in:'Search By' frame:contentframe
  [2:e15] textbox label:'Tax ID' in:'Search By' frame:contentframe
"""

REFS_ON_SCREEN = {
    line.split("]")[0].strip().lstrip("[")
    for line in OBSERVATION.splitlines()
    if line.strip().startswith("[")
}

#: What a competent model should do first on this screen.
SENSIBLE_FIRST_MOVES = {"type_text", "click"}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider", default=DEFAULT_PROVIDER, help="anthropic | openai (default from CUA_PROVIDER)"
    )
    parser.add_argument("--model", default="", help="Override the provider's default model.")
    args = parser.parse_args()

    try:
        provider = build_provider(args.provider, model=args.model or None)
    except LLMError as exc:
        print(f"\n  cannot build provider: {exc}\n")
        return 1

    base = getattr(getattr(provider, "_client", None), "base_url", "")
    print(f"\n  provider : {provider.name}")
    print(f"  model    : {provider.model}")
    if base:
        print(f"  endpoint : {base}")

    prompt = "\n".join(
        [
            render_goal(
                "look up member 10042 and read their current savings balance",
                {"member_id": "10042"},
                ["savings_balance"],
            ),
            OBSERVATION,
        ]
    )

    print("  sending the real prompt, observation and tool definitions...\n")
    try:
        response = await provider.decide(
            SYSTEM_PROMPT, [Message(role="user", content=prompt)], tool_definitions()
        )
    except LLMError as exc:
        print(f"  FAIL  the call itself failed: {exc}\n")
        return 1
    except Exception as exc:  # a compatible endpoint that is not, in fact, compatible
        print(f"  FAIL  {type(exc).__name__}: {exc}\n")
        return 1

    checks: list[tuple[bool, str]] = []
    call = response.tool_call

    checks.append((call is not None, "returned a tool call rather than prose"))
    if call is None:
        _report(checks, response)
        return 1

    known = {tool["name"] for tool in tool_definitions()}
    checks.append((call.name in known, f"named a real tool ({call.name!r})"))

    required = {
        "type_text": {"ref", "text", "why"},
        "click": {"ref", "why"},
        "navigate": {"url", "why"},
        "extract": {"ref", "output", "why"},
        "select_option": {"ref", "option", "why"},
        "press_key": {"ref", "key", "why"},
    }.get(call.name, set())
    missing = required - set(call.arguments)
    checks.append((not missing, f"supplied every required argument{f' (missing {sorted(missing)})' if missing else ''}"))

    ref = str(call.arguments.get("ref", ""))
    if ref:
        checks.append((ref in REFS_ON_SCREEN, f"chose a ref that exists on screen ({ref!r})"))

    checks.append(
        (bool(call.reasoning.strip()), "explained itself, which becomes the step's intent")
    )

    ok = _report(checks, response)

    # Judgment, reported but not scored -- there is more than one defensible
    # opening move, and a check that punishes a reasonable one is a bad check.
    if call.name in SENSIBLE_FIRST_MOVES and ref:
        target = next(
            (line.strip() for line in OBSERVATION.splitlines() if line.strip().startswith(f"[{ref}]")),
            "",
        )
        print(f"  it chose : {target or ref}")
        if ref == "2:e9":
            print("             (the member-number field -- the expected opening move)")
        elif ref == "2:e4":
            print("             (the TOOLBAR link, not the form. It fell for the trap.)")
        print()

    return 0 if ok else 1


def _report(checks: list[tuple[bool, str]], response) -> bool:
    for passed, label in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    if response.input_tokens or response.output_tokens:
        print(f"\n  tokens   : {response.input_tokens} in / {response.output_tokens} out")
    if response.text:
        print(f"  also said: {response.text[:160]}")
    ok = all(passed for passed, _ in checks)
    print(f"\n  {'usable for a discovery run' if ok else 'NOT usable -- the loop needs all of the above'}\n")
    return ok


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
