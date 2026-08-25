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

---

## D22 — Intent text is parameterized the same way step actions are

**Decision.** Before a step's `intent` is stored, any caller value it contains
is replaced with `{{param.name}}`, the same substitution `_value_ref` already
applies to actions.

**Why.** Found running a genuine model against Groq's `openai/gpt-oss-120b`,
not the scripted stand-in. Its stated reason for a step was "Click Search to
retrieve member 10042's details" — it restated the literal member number,
because that is what a model does when it explains its own reasoning in plain
English. The scripted provider used everywhere else in this project's tests
never does this: its canned explanations were written with no real value to
restate, so this path was never exercised until a real model ran the loop.

The recorder's final safety sweep did exactly what it is supposed to: it scans
every recorded step for a literal caller value and refuses to emit the
capability if one is found. It fired, correctly. What was missing was
anything to save the run once it fired — the whole capability was discarded
rather than the one string that tripped the guard being fixed. A new scrub
step closes that (later generalized into `_scrub_text` — see D23): the same
step now reads "Click Search to retrieve member
`{{param.member_id}}`'s details", which says the same thing without the
literal, and the run records cleanly.

**Cost.** None found. The substitution is character-for-character the one
already trusted for actions; applying it to one more field is not a new kind
of risk.

**How it was found.** By running a real, unscripted model against the target
application and reading what it actually produced — the same way as D13
through D18 and D21. The scripted provider's canned strings cannot produce
this class of bug, because they were never written by anything that reasons
in prose about the data it is handling.

## D23 — Descriptor fallback text is parameterized too, not just intent

**Decision.** `_scrub_intent` from D22 is generalized into `_scrub_text` and
applied to two more places a caller value can leak into generated prose: an
element descriptor's `row_text` fallback, and its `robustness_note`.

**Why.** Found in a live human-attended run, driven through the operator
console rather than by a model alone. Opening a sub-account, the model retried
its `extract` call three times hunting for the reference number and, on one of
those tries, targeted the "Initial Deposit" cell instead of the reference cell.
The cell it settled on and recorded was labeled correctly — that part of
`describe_element` was never wrong — but `describe_element` also folds the
whole containing table row into a `FallbackHint` for resilience against layout
drift, and a summary screen's row routinely holds more than the one field a
step cares about. The mis-targeted attempt's row read "Initial Deposit $500",
and `500` — the caller's `initial_deposit` value — rode along into the
fallback text verbatim, well outside the one field the step was ever meant to
touch. The recorder's safety sweep caught it and refused to emit the
capability, same as D22.

A deterministic run against the scripted provider was written to try to
reproduce this offline first, to make sure the fix was aimed at the right
place before touching it. It could not: the scripted provider always targets
the correct cell on the first try, so it never produces the mis-targeted
`extract` that creates the leaking row. That is not a gap in the scripted
tests — it is the same gap D13 through D22 keep landing on. A canned script
cannot misbehave the way a real model occasionally does, so bugs that only
exist on the *path* to a correct answer, not in the answer itself, only surface
by watching a real run.

**Cost.** None found beyond D22's. Same substitution, two more call sites.

**How it was found.** By a human operator actually driving the handoff console
for a real, unscripted run — not by reading the code, and not by the scripted
test suite, which cannot retry into the wrong cell in the first place.

## D24 — Rate-limit retries give up loudly past a bounded silent wait

**Decision.** `_retry_after_seconds` now parses hours/minutes/seconds out of a
429's message, not just bare seconds, and the retry loop raises immediately
rather than sleeping when the server's suggested cooldown exceeds
`_MAX_SILENT_WAIT_SECONDS` (45s). Every retry also prints the wait and attempt
number.

**Why.** Found live: a day of testing against Groq's free tier exhausted its
200,000-token *daily* budget (197,877 used, ~2,100 left, less than one more
tool-calling request needs), which surfaces as a 429 with a message shaped
like the per-minute limit this project had already built retry logic
around — "Please try again in ..." — except naming the cooldown in minutes
("3m14.832s") once it is long enough to need one. The regex only recognised
bare seconds, so it silently missed the match and fell back to an 8-second
default sized for the per-minute case, then retried straight back into the
same wall, in total silence, for four attempts. In the operator console this
was indistinguishable from a genuine hang — an open TCP connection sitting
idle for minutes, no log output, no error — and cost real time diagnosing a
network-level cause (IPv6 path MTU, a repeat of the earlier NVIDIA-endpoint
saga) before the actual cause turned up in a direct reproduction.

A daily quota does not clear itself in 45 seconds, so waiting it out silently
was never going to be the right behaviour regardless of the regex fix. Once
the cooldown is genuinely minutes long, the only honest thing to do is stop
and say so — the operator can decide to wait, retry later, or switch
providers, but they cannot decide anything while the console just looks
frozen.

**Cost.** A cooldown between 8 and 45 seconds is now still waited out
silently except for one printed line per attempt; nothing about the
per-minute case (the one this retry loop was originally built for) changed in
practice.

**How it was found.** By watching a real attended run go quiet for over ten
minutes, ruling out the browser, the network route, and the process itself
before reproducing the exact failure directly against the provider client —
which took seconds once tried, versus the many minutes spent suspecting a
hang. The scripted provider cannot produce a 429 at all, so this class of bug
has no path to a test until a real quota is actually exhausted.

## D25 — Take-control refreshes the console's screenshot

**Decision.** `EscalationBroker.take_control()` takes a fresh screenshot the
moment control actually transfers, and updates the request's `screenshot`
and `url` fields with it, rather than leaving them at whatever was captured
when the escalation was first raised.

**Why.** Reported three times across three different attempted fixes this
session — a direct-render vs. 303-redirect change to the POST handlers, then
explicit `Cache-Control: no-store` headers — each aimed at "I click Take
control and nothing visibly happens until I reload." None of them were wrong
exactly, but none of them were the cause either: a background research pass
tracing the click through `console.py`, `broker.py`, and `session/manager.py`
end to end found that the button and status text were already re-rendering
correctly on every load. What never changed was the screenshot embedded in
the card — captured exactly once, in `raise_intervention`, and never
refreshed by anything afterward, including take-control itself. The
screenshot is the dominant visual element on the page; a human watching it
stay frozen reasonably concludes the click did nothing, and a manual reload
does not fix it either, since it is re-reading the same static file every
time. Two rounds of fixing the page shell around a stale image were never
going to resolve a complaint about the image.

**Cost.** One extra screenshot per handoff -- a single Playwright call,
already the same primitive `raise_intervention` uses for the first one.

**How it was found.** Not by the researcher's own testing -- by chasing the
same symptom the operator kept reporting after two prior "fixes," and, this
time, tracing the actual request lifecycle end to end instead of pattern-
matching to the last plausible cause (caching, redirects) that had already
been tried and had not worked.

## D26 — The caller-data sweep is scoped to string leaves, not raw JSON text

**Decision.** `_assert_no_caller_data` now detects a leak the same way
`_locate_leak` (D23's diagnostic helper) already located one: by walking a
step's dumped structure and checking string leaves, not by substring-matching
the caller's value against the step's raw serialized JSON text.

**Why.** Found live, on a genuinely successful discovery run: the model
signed in, filled the form, an operator handled the irreversible submit by
hand, the reference number was extracted correctly (`discovery.succeeded`
logged) — and then the run still ended in the same `RecorderError` as D23,
naming `initial_deposit` again, in a step D23's own diagnostics said had no
string field containing it. It didn't, because there wasn't one: an unrelated
element's positional `FallbackHint` had `bbox.y = 500.0`, an entirely
ordinary pixel coordinate, and `json.dumps` renders that float as the text
`"500.0"` — which contains `"500"` as a substring for exactly the same reason
a real leak would. The detection check and the diagnostic check were scoped
differently (raw JSON text vs. string leaves only), so the diagnostic could
correctly find nothing while the detector still fired.

Every value that can actually reach a recorded step arrives as text —
`_value_ref` and `_scrub_text` both only ever produce strings — so a bare
JSON number is never how caller data enters a step, only ever how a
coincidence does. A short numeric input (a dollar amount, an account number)
colliding with *some* element's x/y/width/height somewhere in a multi-step
capability is not a rare accident; on a real screen with real pixel
coordinates it is close to certain over enough steps, which makes this guard
essentially unusable for any capability whose inputs include short numbers —
exactly the shape most banking inputs take.

**Cost.** None found. Every real leak this project has hit (D22's intent
text, D23's fallback row text and robustness note, the D13-shaped bare
accessible name) is a string field; narrowing the sweep to strings loses no
coverage this project has ever needed and has a test proving it still catches
one.

**How it was found.** By a live run that *succeeded* at every layer the
system is actually supposed to test — the model, the escalation handoff, the
extraction — and was still discarded at the very last step, which is what
made this worth chasing rather than assuming the input was simply unlucky
twice. D23's own diagnostic addition (reporting which field a leak was found
in) is what made the mismatch visible instead of just a second unexplained
refusal.

## D27 — Time spent waiting on a human is excluded from the wall-clock budget

**Decision.** `DiscoveryOrchestrator` tracks total time spent inside
`_raise_intervention` (i.e. actually waiting on `on_intervention` to return)
and adds it to the wall-clock deadline check, so a real operator's response
time never counts against the automation-loop budget.

**Why.** Found live: an attended run raised an irreversible-action
escalation, a real operator took roughly ten minutes to get back to the
console and resolve it correctly -- signed in, filled the form, approved the
submit -- and the run failed immediately afterward with `exceeded the 300s
wall clock`. Nothing went wrong; the operator did exactly what an attended
run asks of them. The wall clock exists to catch automation that loops
without making progress, which is a real failure mode this project has hit
before (`max_steps`, `max_consecutive_noops` guard the same thing from other
angles) -- it was never meant to bound how long a person takes to read a
screen and click a button. The broker already has its own, much longer
timeout for an *unanswered* escalation (`timeout_seconds=900.0` in
`EscalationBroker`); the orchestrator's 300s budget was silently a second,
much tighter deadline on the same wait, undocumented and almost certainly
unintentional.

**Cost.** None found. A run where automation is genuinely stuck in a loop
still times out exactly as before -- only time spent inside an actual human
wait is excluded, and that time is bounded on its own by the broker's
900s timeout regardless.

**How it was found.** By an operator (this project's own author) taking a
normal, unhurried amount of time to respond to a live escalation while doing
something else -- not a stress test, just an ordinary attended run running
long enough for real human latency to matter. The scripted provider's tests
never wait on anything, so this had no path to a test until a slow human
actually did.

## D28 — Taking control twice is a no-op, not a crash

**Decision.** `EscalationBroker.take_control()` checks the session's actual
control-FSM owner before calling `hand_to_human()`; if control has already
moved off `BLOCKED` (i.e. a human already has it), it returns the request
unchanged instead of transitioning again.

**Why.** Found live: an operator's browser sent `POST /take/{id}` twice for
the same escalation -- a double-click, or a reflexive reload right after
clicking, are both completely ordinary things a real person does. The
control FSM (`cua/session/control.py`) only allows `BLOCKED -> HUMAN` once;
the second call reached `grant_to_human()` with the owner already `HUMAN`,
which correctly raised `ControlViolation` by the FSM's own contract -- and
that exception then propagated straight out of the FastAPI handler,
uncaught, as an unhandled 500 the operator saw as a bare "server error" with
no way to recover except restarting the run. The FSM raising loudly on an
invalid transition is correct and stays that way; what was missing was
anything upstream making the transition attempt conditional on it actually
being needed.

This is race-safe without a lock: `grant_to_human()`'s mutation
(`control.grant_to_human()`) runs synchronously, before `hand_to_human()`'s
first `await`. In asyncio's cooperative model that means whichever request
reaches `take_control()` first has already flipped the owner by the time a
second, concurrently-arriving request reaches the check -- there is no
window where both requests can observe `BLOCKED`.

**Cost.** None found. A genuinely new escalation still transitions exactly
as before; only a request that already succeeded is now idempotent against
being repeated.

**How it was found.** By a real operator's browser, not by reasoning about
the FSM in the abstract -- double-clicks and reload reflexes are the kind of
input a live person produces and a scripted test never does.

## D29 — take_control() flips status before it refreshes the screenshot

**Decision.** Inside `EscalationBroker.take_control()`, `request.status =
IN_CONTROL` is set immediately after `session.hand_to_human()` returns, not
after the screenshot refresh (D25) that follows it.

**Why.** Found live, the fourth distinct bug from the same reported symptom
this session: an operator clicked "Take control", the response took a
moment -- `hand_to_human()` installs a human-action watcher over CDP and
`take_control()` then refreshes the escalation's screenshot, both real round
trips D25 added deliberately, neither free -- and they reloaded out of
impatience before the response arrived. Until this fix, `request.status` was
only set to `IN_CONTROL` *after* that screenshot capture finished, so a
reload landed mid-request saw the console still reporting `OPEN` and
re-rendered the "Take control" button, even though `session.owner` was
already genuinely `HUMAN`. The render was internally consistent with what
the server knew at that exact instant; it was also indistinguishable from
the click having failed, which is exactly what kept getting reported as
"I had to reload."

Status and screenshot answer different questions -- "who has control"
(cheap, decided the moment `hand_to_human()` returns) and "what does the
session look like right now" (a real CDP round trip, worth doing but not
gating). Ordering the fast, decisive fact first means a request that arrives
mid-refresh -- a reload, a poll -- sees the correct state even if the picture
in it is a beat stale, rather than a wrong state because the picture was not
ready yet.

**Cost.** None found. The screenshot still gets refreshed exactly as before;
only the order changed.

**How it was found.** By an operator reloading mid-request during a live
attended run, then confirming precisely what they meant by "reload" through
a direct question rather than assuming -- the answer ("I pressed reload
myself," not an auto-refresh) is what pointed at a real request-timing race
instead of a rendering or caching issue, which the previous three attempts
(D25's stale screenshot, D28's crash, and the caching/redirect work before
that) had already ruled out one at a time.

## D30 — Auto-refresh is switched off entirely once a card is showing

**Decision.** The console's `<meta http-equiv="refresh">` tag is only ever
emitted while idle (no open interventions, waiting for the next one to
appear). The moment any escalation card is on screen -- whether unclaimed or
already in the operator's control -- the tag is omitted entirely.

**Why.** Found live, the fifth distinct bug from the same reported symptom
this session: an operator's "Take control" click produced no server-side
effect at all -- no error, no crash, nothing in the evidence log, meaning the
POST request never reached the handler. The console's escalation cards were
rendered on a 3-second auto-refresh. A browser can only run one navigation at
a time, and a form submission is a navigation: if the meta-refresh's
scheduled reload lands while a POST is still in flight, the reload wins and
the click's own request is abandoned client-side before it is ever sent to
the server. A realistic human's time to read an escalation card and click a
button -- a second or two -- sits squarely inside a 3-second period, which
makes this collision close to the default outcome for a real person, not a
rare coincidence. D25 through D29 each fixed something real (a stale
screenshot, a crash on a duplicate request, an ordering bug, a slow
response), and every one of them was necessary -- but none of them could have
fixed a click that never left the browser in the first place, which is why
the same complaint kept surviving each fix.

Auto-refresh while a card is showing was never buying anything besides this
risk: nothing on an open card changes on its own. It exists to change
between requests -- the operator acts, and every action already lands on a
freshly rendered page via its own redirect. The only genuine use for polling
is noticing a *new* escalation while looking at the idle "no open
interventions" screen, which is exactly the one case this keeps.

**Cost.** None found. An operator who leaves a card open and does nothing
will not see it self-update, but nothing on it was ever going to change
without their own action regardless.

**How it was found.** By checking the one thing all four prior fixes hadn't:
whether the click's request reached the server at all. The evidence log's
`control.granted` line is unconditional the moment a handoff actually
executes; its total absence, combined with the exact refresh period lining
up with realistic human reaction time, pointed at a navigation race rather
than anything server-side left to fix.

## D31 — MOCKAPP_HOST/PORT and CONSOLE_PORT are honoured everywhere, not just some places

**Decision.** Every place a port could be hardcoded now instead defaults from
the environment, consistently: `cua/cli.py`'s `--target` and `--console-port`
defaults, `mockapp/app.py`'s own launcher (which now actually calls
`load_dotenv()`), `cua/policy/engine.py`'s origin allowlist (additively, on
top of whatever `policy.yaml` already lists), and the integration test
fixtures' target URL and allowlist expectations.

**Why.** 8080/8081 are common enough that a developer running
this alongside other local work may already have them taken. Changing
`MOCKAPP_PORT` in `.env` looked like it should be enough -- `.env.example`
documents it -- but four different places each had their own idea of the
port, independently:

1. `mockapp/app.py`'s launcher read `os.environ` directly and never called
   `load_dotenv()`, so `.env`'s value never reached it at all; the app kept
   silently starting on 8080 regardless of what was configured.
2. `cua/cli.py`'s `--target`/`--console-port` defaults were literal strings,
   not environment-derived, so every invocation needed the new port passed
   by hand.
3. `cua/policy/engine.py`'s origin allowlist is loaded once from
   `policy.yaml`, which lists the mock app's origin explicitly for a
   reviewer cloning fresh -- correct for that case, but with nothing to keep
   it in sync with a locally-changed port, navigating to the new origin was
   DENIED outright rather than reaching the action being tested.
4. The integration tests hardcoded `127.0.0.1:8080` for both "is the app
   running" detection and the fixture capability's entry point, so they
   would have silently skipped (or run against the wrong port) rather than
   testing anything real.

Each of these was found by actually trying to change the port and watching
something fail in a different way than expected, not by auditing for the
pattern up front.

**Cost.** None found. Every default is unchanged when the environment
variable is unset; this only removes places that used to ignore it.

**How it was found.** By a real request to move off 8080/8081 because they
were needed for something else -- the kind of environment friction no test
suite surfaces on its own, since the suite's own defaults matched the
hardcoded values everywhere.

## D32 — A senior-engineer pass over the codebase

**Decision.** Four parallel reviews (reuse, simplification, efficiency,
altitude) over `cua/` and `mockapp/`, followed by fixing everything that
passed the bar. One genuine bug, six pieces of dead code, three efficiency
hoists in hot paths, and two cross-cutting consolidations landed; a few
plausible-looking findings were deliberately left alone.

**What was fixed:**
- `ReplayEngine._escalations` (`replay/engine.py`) was declared and read in
  three places -- `ReplayResult.escalations`, `FailureReport.escalated`, the
  per-escalation screenshot label -- but nothing ever appended to it.
  `result.escalations` was silently always `[]`, `escalated` was silently
  always `False`, and a second escalation in one run overwrote the first
  one's screenshot. Not found by any existing test; the integration test
  that exercises `_escalate()` for real never asserted on the result it got
  back. Fixed and now asserted on.
- Six dead members removed after grepping every caller: `CapabilityCatalog.
  openai_tools()`, `launch_web_surface()` (plus the unused `asyncio` it
  existed to smuggle into `web.py`'s `__all__`), `EvidenceRecorder.note_ref()`,
  `slugify()`.
- Three loop-invariant/independent-work hoists: `tool_definitions()` was
  rebuilt from scratch on every discovery step despite being constant for
  the whole run; `_rung_candidates()` re-normalized the loop-invariant
  target string inside four separate per-element comprehensions; `observe()`
  and `_settle()` in `web.py` walked a frameset's frames with one sequential
  `await` per frame where each frame's CDP round trip has no dependency on
  any other's, on what is the hot path for every discovery and replay step.
- Two consolidations, both independently flagged by more than one review
  angle: `MOCKAPP_HOST`/`MOCKAPP_PORT`/`CONSOLE_PORT` were each read from the
  environment independently in `cli.py`, `policy/engine.py`, and the test
  fixtures (three copies of the exact drift D31 had just fixed once), now
  collected into `cua/config.py`. Deliberately *not* shared with
  `mockapp/app.py` -- that module represents the target application, which
  this project's own layering keeps foreign to `cua`; the two sides agree on
  an env-var convention, not on shared code, and importing `cua` into the
  thing `cua` automates would invert that on purpose. Separately,
  `AnthropicProvider` and `OpenAIProvider` each implemented the same
  "explicit arg, else env var, else default" precedence by hand for their
  key/model/base-url; both now call one `resolve_setting()` in `llm/base.py`.

**What was deliberately left alone**, and why:
- `ControlToken.wait_until_resumed()`/`_resumed` (`session/control.py`) --
  unused by the current broker (which needed a return value an `Event`
  can't carry, so it built its own wait on a `Future`), but it is a
  legitimate, directly-tested general primitive on the control FSM, not
  accidental cruft. `EscalationBroker.take_control()` (D25/D28/D29) reads
  coherently as one operation, each of its three point-fixes addressing a
  genuinely distinct failure -- not treated as a rewrite candidate.
- The rate-limit retry (D24) and the wall-clock human-time exclusion (D27)
  solve structurally different problems (bounded retry-or-fail vs.
  unbounded-wait time-exclusion) and were kept separate rather than forced
  into one shared abstraction.
- Schema-level enforcement of "no caller literal reaches a string field" (a
  `field_validator` replacing the three `_scrub_text` call sites) is a real,
  defensible idea, but a bigger and riskier change to a safety-critical
  mechanism than the rest of this pass warranted than the rest of this pass -- left as a
  documented option, not attempted here.
- `SESSION_UNRECOVERABLE` (never produced), `ControlToken.human_action_count`
  (write-only, duplicating `len(SessionManager.human_actions)`), and
  `CapabilityRecorder.record()`'s `input_specs`/`product_version` parameters
  (only exercised from tests) were each judged low-value, non-zero-risk
  touches to code that is either a deliberate taxonomy placeholder or already
  covered by its own tests -- noted rather than changed.

**Cost.** None found beyond the touched files' own diffs; full suite (130
tests, including the two real-browser integration tests) passes, and a live
replay against the approved capability was re-verified end to end after the
`web.py` concurrency changes specifically, since those touch the hot path
every other fix this session depended on being correct.

**How it was found.** Four review agents given the same codebase and
different angles, explicitly instructed to say so rather than manufacture a
finding when a pattern was already the right shape -- two of them did
exactly that for `take_control()` and the retry/wall-clock split, which is
part of why their other findings were trusted enough to act on directly.

## D33 — A selected combobox shows its current value in the prompt

**Decision.** `render_element` (`cua/discovery/prompts.py`) echoes a
control's current `value` for `role in {"textbox", "combobox"}`, not just
`"textbox"`.

**Why.** Found running a deliberately varied test matrix against goals never
tried before: a discovery run picked "Holiday Club" from the Account Type
dropdown, then picked it again, and again -- 17 times across 25 available
steps, never reaching the submit button it had already correctly resolved
and was one click away from. The selection was working; `select_option`
genuinely sets the underlying `<select>`'s value. What was missing was any
way for the model to *see* that it had worked: every prior observation of
that control looked identical to the one before the first selection,
because `render_element` only ever included a control's value in the text
shown to the model when its role was `"textbox"` -- a `<select>` is role
`"combobox"`, so it was silently excluded. The model's own stated reasoning
across all 17 attempts said exactly this: "Account Type still shows
unselected." It wasn't wrong about what it could see. What it could see was
incomplete.

A text field with an unconfirmed value fails visibly and fast -- the field
still looks empty. A dropdown with an unconfirmed value fails by exhausting
the entire step budget on a single control, silently, because there is
nothing else for the model to try that would look any different.

**Cost.** None found. An unselected combobox's value is `""`, which the
existing `if element.value` guard already treats as nothing to show, so the
placeholder ("< select >") state is unaffected.

**How it was found.** By deliberately running discovery against goals and
account types the system had never been asked to handle before, specifically
to find limits rather than re-confirm known-good paths -- this exact
17-select_option pattern had actually appeared once earlier in this same
session, dismissed at the time as the model working through something odd
rather than recognised as a reproducible, fixable bug in what the model was
being shown.

## D34 — CLI output is reconfigured to UTF-8 with a safe fallback

**Decision.** `cli.py` reconfigures `sys.stdout` and `sys.stderr` to
`encoding="utf-8", errors="replace"` once, at import time, rather than
guarding individual `typer.echo()` calls.

**Why.** Found running an injected-500 replay for real: the CLI's own
failure report crashed with `UnicodeEncodeError` while printing the
`observed` text it had just correctly captured -- the target application's
window chrome ("X`□`_", period-accurate faux window controls) contains a
character Windows' default console codepage (cp1252) cannot represent, and
neither `typer.echo()` nor Python's default stdout encoding falls back; they
raise. The run had succeeded at everything that matters -- detecting the
failure, classifying it, capturing the right evidence -- and then the last
step, reporting that back to whoever ran the command, was what broke.

This is a general problem, not a one-line problem: any real application's UI
text is uncontrolled input as far as this CLI's own output is concerned, and
the specific character that broke this run says nothing about which
character breaks the next one. Guarding the one `typer.echo(observed)` call
that happened to crash first would still leave every other line -- an
`intent`, a `summary`, a member name -- exposed to the same class of crash.
Reconfiguring the streams once covers all of them.

**Cost.** A character truly outside Unicode cannot occur (mangled bytes from
a decoding error further upstream are a different, prior problem), so
`errors="replace"` degrading to `?` is not expected to fire in practice; if
it ever does, a `?` in a report is a far better outcome than the whole
command dying on its last line.

**How it was found.** By replaying an injected hard failure for real and
reading past the tail of a truncated terminal panel rather than assuming a
non-zero exit meant only the thing being tested had failed -- the replay
itself worked exactly as intended; the crash was one layer further out, in
the reporting of a result that was already correct.

## D35 — A data-grid cell's label is its column header, not its left neighbour

**Decision.** `labelHint()` (`cua/perception/extract.js`) tells two table
shapes apart by row width. Rows with two cells (a caption and a value,
repeated down a form) keep the existing left-neighbour behaviour unchanged.
Rows with more than two cells that aren't spanning a rowspan (a genuine
multi-column data grid) skip the left-neighbour walk entirely and read the
header cell at the table's actual first row, at the same column position.

**Why.** Found asking a question about a capability that looked fine:
"will asking for a different member's checking balance also just replay?"
It didn't. `cu.member.read_checking_balance` had recorded its Balance cell's
identity as `"0024-1188"` -- the neighbouring *account number* cell's text,
not a label at all -- because `labelHint()` always tried the cell to the
left first, and for a multi-column grid row the cell to the left is another
data value, never a caption. It worked for the one member it was recorded
against purely by coincidence (the resolver matched the literal account
number back to itself) and failed outright, `element_not_found`, for every
other member, since account numbers are unique per record.

The existing "row above" fallback already existed for exactly this shape of
problem, but was gated to never run for a raw table cell at all -- only for
an interactive control embedded in one. Splitting on row width, not element
type, is what turns that dead path into the primary one for grid cells while
leaving every already-correct label/value form (login, search, the
sub-account form, the Balance Summary panel) untouched.

Two corrections landed inside the same fix, both found by actually running
it rather than reasoning about it in the abstract:
- Raw cell *count* over-counted a row carrying a `rowspan` cell for
  something unrelated (this app's search form: a two-cell label/value row
  plus a submit button spanning three rows down the side) -- a rowspan cell
  is never a repeating column, so only cells starting fresh on a row count
  toward its width.
- "The row above" is only the header for a grid's *first* data row -- for
  a second account in a list, the row above is the first account's row, not
  the header, and has the same width. The header is the table's genuine
  first row, however many data rows deep the current one is, not whichever
  row happens to be one sibling up.

**Cost.** None found. The heuristic is right for the common legacy shapes
(paired-row forms, header-plus-columns grids) this project targets, not a
proof for every table shape anyone could author -- the same honest limit
already accepted for the irreversible-action name matcher elsewhere in this
project.

**How it was found.** By being asked, directly, whether a capability that
looked correct would actually generalize to a case nobody had tried -- and
checking rather than assuming, twice over, since the first fix introduced a
real regression (the rowspan miscount) that only running it against the
existing capability suite caught.

## D36 — A row that doesn't exist is a real answer, not a reason to guess

**Decision.** `resolve()` (`cua/targeting/resolver.py`) only falls back from
an empty anchored scope to the whole unfiltered observation when the
descriptor's *structural* anchors (frame, heading, label) matched nothing.
If the structural area is present and only a `CONTAINER_TEXT` anchor came up
empty, the empty scope is respected -- the search is not widened until
something else happens to match.

**Why.** D35's column-header fix solved "which column" but not "which row":
a Balance cell's label is the same ("Balance") for every account a member
has, so a member with both a Savings and a Checking account made the
descriptor genuinely ambiguous. The fix was a `CONTAINER_TEXT` anchor
recording the row's own category (its first peer cell that reads as a label
rather than a value, reusing the same test a generated checkpoint marker
already uses to avoid pinning itself to one record) -- "this row says
Checking" -- which correctly disambiguates a member with several accounts.

But `resolve()`'s existing behaviour on an empty scope is to fall back to
every candidate on the page, written for a different failure shape entirely
(an anchor naming a whole section that drifted off the page, where a better
error message is worth widening the search for). Found live, immediately
after fixing the ambiguity case: a member with *no* checking account at all
-- structural anchors matched fine, only the "Checking" row genuinely didn't
exist -- fell back to every cell on the page, and rung 1 matched that
member's *Savings* balance instead, since it was the only cell left still
named "Balance." The result was `status: success`, with someone else's
account balance silently reported as if it were the one asked for -- worse
than the ambiguity case, which at least refused outright.

A `CONTAINER_TEXT` anchor's whole purpose is to say "does this specific row
exist," and its absence is frequently the correct, final answer -- not
drift to be argued past. Splitting the fallback on anchor kind is what lets
"the section is missing" keep getting a helpful wide-search error message
while "the row doesn't exist" gets an honest `NOT_FOUND` instead of a wrong
answer dressed as a right one.

**Cost.** None found. One existing capability
(`cu.member.read_checking_balance`) does use a `CONTAINER_TEXT` anchor,
being exactly the one D35's fix produced -- re-verified live against this
change specifically (both the disambiguating case, several members with a
Checking account, and the not-found case, a member with none) rather than
assumed unaffected because it predates this entry being written.

A second gap surfaced reviewing this fix itself: `matches_anchors(e, [])`
is vacuously true for every element, so a descriptor carrying *only* a
`CONTAINER_TEXT` anchor (no frame, heading, or label anchor at all) would
have made "the structural area is present" true by construction regardless
of whether anything was actually confirmed -- silently reintroducing the
same wrong-widening this decision exists to prevent, just for a narrower
input shape. Every descriptor this project's own recorder currently
produces always carries at least a `FRAME` anchor, so no existing capability
was actually exposed to it, but the resolver itself did not depend on that
being true. Fixed by requiring at least one real structural anchor before
treating it as confirmed; an empty structural-anchor list now falls back to
the wide search, same as before this decision, rather than skipping it.

**How it was found.** By testing the *previous* fix's edge case
immediately, rather than treating "ambiguity now correctly refused" as the
finish line -- a member with zero matching rows is the natural next case to
check once "more than one matching row" has just been handled, and checking
it caught a silent-wrong-answer bug before it ever reached the committed
capability. The vacuous-anchor gap was found by an independent code review
of this same fix, not by a failing test -- worth noting, since it is exactly
the kind of edge case a hand-written test suite tends to skip when every
real descriptor in the codebase happens to avoid it.
