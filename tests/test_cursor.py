"""Cursor provider:本地 token → cookie、手填 cookie、usage-summary 解析。不发真实请求。"""
import base64
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone

from api.models import KIND_MONEY, TIER_MONTHLY, DeterministicError, TransientFetchError
from api.providers import cursor
from api.registry import get_provider


def fake_jwt(sub="auth0|user_123", exp=None):
    def enc(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'sub': sub, 'exp': exp or time.time() + 3600})}.sig"


SUMMARY = {
    "billingCycleStart": "2026-10-12T08:57:58.000Z",
    "billingCycleEnd": "2026-11-12T08:57:58.000Z",
    "membershipType": "pro_plus",
    "individualUsage": {
        "plan": {"enabled": True, "used": 3500, "limit": 7000,
                 "autoPercentUsed": 40, "apiPercentUsed": 60, "totalPercentUsed": 52},
        "onDemand": {"enabled": True, "used": 1234, "limit": 5000},
    },
    "teamUsage": {},
}


@pytest.fixture
def calls(monkeypatch):
    sent = []
    def fake_get_json(session, url, headers, **kw):
        sent.append((url, headers))
        return SUMMARY, 200
    monkeypatch.setattr(cursor, "get_json", fake_get_json)
    monkeypatch.setattr(cursor, "post_json", lambda *a, **kw: ({}, 200))   # 默认没有 Grok Bot 额度
    return sent


def test_parse_summary_tiers():
    tiers, msg = cursor.parse_usage_summary(SUMMARY)
    assert msg == "Cursor Pro+"
    monthly, auto, api, od = tiers
    assert (monthly.name, monthly.utilization) == (TIER_MONTHLY, 52)
    assert monthly.resets_at
    assert (auto.display_short_label, auto.utilization) == ("Auto", 40)
    assert (api.display_short_label, api.utilization) == ("API", 60)
    assert od.kind == KIND_MONEY and od.used == 12.34 and od.total == 50 and od.remaining == pytest.approx(37.66)


def test_plan_percent_fallbacks():
    assert cursor._plan_percent({"autoPercentUsed": 10, "apiPercentUsed": 30}) == 20
    assert cursor._plan_percent({"apiPercentUsed": 30}) == 30
    assert cursor._plan_percent({"used": 700, "limit": 7000}) == 10
    assert cursor._plan_percent({}) is None
    tiers, _ = cursor.parse_usage_summary({"individualUsage": {"overall": {"used": 50, "limit": 200}}})
    assert tiers[0].utilization == 25


def test_local_token_builds_session_cookie(tmp_path, monkeypatch, calls):
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    token = fake_jwt()
    con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (token,))
    con.commit(); con.close()
    monkeypatch.setattr(cursor, "_state_db_candidates", lambda: [tmp_path / "missing", db])
    snap = get_provider("cursor").fetch({"cookie": ""})
    assert snap.success and snap.tiers[0].utilization == 52
    url, headers = calls[0]
    assert url == "https://cursor.com/api/usage-summary"
    assert headers["Cookie"] == f"WorkosCursorSessionToken=user_123%3A%3A{token}"


def test_expired_or_missing_local_token(monkeypatch, calls):
    monkeypatch.setattr(cursor, "_read_local_token", lambda: fake_jwt(exp=time.time() - 10))
    snap = get_provider("cursor").fetch({})
    assert not snap.success and snap.credential_status == "expired"
    monkeypatch.setattr(cursor, "_read_local_token", lambda: None)
    snap = get_provider("cursor").fetch({})
    assert not snap.success and snap.credential_status == "not_found"
    assert not calls


@pytest.mark.parametrize("text,expected", [
    ("abc%3A%3Aeyj", "WorkosCursorSessionToken=abc%3A%3Aeyj"),
    ("Cookie: a=1; WorkosCursorSessionToken=x", "a=1; WorkosCursorSessionToken=x"),
])
def test_manual_cookie(monkeypatch, calls, text, expected):
    monkeypatch.setattr(cursor, "_read_local_token", lambda: pytest.fail("不应读本地"))
    assert get_provider("cursor").fetch({"cookie": text}).success
    assert calls[0][1]["Cookie"] == expected


def test_unauthorized_marks_expired(monkeypatch):
    def deny(*a, **kw):
        raise DeterministicError(401, "HTTP 401")
    monkeypatch.setattr(cursor, "get_json", deny)
    snap = get_provider("cursor").fetch({"cookie": "x"})
    assert not snap.success and snap.credential_status == "expired"


# ====== Grok Bot 周额度 ======
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
SAND = {"currentPeriodStart": "2026-11-10T23:25:21.167Z",
        "nextResetTimestampUtc": "2026-11-17T23:25:21.167Z",
        "usagePercent": 0.754414, "hasAvailableUsage": True, "hasNonZeroIncludedLimit": True}


def test_parse_sand_weekly():
    tier, shared = cursor.parse_sand_usage(SAND, NOW)
    assert tier.utilization == pytest.approx(0.754414) and tier.resets_at
    assert (tier.display_label, tier.display_short_label) == ("Grok Bot·每周", "Grok")
    assert not shared
    assert cursor.parse_sand_usage({**SAND, "isUnified": True}, NOW)[1]


@pytest.mark.parametrize("body", [
    {**SAND, "hasNonZeroIncludedLimit": False},
    {**SAND, "includedLimitZero": True},
    {**SAND, "usagePercent": None},
    {**SAND, "hasNonZeroIncludedLimit": False, "sandTrialExpiresAt": "2026-11-01T00:00:00Z"},
    [],
])
def test_parse_sand_hidden(body):
    assert cursor.parse_sand_usage(body, NOW) == (None, False)


def test_parse_sand_trial_has_no_reset():
    body = {**SAND, "hasNonZeroIncludedLimit": False, "sandTrialExpiresAt": "2026-12-01T00:00:00Z"}
    tier, shared = cursor.parse_sand_usage(body, NOW)
    assert tier.display_label == "Grok Bot·试用" and tier.resets_at is None and not shared


def _sand_returns(monkeypatch, result):
    def fake_post(session, url, headers, body, **kw):
        assert url == "https://cursor.com/api/dashboard/get-sand-usage-status" and body == {}
        assert kw["extra_headers"]["Origin"] == "https://cursor.com" and "Cookie" in headers
        if isinstance(result, Exception):
            raise result
        return result, 200
    monkeypatch.setattr(cursor, "post_json", fake_post)


def test_fetch_adds_grok_before_on_demand(monkeypatch, calls):
    _sand_returns(monkeypatch, {**SAND, "nextResetTimestampUtc": "2099-01-01T00:00:00Z"})
    names = [t.name for t in get_provider("cursor").fetch({"cookie": "x"}).tiers]
    assert names == [TIER_MONTHLY, "cursor_auto", "cursor_api", "cursor_grok_bot", "cursor_on_demand"]


def test_fetch_unified_pool_replaces_monthly(monkeypatch, calls):
    _sand_returns(monkeypatch, {**SAND, "isUnified": True, "nextResetTimestampUtc": "2099-01-01T00:00:00Z"})
    names = [t.name for t in get_provider("cursor").fetch({"cookie": "x"}).tiers]
    assert names == ["cursor_grok_bot", "cursor_on_demand"]


@pytest.mark.parametrize("err", [TransientFetchError("timeout"), DeterministicError(500, "HTTP 500")])
def test_sand_failure_keeps_cursor_usage(monkeypatch, calls, err):
    _sand_returns(monkeypatch, err)
    snap = get_provider("cursor").fetch({"cookie": "x"})
    assert snap.success and [t.name for t in snap.tiers][0] == TIER_MONTHLY
    assert "cursor_grok_bot" not in [t.name for t in snap.tiers]
