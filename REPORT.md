# Design write-up

## 1. Architecture

Two paths through one set of abstractions.

**Discovery** is expensive and runs once per capability. An LLM observes a
normalized element graph, decides on one typed tool call, and the runtime
executes it — subject to policy — until the goal is met or a stopping condition
fires. **Replay** is cheap and runs on every invocation. It takes a recorded
capability plus typed inputs and drives the same application with no model
involved at all.

```
CLI ──┬── Discovery orchestrator ── LLM provider (Anthropic | OpenAI | scripted)
      ├── Replay engine ─────────── no provider, by construction
      └── Capability catalog ────── Pydantic schema -> agent tool definitions
                │
          Policy gate            allowlist · reversibility · redaction
                │
          Session manager ◄────► Escalation broker + operator console
          (control ownership)
                │
          Surface (ABC) ── WebSurface (Playwright) │ DesktopSurface (documented seam)
```

Key decisions:

**The policy gate sits between the executor and the surface, not in the prompt.**
A guardrail a model can be talked out of is not a guardrail. The agent is told
about the allowlist so it can plan sensibly, but being told is not what enforces
it. The worst case of a confused or injected model is a denied action and a
structured failure.

**Replay holds no provider and asserts `provider_calls == 0` before returning.**
"No model in the decision loop" is checked rather than intended.

**One process, no queues or workers.** The abstractions are shaped so the work
*could* be distributed — capabilities are serialisable, runs are identified,
evidence is filesystem-backed — but building that infrastructure would obscure
the ideas this project exists to show. Named as a cut in §7.

**Trade-off accepted.** The agent gets one rebuilt user message per turn rather
than a growing conversation, so prompt caching covers the system prompt and tool
definitions but not the history. That costs a little cache efficiency and buys a
context that does not fill with stale screens — which degrades decisions as much
as it costs tokens.

## 2. Artifact schema

A capability is a synthesized API for an application that has no API:
`capability_id`, `version`, a description written for a calling agent, a surface
binding, typed `inputs` and `outputs`, ordered `steps`, declared `outcomes`,
`recovery` policies, a `risk` profile, `provenance`, and an `approval` state.

Four decisions shaped it.

**Targets are semantic element descriptors, never selectors or coordinates.**
This is the load-bearing one. In this class of application, element ids are
server-generated per render and test ids do not exist, so a recorded selector
works exactly once; coordinates are worse, because tenants re-brand the same
vendor product and every pixel moves while no control changes identity. A
descriptor carries role, accessible name, a match mode, anchoring context
(frame, panel, row), an ordered fallback list, and a written `robustness_note`
justifying why it should hold. `role` + `accessible_name` was chosen as the
primary key because the same pair exists in the browser accessibility tree, in
Windows UI Automation (`ControlType`/`Name`), and in macOS accessibility
(`AXRole`/`AXTitle`) — so a desktop surface needs no schema change.

**A descriptor states whether its name is the control's own or the caption
beside it** (`name_source`). This sounds like a detail and is not. Legacy forms
label fields by table-cell adjacency rather than markup, so many inputs have no
name at all. And for a data cell the two are different elements: the label reads
"Current Savings Balance" and the value reads "$4,102.55". A descriptor that
does not say which it means resolves to the label and returns the label as the
answer — which is exactly what happened before this field existed.

**Values are references, not literals.** A discovery run necessarily types a real
member number into a real field. Recording that literal would pin the capability
to one record *and* persist regulated data into an artifact that gets committed
and reviewed. Anything that came from a declared input is recorded as
`{{param.member_id}}`; a URL that embeds one is canonicalised into a template.
A final sweep refuses to emit an artifact in which a caller value still appears
in any step, and there is a test guarding it.

**Business outcomes are part of the published contract.** `MEMBER_NOT_FOUND` is
declared alongside inputs and outputs, with a detection predicate, rather than
being inferred from an error string at the call site. Outcomes and recovery
policies are curated per vendor product (`cua/knowledge/legacy-cu.yaml`) and
merged at record time, because they are properties of the *application* rather
than of any one flow: "no member found" looks the same whichever capability
provoked it. A model asked to invent an error taxonomy mid-flow produces a
different one each run, and can only declare outcomes it happened to encounter —
which excludes the ones most likely to matter in production.

## 3. Determinism & error handling

**Targeting.** Replay resolves each descriptor through a seven-rung ladder,
stopping at the first rung that yields exactly one candidate:

| Rung | Strategy | Confidence |
|---|---|---|
| 1 | role + exact name (own name or adjacent label, per `name_source`) | 1.00 |
| 2 | role + normalized name (case, whitespace, punctuation folded) | 0.90 |
| 3 | role + the *other* identifier | 0.80 |
| 4 | role + partial or pattern name | 0.70 |
| 5 | role + section/row anchor | 0.55 |
| 6 | name match ignoring role (survives a link becoming a button) | 0.40 |
| 7 | recorded bounding box — off unless policy enables it | 0.20 |

Anchors narrow before matching; they do not vote. A frame anchor is what
separates the navigation frame's "Member Detail" link from the content frame's.

**More than one candidate at the winning rung is a failure, not a choice.** In a
banking back-office the cost of activating the wrong control is unbounded and
often irreversible, while the cost of refusing is one human interruption. Any
strategy that quietly picks the first match trades an unbounded risk for a
trivial convenience. Resolving below a step's confidence floor still executes but
is marked `DEGRADED`.

**Waits are declarative per step** — a settle period and a bounded timeout, with
an optional condition. No blanket sleeps.

**Error taxonomy, three tiers.** The result contract is
`success` / `business_outcome` / `failure`, and callers distinguish them by
reading `status`, never by parsing a message:

- **Business outcome** — a legitimate answer that happens not to be the happy
  path. Declared in the artifact, detected by predicate, returned with a code.
- **Recoverable** — matched by a `RecoveryPolicy` with an attempt budget:
  dismiss a known interstitial, wait out transient slowness, re-authenticate
  after a timeout. Logged; the run continues.
- **Hard failure** — a structured report naming the step, its intent, what was
  expected, what was observed, a category, and evidence references. Categories
  are split finely enough that the category alone says which part of the system
  to look at: targeting, timing, contract, policy, or the application.

**State is evaluated after every step in a fixed order: outcomes, then recovery,
then checkpoints.** The order *is* the design. A "no member found" screen fails
almost any checkpoint you can write, so evaluating checkpoints first reports an
ordinary business answer as a broken automation — the single most common way
this kind of system is designed wrong. Detectors go first so a legitimate
outcome is recognised as one; recovery goes second so an interstitial is cleared
before the page is judged; only then is it fair to ask whether we arrived.

Two bugs found by running this rather than reading it are worth recording,
because both were the same shape — record-specific data baked into targeting,
producing a capability parameterised in its inputs and hard-coded in its aim.
The extraction target was recorded as `cell "$4,102.55"`, which resolves for
exactly one member. And checkpoint markers could pick up a member number from a
page caught mid-render, asserting a state only one input could reach. Markers
now exclude caller data and anything shaped like a value rather than a label.

A third: checkpoints were passing on pages they should have failed, because the
navigation frame carries a "Member Detail" link on *every* page and an unscoped
text assertion was satisfied by chrome — including on the 500 screen. Checkpoints
are now scoped to the frame their marker was observed in. A checkpoint that
passes everywhere is worse than no checkpoint, because it reports a broken run as
a successful one.

And a fourth, from injecting a slow response: waiting for a navigation *after* the
click is too late. The frame still holds the old, fully loaded document, so
`wait_for_load_state` — and even `networkidle`, which sees no in-flight request
because the click has not issued one yet — is satisfied by the page we just left.
The next observation then asserts against the previous screen. The waiter has to
be armed first, which means knowing in advance whether to expect a navigation;
the recorded step carries that, because the discovery run watched it happen.

**On UI drift**, which is secondary but worth stating: the rung a step
resolved on is recorded per run, so drift is measured rather than assumed. A
capability whose steps migrate toward lower rungs is diverging from its
recording. In the cross-tenant demo, step 4 degrades to rung 5 one step *before*
step 5 fails outright — the signal arrives before the breakage.

## 4. Heterogeneity & multi-tenant

**The surface seam.** `Surface` is four methods: `observe`, `act`, `screenshot`,
`snapshot`. Everything above it — the schema, the ladder, the replay engine, the
policy gate, the escalation model — is written against that interface and knows
nothing about Playwright. The recorded flow says *what to do to which control*;
the surface says *how perceiving and acting happen* on one kind of application.

Extending to a **legacy web app** is already done: the target here is a
frameset with table layout, no test ids, and ids regenerated per render.
Perception traverses frames and tags each element with its frame path, and the
in-page extractor computes a label hint from table-cell adjacency because these
forms have no `<label for>` and an entire class of input would otherwise have no
name at all.

Extending to a **desktop app** is an implementation of the same interface:
populate `Observation` from `UIAutomationClient` on Windows or the AXUIElement
APIs on macOS, and map `ActionRequest` onto Invoke/Value patterns. Role and
accessible name carry over directly, which is why they were chosen. Two rungs do
not: `dom_path` has no desktop analogue (the equivalent is the control's index
path within its parent pane), and `bbox` is if anything less trustworthy because
operators resize windows. `cua/surfaces/desktop_stub.py` documents that mapping
rather than claiming support that is not there.

**Multi-tenant reuse.** A capability is scoped to the *vendor product*, not the
institution — `SurfaceBinding.product` is the join key. A `TenantOverlay` is a
narrow, reviewable patch: entry point, per-step descriptor overrides, disabled or
inserted steps, outcome overrides. `resolve()` deep-merges and returns a new
effective capability; the base is never mutated, so the shared recording stays
the single source of truth and every deviation is explicit and diffable. An
overlay declares the `base_version` it was authored against and refuses to apply
across a re-recording, which forces re-review rather than silently drifting.

This is demonstrated, not just described. Tenant `beta` runs the same product
with different branding and two renamed controls. The alpha recording replayed
against it degrades to rung 5 and then fails; a nine-line overlay overriding two
descriptors brings it back to rung 1 and success. That is the intended shape at
scale: one recording per product, small reviewed patches per tenant, and rung
telemetry to catch divergence before it breaks.

## 5. Escalation & handoff

**Detecting stuck.** Escalation is raised on an unresolvable or ambiguous target,
a checkpoint violation, an irreversible action awaiting approval, a session
expiry the system will not resolve itself, an agent that calls `give_up`, and
discovery hitting its step ceiling.

**The request carries enough to act on** without redoing the diagnosis: which
capability and goal, which step and its intent, what was expected, what was
observed, the URL, and a screenshot. The resume options offered depend on *why*
we stopped — there is no sense offering "retry" for an irreversible action
awaiting approval, or "approve" for a control that could not be found.

**Control ownership is an explicit state machine** over one long-lived browser
context:

```
AUTOMATION ── intervention ──► BLOCKED ── operator takes control ──► HUMAN
     ▲                                                                │
     └── re-observe, re-assert ── HANDING_BACK ◄── operator resumes ───┘
```

The requirement is that a human takes over *the same live session*, with cookies,
form state and history intact. That is a constraint on session lifetime, which
makes it architectural rather than a feature to bolt on — and it is why the
session manager owns the browser context instead of the engine creating one per
step. `Surface.act()` asserts the caller owns control, so an automation action
taken while a person is mid-keystroke is a loud error where it happens rather
than a race that produces a confusing screenshot ten seconds later.

The return path goes through `HANDING_BACK` deliberately: automation re-observes
and re-checks its expectations before acting again, because it has no idea what
the human did and must not assume they left the screen where it was.

`APPROVE` and `PERFORMED_MANUALLY` are distinct answers. Collapsing them would
either repeat an irreversible action the operator already took, or skip one that
never happened.

**What the human does is recorded** into the same evidence stream, values
withheld. That matters beyond audit: a capability that needed manual help has a
gap in it, and the captured actions are the raw material for closing that gap in
the next version. An intervention that teaches the system nothing will recur
tomorrow.

**Deliberately mocked:** the console itself is one HTML page that polls. A real
co-browsing console is out of scope and building a polished one would say nothing
about the part that matters. What is real is the transfer model — the automation
genuinely stops, the person genuinely gets the session it was using, their
actions are genuinely recorded, and expectations are genuinely re-checked on
resume.

An unattended run still raises and records interventions but does not wait on
one, because blocking for fifteen minutes on an operator who was never coming is
worse than failing fast with a debuggable report.

## 6. Safety

**Allowlist.** Permitted origins, path patterns, and action types, enforced in
both discovery and replay before any action reaches a surface. A denial is a
first-class outcome the caller sees, never a silently skipped step.

**Reversibility.** An action is classified irreversible when *both* hold: the
control is one that can commit (a button, not a link) and its accessible name
matches a configured verb — submit, transfer, delete, approve, post, close.
Requiring both matters: "Open Sub-Account" names the link that opens the form as
well as the button that submits it, and treating the link as irreversible stopped
the agent before it could reach the screen it needed.

Handling differs by path. In **discovery** an irreversible action always stops
for a human: a model still working out how the application behaves has no
standing to move someone's money. In **replay** it proceeds only when a reviewer
has approved both the step (`approved_risky`) and the capability
(`approval: approved`); otherwise it escalates. A human approving an action
during discovery approves *that* action in *that* session — it does not authorise
the capability to take it unattended forever after, which is a separate review.

Demonstrated: a draft capability refuses its irreversible step unattended and
reports why; the reviewed and approved version completes and returns its
confirmation reference.

**Data handling.** Two layers, and the order matters. Structurally, sensitive
values are parameters and parameters are recorded as references, so nothing
sensitive is written down in the first place — that is the control that actually
works. Everything reaching disk then passes through a redactor that masks
recognisable secrets and PII by pattern, and the concrete values of inputs
declared `sensitive` are registered so they are masked even if the application
echoes one back in an error message. Screenshots taken while a sensitive field
holds a value are masked before capture. Typed values are never echoed into the
agent's history, only the fact that a field was filled.

**Limits, honestly.** Pattern redaction cannot recognise every shape of sensitive
data; it is defence in depth, and a system relying on it alone would leak. The
reversibility classifier is a name heuristic — deliberately biased, since a false
positive costs one confirmation and a false negative costs a real transaction —
but a control named "Continue" that commits a transfer would not be caught. The
allowlist is origin and route level, not semantic: it cannot tell a legitimate
`/member` lookup from an inappropriate one. And the operator credentials in the
demo are a literal in the recording, because credential management is out of
scope here; in production those would come from a secret store, and the
re-authentication policy deliberately routes to a human rather than letting this
process hold them.

## 7. Cuts

Deliberately left out, with the seam kept real:

- **Desktop surface.** `Surface` ABC plus a documented UIA/AX mapping. Not built.
- **Multi-tenant infrastructure.** Overlays and resolution are built and
  demonstrated; a registry service, per-tenant config store, and scheduling are
  not. This project explicitly does not reward that plumbing.
- **Real co-browsing console.** Control transfer is real; the UI is one polling
  page.
- **Credential management.** Out of scope; noted in §6.
- **Persistence beyond the filesystem.** No queue, workers, or clustering.
- **Automated re-authentication.** Routes to a human by design, because the
  alternative is this process holding operator credentials.

What I would build next, in order:

1. **Assisted single-step recovery.** On a resolution failure, allow one bounded,
   policy-checked LLM call to re-identify *that one control* — never open-ended
   re-planning — and record it as evidence. This is the highest-value addition
   because it converts the most common failure into a self-healing one without
   putting a model back in the decision loop.
2. **Confidence scoring from replay history.** The rung data is already recorded
   per step per run; aggregating it gives a per-capability reliability score and
   a principled gate for promoting `draft` to `approved`, replacing the manual
   review that currently gates unattended irreversible replay.
3. **Promoting recorded human actions into a new capability version.** The
   handoff already captures what the operator did; turning that into a proposed
   step diff closes the loop so an intervention improves the capability rather
   than just unblocking one run.
4. **Outcome discovery.** Business outcomes are currently curated per product. A
   review workflow that proposes new ones from unrecognised screens encountered
   during replay would keep that taxonomy current without asking a model to
   invent it mid-flow.

### On the discovery run

> **PENDING — must be resolved .** The runs currently in
> `evidence/` were produced by the scripted provider. A genuine model-driven
> discovery run has not been recorded yet, because no API key was configured
> while this was built. The command is unchanged apart from dropping
> `--provider scripted`, and the run must be committed under
> `evidence/discovery-*` before this is sent. Leaving this note in rather than
> quietly implying otherwise: this project is explicit that the discovery run has
> to be real, and a scripted run is not one.

The scripted provider exists so the loop, the guardrails and the recorder can be
tested deterministically without a key, and so anyone without one can run the
full demo path. It proves the plumbing, not the agent's judgment, and every place
it can be selected says so.
