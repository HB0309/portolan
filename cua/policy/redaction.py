"""Keeping regulated data out of artifacts, logs and transcripts.

There are two layers, and the order matters:

1. **Structural.** Sensitive values are declared as parameters, and parameters
   are recorded as references rather than literals. Nothing sensitive is ever
   written down in the first place. This is the control that actually works.

2. **Pattern scrubbing.** Everything that reaches disk goes through
   ``Redactor.scrub`` on the way, which masks recognisable secrets and PII.

The second layer is defence in depth and is honestly described as such in the
report: regex-based redaction cannot recognise every shape of sensitive data,
and a system that relied on it alone would be a system that leaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

MASK = "[REDACTED]"


@dataclass
class RedactionPattern:
    name: str
    regex: re.Pattern[str]


@dataclass
class Redactor:
    """Scrubs strings and nested structures on their way to disk."""

    patterns: list[RedactionPattern] = field(default_factory=list)
    sensitive_field_names: list[str] = field(default_factory=list)
    _extra_values: set[str] = field(default_factory=set, init=False)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Redactor:
        patterns = [
            RedactionPattern(name=p["name"], regex=re.compile(p["regex"]))
            for p in config.get("patterns", [])
        ]
        return cls(
            patterns=patterns,
            sensitive_field_names=[s.lower() for s in config.get("sensitive_field_names", [])],
        )

    def register_values(self, values: Iterable[Any]) -> None:
        """Mask these exact values wherever they appear.

        Used for the concrete values of parameters the capability declared
        ``sensitive``. Registering them means that even if such a value reaches
        a log line by an unexpected route -- a page's own error message echoing
        it back, say -- it still does not land on disk.
        """
        for value in values:
            text = str(value).strip()
            if len(text) >= 4:
                self._extra_values.add(text)

    def is_sensitive_field(self, label: str | None) -> bool:
        if not label:
            return False
        lowered = label.lower()
        return any(name in lowered for name in self.sensitive_field_names)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for value in self._extra_values:
            if value in text:
                text = text.replace(value, MASK)
        for pattern in self.patterns:
            text = pattern.regex.sub(MASK, text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        """Recursively scrub a JSON-shaped structure."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for key, value in obj.items():
                if isinstance(key, str) and self.is_sensitive_field(key):
                    out[key] = MASK
                else:
                    out[key] = self.scrub_obj(value)
            return out
        if isinstance(obj, list):
            return [self.scrub_obj(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self.scrub_obj(item) for item in obj)
        return obj
