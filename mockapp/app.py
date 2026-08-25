"""The legacy-cu application.

Flows:

    /              frameset: nav + content
    /login         mock sign-in (any non-empty operator id works)
    /search        member lookup
    /member        member detail, accounts, savings balance
    /subaccount    open a sub-account -> review -> confirmation

State is held in a signed-cookie-free session dict keyed by a cookie, which is
plenty for a stand-in and keeps the app dependency-light.

Injection is set once via ``?inject=`` on any request and then persists in the
session, so a capability can be replayed against a chosen runtime condition by
navigating to the entry point with the flag set.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mockapp.data import (
    DEFAULT_TENANT,
    INJECTIONS,
    MEMBERS,
    RESTRICTED_MEMBER_IDS,
    SUBACCOUNT_TYPES,
    TENANTS,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="legacy-cu", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

SESSION_COOKIE = "CORESERVSESSID"
_SESSIONS: dict[str, dict[str, Any]] = {}


def _gid(prefix: str = "txt") -> str:
    """A framework-style element id, regenerated on every render.

    This is the single most important hostile detail in the app. Any automation
    that records ``#ctl00_wpz_ctl03_txt7`` will work exactly once. It is how
    these applications really behave, and it is why the capability schema
    records semantic descriptors instead of selectors.
    """
    return f"ctl00_wpz_ctl{secrets.randbelow(90) + 10}_{prefix}{secrets.randbelow(90) + 10}"


templates.env.globals["gid"] = _gid


def _session(request: Request) -> dict[str, Any]:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid or sid not in _SESSIONS:
        sid = uuid.uuid4().hex
        _SESSIONS[sid] = {"authenticated": False, "inject": "none", "tenant": DEFAULT_TENANT}
    return _SESSIONS[sid] | {"__sid": sid}


def _persist(response: Response, session: dict[str, Any]) -> Response:
    sid = session.pop("__sid", None) or uuid.uuid4().hex
    _SESSIONS[sid] = {k: v for k, v in session.items() if not k.startswith("__")}
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return response


def _apply_query_flags(request: Request, session: dict[str, Any]) -> None:
    inject = request.query_params.get("inject")
    if inject is not None and inject in INJECTIONS:
        session["inject"] = inject
        session["injection_armed"] = True
    tenant = request.query_params.get("tenant")
    if tenant in TENANTS:
        session["tenant"] = tenant


def _viewstate() -> str:
    """An inert stand-in for the framework's view state.

    It carries nothing, but its presence is the reason a page in this class of
    application cannot be reached by constructing a URL: navigation is a form
    post that echoes this blob back, so every screen is downstream of the one
    before it.
    """
    return "/wEPDwUKM" + base64.b64encode(secrets.token_bytes(48)).decode().rstrip("=")


def _ctx(request: Request, session: dict[str, Any], **extra: Any) -> dict[str, Any]:
    tenant = TENANTS[session.get("tenant", DEFAULT_TENANT)]
    return {
        "request": request,
        "t": tenant,
        "session": session,
        "viewstate": _viewstate(),
        **extra,
    }


def _render(
    request: Request, session: dict[str, Any], template: str, status: int = 200, **extra: Any
) -> Response:
    response = templates.TemplateResponse(
        request, template, _ctx(request, session, **extra), status_code=status
    )
    return _persist(response, session)


def _maybe_slow(session: dict[str, Any]) -> None:
    """Transient slowness. Enough to exercise waits, not enough to hang a demo."""
    if session.get("inject") == "slow_load":
        time.sleep(2.5)


def _session_expired(session: dict[str, Any], step: str) -> bool:
    """Expire the session once, at a chosen point mid-flow.

    Firing once rather than every request is deliberate: a recovery policy that
    re-authenticates should be able to succeed, which is the behaviour worth
    demonstrating.
    """
    if session.get("inject") != "session_expiry":
        return False
    if session.get("expired_at_step"):
        return False
    if step == "member":
        session["expired_at_step"] = step
        session["authenticated"] = False
        return True
    return False


def _needs_interstitial(session: dict[str, Any], step: str) -> bool:
    if session.get("inject") != "interstitial":
        return False
    if session.get("interstitial_dismissed"):
        return False
    return step == "member"


# ---------------------------------------------------------------------------
# Frameset shell and navigation
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    session = _session(request)
    _apply_query_flags(request, session)
    target = "/search" if session.get("authenticated") else "/login"
    return _render(request, session, "frameset.html", content_url=target)


@app.get("/nav", response_class=HTMLResponse)
def nav(request: Request) -> Response:
    session = _session(request)
    return _render(request, session, "nav.html")


@app.post("/postback")
def postback(
    request: Request,
    # The wire names are the framework's, complete with leading underscores.
    # They cannot be parameter names -- Pydantic reserves that spelling -- so
    # they arrive by alias.
    event_target: str = Form(default="", alias="__EVENTTARGET"),
    event_argument: str = Form(default="", alias="__EVENTARGUMENT"),
    ctx: str = Form(default="", alias="__ctx"),
) -> Response:
    """Where every javascript: control on a page lands.

    Navigation in this application is not a link with an href to follow -- it is
    a form post naming the control that raised it. That is faithful to the
    framework these systems are built on, and it is a genuine obstacle: the URL
    of a screen tells you nothing about how to get there.
    """
    session = _session(request)
    member = ctx or session.get("current_member", "")

    routes = {
        "ctl00$navMemberSearch": "/search",
        "ctl00$navMemberDetail": f"/member?id={member}" if member else "/search",
        "ctl00$navSignOut": "/logout",
        "ctl00$tbrSearch": "/search",
        "ctl00$lnkBackToSearch": "/search",
        "ctl00$lnkOpenSubAccount": f"/subaccount/new?member={member}",
    }

    if event_target.startswith("ctl00$grdAccounts$Page"):
        page = event_target.rsplit("Page", 1)[-1] or "1"
        target = f"/member?id={member}&page={page}"
    else:
        # Toolbar buttons that do nothing are still worth having: real toolbars
        # are full of them, and an agent has to work out which controls matter.
        target = routes.get(event_target) or (
            f"/member?id={member}" if member else "/search"
        )

    return _persist(RedirectResponse(target, status_code=303), session)


@app.get("/help", response_class=HTMLResponse)
def help_window(request: Request) -> Response:
    """Opened with window.open, in a separate browser window.

    Deliberately present and deliberately not part of any recorded flow. The
    Surface abstraction tracks frames within one page and does not follow a new
    window, so this is a real, documented limitation rather than a hidden one --
    see REPORT.md section 7.
    """
    session = _session(request)
    return _render(request, session, "help.html")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    session = _session(request)
    _apply_query_flags(request, session)
    return _render(request, session, "login.html", error=request.query_params.get("error"))


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request, operator_id: str = Form(default=""), password: str = Form(default="")
) -> Response:
    session = _session(request)
    if not operator_id.strip():
        return _render(request, session, "login.html", error="Operator ID is required.")
    session["authenticated"] = True
    session["operator"] = operator_id.strip()
    return _persist(RedirectResponse("/search", status_code=303), session)


@app.get("/logout")
def logout(request: Request) -> Response:
    session = _session(request)
    session["authenticated"] = False
    return _persist(RedirectResponse("/login", status_code=303), session)


# ---------------------------------------------------------------------------
# Member search
# ---------------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request) -> Response:
    session = _session(request)
    _apply_query_flags(request, session)
    if not session.get("authenticated"):
        return _persist(RedirectResponse("/login", status_code=303), session)
    _maybe_slow(session)
    return _render(request, session, "search.html")


@app.post("/search", response_class=HTMLResponse)
def search_submit(request: Request, member_id: str = Form(default="")) -> Response:
    session = _session(request)
    if not session.get("authenticated"):
        return _persist(RedirectResponse("/login?error=expired", status_code=303), session)

    if session.get("inject") == "server_error":
        return _render(request, session, "error.html", status=500)

    _maybe_slow(session)

    member_id = member_id.strip()
    if not member_id:
        return _render(
            request, session, "search.html", error="Enter a member number to continue."
        )

    if session.get("inject") == "member_not_found" or member_id not in MEMBERS:
        return _render(request, session, "not_found.html", member_id=member_id)

    session["current_member"] = member_id
    return _persist(RedirectResponse(f"/member?id={member_id}", status_code=303), session)


# ---------------------------------------------------------------------------
# Member detail
# ---------------------------------------------------------------------------


@app.get("/member", response_class=HTMLResponse)
def member_detail(request: Request) -> Response:
    session = _session(request)
    _apply_query_flags(request, session)
    member_id = request.query_params.get("id", session.get("current_member", ""))

    if _session_expired(session, "member"):
        return _persist(RedirectResponse("/login?error=expired", status_code=303), session)

    if not session.get("authenticated"):
        return _persist(RedirectResponse("/login?error=expired", status_code=303), session)

    if session.get("inject") == "server_error":
        return _render(request, session, "error.html", status=500)

    if _needs_interstitial(session, "member"):
        return _render(request, session, "notice.html", next_url=f"/member?id={member_id}")

    _maybe_slow(session)

    member = MEMBERS.get(member_id)
    if member is None or session.get("inject") == "member_not_found":
        return _render(request, session, "not_found.html", member_id=member_id)

    if member_id in RESTRICTED_MEMBER_IDS or session.get("inject") == "permission_denied":
        return _render(request, session, "denied.html", member_id=member_id, status=200)

    session["current_member"] = member_id
    return _render(request, session, "member.html", member=member)


@app.post("/notice/dismiss")
def dismiss_notice(request: Request, next_url: str = Form(default="/search")) -> Response:
    session = _session(request)
    session["interstitial_dismissed"] = True
    return _persist(RedirectResponse(next_url, status_code=303), session)


# ---------------------------------------------------------------------------
# Sub-account creation -- the irreversible flow
# ---------------------------------------------------------------------------


@app.get("/subaccount/new", response_class=HTMLResponse)
def subaccount_form(request: Request) -> Response:
    session = _session(request)
    _apply_query_flags(request, session)
    if not session.get("authenticated"):
        return _persist(RedirectResponse("/login?error=expired", status_code=303), session)

    member_id = request.query_params.get("member", session.get("current_member", ""))
    member = MEMBERS.get(member_id)
    if member is None:
        return _render(request, session, "not_found.html", member_id=member_id)

    _maybe_slow(session)
    return _render(
        request,
        session,
        "subaccount_new.html",
        member=member,
        types=SUBACCOUNT_TYPES,
        error=None,
    )


@app.post("/subaccount/new", response_class=HTMLResponse)
def subaccount_submit(
    request: Request,
    member_id: str = Form(default=""),
    account_type: str = Form(default=""),
    nickname: str = Form(default=""),
    initial_deposit: str = Form(default=""),
) -> Response:
    session = _session(request)
    if not session.get("authenticated"):
        return _persist(RedirectResponse("/login?error=expired", status_code=303), session)

    member = MEMBERS.get(member_id)
    if member is None:
        return _render(request, session, "not_found.html", member_id=member_id)

    error = None
    if session.get("inject") == "validation_error":
        error = "Initial deposit does not meet the minimum for this account type."
    elif not account_type:
        error = "Select an account type."
    elif not nickname.strip():
        error = "Account nickname is required."
    else:
        try:
            amount = float(initial_deposit.replace(",", "").replace("$", "").strip() or "0")
        except ValueError:
            error = "Initial deposit must be a number."
        else:
            if amount < 25:
                error = "Initial deposit must be at least $25.00."

    if error:
        return _render(
            request,
            session,
            "subaccount_new.html",
            member=member,
            types=SUBACCOUNT_TYPES,
            error=error,
        )

    reference = f"SA-{secrets.randbelow(900000) + 100000}"
    session["last_reference"] = reference
    return _render(
        request,
        session,
        "subaccount_confirm.html",
        member=member,
        account_type=account_type,
        nickname=nickname.strip(),
        initial_deposit=initial_deposit,
        reference=reference,
    )


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    # cua/cli.py loads .env for every discover/replay invocation; this never
    # did, so MOCKAPP_HOST/MOCKAPP_PORT in .env silently did nothing when the
    # app was launched directly with `python -m mockapp` -- os.environ only
    # sees a real exported shell variable, never the .env file's contents,
    # without this. Found changing the default port away from 8080: the app
    # kept starting on 8080 regardless of what .env said.
    load_dotenv()

    host = os.environ.get("MOCKAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCKAPP_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="warning")
