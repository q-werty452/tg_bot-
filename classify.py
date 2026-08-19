"""
classify.py — определение темы обращения.

После первого сообщения жителя бот фоново спрашивает модель: короткий
заголовок, категория (строго из списка панели), район и адрес — и отправляет
результат в панель. Правила надёжности:

  1. Классификация НИКОГДА не задерживает ответ жителю — она в отдельной
     фоновой задаче и с собственным таймаутом.
  2. Ответ модели разбирается защищённо: любой мусор вместо JSON — берём
     запасной вариант (заголовок из первых слов, категория «прочее»).
  3. Ошибка сети или модели — просто пропуск: заявка остаётся с заголовком
     по умолчанию, сотрудник поправит руками.
"""

import asyncio
import json
import logging

from providers import ProviderError, get_provider
from remote_config import remote

logger = logging.getLogger(__name__)

# Категории на случай, если панель ещё ни разу не отвечала.
FALLBACK_CATEGORIES = [
    {"slug": "zhkh", "name": "ЖКХ"},
    {"slug": "utilities", "name": "Вода и свет"},
    {"slug": "roads", "name": "Дороги и транспорт"},
    {"slug": "cleanup", "name": "Благоустройство и мусор"},
    {"slug": "docs", "name": "Справки и документы"},
    {"slug": "build", "name": "Земля и строительство"},
    {"slug": "social", "name": "Социальные вопросы"},
    {"slug": "other", "name": "Прочее"},
]

PROMPT = """\
Ты сортируешь обращения жителей в мэрию города Манас (Кыргызстан).
Прочитай обращение и верни СТРОГО один JSON-объект без пояснений:

{{"title": "...", "category": "...", "district": "...", "address": "..."}}

Правила:
- title: краткая суть обращения, 3-7 слов, на языке обращения.
  Например: "Не вывозят мусор на Токтогула 12".
- category: ровно один код из списка: {categories}
  Не подходит ничего — пиши "other".
- district: код района из списка: {districts}
  Если район не назван и не очевиден — пустая строка.
- address: улица и дом из текста обращения, как написал житель.
  Нет адреса — пустая строка.
"""


def _fallback(text: str) -> dict:
    """Когда модель не помогла: заголовок из начала текста, тема «прочее»."""
    title = " ".join(text.split())[:60]
    return {"title": title, "category": "other", "district": "", "address": ""}


def parse_answer(raw: str, valid_categories: set[str],
                 valid_districts: set[str], text: str) -> dict:
    """Разобрать ответ модели, не доверяя ему ни в чём."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        assert isinstance(data, dict)
    except (ValueError, AssertionError):
        logger.warning("Классификатор вернул не-JSON: %.120s", raw)
        return _fallback(text)

    result = _fallback(text)
    title = str(data.get("title") or "").strip()
    if title:
        result["title"] = title[:200]
    category = str(data.get("category") or "").strip().lower()
    if category in valid_categories:
        result["category"] = category
    district = str(data.get("district") or "").strip().lower()
    if district in valid_districts:
        result["district"] = district
    result["address"] = str(data.get("address") or "").strip()[:250]
    return result


async def classify_ticket(crm, ticket_id: int, text: str,
                          provider_name: str) -> None:
    """Фоновая задача: определить тему и отправить в панель."""
    categories = remote.categories or FALLBACK_CATEGORIES
    districts = remote.districts

    prompt = PROMPT.format(
        categories=", ".join(f'{c["slug"]} ({c["name"]})' for c in categories),
        districts=", ".join(f'{d["slug"]} ({d["name"]})' for d in districts)
                  or "нет данных",
    )
    try:
        provider = get_provider(provider_name)
        raw = await asyncio.wait_for(
            provider.ask(system=prompt,
                         history=[{"role": "user", "content": text[:2000]}],
                         max_tokens=300, detailed=False),
            timeout=45,
        )
        result = parse_answer(raw, {c["slug"] for c in categories},
                              {d["slug"] for d in districts}, text)
    except (ProviderError, asyncio.TimeoutError) as e:
        logger.info("Классификация не удалась (%s) — берём запасной вариант", e)
        result = _fallback(text)
    except Exception:
        logger.exception("Неожиданная ошибка классификации")
        result = _fallback(text)

    await crm.classify(ticket_id, result)
