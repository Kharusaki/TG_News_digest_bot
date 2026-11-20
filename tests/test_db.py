"""Проверка хранилища подписок."""

from database.db import (
    get_settings,
    get_subscribers,
    init_db,
    set_topics,
    upsert_user,
)


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_db()
    upsert_user(42, "tester")
    set_topics(42, {"news": True, "msfo": True})

    settings = get_settings(42)
    assert settings["username"] == "tester"
    assert settings["topics"] == {
        "news": True,
        "jobs": False,
        "tech": False,
        "msfo": True,
    }
    assert [row["user_id"] for row in get_subscribers()] == [42]

    set_topics(42, {"news": False, "msfo": False})
    assert get_subscribers() == []
