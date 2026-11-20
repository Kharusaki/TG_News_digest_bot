"""Проверка источников: дедупликация, даты и фильтр свежести."""

from datetime import datetime, timedelta

from sources import MAX_RAW_ITEMS, dedupe, max_age_days
from sources.base import MOSCOW, UTC, is_fresh, normalize_url, parse_date
from sources.hh import _salary

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_dedupe_by_title_and_url():
    items = [
        {"title": "Новость Один", "url": "https://a"},
        {"title": "новость один!", "url": "https://b"},
        {"title": "Вторая новость", "url": "https://a"},
        {"title": "Третья новость", "url": "https://c"},
    ]
    result = dedupe(items)
    assert [item["title"] for item in result] == [
        "Новость Один",
        "Третья новость",
    ]


def test_dedupe_respects_limit():
    items = [{"title": str(index), "url": str(index)} for index in range(30)]
    assert len(dedupe(items)) == MAX_RAW_ITEMS


def test_dedupe_ignores_utm_marks():
    items = [
        {"title": "Первая", "url": "https://a.ru/news?id=1&utm_source=rss"},
        {"title": "Вторая", "url": "https://a.ru/news?id=1"},
    ]
    assert len(dedupe(items)) == 1


def test_parse_date_formats():
    assert parse_date("Thu, 19 Nov 2026 18:20:01 +0300") == datetime(
        2026, 11, 19, 15, 20, 1, tzinfo=UTC
    )
    assert parse_date("2026-11-20T10:10:00+03:00") == datetime(
        2026, 11, 20, 7, 10, tzinfo=UTC
    )
    assert parse_date("19.11.2026") == datetime(2026, 11, 18, 21, 0, tzinfo=UTC)
    assert parse_date("") is None


def test_is_fresh_drops_old_and_undated():
    assert is_fresh(NOW - timedelta(days=2), 3, NOW)
    assert is_fresh(NOW - timedelta(days=3), 3, NOW)
    assert not is_fresh(NOW - timedelta(days=4), 3, NOW)
    assert not is_fresh(None, 3, NOW)
    assert not is_fresh(NOW + timedelta(days=2), 3, NOW)


def test_normalize_url_strips_tracking():
    assert (
        normalize_url("https://A.ru/news/?utm_source=x&id=7#top")
        == "https://a.ru/news?id=7"
    )


def test_age_limits_from_env(monkeypatch):
    monkeypatch.delenv("DIGEST_MAX_AGE_DAYS", raising=False)
    monkeypatch.delenv("DIGEST_MAX_AGE_DAYS_JOBS", raising=False)
    assert max_age_days("news") == 3
    assert max_age_days("jobs") == 14

    monkeypatch.setenv("DIGEST_MAX_AGE_DAYS", "5")
    monkeypatch.setenv("DIGEST_MAX_AGE_DAYS_JOBS", "30")
    assert max_age_days("tech") == 5
    assert max_age_days("jobs") == 30


def test_moscow_label_is_utc_plus_three():
    assert MOSCOW.utcoffset(None) == timedelta(hours=3)


def test_salary_fallback():
    assert _salary(None) == ""
    assert _salary({"from": 100, "to": 200, "currency": "RUR"}) == "100–200 RUR"
    assert _salary("от 50 000 руб.") == "от 50 000 руб."
