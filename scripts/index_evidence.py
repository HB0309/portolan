"""Write evidence/README.md from the runs actually on disk.

Run ids are timestamps, which tell a reader nothing about what each run
demonstrates. This reads every result.json and produces an index, so the table
cannot drift away from what the directory actually contains.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"

HEADER = """# Evidence

Every run writes a directory here containing `run.jsonl` (one structured record
per event), `result.json` (the typed result the caller received), per-step
screenshots, and -- on failure -- a DOM snapshot. Discovery runs also write
`capability.json` and `transcript.jsonl`.

`run.jsonl` and `transcript.jsonl` are kept apart on purpose, mirroring the
separation the artifact itself embodies: the transcript is what the model said,
the run log is what the system did. Confusing the two is how a "recording" ends
up being a chat log that cannot be replayed.

| Run | Kind | Status | What it shows |
|---|---|---|---|
"""

FOOTER = """
## Reading a failure

`result.json` for a failed run names the step, its recorded intent, what was
expected and what was actually observed, so a failure can be diagnosed without
re-running anything.

The `steps` array records which rung of the resolution ladder each target
matched on. That is the drift signal: compare the two tenant-beta runs above --
without an overlay a step degrades to rung 5 one step *before* the run breaks
outright, which is the point at which it would have been worth reviewing.
"""

SCRIPTED_NOTE_WITH_REAL_RUNS = """
## Note on the scripted discovery runs here

> The runs not marked **real model** above were produced by the **scripted
> provider**, which drives the identical loop, policy gate, and recorder
> through a fixed script -- useful for testing the plumbing without spending
> a model call, but not what produced the real-model entries.
"""

SCRIPTED_NOTE_NO_REAL_RUNS = """
## Note on the discovery runs here

> These were produced by the **scripted provider**, which replays a fixed flow
> through the identical loop, policy gate and recorder. It demonstrates the
> plumbing, not the agent's judgment. A genuine model-driven discovery run is
> still outstanding.
"""


def describe(payload: dict, name: str) -> tuple[str, str, str]:
    if name.startswith("discovery"):
        status = "succeeded" if payload.get("succeeded") else "failed"
        real_model = payload.get("provider") not in (None, "scripted")
        prefix = f"**real model** (`{payload.get('provider')}`/`{payload.get('model')}`), no script: " if real_model else ""
        if payload.get("succeeded"):
            detail = (
                f"{prefix}recorded `{payload.get('capability_id')}` in "
                f"{payload.get('steps_taken')} steps"
            )
        else:
            failure = payload.get("failure") or {}
            detail = f"{prefix}stopped: {failure.get('message', '')[:90]}"
        return "discovery", status, detail

    status = payload["status"]
    if status == "success":
        detail = f"outputs `{payload.get('outputs')}`"
    elif status == "business_outcome":
        detail = f"`{payload['outcome']['code']}`"
    else:
        failure = payload["failure"]
        detail = f"`{failure['category']}`"
        if failure.get("step_id"):
            detail += f" at step `{failure['step_id']}`"

    degraded = [s for s in payload.get("steps", []) if s.get("status") == "degraded"]
    if degraded:
        detail += f" -- {len(degraded)} degraded step(s), a drift signal"
    for recovery in payload.get("recoveries") or []:
        detail += f" -- recovery `{recovery['policy_id']}`"
        detail += " applied" if recovery["succeeded"] else " exhausted"
    if payload.get("tenant_id"):
        detail += f" -- tenant `{payload['tenant_id']}`"
    return "replay", status, detail


def main() -> None:
    rows = []
    scripted = False
    real = False
    for directory in sorted(EVIDENCE.iterdir()):
        result = directory / "result.json"
        if not result.is_file():
            continue
        payload = json.loads(result.read_text(encoding="utf-8"))
        kind, status, detail = describe(payload, directory.name)
        if kind == "discovery":
            if payload.get("provider") == "scripted":
                scripted = True
            else:
                real = True
        rows.append(f"| `{directory.name}` | {kind} | **{status}** | {detail} |")

    body = HEADER + "\n".join(rows) + "\n"
    if scripted and real:
        body += SCRIPTED_NOTE_WITH_REAL_RUNS
    elif scripted:
        body += SCRIPTED_NOTE_NO_REAL_RUNS
    body += FOOTER

    (EVIDENCE / "README.md").write_text(body, encoding="utf-8")
    print(f"indexed {len(rows)} runs")


if __name__ == "__main__":
    main()
