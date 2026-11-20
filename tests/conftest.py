"""Изоляция тестов от рабочей базы: каждый тест получает свежий SQLite."""

import pytest

from database.db import init_db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    init_db()
