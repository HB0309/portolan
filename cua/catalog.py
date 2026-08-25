"""Capabilities as an agent-callable catalog.

This is where the project's claim becomes checkable. A capability declares typed
inputs, typed outputs and named business outcomes; that is already a function
signature, so turning it into a tool definition is a translation rather than a
design exercise -- which is the point. The schema was shaped so this would fall
out of it.

The outcomes matter as much as the arguments. An agent that can call
``read_savings_balance`` needs to know that ``MEMBER_NOT_FOUND`` is a possible
answer rather than an error, or it will treat a perfectly good response as a
failure and retry it. So the declared outcomes go into the tool description.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cua.schema.capability import Capability


def _rank(capability: Capability) -> tuple[int, int]:
    return (1 if capability.approval == "approved" else 0, capability.version)


@dataclass
class CapabilityCatalog:
    capabilities: list[Capability] = field(default_factory=list)

    @classmethod
    def load(cls, directory: str | Path) -> CapabilityCatalog:
        path = Path(directory)
        if not path.exists():
            return cls()

        found: list[Capability] = []
        for file in sorted(path.glob("*.json")):
            try:
                found.append(Capability.model_validate_json(file.read_text(encoding="utf-8")))
            except Exception:
                # A malformed file should not take the whole catalog down; the
                # agent simply cannot call that one.
                continue
        return cls(capabilities=cls._best_per_id(found))

    @staticmethod
    def _best_per_id(candidates: list[Capability]) -> list[Capability]:
        """One entry per capability id: the version an agent should call.

        Several versions of the same capability legitimately sit side by side --
        a draft that was just recorded and the reviewed version that supersedes
        it. An agent asking for the capability by name must get the approved one;
        offering it whichever happened to sort first would mean a review could be
        silently bypassed by filename.

        Approved beats draft, and within the same approval state the higher
        version wins.
        """
        best: dict[str, Capability] = {}
        for capability in candidates:
            incumbent = best.get(capability.capability_id)
            if incumbent is None or _rank(capability) > _rank(incumbent):
                best[capability.capability_id] = capability
        return sorted(best.values(), key=lambda c: c.capability_id)

    def get(self, capability_id: str) -> Capability | None:
        for capability in self.capabilities:
            if capability.capability_id == capability_id:
                return capability
        return None

    def describe(self, capability: Capability) -> str:
        lines = [capability.description]
        if capability.outputs:
            returns = ", ".join(f"{o.name} ({o.description})" for o in capability.outputs)
            lines.append(f"Returns: {returns}.")
        if capability.outcomes:
            codes = ", ".join(f"{o.code} ({o.description})" for o in capability.outcomes)
            lines.append(
                f"May instead return one of these business outcomes, which are "
                f"legitimate answers rather than errors: {codes}"
            )
        if capability.risk.contains_irreversible:
            lines.append(
                "This capability performs an action that cannot be undone and "
                "requires human approval unless it has been reviewed and approved."
            )
        lines.append(f"Approval state: {capability.approval}.")
        return " ".join(lines)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """The catalog as a provider-agnostic tool list."""
        return [
            {
                "name": capability.capability_id.replace(".", "_"),
                "description": self.describe(capability),
                "input_schema": capability.input_json_schema(),
            }
            for capability in self.capabilities
        ]

    def resolve_tool_name(self, tool_name: str) -> Capability | None:
        for capability in self.capabilities:
            if capability.capability_id.replace(".", "_") == tool_name:
                return capability
        return None
