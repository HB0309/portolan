"""Ask in plain language; a model picks the capability and calls it.

    python scripts/ask.py "what is the savings balance for member 10077?"

This is the seam the whole project exists to serve, made concrete. The
conversational agent decides *what* to do; the capability is *how* it gets done
inside an application that has no API.

What happens:

    1. Load the catalog and hand it to a model as tool definitions.
    2. The model reads the request and chooses a capability + arguments.
       It never sees the application, the steps, or the browser -- only a
       function signature and a description.
    3. Deterministic replay executes it. No model is involved in that part.
    4. The typed result goes back to the model to be phrased as an answer.

The point of step 2 is that the model is choosing from a *contract*. It is told
which business outcomes are legitimate answers, so it can say "that member does
not exist" rather than reporting a failure -- which is the whole reason outcomes
are declared in the artifact instead of being inferred from an error string.

The model that answers here need not be the model that recorded the capability.
Running this with --provider openai against a Claude-recorded capability is the
demonstration that the artifact is decoupled from whatever discovered it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from cua.catalog import CapabilityCatalog  # noqa: E402
from cua.config import DEFAULT_PROVIDER  # noqa: E402
from cua.discovery import new_run_id  # noqa: E402
from cua.evidence import EvidenceRecorder  # noqa: E402
from cua.llm import LLMError, Message, build_provider  # noqa: E402
from cua.policy import PolicyEngine  # noqa: E402
from cua.replay import ReplayEngine  # noqa: E402
from cua.schema.results import ReplayStatus  # noqa: E402
from cua.surfaces.web import WebSurface  # noqa: E402

load_dotenv(ROOT / ".env")

SYSTEM = """\
You are the servicing assistant for a credit union. You have tools that operate
the back-office system on a member's behalf.

Choose exactly one tool and supply its arguments from the user's request. Do not
guess a member number that was not given to you -- if the request does not
contain the arguments a tool needs, say so instead of calling it.

Some tools can return a business outcome instead of a result. Those are
legitimate answers, not errors: report them plainly to the user rather than
saying something went wrong.
"""


async def execute(capability, arguments: dict, headless: bool) -> dict:
    """Run the chosen capability. This is the part with no model in it."""
    policy = PolicyEngine.load()
    run_id = new_run_id("invoke")
    evidence = EvidenceRecorder(run_id, root=ROOT / "evidence", redactor=policy.redactor)
    surface = await WebSurface.launch(headless=headless)
    try:
        engine = ReplayEngine(surface=surface, policy=policy, evidence=evidence)
        result = await engine.run(capability, arguments)
    finally:
        await surface.close()

    if result.status is ReplayStatus.SUCCESS:
        payload = {"status": "success", "outputs": result.outputs}
    elif result.status is ReplayStatus.BUSINESS_OUTCOME:
        payload = {
            "status": "business_outcome",
            "outcome": result.outcome.code,
            "meaning": result.outcome.description,
        }
    else:
        payload = {
            "status": "failure",
            "category": result.failure.category.value,
            "detail": result.failure.message,
        }
    payload["_run"] = run_id
    return payload


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", help="What you want, in plain language.")
    parser.add_argument(
        "--provider", default=DEFAULT_PROVIDER, help="anthropic | openai (default from CUA_PROVIDER)"
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    catalog = CapabilityCatalog.load(ROOT / "capabilities")
    if not catalog.capabilities:
        print("  no capabilities recorded yet - run `cua discover` first")
        return 1

    try:
        provider = build_provider(args.provider, model=args.model or None)
    except LLMError as exc:
        print(f"  {exc}")
        return 1

    tools = catalog.tool_definitions()
    print(f'\n  you: "{args.request}"')
    print(f"  {len(tools)} capability(ies) offered to {provider.name}/{provider.model}\n")

    # --- the model chooses -------------------------------------------------
    choice = await provider.decide(
        SYSTEM, [Message(role="user", content=args.request)], tools
    )
    if choice.tool_call is None:
        print(f"  the model declined to call anything: {choice.text}")
        return 1

    capability = catalog.resolve_tool_name(choice.tool_call.name)
    if capability is None:
        print(f"  the model asked for an unknown tool: {choice.tool_call.name}")
        return 1

    arguments = {k: v for k, v in choice.tool_call.arguments.items() if k != "why"}
    print(f"  chose : {capability.capability_id}  (v{capability.version})")
    print(f"  args  : {json.dumps(arguments)}")

    # --- deterministic execution ------------------------------------------
    print("  running the capability (no model in this part)...\n")
    result = await execute(capability, arguments, args.headless)
    print(f"  result: {json.dumps({k: v for k, v in result.items() if k != '_run'})}")

    # --- the model phrases the answer -------------------------------------
    followup = (
        f"{args.request}\n\n"
        f"You called {choice.tool_call.name} and it returned:\n"
        f"{json.dumps({k: v for k, v in result.items() if k != '_run'})}\n\n"
        f"Answer the user in one or two sentences. If this is a business outcome, "
        f"state it plainly as the answer -- it is not a malfunction."
    )
    spoken = await provider.decide(
        SYSTEM.replace("Choose exactly one tool", "Answer the user"),
        [Message(role="user", content=followup)],
        # A single no-op tool keeps providers that require a tool call happy
        # while the model is only being asked to speak.
        [
            {
                "name": "reply",
                "description": "Give the user the answer.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ],
    )
    answer = ""
    if spoken.tool_call is not None:
        answer = str(spoken.tool_call.arguments.get("text", ""))
    answer = answer or spoken.text or "(no answer produced)"

    print(f'\n  agent: "{answer.strip()}"')
    print(f"\n  evidence: evidence/{result['_run']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
