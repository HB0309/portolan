"""The retry-after parser used against Groq's (and any OpenAI-compatible
provider's) 429 responses.

Found live, twice, against the same free tier: a per-minute limit names its
cooldown as bare seconds ("Please try again in 5.69s"), and a per-day limit --
found only once the day's token budget was actually exhausted from testing --
names the same thing in minutes ("Please try again in 3m14.832s"). A regex
written against only the first shape silently fell back to an 8-second default
against the second, retrying straight back into a wall that would not clear
for minutes -- four times over, with nothing printed, which read as a hang
rather than a rate limit.
"""

from __future__ import annotations

from cua.llm.openai_provider import _MAX_SILENT_WAIT_SECONDS, _retry_after_seconds


class _FakeRateLimitError(Exception):
    """Stands in for openai.RateLimitError: the message is all this reads."""


class TestRetryAfterSeconds:
    def test_a_bare_seconds_message_is_parsed(self):
        exc = _FakeRateLimitError("rate_limit_exceeded: Please try again in 5.69s.")
        assert _retry_after_seconds(exc, default=8.0) == 6.19

    def test_a_minutes_and_seconds_message_is_parsed(self):
        exc = _FakeRateLimitError(
            "rate_limit_exceeded: ... Please try again in 3m14.832s. Need more tokens?"
        )
        assert _retry_after_seconds(exc, default=8.0) == 194.832 + 0.5

    def test_an_hours_minutes_and_seconds_message_is_parsed(self):
        exc = _FakeRateLimitError("Please try again in 1h2m3s.")
        assert _retry_after_seconds(exc, default=8.0) == 3600 + 120 + 3 + 0.5

    def test_no_hint_at_all_falls_back_to_the_default(self):
        exc = _FakeRateLimitError("rate_limit_exceeded")
        assert _retry_after_seconds(exc, default=8.0) == 8.0

    def test_a_daily_quota_cooldown_exceeds_the_silent_wait_budget(self):
        """This is the property the caller actually relies on: a per-day
        cooldown must come back large enough that the caller's own cap
        (_MAX_SILENT_WAIT_SECONDS) kicks in and raises instead of blocking.
        """
        exc = _FakeRateLimitError("Please try again in 3m14.832s.")
        assert _retry_after_seconds(exc, default=8.0) > _MAX_SILENT_WAIT_SECONDS
