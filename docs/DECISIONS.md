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

---

## D13 — Descriptors state whether their name is the control's own or the label beside it

**Decision.** `ElementDescriptor.name_source` is either `accessible_name` or
`adjacent_label`, and the resolution ladder matches against whichever the
descriptor names.

**Why.** This was added after watching a replay return the string
"Current Savings Balance" as a member's savings balance. The label cell and the
value cell are two different elements: the label's accessible name is
"Current Savings Balance" and the value's is "$4,102.55". A descriptor saying
only `cell "Current Savings Balance"` matches the label first, at the highest
rung, and reads it back as the answer.

The related half of the same problem is that recording the value cell by its own
text produces `cell "$4,102.55"`, which resolves for exactly one member. That is
the worst failure shape available here — a capability parameterised in its
inputs and hard-coded in its aim, which passes every test run against the record
it was recorded from.

**Cost.** One more field to get right at record time, and a resolver that has to
consult it. Both are cheap next to the failure it prevents.

---

## D14 — Checkpoints are scoped to the frame their marker was observed in

**Decision.** A generated checkpoint asserts its marker within a frame, not
across the whole screen.

**Why.** Also found by running it. The navigation frame in this application
carries a "Member Detail" link on every page, so the unscoped assertion
"'Member Detail' is present" passed on the application-error screen — the run
had visibly failed and the checkpoint reported success. A checkpoint that passes
everywhere is worse than no checkpoint, because it converts a broken run into a
reported-good one, which is precisely the thing checkpoints exist to catch.

**Cost.** The marker's frame has to be captured at record time and the predicate
evaluator has to support scoping. The scoping mechanism was already needed for
element anchoring, so this was reuse rather than new machinery.

---

## D15 — Recovery after an action does not re-run the action

**Decision.** Pre-action recovery re-observes and retries the step. Post-action
recovery clears the condition, re-observes, and re-evaluates the checkpoint
*without* repeating the step.

**Why.** An interstitial that appears on the way to the next screen is not a
reason to click Search again — and on a submit it would be a double posting,
which in this domain means a duplicate transaction. The visible symptom was
milder: after dismissing the notice, replay went looking for the Search button
on a page that had already moved past it.

**Cost.** Two recovery paths instead of one. The distinction is real, though —
"the action has not happened yet" and "the action has happened" genuinely call
for different responses.

---

## D16 — Reversibility needs two signals, not one

**Decision.** An action is irreversible only when the control can commit (a
button, not a link) *and* its name matches a committing verb.

**Why.** The name pattern alone fired on the link captioned "Open Sub-Account",
which merely opens the form, and stopped the agent before it could reach the
screen it needed. Requiring the role as well keeps the classifier from tripping
on every link with a verb in it, without weakening the case that matters.

**Cost.** An application that uses `<a>` for genuinely committing actions would
slip through. Named as a limit in REPORT §6 rather than papered over; the fix
would be to treat any control inside a form's submit path as committing,
regardless of tag.

---

## D17 — The recorded step says whether it expects a navigation

**Decision.** `WaitSpec.await_navigation` is captured during discovery and used
by replay to arm a navigation waiter *before* the click.

**Why.** Found by injecting a slow server response. Immediately after a click the
frame still holds the old, fully loaded document, so every "is it loaded yet"
check — `wait_for_load_state`, and even `networkidle`, which sees no in-flight
request because the click has not issued one yet — is satisfied by the page we
just clicked away from. The next observation then asserts against the previous
screen, and a page that was merely still arriving is reported as a checkpoint
violation.

The waiter has to be armed first, which means knowing in advance whether to
expect a navigation. Guessing costs either the full navigation budget after
every click that does not navigate, or a race on the ones that do. The recording
already knows, because the discovery run watched it happen.

**Cost.** A step recorded as navigating that does not navigate this time — a
validation error keeping us on the page — waits out its budget. That is
acceptable: it is a real state the caller needs to see, and the observation
reports it rather than the wait swallowing it.

**Related bug.** Navigation detection during discovery originally compared only
the top-level URL, which never changes when a child frame navigates — so every
step in a frameset application recorded `await_navigation: false`. Detection now
compares the acting frame's URL as well.

---

## D18 — The surface settles before automation resumes from a handoff

**Decision.** `Surface.settle()` is called after an escalation resolves with
anything other than abort, before automation observes again.

**Why.** An operator who performs the action and immediately clicks resume
leaves a navigation in flight. Automation then observes a half-rendered page,
correctly refuses to act on it, and escalates a second time — over a screen that
was merely still arriving. In an attended run that is a confusing double
interruption; in a run where the operator has since walked away, it is a stall.

Found while testing the handoff end to end rather than by reading the code, which
is the argument for testing that path against a real browser rather than
asserting the state machine in isolation.

**Cost.** One more method on the surface interface, defaulting to a no-op so a
surface with nothing in flight is unaffected.

---

## D19 — The catalog serves one version per capability, approved first

**Decision.** `CapabilityCatalog` deduplicates by `capability_id`, preferring
`approved` over `draft` and then the higher version.

**Why.** A freshly recorded draft and the reviewed version that supersedes it
legitimately sit side by side. Listing both means an agent asking for the
capability by name gets whichever sorted first by filename — so a review could
be bypassed by naming, which defeats the point of having an approval state at
all.

**Cost.** Older versions are not directly callable through the catalog. That is
the intent; they remain on disk and can be replayed by path for debugging.

---

## D20 — Capabilities are discovered by a model, not recorded from a human demo

**Decision.** A capability is authored by an LLM-driven discovery run. It is not
authored by a browser extension capturing an operator demonstrating the flow.

**Why.** Record-and-replay is a real technique — it is the foundation of the RPA
industry, and Playwright's own codegen does it. Four things make it the wrong
primary path here.

*Capturing clicks is the easy part.* The hard part is what gets recorded
**about** an element so that it still resolves on a later render, at a different
institution, after someone re-brands the product. Ids in this class of
application are server-generated per render — `ctl00_wpz_ctl47_txt83` is a
different string next time — so a recorded selector works exactly once. A
recorder still needs the resolution ladder, the `name_source` distinction, and
the frame and panel anchoring. Recording changes who initiates the capture; it
changes nothing about what has to be inside the artifact. It relocates the
problem rather than solving it.

*The cost floor never comes down.* Hundreds of tenants running about twenty apps
each means human demonstration costs one trained operator's time per flow, per
app, forever, and again whenever a vendor ships an update. The value of the model
is not that it clicks — it is that it can be pointed at an application nobody has
ever recorded, given a goal in English, and work it out on the four-hundredth
tenant as readily as the first.

*A browser extension is web-only.* Native desktop applications are in scope for
this environment. The `Surface` interface extends to Windows UI Automation and
the macOS accessibility API; an extension architecturally cannot follow it there.

*It buys nothing over CDP.* The same in-page event capture already exists in
`cua/session/manager.py`, where the handoff records what an operator did in every
frame. An extension would be a second codebase, in another language, doing what
Playwright already gives us — plus an install on every operator's machine at an
institution with opinions about browser extensions.

**Cost.** LLM discovery costs an API call per step, and it can get stuck on a
screen a human who knows the flow would walk through without thinking. That is
what the escalation path is for.

**What would change our mind.** Nothing about *authoring* being plural. Human
demonstration is a legitimate second way to author a capability, and the machinery
is largely already here — see REPORT.md section 7. The load-bearing constraint is
not which path authored it but that every path emits the same artifact: the
artifact is the centre of this system, and discovery is one way to fill it rather
than the definition of it.

---

## D21 — Section detection reads structure before class names

**Decision.** `sectionOf` in the perception layer looks for a `<legend>`, then a
`<caption>` or `<th>`, and only then for the app's own panel-header class names.

**Why.** It originally looked for `.panelhdr` alone, which was the target
application's convention at the time. Rebuilding that application's markup
changed the class to `.caption`, and every section went empty — which sounds
cosmetic and is not. Sections are what let a descriptor say "the Search button in
Member Search" instead of "one of the four Search buttons", and they are what a
generated checkpoint asserts on. With no section, checkpoint markers fell back to
whatever text was nearby, which on an application with a menu bar and a toolbar
means chrome that appears on every page. The injected-500 replay went from
correctly reporting a checkpoint violation to sailing past it and failing two
steps later for an unrelated reason.

The general point is that a perception layer coupled to one application's CSS
conventions is not a perception layer. Structural cues — a legend naming its
fieldset, a caption naming its table — carry the same meaning in any markup that
has them, so they belong first. Class names stay as a last resort because legacy
markup often has nothing else.

**Cost.** Three lookups instead of one, and applications with neither structure
nor a recognised class name still yield no section. Those degrade to unanchored
descriptors, which the ambiguity rule then catches rather than mis-resolving.

**How it was found.** By rebuilding the target application's markup and watching
which replays changed behaviour — not by reading the code. The same is true of
most of D13 through D18.
