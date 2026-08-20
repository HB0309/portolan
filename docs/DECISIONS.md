# Decision log

Every entry is something someone could reasonably have done differently.
Each states the decision, the reasoning, what it costs, and what would change
our mind. Anything here should be answerable in one sentence out loud.

---

## D1 — Python, Playwright, Pydantic, FastAPI

**Decision.** Python 3.12 with Playwright for the web surface, Pydantic v2 for
the artifact contracts, FastAPI for the operator console, Typer for the CLI.

**Why.** Pydantic gives runtime-validated typed artifacts *and* emits JSON
Schema, which is what makes the agent-facing capability catalog nearly free: a
capability's input schema is already a tool definition. Playwright gives the
accessibility tree, frame traversal, and CDP access we need for a legacy surface
with framesets.

**Cost.** A compiled language would give stronger guarantees at the artifact
boundary. Pydantic's runtime validation is a reasonable substitute given the
artifacts cross a process boundary as JSON anyway.

---

## D2 — Build the target application rather than using a public demo site

**Decision.** The target is `mockapp/`, a purpose-built, deliberately hostile
stand-in for a credit-union servicing console, rather than a public sandbox site.

**Why.** Three requirements are unreachable against a site we do not control.
First, replay has to be exercised against a replay that hits an error or exceptional state
— we need to inject a session timeout, a permission denial, a validation error
on demand. Second, the interesting failure modes here are runtime
conditions, not layout drift, and only a controlled app can produce them
reproducibly. Third, cross-tenant reuse needs two instances of the same product
configured differently, which is a config flag here and impossible elsewhere.
It also means anyone can clone the repo and run everything offline.

**Cost.** A mock app is a surface we designed, so there is a risk of accidentally
building something our own automation finds easy. Mitigated by making it
deliberately hostile: framesets, table layout, no test ids, and server-generated
element ids that change on every render, which specifically defeats the naive
selector strategy.

**What would change our mind.** If a public sandbox existed with genuine banking
flows *and* a documented failure-injection mode, it would be the better proof.

---

## D3 — Perception is accessibility-tree first, screenshots for grounding

**Decision.** The agent observes through a normalized element graph derived from
the accessibility tree. Screenshots go to the model for visual grounding and to
evidence, but they are not the targeting mechanism.

**Why.** The environment has hundreds of tenants running the same vendor product
with different branding. Pixel coordinates do not survive a re-theme; role and
accessible name do. The accessibility tree is also the one representation that
exists on all three surface types that matter — modern web, legacy web, and
native desktop — which makes it the right abstraction to build the artifact
schema on.

**Cost.** Applications with a genuinely broken accessibility layer degrade badly.
The fallback ladder (D4) exists for exactly that case, ending at coordinates,
which are permitted only by explicit policy opt-in.

---

## D4 — Artifacts record semantic element descriptors, not selectors or coordinates

**Decision.** A step's target is an `ElementDescriptor`: role, accessible name,
a match mode, anchoring context, an ordered fallback list, and a written note
justifying its robustness. Replay resolves it through a seven-rung ladder and
records which rung matched.

**Why.** This is the load-bearing decision. A CSS selector recorded against a
legacy enterprise app — where ids are server-generated per render and test ids do
not exist — is a coin flip next month. Coordinates are worse. Role plus
accessible name is stable across the re-branding and re-configuration that the
multi-tenant reality imposes, and it is the pair that also exists in Windows UI
Automation and the macOS accessibility API, so a desktop surface can be added
later without changing the schema.

Recording *which rung matched* turns the same mechanism into drift telemetry: a
tenant whose steps increasingly resolve on lower rungs is diverging from the
recording, and can be flagged for review before it breaks outright.

**Cost.** Resolution is more work per step than `page.click("#id")`, and a
descriptor is bulkier to read. The bulk is largely the robustness note, which is
the part a human reviewer actually needs.

---

## D5 — Replay returns three states, not two

**Decision.** `success` / `business_outcome` / `failure`.

**Why.** "No such member" is a legitimate answer the calling agent needs to say
out loud, not a malfunction. Collapsing it into failure turns a normal response
into an on-call page and makes the capability unusable for the product it exists
to serve. Business outcomes are therefore declared in the artifact alongside
inputs and outputs — part of the published contract, not something the caller
infers by parsing an error string.

**Cost.** The recorder has to anticipate the outcomes worth naming. Ones it does
not anticipate surface as checkpoint violations, which is the correct
conservative default: an unrecognised state stops the run rather than being
guessed at.

---

## D6 — Control ownership is an explicit state machine over one long-lived session

**Decision.** `AUTOMATION → BLOCKED → HUMAN → HANDING_BACK → AUTOMATION`, with
one browser context alive for the whole run. `Surface.act()` asserts the caller
owns control.

**Why.** The requirement is that a human takes over *the same live session* — not
a fresh one, with the cookies, form state and navigation history intact. That is
a constraint on session lifetime, which is an architectural property, not a
feature that can be bolted on afterwards. Making ownership explicit also answers
"who is in control right now" with a value rather than an assumption, and makes
an automation action during human control a loud programming error instead of a
race.

**Cost.** The session manager owns the browser context, so it has to be threaded
through everything that acts. That is the price of being able to answer the
ownership question at all.

---

## D7 — The policy gate lives between the executor and the surface

**Decision.** Every action in both discovery and replay passes through
`PolicyEngine.check()` before reaching a surface. The agent is *told* about the
allowlist so it can plan sensibly, but being told is not what enforces it.

**Why.** A guardrail expressed only in a system prompt is a guardrail the model
can be talked out of. Putting it in the runtime means the worst case of a prompt
injection or a confused model is a denied action and a structured failure, not an
unauthorised transaction.

**Cost.** The allowlist has to be maintained per deployment, and an
over-restrictive one shows up as denials mid-run rather than as a warning up
front.

---

## D8 — Ambiguity is a failure, never a guess

**Decision.** If a descriptor resolves to more than one candidate at the winning
rung, replay stops with `ELEMENT_AMBIGUOUS` or escalates. It does not pick the
first match.

**Why.** In a banking back-office, the cost of clicking the wrong control is
unbounded and often irreversible. Refusing costs one human interruption.
Guessing costs a transaction on the wrong account.

**Cost.** More escalations on genuinely ambiguous pages. The fix is a better
descriptor — usually an additional anchor — which is the outcome we want anyway.

---

## D9 — Single process; no queues, workers, or clustering

**Decision.** Everything runs in one process. The operator console is a small
FastAPI app in the same process as the run it serves.

**Why.** Scaling infrastructure is not what this project demonstrates, not building
infrastructure, and prematurely adding it would obscure the parts being
evaluated. The abstractions are shaped so that the work *could* be distributed —
capabilities are serialisable, runs are identified, evidence is filesystem-backed
— without any of that being built.

**Cost.** Not production-shaped for concurrent tenants. Named in the report as a
deliberate cut with a sketch of what would change.

---

## D10 — Two LLM providers behind one protocol

**Decision.** An `LLMProvider` protocol with Anthropic and OpenAI adapters, plus
a scripted adapter for offline runs. Claude is the primary discovery driver.

**Why.** Model portability is a real procurement requirement for regulated
buyers, so the seam is worth having on its own merits. It also enables the
demonstration that matters most for the thesis: a capability discovered by one
vendor's model, invoked by a different vendor's agent through the catalog. That
is the clearest possible evidence that the artifact is decoupled from the model
that produced it.

**Cost.** Two adapters to keep working. Kept cheap by making the protocol narrow
— one method that takes an observation and returns a typed tool call.

---

## D11 — The scripted provider

**Decision.** A third `LLMProvider` implementation that replays a fixed sequence
of tool calls instead of calling a model.

**Why.** Two reasons, both practical. It lets the discovery loop, the recorder,
and the artifact emission be tested deterministically in CI with no key and no
network. And it gives a reviewer without an API key a way to run the full demo
path, . It is not a substitute for the
real run — the committed evidence includes a genuine model-driven discovery.

**Cost.** A scripted run proves the plumbing, not the agent's judgment. The
distinction is stated wherever the scripted path appears so nothing is
overclaimed.

---

## D12 — Cost controls in the agent loop from the start

**Decision.** Prompt caching on the system prompt and tool definitions, and
observation-history trimming: the latest observation in full, earlier ones
collapsed to one-line summaries.

**Why.** A twenty-five step loop that re-sends full accessibility trees grows
quadratically in tokens. Beyond the bill, a context stuffed with fifteen stale
page snapshots measurably degrades the agent's decisions — the relevant
observation is the current one, and the history only needs to record what was
already tried.

**Cost.** The agent cannot re-examine an old page in detail. In practice it does
not need to; when it does, it can navigate back and observe again.
