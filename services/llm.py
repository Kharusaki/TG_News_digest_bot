"""LLM-конспекты материалов.

Модель больше не пишет заголовки, даты и ссылки: она получает пронумерованные
тексты и возвращает строгий JSON ``{"id": N, "summary": "..."}``. Всё, что можно
перепутать или выдумать, проверяется до показа пользователю.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — редактор дайджеста аудиторской компании. Тема: "{topic_name}".

Ниже — пронумерованные материалы из проверенных источников. Для каждого
подходящего материала напиши краткую суть на русском: 1–2 предложения, только
факты из его текста.

Верни ТОЛЬКО JSON без пояснений и без markdown:
{{"intro": "одно короткое предложение о подборке", "items": [{{"id": 1, "summary": "суть"}}]}}

Строгие правила:
1. Используй только факты из блока ниже. Чего нет в тексте — того не пиши.
2. Не выдумывай ссылки, URL, даты, числа, суммы, названия компаний и должностей.
3. Не пиши URL и markdown-разметку.
4. Пропусти рекламу, вебинары, поздравления и материалы не по теме.
5. Если подходящих материалов нет — верни {{"intro": "", "items": []}}.

Материалы:
{raw_texts}
"""

TOPIC_NAMES = {
    "news": "Новости отрасли",
    "jobs": "Вакансии",
    "tech": "Технологии",
    "msfo": "Аудит и МСФО",
}
MAX_ITEM_CHARS = 700
MAX_SUMMARY_CHARS = 420
_URL_RE = re.compile(r"(https?://|www\.|<a\s|tg://)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d\s.,:/-]*\d|\d")
_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")


def build_prompt(topic_name: str, raw_texts: str) -> str:
    """Подставить тему и материалы в промпт."""
    return SYSTEM_PROMPT.format(topic_name=topic_name, raw_texts=raw_texts)


def format_raw_items(items: list[dict[str, Any]]) -> str:
    """Сжать материалы до пронумерованного блока без ссылок."""
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()[:MAX_ITEM_CHARS]
        lines.append(f"[{index}] {title}\n{text}".strip())
    return "\n\n".join(lines)


def _numbers(value: str) -> set[str]:
    return {
        re.sub(r"\D", "", match.group(0))
        for match in _NUMBER_RE.finditer(value)
        if re.sub(r"\D", "", match.group(0))
    }


def validate_summary(summary: Any, source_text: str) -> str:
    """Вернуть безопасный конспект или пустую строку.

    Отбрасываем URL, markdown-ссылки и любые числа/даты, которых нет в исходном
    тексте: это дешёвая, но действенная защита от выдуманных фактов.
    """
    text = re.sub(r"\s+", " ", str(summary or "")).strip()
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return ""
    if _URL_RE.search(text):
        return ""
    if _DATE_RE.search(text) and not _DATE_RE.search(source_text):
        return ""
    if not _numbers(text).issubset(_numbers(source_text)):
        return ""
    return text


def _parse_json(raw: str) -> dict[str, Any]:
    """Достать JSON даже из ответа, обёрнутого в ```json ... ```."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sanitize(data: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Оставить только существующие id и проверенные формулировки."""
    sources = {
        str(index): f"{item.get('title', '')} {item.get('text', '')}"
        for index, item in enumerate(items, start=1)
    }
    intro = validate_summary(data.get("intro"), " ".join(sources.values()))
    raw_items = data.get("items")
    if isinstance(raw_items, dict):
        raw_items = [{"id": key, "summary": value} for key, value in raw_items.items()]
    if not isinstance(raw_items, list):
        raw_items = []

    summaries: dict[int, str] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id", "")).strip()
        source = sources.get(key)
        if source is None:
            continue
        summary = validate_summary(entry.get("summary"), source)
        if summary:
            summaries[int(key)] = summary
    return {"intro": intro, "items": summaries}


async def _gemini(prompt: str) -> str:
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    client = genai.Client(api_key=api_key)
    response = await asyncio.to_thread(
        lambda: client.models.generate_content(model=model, contents=prompt)
    )
    return str(getattr(response, "text", "") or "")


async def _proxy(prompt: str) -> str:
    api_key = os.getenv("PROXY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Нет ни GEMINI_API_KEY, ни PROXY_API_KEY")
    base_url = os.getenv("PROXY_BASE_URL", "https://api.proxyapi.ru").strip().rstrip("/")
    model = os.getenv("PROXY_MODEL", "openai/gpt-4.1-mini").strip()
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
        )
        response.raise_for_status()
        data = response.json()
    return str(data["choices"][0]["message"]["content"] or "")


async def _complete(prompt: str, topic: str) -> str:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    text = ""
    if provider == "gemini" and os.getenv("GEMINI_API_KEY", "").strip():
        try:
            text = await _gemini(prompt)
        except Exception as exc:
            logger.warning("Gemini недоступен для темы %s: %s", topic, exc)
    if not text.strip():
        try:
            text = await _proxy(prompt)
        except Exception as exc:
            logger.warning("Резервный провайдер недоступен для темы %s: %s", topic, exc)
            return ""
    return text.strip()


async def summarize(topic: str, raw_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Вернуть ``{"intro": str, "items": {id: summary}}`` для рендера."""
    if not raw_items:
        return {"intro": "", "items": {}}
    topic_name = TOPIC_NAMES.get(topic, topic)
    prompt = build_prompt(topic_name, format_raw_items(raw_items))
    raw = await _complete(prompt, topic)
    if not raw:
        return {"intro": "", "items": {}}
    result = _sanitize(_parse_json(raw), raw_items)
    if not result["items"]:
        logger.warning("LLM вернула ответ без валидных конспектов для темы %s", topic)
    return result


__all__ = (
    "MAX_ITEM_CHARS",
    "TOPIC_NAMES",
    "build_prompt",
    "format_raw_items",
    "summarize",
    "validate_summary",
)
