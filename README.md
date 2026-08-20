# Computer-use automation for applications with no API

An LLM works out how to complete a task inside a legacy UI. The successful run
is recorded as a typed, reusable capability. That capability then replays
deterministically, with no model in the decision loop, and returns typed results
to whatever called it.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the AI agent invokes it in production.

The design write-up is in [REPORT.md](REPORT.md). Decisions and their trade-offs
are logged in [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Setup

Requires Python 3.11+.

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
venv/Scripts/python -m playwright install chromium
```

Copy `.env.example` to `.env` and fill in a key:

```
ANTHROPIC_API_KEY=sk-ant-...      # primary discovery driver
OPENAI_API_KEY=sk-...             # optional second provider
```

### Running without a model

Discovery needs an API key. Everything else does not, and there is a scripted
provider that drives the identical loop offline:

```bash
venv/Scripts/python -m cua discover --provider scripted --script read_savings_balance ...
```

The scripted provider exercises the agent loop, the policy gate, the recorder
and the artifact emission. It does **not** demonstrate that an agent can work
the application out — only a real model-driven run does that, and one is
committed under `evidence/`. Anywhere the scripted path can be selected, the CLI
says so.

---

## Demo path

Two terminals. The target application must be running first.

```bash
# terminal 1 -- the target application
venv/Scripts/python -m mockapp                       # serves legacy-cu on :8080
```

### 1. Discover a capability

```bash
# terminal 2
venv/Scripts/python -m cua discover \
  --goal "look up member 10042 and read their current savings balance" \
  --inputs '{"member_id":"10042"}' \
  --outputs "savings_balance" \
  --capability-id "cu.member.read_savings_balance" \
  --name "Read member savings balance" \
  --description "Look up a member by number and return their current savings balance."
```

Add `--provider scripted --script read_savings_balance` to run it without a key.
Add `--headless` to hide the browser.

Writes `capabilities/cu.member.read_savings_balance.json` and an evidence
directory.

### 2. Replay it

```bash
venv/Scripts/python -m cua replay \
  --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}'
```

```
  status : success
  success: {'savings_balance': '$4,102.55'}
  targeting : s1=rung1, s2=rung1, s3=rung1, s4=rung1, s5=rung1
```

A **different member** works from the same recording — that is the point of
parameterisation:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10077"}'          # -> success: {'savings_balance': '$22,981.73'}
```

### 3. Replay into an error state

A member who does not exist is a **business outcome**, not a failure:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"99999"}'
#   status : business_outcome
#   business outcome: MEMBER_NOT_FOUND -- No member exists with the supplied member number.
```

An application error is a **hard failure**, with enough detail to debug:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}' --entry "http://127.0.0.1:8080/?inject=server_error"
#   status : failure
#   failure (checkpoint_violated) at step s4
#   expected  : 'Member Detail' is present in contentframe
#   observed  : url=... :: Application Error / CORESERV-500
```

An unexpected interstitial is **recovered** and the run completes:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}' --entry "http://127.0.0.1:8080/?inject=interstitial"
#   status : success
#   recovery  : dismiss_system_notice on s4 (applied)
```

### 4. Human escalation on the live session

Record a capability that contains an irreversible step, then replay it attended:

```bash
venv/Scripts/python -m cua discover \
  --goal "open a savings sub-account for member 10042 and reach the confirmation screen" \
  --inputs '{"member_id":"10042","account_type":"Savings","nickname":"Vacation Fund","initial_deposit":"500"}' \
  --outputs "reference_number" --capability-id "cu.member.open_subaccount" \
  --provider scripted --script open_subaccount --attended

venv/Scripts/python -m cua replay \
  --capability capabilities/cu.member.open_subaccount.json \
  --inputs '{"member_id":"10043","account_type":"Savings","nickname":"Holiday","initial_deposit":"250"}' \
  --attended
```

The run stops at the submit button and opens an operator console on
<http://127.0.0.1:8081>. The console shows why it stopped and the live screen.
**Take control of the live session** hands you the same browser window the
automation was driving — do whatever is needed in it — then tell the run how to
continue. What you do is recorded into the evidence log.

Unattended, the same capability refuses rather than committing:

```
  failure (human_aborted) at step s9: irreversible action (button named
  'Open Sub-Account' matches the irreversible pattern 'open sub-account') on a
  capability that is not approved for unattended execution
```

Once reviewed — `approval: approved` and `approved_risky: true` on that step —
it proceeds unattended and returns `{'reference_number': 'SA-714516'}`.

### 5. Reuse across tenants

`?tenant=beta` serves the same product configured for a different institution:
different branding, and two controls renamed. Replaying the alpha recording
against it without an overlay degrades and then fails:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}' --entry "http://127.0.0.1:8080/?tenant=beta"
#   targeting : s1=rung1, s2=rung1, s3=rung1, s4=rung5
#   DEGRADED  : 1 step(s) resolved below their confidence floor - a drift signal
#   failure (element_not_found) at step s5
```

Step 4 dropping to rung 5 is the drift signal, and it appears one step *before*
the run breaks. With a small overlay instead of a re-recording:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}' \
  --tenant tenants/cascade.cu.member.read_savings_balance.json
#   status : success -- all steps back to rung1
```

### 6. The capability catalog

Capabilities are agent-callable by name:

```bash
venv/Scripts/python -m cua catalog              # human-readable
venv/Scripts/python -m cua catalog --as-tools   # provider tool definitions
venv/Scripts/python -m cua invoke cu.member.read_savings_balance --args '{"member_id":"10042"}'
```

---

## The target application

`mockapp/` is **legacy-cu**, a stand-in for a credit-union servicing console. It
is deliberately unpleasant to automate, because the real thing is: a frameset,
table layout, `<font>` tags, no test ids, and element ids regenerated on every
render so that any recorded selector works exactly once.

No real credentials and no real data. Any operator id signs in. Members are
`10042`, `10043`, `10077`, and `10099` (restricted).

It can inject the runtime conditions the replay engine has to survive, via
`?inject=`:

| Injection | Simulates | Handled as |
|---|---|---|
| `member_not_found` | record not found | business outcome |
| `validation_error` | rejected form input | business outcome |
| `permission_denied` | insufficient rights | business outcome |
| `interstitial` | unexpected dialog | recovered |
| `slow_load` | transient slowness | recovered |
| `session_expiry` | timeout mid-flow | recovered, else escalated |
| `server_error` | application 500 | hard failure |

`?tenant=alpha|beta` serves two configurations of the same product.

---

## Layout

```
cua/
  schema/      the capability, results, and tenant overlays
  policy/      allowlist, reversibility classification, redaction
  surfaces/    Surface ABC, Playwright web surface, desktop seam
  perception/  in-page extraction -> normalized element graph
  targeting/   the resolution ladder
  discovery/   agent loop, prompts, tools, capability recorder
  replay/      deterministic execution, predicates, error taxonomy
  session/     control-ownership state machine
  escalation/  intervention broker and operator console
  llm/         Anthropic, OpenAI, and scripted providers
  catalog.py   capabilities as agent tool definitions
mockapp/       the target application
evidence/      committed runs
tests/
```

## Tests

```bash
venv/Scripts/python -m pytest -q
```

The tests cover the load-bearing contracts rather than sweeping for coverage:
the artifact's integrity rules and its guarantee that caller data is never
recorded as a literal, every rung of the resolution ladder including its refusal
to guess when a match is ambiguous, and the control-ownership state machine.
