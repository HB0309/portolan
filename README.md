# Portolan

**Computer-use automation for applications with no API.**

An LLM works out how to complete a task inside a legacy UI. The successful run
is recorded as a typed, reusable capability. That capability then replays
deterministically, with no model in the decision loop, and returns typed results
to whatever called it.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the AI agent invokes it in production.

## Why it is called Portolan

A [portolan chart](https://en.wikipedia.org/wiki/Portolan_chart) was not drawn by
surveying a coastline. It was compiled from the bearings and distances of voyages
that had actually been sailed. One ship worked the route out the hard way; every
ship afterward sailed it from the chart.

That is the architecture. Discovery is the voyage, expensive and uncertain and
done once. The capability artifact is the chart. Replay is every voyage after,
which needs no navigator because the route is already written down.

## Why I built it

I kept running into the same idea from two directions: everyone wants to point an
LLM at a screen and have it click things, and nobody wants a model in the loop of
something that has to run correctly a thousand times a day. Those seemed
reconcilable if the model only had to be right once. I wanted to find out whether
that actually holds up when you build it, so I built it.

It turned out to be a much more interesting problem than I expected, mostly
because of everything that happens after the model succeeds: how you record a
step so it survives the page changing, what you do when a control is ambiguous,
and who is allowed to decide that an action is safe. Those questions are what the
design write-up is really about.

## The problem this solves

Plenty of systems that a business depends on have a user interface and no API.
The obvious move is to point an LLM at the screen, but running a model on every
execution is slow, costly, and non-deterministic, which is exactly what you
cannot accept for something that has to run correctly thousands of times.

So the model runs once. What it learns becomes an artifact, and the artifact is
what runs in production:

- **Steps record semantics, not selectors.** A step stores an element's
  accessible name and role, never a CSS selector or pixel coordinates, because
  those are precisely what break when a UI shifts.
- **Ambiguity is a failure, not a guess.** On replay each descriptor resolves
  through a seven-rung ladder and stops at the first rung yielding exactly one
  candidate. More than one match at the winning rung fails the step.
- **The policy gate sits between the executor and the surface, not in the
  prompt.** An unsafe action is refused by the system regardless of what the
  model decided, and irreversible steps escalate to a human.

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

Only have an OpenAI key? Set `CUA_PROVIDER=openai` in `.env` (also update
`OPENAI_MODEL` to a model with tool calling, e.g. `gpt-4o`) and every command
below works unchanged -- no need to pass `--provider` by hand each time. The
`--provider` flag on any command always overrides this default if you want to
mix providers in one session.

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
against it without an overlay stops at the first step that genuinely diverges:

```bash
venv/Scripts/python -m cua replay --capability capabilities/cu.member.read_savings_balance.json \
  --inputs '{"member_id":"10042"}' --entry "http://127.0.0.1:8080/?tenant=beta"
#   targeting : s1=rung1, s2=rung1, s3=rung1
#   failure (element_ambiguous) at step s4: 2 controls match button 'Search' in
#     frame:contentframe > heading:Search By at rung 5; refusing to guess
#   observed  : button 'Find Member' ...; button 'Clear' ...
```

This tenant renamed the search action, so the recorded name no longer identifies
one control — and by the time the ladder has loosened enough to match anything,
it matches *two*. It names both and stops rather than picking one. In a
back-office where the wrong button moves money, refusing costs one interruption
and guessing costs a transaction.

With a small overlay instead of a re-recording:

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

### 7. Asking in plain language

`scripts/ask.py` is the seam this project exists to serve, made concrete: a
conversational agent decides *what* to do, and a capability is *how* it happens
inside an application with no API.

```bash
venv/Scripts/python scripts/ask.py "what is the savings balance for member 10077?"
```

```
  you: "what is the savings balance for member 10077?"
  2 capability(ies) offered to anthropic/claude-sonnet-5

  chose : cu.member.read_savings_balance  (v1)
  args  : {"member_id": "10077"}
  running the capability (no model in this part)...

  result: {"status": "success", "outputs": {"savings_balance": "$22,981.73"}}

  agent: "Member 10077 has $22,981.73 in savings."
```

The model never sees the application, the recorded steps, or the browser — only
a function signature and a description. It is also told which business outcomes
are legitimate answers, so asking about a member who does not exist produces
*"that member does not exist"* rather than a reported failure.

Run it with `--provider openai` against a Claude-recorded capability to show
that the artifact is decoupled from whatever model discovered it.

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
