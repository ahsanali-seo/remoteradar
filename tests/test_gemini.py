"""Gemini provider with a fake HTTP client: no network, no key needed."""
import json

import pytest

from radar import ai


class Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self.replies.pop(0)


def ok(text, thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return Resp(200, {"candidates": [{"content": {"parts": parts}}],
                      "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20}})


@pytest.fixture
def g(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    def make(replies):
        return ai.Gemini(api_key="test", http=FakeHttp(replies))
    return make


POST = "Hiring SEO Executive, remote, Lahore. Email hr@acme.pk"


def test_check(g):
    reply = json.dumps({"match": True, "reason": "remote SEO", "title": "SEO Executive",
                        "company": "Acme", "city": "Lahore", "contact_email": "hr@acme.pk"})
    gem = g([ok(reply, thought="thinking...")])
    r = gem.check(POST, {"source": "linkedin"})
    assert r.match and r.contact_email == "hr@acme.pk" and r.company == "Acme"
    url, body, headers = gem.http.calls[0]
    assert "gemini-flash-lite-latest:generateContent" in url and headers["x-goog-api-key"] == "test"
    assert "<job_post>" in body["contents"][0]["parts"][0]["text"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "untrusted" in body["systemInstruction"]["parts"][0]["text"]


def test_invented_email_dropped(g):
    reply = json.dumps({"match": True, "reason": "x", "contact_email": "ceo@fake.pk"})
    r = g([ok(reply)]).check(POST, {})
    assert r.contact_email == "hr@acme.pk"          # only emails really in the post


def test_write_uses_profile_and_write_model(g):
    gem = g([ok(json.dumps({"body": "Hi Sara, ..."}))])
    body = gem.write("PROFILE FACTS", "TEMPLATE [Name]", {"title": "SEO Executive", "text": POST}, "site")
    assert body == "Hi Sara, ..."
    url, req, _ = gem.http.calls[0]
    assert "gemini-flash-latest:generateContent" in url
    sys_text = req["systemInstruction"]["parts"][0]["text"]
    assert "PROFILE FACTS" in sys_text and "Never invent numbers" in sys_text


def test_vision(g):
    gem = g([ok("We are hiring SEO")])
    assert gem.read_image(b"png-bytes") == "We are hiring SEO"
    part = gem.http.calls[0][1]["contents"][0]["parts"][0]
    assert part["inline_data"]["mime_type"] == "image/png"


def test_rate_limit_retry_then_ok(g):
    gem = g([Resp(429, {"error": "quota"}), ok(json.dumps({"match": False, "reason": "no"}))])
    assert gem.check(POST, {}).match is False and len(gem.http.calls) == 2


def test_daily_limit_message(g):
    gem = g([Resp(429, {})] * 4)
    with pytest.raises(RuntimeError, match="daily limit"):
        gem.check(POST, {})


def test_cost_is_zero(g):
    from radar import db
    g([ok(json.dumps({"match": False, "reason": "no"}))]).check(POST, {})
    assert db.ai_cost_today() == 0


def test_make_ai_picks_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("RADAR_AI", raising=False)
    assert isinstance(ai.make_ai(), ai.Gemini)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert ai.make_ai() is None
