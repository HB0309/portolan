# CONVENTIONS.md — working notes for this repo

Engineering conventions and hard rules for this codebase. Read this before
making changes.

## What this is

Portolan builds the integration layer that
gives an AI agent hands inside legacy applications that expose no API: an LLM
works out how to complete a task in a real UI, the successful run is recorded as
a typed reusable capability, and that capability replays deterministically
afterward with no model in the decision loop.

The thesis, which every design decision is measured against:

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the AI agent invokes it in production.

## Hard rules

1. **No secrets in the repo.** Keys live in `.env`, which is gitignored. Check
   `git status` and grep the diff before every commit.
2. **Replay never calls a model.** This is asserted in code
   (`ReplayResult.provider_calls` must be 0), not left as a convention. If you
   are tempted to add an LLM call to the replay path, you have misunderstood the
   project.
3. **Never guess on an ambiguous element match.** More than one candidate at the
   winning resolution rung is a failure or an escalation, never a "pick the
   first one." Clicking the wrong "Transfer" in a bank is unrecoverable.
4. **Business outcomes are not failures.** "No such member" is a legitimate
   answer the caller needs. Keep the three result states distinct.
5. **Artifacts never store caller data as literals.** Values that came from a
   declared input are recorded as `{{param.x}}` references. There is a test
   guarding this; do not weaken it.

## Layout

```
cua/
  schema/      capability.py, results.py, overlay.py   -- the typed contracts
  policy/      engine.py, redaction.py, policy.yaml    -- the runtime guardrails
  surfaces/    base.py (ABC), web.py, desktop_stub.py  -- perceive and act
  perception/  a11y.py, normalize.py                   -- tree -> Observation
  targeting/   resolver.py                             -- the resolution ladder
  discovery/   orchestrator.py, tools.py, prompts.py, recorder.py
  replay/      engine.py, checkpoints.py, outcomes.py, recovery.py
  session/     manager.py, control.py                  -- control ownership FSM
  escalation/  broker.py, console/                     -- human handoff
  llm/         base.py, anthropic_provider.py, openai_provider.py, scripted.py
  evidence/    recorder.py
  catalog/     tools.py                                -- agent-facing catalog
mockapp/       the deliberately hostile target application
scripts/       build_evidence.py, index_evidence.py
evidence/      committed demo runs, indexed by evidence/README.md
tests/
```

Evidence is generated, never hand-written:

```bash
venv/Scripts/python -m mockapp                   # terminal 1
venv/Scripts/python scripts/build_evidence.py    # terminal 2
```

Add `--provider anthropic` to record the discovery runs with a real model.

## Design decisions

Recorded with rationale in `docs/DECISIONS.md`. Do not silently reverse one; if
a decision turns out wrong, amend that file and say why. Every decision must be
defensible in a one-sentence answer, because the follow-up round is a
conversation about the design.

## Current state

Design decisions and their trade-offs are logged in `docs/DECISIONS.md`. Add an
entry whenever you make a call someone could reasonably have made differently.

## Running things

```bash
# Windows paths; the venv is not committed.
./venv/Scripts/python.exe -m pytest tests/ -q
./venv/Scripts/python.exe -m mockapp          # target app on :8080
./venv/Scripts/python.exe -m cua --help
```

There are no API keys checked in. Discovery against a real model needs
`ANTHROPIC_API_KEY` in `.env`. The `scripted` provider drives the identical loop
offline for tests and for anyone who has no key.

## Style

- Type hints throughout; `from __future__ import annotations` at the top.
- Docstrings explain *why*, not *what* the code obviously does. The reader is
  reading for judgment, so the reasoning belongs next to the decision.
- Comments earn their place. A comment restating the line below it is noise.
- Keep the schema honest: `extra="forbid"` everywhere, validators that actually
  check referential integrity.
