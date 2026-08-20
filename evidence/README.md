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
| `discovery-20260820-100251-fbf6b6` | discovery | **succeeded** | recorded `cu.member.read_savings_balance` in 5 steps |
| `discovery-20260820-100254-118371` | discovery | **succeeded** | recorded `cu.member.open_subaccount` in 10 steps |
| `replay-20260820-100258-0b8caa` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` |
| `replay-20260820-100301-e36981` | replay | **success** | outputs `{'savings_balance': '$22,981.73'}` |
| `replay-20260820-100305-a62613` | replay | **business_outcome** | `MEMBER_NOT_FOUND` |
| `replay-20260820-100309-e8552c` | replay | **business_outcome** | `PERMISSION_DENIED` |
| `replay-20260820-100312-76e93f` | replay | **failure** | `checkpoint_violated` at step `s4` |
| `replay-20260820-100316-23cbeb` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` -- recovery `dismiss_system_notice` applied |
| `replay-20260820-100320-7fd4e7` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` |
| `replay-20260820-100331-f2605a` | replay | **failure** | `recovery_exhausted` at step `s4` |
| `replay-20260820-100334-02b66a` | replay | **failure** | `element_not_found` at step `s5` -- 1 degraded step(s), a drift signal |
| `replay-20260820-100338-e0b3e8` | replay | **success** | outputs `{'savings_balance': '$4,102.55'}` -- tenant `cascade` |
| `replay-20260820-100342-36bc64` | replay | **failure** | `human_aborted` at step `s9` |
| `replay-20260820-100346-f4efe9` | replay | **success** | outputs `{'reference_number': 'SA-938656'}` |
| `replay-20260820-100352-b88680` | replay | **business_outcome** | `VALIDATION_REJECTED` |
| `replay-20260820-100357-ac45be` | replay | **failure** | `input_invalid` |

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
