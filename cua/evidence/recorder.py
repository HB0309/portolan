"""Run evidence.

Every run -- discovery or replay -- writes a directory under ``evidence/`` that
is sufficient to understand what happened without re-running anything:

    evidence/<run_id>/
        run.jsonl          one structured record per event, in order
        result.json        the typed result the caller received
        capability.json    (discovery) the artifact that was produced
        transcript.jsonl   (discovery) the raw model exchange
        steps/*.png        per-step screenshots
        failures/*.html    markup snapshots, written only when something breaks

The split between ``run.jsonl`` and ``transcript.jsonl`` is deliberate and is the
same separation the artifact itself embodies: the transcript is what the model
said, the run log is what the system did. Confusing the two is how a "recording"
ends up being a chat log that cannot be replayed.

Everything written here passes through the redactor on the way out.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.policy.redaction import Redactor
from cua.schema.results import EvidenceRef


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class EvidenceRecorder:
    def __init__(
        self, run_id: str, root: str | Path = "evidence", redactor: Redactor | None = None
    ) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "steps").mkdir(exist_ok=True)
        self.redactor = redactor or Redactor()
        self._log_path = self.dir / "run.jsonl"
        self._transcript_path = self.dir / "transcript.jsonl"
        self.refs: list[EvidenceRef] = []
        self._seq = 0

    # -- structured logging ----------------------------------------------

    def log(self, event: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **_jsonable(fields),
        }
        record = self.redactor.scrub_obj(record)
        with open(self._log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def transcript(self, direction: str, payload: Any) -> None:
        """The raw model exchange. Kept apart from the run log on purpose."""
        record = self.redactor.scrub_obj(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "direction": direction,
                "payload": _jsonable(payload),
            }
        )
        with open(self._transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # -- artifacts on disk ------------------------------------------------

    def write_json(self, name: str, value: Any, *, scrub: bool = True) -> str:
        payload = _jsonable(value)
        if scrub:
            payload = self.redactor.scrub_obj(payload)
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    async def screenshot(self, surface: Any, label: str, mask_refs: list[str] | None = None) -> str:
        path = self.dir / "steps" / f"{label}.png"
        try:
            await surface.screenshot(str(path), mask_refs=mask_refs)
        except Exception as exc:  # evidence must never be the reason a run dies
            self.log("evidence.screenshot_failed", label=label, error=str(exc))
            return ""
        self.refs.append(EvidenceRef(kind="screenshot", path=str(path), note=label))
        return str(path)

    async def snapshot(self, surface: Any, label: str) -> str:
        path = self.dir / "failures" / f"{label}.html"
        try:
            await surface.snapshot(str(path))
        except Exception as exc:
            self.log("evidence.snapshot_failed", label=label, error=str(exc))
            return ""
        self.refs.append(EvidenceRef(kind="dom_snapshot", path=str(path), note=label))
        return str(path)

    def note_ref(self, kind: str, path: str, note: str = "") -> None:
        self.refs.append(EvidenceRef(kind=kind, path=path, note=note))  # type: ignore[arg-type]
