"""v6.1: Gemini limits are read properly, Google search falls back to a second model,
AI errors are re-checked, and a skipped cover letter always says why."""
import json

import pytest

from radar import ai, db
from radar.models import RawJob
from radar.pipeline import Pipeline, retry_ai_errors
from tests.test_gemini import FakeHttp, Resp, ok
from tests.test_v6 import GOOD, CoverAPI, _apply_on_site_job


def day_429(model="gemini-flash-lite"):
    return Resp(429, {"error": {"code": 429, "message": "You exceeded your current quota.", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}})


@pytest.fixture
def g(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    return lambda replies: ai.Gemini(api_key="t", http=FakeHttp(replies))


def test_quota_info_reads_google_answer():
    kind, detail, delay = ai.quota_info(day_429())
    assert kind == "day" and "PerDay" in detail and delay == 3600
    minute = Resp(429, {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                              {"retryDelay": "31s"}]}})
    assert ai.quota_info(minute)[0] == "minute" and ai.quota_info(minute)[2] == 31
    assert ai.quota_info(Resp(429, {}))[0] == ""


def test_daily_limit_stops_at_once_and_says_which_quota(g):
    gem = g([day_429()])
    with pytest.raises(RuntimeError, match="daily limit reached for gemini-flash-lite-latest .*PerDay"):
        gem.check("SEO job", {})
    assert len(gem.http.calls) == 1                     # no useless retries
    with pytest.raises(RuntimeError):
        gem.check("SEO job 2", {})
    assert len(gem.http.calls) == 1                     # model remembered as used up for this run


def test_search_falls_back_to_second_model(g):
    gem = g([day_429(), ok('{"jobs": []}')])
    out = gem._call("search", gem.check_model, "s", "find jobs", 100)
    assert out == '{"jobs": []}'
    assert "flash-lite" in gem.http.calls[0][0] and "gemini-flash-latest:" in gem.http.calls[1][0]
    assert gem.http.calls[1][1]["tools"] == [{"google_search": {}}]


def test_search_used_up_does_not_block_checks(g):
    gem = g([day_429(), day_429(), ok(json.dumps({"match": False, "reason": "no"}))])
    with pytest.raises(RuntimeError, match="daily limit"):
        gem._call("search", gem.check_model, "s", "q", 100)
    assert gem.find_website("Acme") == ""               # skipped without a call
    assert gem.check("SEO", {}).match is False          # plain checks still run
    assert len(gem.http.calls) == 3


def test_google_search_retries_next_run_when_both_fail(tmp_path):
    from radar.scrapers.google import GoogleJobsScraper

    class Down:
        can_search, check_model, last_sources = True, "m", []

        def _call(self, *a):
            raise RuntimeError("Gemini 429: daily limit reached")
    sc = GoogleJobsScraper(Down(), client=object(), every_hours=10)
    sc.stamp = tmp_path / "stamp"
    logs = []
    assert sc.run(None, logs.append) == []
    assert not sc.stamp.exists() and "daily limit" in logs[0]


def test_ai_error_is_rechecked_on_next_run():
    api = CoverAPI(GOOD)
    good = ai.Claude(client=api)

    class Limited(ai.Claude):
        def check(self, *a, **k):
            raise RuntimeError("Gemini 429: daily limit reached")
    assert Pipeline(Limited(client=api), None, site_fetcher=lambda u: "").process(_apply_on_site_job()) == "error"
    jid = db.jobs_where("stage='error'")[0]["id"]
    assert retry_ai_errors(Pipeline(good, None, site_fetcher=lambda u: "")) == 1
    j = db.get_job(jid)
    assert j["stage"] == "no_email" and j["cover_letter"] == GOOD


def test_cover_letter_without_profile_is_logged(monkeypatch):
    from radar import pipeline
    monkeypatch.setattr(pipeline, "load_profile", lambda: (_ for _ in ()).throw(RuntimeError("profile.md missing")))
    pipe = Pipeline(ai.Claude(client=CoverAPI(GOOD)), None, site_fetcher=lambda u: "")
    assert pipe.process(_apply_on_site_job()) == "no_email"
    msgs = [e["message"] for e in db.recent_events(0, 50)]
    assert any("profile not ready (profile.md missing)" in m for m in msgs)
