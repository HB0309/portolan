"""Command line entry points.

    cua discover   run the agent on a goal and record what it learned
    cua replay     execute a recorded capability, with no model involved
    cua catalog    list capabilities as agent-callable tool definitions
    cua invoke     call a capability by name with typed arguments
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from cua.config import CONSOLE_PORT as _DEFAULT_CONSOLE_PORT, MOCKAPP_TARGET as _DEFAULT_TARGET
from cua.discovery import CapabilityRecorder, DiscoveryOrchestrator, InterventionOutcome, new_run_id
from cua.escalation.broker import EscalationBroker
from cua.escalation.models import InterventionRequest, OperatorDecision, ResumeMode
from cua.evidence import EvidenceRecorder
from cua.policy import PolicyEngine
from cua.replay import ReplayEngine, ResumeDecision
from cua.schema.capability import Capability
from cua.schema.overlay import TenantOverlay, resolve as resolve_overlay
from cua.session import SessionManager
from cua.surfaces.web import WebSurface

# The target application is real, uncontrolled UI text -- a legacy app's own
# window chrome ("X<square><underscore>") is exactly the kind of thing that
# ends up quoted back in a failure report. Found live: reporting a captured
# 500-error page's text crashed the whole CLI with UnicodeEncodeError, because
# Windows' console defaults to a codepage (cp1252) that cannot represent most
# of Unicode, and typer.echo() does not fall back -- a result the run itself
# produced correctly then became a crash on the last line meant to report it.
# Reconfiguring stdout/stderr once, here, rather than encoding-guarding every
# individual echo call, is what makes any future line of arbitrary captured
# text safe to print instead of only the ones this specific crash was found in.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(add_completion=False, help=__doc__)

CAPABILITY_DIR = Path("capabilities")
EVIDENCE_DIR = Path("evidence")


def _parse_json(raw: str | None, what: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{what} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter(f"{what} must be a JSON object")
    return value


def _auto_approve_responder(request: InterventionRequest) -> OperatorDecision:
    """Stands in for an operator so the flow can be shown end to end.

    Never appropriate against a real institution -- both discover and replay
    built this same closure independently; one place means the note text
    can't drift between the two.
    """
    return OperatorDecision(mode=ResumeMode.APPROVE, note="auto-approved (unattended demo)")


def _auto_commit_capability(
    path: Path, capability_id: str, run_id: str, provider: str, model: str, steps: int
) -> None:
    """Commit a successfully recorded capability on its own, right away.

    A discovery run used to leave its result sitting as an uncommitted
    working-tree change -- easy to lose track of, easy to overwrite with the
    next run before anyone looked at it, and one more thing to remember to
    stage. This never touches anything but the one capability file this run
    just produced; evidence and everything else stay exactly as uncommitted
    as before, since only the capability is the thing worth a checkpoint.

    Best-effort: not being in a git repo, git not being on PATH, or nothing
    actually changing (a rerun that reproduces byte-identical output) are all
    reported, not raised -- a discovery run that succeeded should not fail
    the whole command over a commit.
    """
    try:
        subprocess.run(
            ["git", "add", "--", str(path)], check=True, capture_output=True, text=True
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", str(path)], capture_output=True
        )
        if staged.returncode == 0:
            return  # nothing changed -- a rerun that reproduced the same recording
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"Record {capability_id} from a live discovery run\n\n"
                f"run {run_id}, {provider}/{model}, {steps} steps",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        typer.echo(f"  committed: {path}")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        typer.echo(f"  (not committed automatically: {detail})")


def _load_capability(path: Path) -> Capability:
    if not path.exists():
        raise typer.BadParameter(f"no capability at {path}")
    return Capability.model_validate_json(path.read_text(encoding="utf-8"))


def _echo_result(result: Any) -> None:
    typer.echo("")
    typer.echo(f"  status : {result.status.value}")
    typer.echo(f"  {result.summary_line()}")
    if getattr(result, "steps", None):
        degraded = result.degraded_steps()
        rungs = ", ".join(
            f"{t.step_id}=rung{t.resolution.rung}" for t in result.steps if t.resolution
        )
        typer.echo(f"  targeting : {rungs}")
        if degraded:
            typer.echo(
                f"  DEGRADED  : {len(degraded)} step(s) resolved below their confidence "
                f"floor - a drift signal worth reviewing"
            )
    if result.recoveries:
        for rec in result.recoveries:
            typer.echo(
                f"  recovery  : {rec.policy_id} on {rec.step_id} "
                f"({'applied' if rec.succeeded else 'exhausted'})"
            )
    if result.failure:
        typer.echo(f"  expected  : {result.failure.expected}")
        observed = " ".join(result.failure.observed.split())
        typer.echo(f"  observed  : {observed[:200]}")
    typer.echo(f"  evidence  : {EVIDENCE_DIR / result.run_id}")
    typer.echo("")


# ---------------------------------------------------------------------------


@app.command()
def discover(
    goal: str = typer.Option(..., help="What to accomplish, in plain language."),
    target: str = typer.Option(_DEFAULT_TARGET, help="Entry point URL."),
    inputs: str = typer.Option("{}", help='JSON object of declared inputs.'),
    outputs: str = typer.Option("", help="Comma-separated names of values to collect."),
    capability_id: str = typer.Option(..., help="Dotted id to save the capability under."),
    name: str = typer.Option("", help="Human-readable capability name."),
    description: str = typer.Option("", help="Description written for a calling agent."),
    provider: str = typer.Option("anthropic", help="anthropic | openai | scripted"),
    model: str = typer.Option("", help="Override the provider's default model."),
    script: str = typer.Option(
        "", help="With --provider scripted: which built-in script to run."
    ),
    product: str = typer.Option("legacy-cu", help="Vendor product this is recorded against."),
    sensitive: str = typer.Option("", help="Comma-separated inputs to treat as sensitive."),
    headless: bool = typer.Option(False, help="Run without a visible browser window."),
    attended: bool = typer.Option(
        False, help="Serve the operator console and wait for a human on escalation."
    ),
    auto_approve: bool = typer.Option(
        False,
        help=(
            "Unattended demo: approve interventions automatically. Stands in for an "
            "operator so the flow can be shown end to end; never appropriate against "
            "a real institution."
        ),
    ),
    console_port: int = typer.Option(
        _DEFAULT_CONSOLE_PORT, help="Port for the operator console."
    ),
) -> None:
    """Run the agent on a goal and record the successful flow as a capability."""
    input_map = {k: str(v) for k, v in _parse_json(inputs, "--inputs").items()}
    output_names = [o.strip() for o in outputs.split(",") if o.strip()]
    sensitive_names = {s.strip() for s in sensitive.split(",") if s.strip()}

    if provider == "scripted":
        from cua.demo.provider import ScriptedRunnerProvider
        from cua.demo.scripts import SCRIPTS

        if script not in SCRIPTS:
            raise typer.BadParameter(
                f"--script must be one of {sorted(SCRIPTS)} when using the scripted provider"
            )
        llm: Any = ScriptedRunnerProvider(SCRIPTS[script], input_map)
        typer.echo(
            "  note: the scripted provider replays a fixed flow. It exercises the loop, "
            "the guardrails and the recorder, but it does not demonstrate discovery."
        )
    else:
        from cua.llm import build_provider

        llm = build_provider(provider, model=model or None)

    async def go() -> int:
        policy = PolicyEngine.load()
        run_id = new_run_id("discovery")
        evidence = EvidenceRecorder(run_id, root=EVIDENCE_DIR, redactor=policy.redactor)
        surface = await WebSurface.launch(headless=headless)
        session = SessionManager(surface=surface, evidence=evidence)
        responder = _auto_approve_responder if auto_approve else None
        broker = EscalationBroker(
            session, evidence, run_id=run_id, auto_responder=responder, attended=attended
        )

        async def intervene(reason: str, context: dict[str, Any]) -> InterventionOutcome:
            if attended:
                typer.echo(
                    f"\n  escalation: {reason} - respond at "
                    f"http://127.0.0.1:{console_port}"
                )
            elif auto_approve:
                typer.echo(f"\n  escalation: {reason} - auto-approved (unattended demo)")
            else:
                typer.echo(
                    f"\n  escalation: {reason} - recorded, but this run is unattended "
                    f"so the action is refused"
                )
            decision = await broker.raise_intervention(reason, context)
            return InterventionOutcome(
                approved=decision.mode in {ResumeMode.APPROVE, ResumeMode.PERFORMED_MANUALLY},
                performed_by_human=decision.mode is ResumeMode.PERFORMED_MANUALLY,
            )

        server = None
        if attended:
            server = await _serve_console(broker, console_port)
            typer.echo(f"  operator console: http://127.0.0.1:{console_port}")

        try:
            orchestrator = DiscoveryOrchestrator(
                surface, llm, policy, evidence, on_intervention=intervene
            )
            result, trace = await orchestrator.run(
                goal=goal, entry_point=target, inputs=input_map, outputs=output_names
            )

            if not result.succeeded:
                evidence.write_json("result.json", result)
                typer.echo(f"\n  discovery failed: {result.summary_line()}")
                typer.echo(f"  evidence: {EVIDENCE_DIR / run_id}\n")
                return 1

            recorder = CapabilityRecorder(product, sensitive_inputs=sensitive_names)
            capability = recorder.record(
                trace,
                capability_id=capability_id,
                name=name or capability_id,
                description=description or goal,
                run_id=run_id,
                provider=llm.name,
                model=llm.model,
                human_assisted=bool(session.human_actions),
            )

            CAPABILITY_DIR.mkdir(exist_ok=True)
            path = CAPABILITY_DIR / f"{capability_id}.json"
            path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")
            evidence.write_json("capability.json", capability)
            _auto_commit_capability(
                path, capability_id, run_id, llm.name, llm.model, len(capability.steps)
            )

            # Record what the run produced, so the evidence directory says which
            # artifact came out of it rather than leaving that to the filename.
            result.capability_id = capability.capability_id
            result.capability_path = str(path)
            evidence.write_json("result.json", result)

            typer.echo(f"\n  {result.summary_line()}")
            typer.echo(f"  capability: {path}")
            typer.echo(
                f"  {len(capability.steps)} steps, "
                f"{len(capability.inputs)} input(s), {len(capability.outputs)} output(s), "
                f"{len(capability.outcomes)} declared outcome(s)"
            )
            typer.echo(f"  tokens: {result.input_tokens} in / {result.output_tokens} out")
            if trace.collected:
                # Terminal-only, for whoever just watched the run: the
                # capability itself never gets this, by design (Hard Rule #6 --
                # caller data is never a literal in the artifact). Printing it
                # here is the only place a value collected during discovery is
                # visible at all; otherwise it is captured, used to prove the
                # run succeeded, and then discarded when the process exits.
                typer.echo(f"  collected: {trace.collected}")
            typer.echo(f"  evidence: {EVIDENCE_DIR / run_id}\n")
            return 0
        finally:
            if server is not None:
                server.should_exit = True
            await surface.close()

    raise typer.Exit(asyncio.run(go()))


@app.command()
def replay(
    capability: Path = typer.Option(..., help="Path to a saved capability JSON."),
    inputs: str = typer.Option("{}", help="JSON object of arguments."),
    tenant: Path = typer.Option(None, help="Optional tenant overlay JSON."),
    entry: str = typer.Option("", help="Override the entry point (used to inject faults)."),
    attended: bool = typer.Option(
        False, help="Serve the operator console and wait for a human on escalation."
    ),
    auto_approve: bool = typer.Option(
        False, help="Unattended demo: approve interventions automatically."
    ),
    headless: bool = typer.Option(False, help="Run without a visible browser window."),
    console_port: int = typer.Option(
        _DEFAULT_CONSOLE_PORT, help="Port for the operator console."
    ),
) -> None:
    """Execute a recorded capability deterministically. No model is consulted."""
    cap = _load_capability(capability)
    argument_map = _parse_json(inputs, "--inputs")

    overlay = None
    if tenant is not None:
        overlay = TenantOverlay.model_validate_json(tenant.read_text(encoding="utf-8"))
        cap = resolve_overlay(cap, overlay)

    if entry:
        cap = cap.model_copy(
            update={"surface": cap.surface.model_copy(update={"entry_point": entry})}
        )

    async def go() -> int:
        policy = PolicyEngine.load()
        run_id = new_run_id("replay")
        evidence = EvidenceRecorder(run_id, root=EVIDENCE_DIR, redactor=policy.redactor)
        surface = await WebSurface.launch(headless=headless)
        session = SessionManager(surface=surface, evidence=evidence)

        responder = _auto_approve_responder if auto_approve else None
        broker = EscalationBroker(
            session, evidence, run_id=run_id, auto_responder=responder, attended=attended
        )
        server = None
        if attended:
            server = await _serve_console(broker, console_port)
            typer.echo(f"  operator console: http://127.0.0.1:{console_port}")

        async def escalate(reason: str, context: dict[str, Any]) -> ResumeDecision:
            typer.echo(f"\n  escalation raised: {reason}")
            if attended:
                typer.echo(f"  waiting for an operator at http://127.0.0.1:{console_port}")
            decision = await broker.raise_intervention(reason, context)
            return ResumeDecision(
                mode=_resume_mode(decision.mode),
                note=decision.note,
                human_actions=int(decision.extra.get("human_actions", 0)),
            )

        # Only offer an escalation channel when there is genuinely someone on the
        # other end. An unattended run with nobody watching should fail fast with
        # a debuggable report, not sit for fifteen minutes waiting for an
        # operator who was never coming.
        escalation_hook = escalate if (attended or auto_approve) else None

        try:
            engine = ReplayEngine(
                surface=surface,
                policy=policy,
                evidence=evidence,
                on_escalation=escalation_hook,
                tenant_id=overlay.tenant_id if overlay else None,
            )
            result = await engine.run(cap, argument_map)
            _echo_result(result)
            return 0 if result.ok else 1
        finally:
            if server is not None:
                server.should_exit = True
            await surface.close()

    raise typer.Exit(asyncio.run(go()))


@app.command()
def catalog(
    directory: Path = typer.Option(CAPABILITY_DIR, help="Where capabilities live."),
    as_tools: bool = typer.Option(False, help="Emit provider tool definitions as JSON."),
) -> None:
    """List saved capabilities as an agent would discover them."""
    from cua.catalog import CapabilityCatalog

    cat = CapabilityCatalog.load(directory)
    if not cat.capabilities:
        typer.echo(f"  no capabilities in {directory}")
        raise typer.Exit(0)

    if as_tools:
        typer.echo(json.dumps(cat.tool_definitions(), indent=2))
        raise typer.Exit(0)

    for capability in cat.capabilities:
        typer.echo(f"\n  {capability.capability_id}  (v{capability.version}, {capability.approval})")
        typer.echo(f"    {capability.description}")
        args = ", ".join(
            f"{p.name}: {p.type.value}{'' if p.required else '?'}" for p in capability.inputs
        )
        returns = ", ".join(f"{o.name}: {o.type.value}" for o in capability.outputs)
        typer.echo(f"    ({args}) -> {{{returns}}}")
        if capability.outcomes:
            typer.echo(f"    outcomes: {', '.join(o.code for o in capability.outcomes)}")
    typer.echo("")


@app.command()
def invoke(
    capability_id: str = typer.Argument(..., help="The capability to call."),
    args: str = typer.Option("{}", help="JSON object of arguments."),
    directory: Path = typer.Option(CAPABILITY_DIR),
    headless: bool = typer.Option(True),
) -> None:
    """Call a capability by name, the way an agent would."""
    from cua.catalog import CapabilityCatalog

    cat = CapabilityCatalog.load(directory)
    capability = cat.get(capability_id)
    if capability is None:
        typer.echo(f"  no capability named {capability_id!r}")
        raise typer.Exit(1)

    path = directory / f"{capability_id}.json"
    replay(
        capability=path,
        inputs=args,
        tenant=None,
        entry="",
        attended=False,
        auto_approve=False,
        headless=headless,
        console_port=_DEFAULT_CONSOLE_PORT,
    )


def _resume_mode(mode: ResumeMode) -> str:
    return {
        ResumeMode.APPROVE: "continue",
        ResumeMode.PERFORMED_MANUALLY: "continue",
        ResumeMode.RETRY_STEP: "retry_step",
        ResumeMode.SKIP_STEP: "skip_step",
        ResumeMode.ABORT: "abort",
    }[mode]


async def _serve_console(broker: EscalationBroker, port: int):
    import uvicorn

    from cua.escalation.console import build_console

    config = uvicorn.Config(
        build_console(broker),
        host="127.0.0.1",
        port=port,
        log_level="error",
        # The console has no startup or shutdown hooks, and leaving the lifespan
        # protocol on means cancelling the server at the end of a run prints a
        # CancelledError traceback over the result the operator is trying to
        # read. Nothing is wrong when that happens, which is exactly why it
        # should not look like something is.
        lifespan="off",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    await asyncio.sleep(0.4)
    return server


def main() -> None:
    app()


if __name__ == "__main__":
    main()
