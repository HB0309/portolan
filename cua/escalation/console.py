"""The operator console.

Deliberately bare. A real co-browsing console is out of scope for this project,
and building a polished one would say nothing about the part that matters. What
has to be real is the *control-transfer model*: the automation genuinely stops,
the person genuinely gets the session it was using, what they do is genuinely
recorded, and the automation genuinely re-checks its expectations before it
starts acting again. That is all real. The styling is not.

The console runs in the same process as the run it serves, so "show me the live
session" is a screenshot of the very page the automation is parked on, and
"take control" hands over a headed browser window the operator can click in
directly.
"""

from __future__ import annotations

import base64
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from cua.escalation.broker import EscalationBroker
from cua.escalation.models import InterventionStatus, OperatorDecision, ResumeMode

_PAGE = """<!doctype html>
<html><head><title>Operator console</title>
<meta http-equiv="refresh" content="{refresh}">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #11151a; color: #e6edf3; }}
  header {{ background: #1b2530; padding: 12px 20px; border-bottom: 1px solid #2c3a48; }}
  h1 {{ font-size: 15px; margin: 0; font-weight: 600; }}
  .sub {{ font-size: 12px; color: #8b98a5; margin-top: 3px; }}
  main {{ padding: 20px; max-width: 1000px; }}
  .card {{ background: #161c24; border: 1px solid #2c3a48; border-radius: 6px;
           padding: 16px; margin-bottom: 16px; }}
  .reason {{ font-size: 14px; font-weight: 600; color: #ffb454; margin-bottom: 10px; }}
  table {{ border-collapse: collapse; font-size: 13px; width: 100%; }}
  td {{ padding: 4px 10px 4px 0; vertical-align: top; }}
  td.k {{ color: #8b98a5; width: 150px; white-space: nowrap; }}
  img {{ max-width: 100%; border: 1px solid #2c3a48; border-radius: 4px; margin-top: 12px; }}
  form {{ display: inline; }}
  button {{ font: inherit; font-size: 13px; padding: 7px 13px; margin: 12px 6px 0 0;
            border-radius: 4px; border: 1px solid #2c3a48; background: #22303d;
            color: #e6edf3; cursor: pointer; }}
  button.primary {{ background: #1f6feb; border-color: #1f6feb; }}
  button.danger {{ background: #6e2733; border-color: #8b3a47; }}
  .idle {{ color: #8b98a5; font-size: 13px; }}
  .live {{ color: #3fb950; font-size: 12px; }}
  code {{ background: #0d1117; padding: 1px 5px; border-radius: 3px; font-size: 12px; }}
</style></head>
<body>
<header>
  <h1>Operator console</h1>
  <div class="sub">run {run_id} &middot; control: <b>{owner}</b></div>
</header>
<main>{body}</main>
</body></html>
"""


def build_console(broker: EscalationBroker) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def render() -> str:
        open_requests = broker.open_requests
        refresh = 3 if open_requests else 5

        if not open_requests:
            body = (
                '<div class="card"><div class="idle">No open interventions. '
                "The automation is running unattended.</div></div>"
            )
            return _PAGE.format(
                refresh=refresh,
                run_id=broker.run_id,
                owner=broker.session.owner.value,
                body=body,
            )

        chunks = []
        for request in open_requests:
            rows = "".join(
                f'<tr><td class="k">{key}</td><td>{_escape(value)}</td></tr>'
                for key, value in request.context_lines()
            )

            shot = ""
            if request.screenshot and Path(request.screenshot).exists():
                encoded = base64.b64encode(Path(request.screenshot).read_bytes()).decode()
                shot = f'<img src="data:image/png;base64,{encoded}" alt="live session">'

            if request.status is InterventionStatus.OPEN:
                controls = (
                    f'<form method="post" action="/take/{request.id}">'
                    f'<button class="primary">Take control of the live session</button>'
                    f"</form>"
                )
            else:
                controls = (
                    '<div class="live">You have the live session. Do what is needed in '
                    "the browser window, then tell the run how to continue.</div>"
                )

            buttons = "".join(
                f'<form method="post" action="/resolve/{request.id}">'
                f'<input type="hidden" name="mode" value="{mode.value}">'
                f'<button class="{_button_class(mode)}">{_label(mode)}</button></form>'
                for mode in request.resume_options
            )

            chunks.append(
                f'<div class="card"><div class="reason">{_escape(request.reason)}</div>'
                f"<table>{rows}</table>{shot}<div>{controls}</div>"
                f"<div>{buttons}</div></div>"
            )

        return _PAGE.format(
            refresh=refresh,
            run_id=broker.run_id,
            owner=broker.session.owner.value,
            body="".join(chunks),
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return render()

    @app.post("/take/{intervention_id}")
    async def take(intervention_id: str) -> RedirectResponse:
        await broker.take_control(intervention_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/resolve/{intervention_id}")
    async def resolve(intervention_id: str, mode: str = Form(...)) -> RedirectResponse:
        broker.resolve(
            intervention_id,
            OperatorDecision(mode=ResumeMode(mode), note="resolved from the console"),
        )
        return RedirectResponse("/", status_code=303)

    return app


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _label(mode: ResumeMode) -> str:
    return {
        ResumeMode.APPROVE: "Approve and continue",
        ResumeMode.PERFORMED_MANUALLY: "I did it - resume",
        ResumeMode.RETRY_STEP: "Retry the step",
        ResumeMode.SKIP_STEP: "Skip the step",
        ResumeMode.ABORT: "Abort the run",
    }[mode]


def _button_class(mode: ResumeMode) -> str:
    if mode is ResumeMode.ABORT:
        return "danger"
    if mode in (ResumeMode.APPROVE, ResumeMode.PERFORMED_MANUALLY):
        return "primary"
    return ""
