"""Local environment configuration, read once.

Not shared with ``mockapp/app.py`` on purpose. That module represents a
target application this project automates -- deliberately foreign to this
package -- and importing ``cua`` into it would invert the intended layering
(the automation framework depending on the thing it automates, not the other
way round). The two sides agree on a convention instead: the same env var
names, the same defaults. ``mockapp/app.py`` keeps its own independent read.

Everything inside ``cua`` that needs to know where the target app or the
operator console live should import from here rather than reading
``os.environ`` itself -- found live, changing the default port away from
8080/8081: ``cli.py``'s CLI defaults, ``policy/engine.py``'s origin
allowlist, and the test fixtures each had their own copy of the same two
reads, and each one had to be found and fixed independently (D31).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

MOCKAPP_HOST = os.environ.get("MOCKAPP_HOST", "127.0.0.1")
MOCKAPP_PORT = os.environ.get("MOCKAPP_PORT", "8080")
MOCKAPP_TARGET = f"http://{MOCKAPP_HOST}:{MOCKAPP_PORT}/"
CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "8081"))

#: Which LLM provider drives discovery when --provider isn't passed explicitly.
#: Documented in .env.example as the way to run against a single vendor --
#: e.g. someone with only an OpenAI key sets CUA_PROVIDER=openai once instead
#: of passing --provider openai on every discover/ask invocation. Previously
#: only documented, never read: every CLI default was hardcoded to
#: "anthropic" regardless of this variable, so the documented instruction
#: silently did nothing.
DEFAULT_PROVIDER = os.environ.get("CUA_PROVIDER", "anthropic")
