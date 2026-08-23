"""Regenerate the committed evidence set.

Every scenario the report claims is produced by running it, not by describing it.
Run with the mock app up:

    venv/Scripts/python -m mockapp                  # terminal 1
    venv/Scripts/python scripts/build_evidence.py   # terminal 2

Add --provider anthropic to record the discovery runs with a real model rather
than the scripted stand-in.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / "venv" / "Scripts" / "python.exe")
if not Path(PY).exists():  # non-Windows checkouts
    PY = sys.executable

BALANCE = "capabilities/cu.member.read_savings_balance.json"
SUBACCOUNT = "capabilities/cu.member.open_subaccount.json"
APPROVED = "capabilities/cu.member.open_subaccount.approved.json"
OVERLAY = "tenants/cascade.cu.member.read_savings_balance.json"


def run(args: list[str], label: str) -> None:
    print(f"\n=== {label}")
    result = subprocess.run([PY, "-m", "cua", *args], cwd=ROOT, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.strip().startswith(("status", "success:", "business outcome", "failure",
                                    "recovery", "DEGRADED", "discovered", "capability:")):
            print("   " + line.strip())


def approve(source: Path, target: Path) -> None:
    """Stand in for a human review, so the approved path can be demonstrated."""
    capability = json.loads(source.read_text(encoding="utf-8"))
    capability["approval"] = "approved"
    capability["version"] = 2
    for step in capability["steps"]:
        if step["risk"] == "irreversible":
            step["approved_risky"] = True
    capability["risk"]["notes"] = (
        "Reviewed: the sub-account submit is approved for unattended replay."
    )
    target.write_text(json.dumps(capability, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="scripted")
    parser.add_argument("--keep", action="store_true", help="Do not wipe evidence/ first.")
    args = parser.parse_args()

    if not args.keep and (ROOT / "evidence").exists():
        shutil.rmtree(ROOT / "evidence")

    discovery_flags = ["--headless"]
    if args.provider == "scripted":
        discovery_flags += ["--provider", "scripted"]
    else:
        discovery_flags += ["--provider", args.provider]

    run(
        [
            "discover",
            "--goal", "look up member 10042 and read their current savings balance",
            "--inputs", '{"member_id":"10042"}',
            "--outputs", "savings_balance",
            "--capability-id", "cu.member.read_savings_balance",
            "--name", "Read member savings balance",
            "--description",
            "Look up a member by number and return their current savings balance.",
            *(["--script", "read_savings_balance"] if args.provider == "scripted" else []),
            *discovery_flags,
        ],
        "discovery: read a member's savings balance",
    )

    run(
        [
            "discover",
            "--goal",
            "open a savings sub-account for member 10042 and reach the confirmation screen",
            "--inputs",
            '{"member_id":"10042","account_type":"Savings","nickname":"Vacation Fund",'
            '"initial_deposit":"500"}',
            "--outputs", "reference_number",
            "--capability-id", "cu.member.open_subaccount",
            "--name", "Open a member sub-account",
            "--description",
            "Open a new sub-account for a member and return the confirmation reference.",
            "--auto-approve",
            *(["--script", "open_subaccount"] if args.provider == "scripted" else []),
            *discovery_flags,
        ],
        "discovery: open a sub-account (stops on the irreversible step)",
    )

    approve(ROOT / SUBACCOUNT, ROOT / APPROVED)

    scenarios = [
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}'],
         "replay: success"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10077"}'],
         "replay: a different member, same recording"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"99999"}'],
         "replay: business outcome -- no such member"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10099"}'],
         "replay: business outcome -- permission denied"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--entry", "http://127.0.0.1:8080/?inject=server_error"],
         "replay: hard failure -- application error"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--entry", "http://127.0.0.1:8080/?inject=interstitial"],
         "replay: recovered -- unexpected interstitial dismissed"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--entry", "http://127.0.0.1:8080/?inject=slow_load"],
         "replay: recovered -- transient slowness waited out"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--entry", "http://127.0.0.1:8080/?inject=session_expiry"],
         "replay: session expiry routed to a human (declined when unattended)"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--entry", "http://127.0.0.1:8080/?tenant=beta"],
         "replay: another tenant without an overlay -- refuses to guess"),
        (["--capability", BALANCE, "--inputs", '{"member_id":"10042"}',
          "--tenant", OVERLAY],
         "replay: the same tenant with an overlay -- back to rung 1"),
        (["--capability", SUBACCOUNT, "--inputs",
          '{"member_id":"10043","account_type":"Savings","nickname":"Holiday",'
          '"initial_deposit":"250"}'],
         "replay: irreversible step refused on a draft capability"),
        (["--capability", APPROVED, "--inputs",
          '{"member_id":"10043","account_type":"Savings","nickname":"Holiday Club",'
          '"initial_deposit":"250"}'],
         "replay: the same capability once reviewed and approved"),
        (["--capability", APPROVED, "--inputs",
          '{"member_id":"10042","account_type":"Savings","nickname":"X",'
          '"initial_deposit":"500"}',
          "--entry", "http://127.0.0.1:8080/?inject=validation_error"],
         "replay: business outcome -- the application rejected the input"),
        (["--capability", BALANCE, "--inputs", '{"nope":"x"}'],
         "replay: invalid input, refused before the browser opens"),
    ]

    for flags, label in scenarios:
        run(["replay", *flags, "--headless"], label)

    print("\n=== rebuilding evidence/README.md")
    subprocess.run([PY, str(ROOT / "scripts" / "index_evidence.py")], cwd=ROOT, check=True)
    print("done")


if __name__ == "__main__":
    main()
