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

from fastapi import FastAPI, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse

# A GET response with no Cache-Control and no Last-Modified is not supposed to
# be cached per RFC 7234's heuristic-freshness rules -- but "not supposed to"
# is not the same guarantee as an explicit header, and this page's whole job
# is to show the operator state that changes underneath them every few
# seconds. Silently serving a stale copy of "/" here reads as exactly the
# "I clicked it and nothing happened" symptom this console keeps getting
# reported for. Cheap to rule out outright rather than keep suspecting it.
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

from cua.escalation.broker import EscalationBroker
from cua.escalation.models import InterventionStatus, OperatorDecision, ResumeMode

_PAGE = """<!doctype html>
<html><head><title>Operator console</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
{refresh_tag}
<style>
  :root {{ color-scheme: dark; }}
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
  form {{ display: inline-block; }}
  button {{ -webkit-appearance: none; appearance: none; font: inherit; font-size: 14px;
            font-weight: 600; padding: 10px 16px; margin: 12px 6px 0 0; min-width: 140px;
            border-radius: 4px; border: 1px solid #46566a; background: #22303d;
            color: #f2f6fa; cursor: pointer; }}
  button.primary {{ background: #1f6feb; border-color: #4d94ff; color: #ffffff; }}
  button.danger {{ background: #6e2733; border-color: #d1697b; color: #ffffff; }}
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
        # Auto-refresh only while idle, waiting for the *next* escalation to
        # appear -- there is nothing else to wait for passively there, so
        # nothing a reload can interrupt. The instant a card is showing, this
        # is switched off entirely.
        #
        # Found live: with a card showing, on a short refresh, the operator's
        # own click raced the timer -- a human's realistic time to read the
        # card and click "Take control" is a couple of seconds, squarely
        # inside what a 3s refresh needs to fire again, and a browser can only
        # run one navigation at a time. When both land close together the
        # scheduled reload wins and the click's own POST is abandoned before
        # it reaches the server -- nothing logged, nothing broken, the button
        # simply never fired. That is not a rare coincidence at a 3s period;
        # ordinary human reaction time sits right in the collision zone. Once
        # a card is up, refreshing serves no purpose anyway -- nothing on it
        # changes until the operator acts through one of its own buttons,
        # which itself is a fresh page load.
        refresh_tag = "" if open_requests else '<meta http-equiv="refresh" content="5">'

        if not open_requests:
            body = (
                '<div class="card"><div class="idle">No open interventions. '
                "The automation is running unattended.</div></div>"
            )
            return _PAGE.format(
                refresh_tag=refresh_tag,
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

            in_control = request.status is not InterventionStatus.OPEN

            # Exactly one set of actions is on screen at a time, matched to
            # what is actually true right now. Showing "I did it - resume"
            # before control has been taken invites clicking a lie -- there is
            # nothing to have done yet -- and showing "Take control" once it
            # has already been taken invites taking it a second time. Every
            # button visible in either state does something that is actually
            # possible in that state.
            if not in_control:
                controls = (
                    f'<form method="post" action="/take/{request.id}">'
                    f'<button class="primary">Take control of the live session</button>'
                    f"</form>"
                )
                visible_modes = [
                    m for m in request.resume_options if m is not ResumeMode.PERFORMED_MANUALLY
                ]
            else:
                controls = (
                    '<div class="live">You have the live session. Do what is needed in '
                    "the browser window, then tell the run how to continue.</div>"
                )
                visible_modes = [
                    m
                    for m in request.resume_options
                    if m not in (ResumeMode.APPROVE, ResumeMode.PERFORMED_MANUALLY)
                ]
                if ResumeMode.PERFORMED_MANUALLY in request.resume_options:
                    visible_modes.insert(0, ResumeMode.PERFORMED_MANUALLY)

            buttons = "".join(
                f'<form method="post" action="/resolve/{request.id}">'
                f'<input type="hidden" name="mode" value="{mode.value}">'
                f'<button class="{_button_class(mode)}">{_label(mode)}</button></form>'
                for mode in visible_modes
            )

            chunks.append(
                f'<div class="card"><div class="reason">{_escape(request.reason)}</div>'
                f"<table>{rows}</table>{shot}<div>{controls}</div>"
                f"<div>{buttons}</div></div>"
            )

        return _PAGE.format(
            refresh_tag=refresh_tag,
            run_id=broker.run_id,
            owner=broker.session.owner.value,
            body="".join(chunks),
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return HTMLResponse(render(), headers=_NO_STORE)

    @app.post("/take/{intervention_id}")
    async def take(intervention_id: str) -> RedirectResponse:
        # Redirect back to "/" rather than rendering the result of the POST
        # directly. Direct-rendering was tried first, on the theory that a
        # redirect's extra round trip reads as "the button did nothing" --
        # but it has a worse problem: it leaves the browser's address bar
        # sitting on this POST-only route, where the page's own meta-refresh
        # tag (content="{refresh}", no explicit url -- refreshes whatever URL
        # is current) then issues a GET against a route with no GET handler.
        # That GET fails silently, auto-refresh stops working from this point
        # on, and the page looks frozen until the operator manually navigates
        # back to "/" -- exactly the "I still had to reload" symptom this is
        # fixing. A 303 redirect keeps the address bar on "/", where
        # auto-refresh keeps working, and happens fast enough on localhost
        # that the "gap" the direct-render was solving for was never real.
        await broker.take_control(intervention_id)
        return RedirectResponse("/", status_code=303, headers=_NO_STORE)

    @app.post("/resolve/{intervention_id}")
    async def resolve(intervention_id: str, mode: str = Form(...)) -> RedirectResponse:
        broker.resolve(
            intervention_id,
            OperatorDecision(mode=ResumeMode(mode), note="resolved from the console"),
        )
        return RedirectResponse("/", status_code=303, headers=_NO_STORE)

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
