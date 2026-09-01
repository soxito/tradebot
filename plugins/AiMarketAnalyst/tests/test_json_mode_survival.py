"""JSON mode must survive the trip, or the agent has no decision to report.

Both failures here produced the same user-visible symptom — a trading room where
every seat says HOLD at 20% confidence and "AI calls: 0" — while the provider,
the key and the model were all fine:

* A 4xx of *any* kind used to strip ``response_format`` from the payload. The
  payload object is shared with the backoff layer, so one routine 429 silently
  turned every later attempt into a free-text request.
* Models that narrate before emitting the object run past the token budget and
  return prose ending mid-sentence. Which models do that is not knowable in
  advance — the answer's own ``finish_reason`` is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services import ai_router  # noqa: E402


# ── Which 400s actually mean "I don't do JSON mode" ──────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "response_format is not supported"}}',
        '{"detail": "json_object mode unavailable for this model"}',
        "Structured output is not supported by this endpoint (json)",
    ],
)
def test_a_genuine_refusal_is_recognised(body):
    assert ai_router._rejects_json_mode(body)


@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "Rate limit exceeded"}}',
        '{"error": {"message": "Incorrect API key provided"}}',
        "context length exceeded: 9000 > 8192 tokens",
        "",
        None,
    ],
)
def test_an_unrelated_error_keeps_json_mode(body):
    """Dropping it here costs the whole turn; keeping it costs one retry."""
    assert not ai_router._rejects_json_mode(body)


# ── The widened retry ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.headers: dict = {}
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _answer(content: str, finish: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 2800, "completion_tokens": 1200, "total_tokens": 4000},
    }


class _Client:
    """Captures each request; first answer is truncated prose, second is JSON."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, _url, headers=None, json=None):  # noqa: A002
        self.sent.append(json)
        return _Resp(self.answers.pop(0))


@pytest.fixture()
def client(monkeypatch):
    """Install a fake httpx client and hand the test the one that gets used."""
    made: list[_Client] = []

    def _factory(answers):
        def _build(*_a, **_kw):
            c = _Client(answers)
            made.append(c)
            return c

        monkeypatch.setattr(ai_router.httpx, "AsyncClient", _build)
        return made

    return _factory


async def _call(**overrides):
    kwargs = dict(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="nvapi-test",
        model="nvidia/nemotron-3-ultra-550b-a55b",
        messages=[{"role": "user", "content": "read XAUUSD"}],
        temperature=0.2,
        max_tokens=1200,
        json_mode=True,
    )
    kwargs.update(overrides)
    return await ai_router._call_openai_compatible_msg(**kwargs)


@pytest.mark.asyncio
async def test_a_cut_off_answer_is_asked_again_with_more_room(client):
    made = client([
        _answer("Looking at the 1h structure, resistance sits at 4416 and the", "length"),
        _answer('{"action": "hold", "confidence": 0.4}', "stop"),
    ])

    content, _usage, _via, _msg = await _call()

    assert json.loads(content)["action"] == "hold"
    sent = made[0].sent
    assert [s["max_tokens"] for s in sent] == [1200, 2400], "budget was not widened"
    assert all("_widened" not in s for s in sent), "bookkeeping key leaked upstream"


@pytest.mark.asyncio
async def test_a_complete_answer_is_not_retried(client):
    made = client([_answer('{"action": "buy", "confidence": 0.7}', "stop")])

    await _call()

    assert len(made[0].sent) == 1


@pytest.mark.asyncio
async def test_the_widening_is_capped(client):
    """A model that never stops cannot spend the whole free tier on one turn."""
    made = client([
        _answer("thinking…", "length"),
        _answer('{"action": "hold"}', "stop"),
    ])

    # Doubling this would overshoot the ceiling, so the retry lands on it.
    await _call(max_tokens=ai_router._MAX_WIDENED_TOKENS - 1000)

    assert made[0].sent[1]["max_tokens"] == ai_router._MAX_WIDENED_TOKENS


@pytest.mark.asyncio
async def test_a_second_cut_off_gets_one_more_widening(client):
    """One doubling covers a model that narrated a little; two covers the rest.

    Stopping after a single retry is what left a published read ending in a
    half-finished sentence — the model was still narrating when the widened
    budget ran out too.
    """
    made = client([
        _answer("thinking…", "length"),
        _answer("still thinking…", "length"),
        _answer('{"action": "buy", "confidence": 0.7}', "stop"),
    ])

    await _call(max_tokens=1000)

    budgets = [sent["max_tokens"] for sent in made[0].sent]
    assert budgets == [1000, 2000, 4000]


@pytest.mark.asyncio
async def test_a_truncated_prose_answer_is_widened_too(client):
    """A prose answer cut mid-sentence is exactly what users report as broken.

    JSON used to be the only mode that earned a widening retry; a narrative
    analysis that ran out of room was published half-finished instead.
    """
    made = client([
        _answer("a long narrative that ran out of room", "length"),
        _answer("the full narrative, ending on a complete sentence.", "stop"),
    ])

    await _call(json_mode=False)

    assert len(made[0].sent) == 2, "truncated prose was not widened"
    assert made[0].sent[1]["max_tokens"] == 2400


@pytest.mark.asyncio
async def test_a_truncated_answer_is_retried_even_late_in_the_deadline(client, monkeypatch):
    """Failing over past half the deadline still ends in a short answer.

    The old behaviour skipped the widening retry when more than half the
    deadline had been spent — but the failover provider needs at least as long
    and produces another short read. Spending the round on the model that has
    already done the thinking is the better trade, so retries are unconditional
    now and the deadline is extended by taking them.
    """
    made = client([
        _answer("still thinking about the 1h structure and", "length"),
        _answer('{"action": "hold"}', "stop"),
    ])
    # A clock pinned deep into the deadline: the retry must happen regardless.
    monkeypatch.setattr(ai_router.time, "monotonic", lambda: 90.0)

    content, _usage, _via, _msg = await _call(max_tokens=2048)

    assert len(made[0].sent) == 2, "late truncation must still be retried"
    assert json.loads(content)["action"] == "hold"
