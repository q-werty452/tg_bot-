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


# ------------------------------------------------------- доводка позже

# И заголовок, и адрес/район при создании заявки ставятся по одной фразе
# («Привет» — и всё, адреса там обычно ещё нет), поэтому через некоторое
# время смотрим на уже сложившуюся переписку и дозаполняем то, что стало
# известно позже. Заголовок при этом перезаписывается принудительно (ровно
# один раз — дальше панель сама не даст сработать повторно, см. RetitleView
# в manas-crm), а адрес/район/категория — как обычно, только если ещё
# пустые (см. ClassifyView, чтобы не затирать правки сотрудника).
RETITLE_DELAY_SECONDS = 300

RETITLE_PROMPT = """\
Определи короткую тему обращения жителя в мэрию города Манас (Кыргызстан) по
всей переписке ниже. Ответь СТРОГО одной строкой — суть в 3-7 слов, на языке
обращения, без кавычек и пояснений. Если по переписке до сих пор не ясно, о
чём вообще речь (например, житель просто поздоровался и ничего не спросил),
ответь ровно словом: НЕЯСНО
"""


async def refine_ticket(crm, ticket_id: int, provider_name: str,
                        delay: float = RETITLE_DELAY_SECONDS) -> None:
    """
    Фоновая задача: подождать, затем досмотреть всю переписку и дозаполнить
    заголовок и адрес/район/категорию, если раньше по одной фразе их было
    не определить.

    Как и classify_ticket — никогда не мешает ответу жителю (отдельная
    задача, свой таймаут) и не падает при сбое сети или модели: заявка
    просто остаётся как есть.
    """
    await asyncio.sleep(delay)

    history = await crm.history(ticket_id, limit=30)
    if not history or not history.get("messages"):
        return
    conversation = "\n".join(
        f"{m['author']}: {m['text']}" for m in history["messages"] if m.get("text")
    ).strip()
    if not conversation:
        return

    try:
        provider = get_provider(provider_name)
    except ProviderError:
        return

    # Заголовок — отдельным, узким запросом: тут нужна короткая фраза,
    # а не JSON с категорией и районом.
    try:
        raw_title = await asyncio.wait_for(
            provider.ask(system=RETITLE_PROMPT,
                         history=[{"role": "user", "content": conversation[:4000]}],
                         max_tokens=60, detailed=False),
            timeout=30,
        )
        title = raw_title.strip().strip('"').strip("«»").strip()
        if title and title.upper() != "НЕЯСНО":
            await crm.retitle(ticket_id, title[:200])
    except (ProviderError, asyncio.TimeoutError) as e:
        logger.info("Уточнение темы не удалось (%s) — пропуск", e)
    except Exception:
        logger.exception("Неожиданная ошибка при уточнении темы")

    # Адрес/район/категория — тем же промптом и разбором, что и при
    # создании заявки, только по всей переписке, а не по первой фразе.
    categories = remote.categories or FALLBACK_CATEGORIES
    districts = remote.districts
    prompt = PROMPT.format(
        categories=", ".join(f'{c["slug"]} ({c["name"]})' for c in categories),
        districts=", ".join(f'{d["slug"]} ({d["name"]})' for d in districts)
                  or "нет данных",
    )
    try:
        raw = await asyncio.wait_for(
            provider.ask(system=prompt,
                         history=[{"role": "user", "content": conversation[:4000]}],
                         max_tokens=300, detailed=False),
            timeout=45,
        )
        result = parse_answer(raw, {c["slug"] for c in categories},
                              {d["slug"] for d in districts}, conversation)
    except (ProviderError, asyncio.TimeoutError) as e:
        logger.info("Повторная классификация не удалась (%s) — пропуск", e)
        return
    except Exception:
        logger.exception("Неожиданная ошибка повторной классификации")
        return

    await crm.classify(ticket_id, result)
