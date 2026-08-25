# Evidence

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
| `discovery-20260823-050746-b2ef0a` | discovery | **succeeded** | recorded `cu.member.read_savings_balance` in 5 steps |
| `discovery-20260823-050749-7ef011` | discovery | **succeeded** | recorded `cu.member.open_subaccount` in 10 steps |
| `discovery-20260824-080430-631888` | discovery | **failed** | stopped: openai call failed: Error code: 429 - {'error': {'message': 'Rate limit reached for model  |
| `discovery-20260824-081238-3217a7` | discovery | **succeeded** | recorded `cu.member.read_savings_balance` in 6 steps |
| `replay-20260823-050753-672eb3` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` |
| `replay-20260823-050757-16681e` | replay | **success** | outputs `{'savings_balance': '$22,981.73'}` |
| `replay-20260823-050801-6c955e` | replay | **business_outcome** | `MEMBER_NOT_FOUND` |
| `replay-20260823-050805-d83edd` | replay | **business_outcome** | `PERMISSION_DENIED` |
| `replay-20260823-050808-6e3857` | replay | **failure** | `checkpoint_violated` at step `s4` |
| `replay-20260823-050812-8ad2e2` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` -- recovery `dismiss_system_notice` applied |
| `replay-20260823-050816-99f2a2` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` |
| `replay-20260823-050828-a8f1ac` | replay | **failure** | `recovery_exhausted` at step `s4` |
| `replay-20260823-050831-703283` | replay | **failure** | `element_ambiguous` at step `s4` |
| `replay-20260823-050834-1263e8` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` -- tenant `cascade` |
| `replay-20260823-050838-8af047` | replay | **failure** | `human_aborted` at step `s9` |
| `replay-20260823-050843-f1097a` | replay | **success** | outputs `{'reference_number': 'SA-994123'}` |
| `replay-20260823-050849-437813` | replay | **business_outcome** | `VALIDATION_REJECTED` |
| `replay-20260823-050854-155106` | replay | **failure** | `input_invalid` |
| `replay-20260824-081410-75d157` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` |
| `replay-20260824-081415-8e4038` | replay | **success** | outputs `{'savings_balance': '$22,981.73'}` |

## Note on the discovery runs here

> These were produced by the **scripted provider**, which replays a fixed flow
> through the identical loop, policy gate and recorder. It demonstrates the
> plumbing, not the agent's judgment. A genuine model-driven discovery run is
> still outstanding.

## Reading a failure

`result.json` for a failed run names the step, its recorded intent, what was
expected and what was actually observed, so a failure can be diagnosed without
re-running anything.

The `steps` array records which rung of the resolution ladder each target
matched on. That is the drift signal: compare the two tenant-beta runs above --
without an overlay a step degrades to rung 5 one step *before* the run breaks
outright, which is the point at which it would have been worth reviewing.
