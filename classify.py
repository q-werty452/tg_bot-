"""
classify.py — разбор переписки для карточки обращения в панели.

После каждого ответа бота фоново перечитываем переписку и дозаполняем
карточку: тип обращения (обращение / справочный вопрос / прочее), тему,
категорию, ФИО с отчеством, телефон, район, населённый пункт, адрес, суть
и профильную организацию-исполнителя. Если житель заговорил о другой
проблеме — просим панель вынести её в отдельную заявку.

Правила надёжности:
  1. Разбор НИКОГДА не задерживает ответ жителю — он в отдельной фоновой
     задаче и с собственным таймаутом.
  2. Ответу модели не доверяем: не-JSON — пропуск; коды категорий, районов
     и организаций проверяются по спискам, телефон — по цифрам.
  3. Ошибка сети или модели — просто пропуск: карточка остаётся как есть.
  4. Что можно перезаписать, решает панель (ClassifyView в manas-crm):
     правки сотрудника бот не трогает.

Стоимость: один короткий запрос (~1-2 тыс. токенов) на сообщение жителя,
пока карточка не заполнена; заполнена — запросы прекращаются.
"""

import asyncio
import json
import logging

from providers import ProviderError, get_provider
from remote_config import remote

logger = logging.getLogger(__name__)

# Темы обращений на случай, если панель ещё ни разу не отвечала. Совпадают
# с `manage.py setup_categories` в manas-crm — по разделам из документов
# администрации области (вода, свет, дороги, земля, соцпомощь и т.д.).
FALLBACK_CATEGORIES = [
    {"slug": "water", "name": "Вода: питьевая и поливная"},
    {"slug": "power", "name": "Электроснабжение и уличное освещение"},
    {"slug": "zhkh", "name": "ЖКХ: канализация, отопление, газ"},
    {"slug": "cleanup", "name": "Мусор, санитария, благоустройство"},
    {"slug": "roads", "name": "Дороги, мосты и транспорт"},
    {"slug": "land", "name": "Земля и кадастр"},
    {"slug": "build", "name": "Строительство и архитектура"},
    {"slug": "social", "name": "Социальная помощь, пособия, занятость"},
    {"slug": "health", "name": "Здравоохранение"},
    {"slug": "education", "name": "Образование: школы и детсады"},
    {"slug": "safety", "name": "Правопорядок и безопасность"},
    {"slug": "agro", "name": "Сельское хозяйство и ветеринария"},
    {"slug": "docs", "name": "Документы и госуслуги"},
    {"slug": "officials", "name": "Жалобы на должностных лиц"},
    {"slug": "other", "name": "Прочее"},
]

EXTRACT_PROMPT = """\
Ты разбираешь переписку жителя Джалал-Абадской области (Кыргызстан) с ботом
аппарата полномочного представителя Президента КР. Верни СТРОГО один
JSON-объект без пояснений:

{{"kind": "", "title": "", "summary": "", "category": "", "district": "",
"settlement": "", "address": "", "last_name": "", "first_name": "",
"middle_name": "", "phone": "", "executor": "", "new_topic": false}}

Правила:
- kind: "appeal" — обращение или жалоба (проблема, просьба принять меры,
  нужна помощь); "question" — справочный вопрос (куда обратиться, адрес,
  телефон, порядок получения услуги); "other" — приветствие, благодарность,
  непонятное или не по теме.
- title: суть в 3-7 словах на языке жителя. Суть пока не ясна — "".
- summary: суть обращения в 1-2 предложениях по-русски, только факты из
  переписки. Не ясна — "".
- category: ровно один код из списка: {categories}
  Не подходит ничего — "other".
- district: код из списка: {districts}
  Только если район или город назван или однозначно следует из названия
  села (например, Кочкор-Ата — Ноокенский район). Иначе "".
- settlement: населённый пункт (город, село, айыл), как назвал житель.
- address: улица, дом или ориентир, как назвал житель.
- last_name, first_name, middle_name: фамилия, имя и отчество, ТОЛЬКО если
  житель сам назвал себя. Отчество у кыргызов часто вида «Асанович»,
  «Асан уулу», «Асан кызы» — это middle_name. Не названо — "".
- phone: номер телефона, если житель его написал, только цифры с кодом
  страны (996...). Иначе "".
- executor: id организации из списка кандидатов, которая отвечает за
  решение именно этого вопроса на этой территории. Ни одна явно не
  подходит или кандидатов нет — "".
  Кандидаты: {candidates}
- new_topic: true ТОЛЬКО если выше указано «Текущее обращение», а ПОСЛЕДНЕЕ
  сообщение жителя — о другой, не связанной с ним проблеме (другая беда
  или другое место). Ответы на вопросы бота, уточнения, «когда сделают?»,
  благодарности и данные о себе — это НЕ новая тема: false.
Данные о жителе бери только из его собственных сообщений, реплики бота —
лишь для контекста. Ничего не придумывай: нет сведений — пустая строка.
"""

KINDS = {"appeal", "question", "other"}

# Сколько раз за одну заявку можно перечитать переписку: страховка от
# бесконечных трат на очень длинный разговор.
MAX_EXTRACT_RUNS = 20

# Сколько последних реплик текущей темы показывать модели. Всё названное
# раньше уже лежит в Session.known и передаётся сводкой — гонять всю
# переписку на каждое сообщение незачем, это вдвое дороже ответа.
EXTRACT_MESSAGES = 10

# Когда обращение уже заполнено, короткие реплики («спасибо», «ок», «когда
# сделают?») не перечитываем: новая проблема так коротко не описывается.
SHORT_REPLY_CHARS = 25

# Без чего обращение считается незаполненным.
_APPEAL_FIELDS = ("summary", "last_name", "first_name", "phone", "district", "executor")

# Сведения о самом жителе — переживают смену заявки. Остальное в
# Session.known относится к текущей заявке и сбрасывается вместе с ней.
CITIZEN_KEYS = ("last_name", "first_name", "middle_name", "phone", "phone_source")

# Заявка -> идёт ли сейчас разбор и пришло ли за это время новое сообщение.
_RUNNING: dict[int, asyncio.Task] = {}
_DIRTY: set[int] = set()


def _clean_phone(raw) -> str:
    """Телефон из ответа модели: только цифры, похоже на номер — иначе ""."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "996" + digits[1:]          # 0555 123456 -> 996555123456
    return digits if 9 <= len(digits) <= 15 else ""


def parse_extract(raw: str, valid_categories: set[str], valid_districts: set[str],
                  valid_executors: set[str]) -> dict | None:
    """Разобрать JSON модели, не доверяя ему: всё лишнее и чужое — выкинуть."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        assert isinstance(data, dict)
    except (ValueError, AssertionError):
        logger.warning("Разбор переписки вернул не-JSON: %.120s", raw)
        return None

    def text(key: str, limit: int) -> str:
        return " ".join(str(data.get(key) or "").split())[:limit]

    result = {
        "kind": text("kind", 16).lower(),
        "title": text("title", 200),
        "summary": text("summary", 1000),
        "category": text("category", 50).lower(),
        "district": text("district", 50).lower(),
        "settlement": text("settlement", 150),
        "address": text("address", 250),
        "last_name": text("last_name", 120),
        "first_name": text("first_name", 120),
        "middle_name": text("middle_name", 120),
        "phone": _clean_phone(data.get("phone")),
        "executor": text("executor", 120),
        "new_topic": data.get("new_topic") is True,
    }
    if result["kind"] not in KINDS:
        result["kind"] = ""
    if result["category"] not in valid_categories:
        result["category"] = ""
    if result["district"] not in valid_districts:
        result["district"] = ""
    if result["executor"] not in valid_executors:
        result["executor"] = ""
    return result


def describe_known(known: dict) -> str:
    """
    Строка «что уже известно о жителе» для модели (см. prompts.compose_user_turn).

    Сам номер не передаём — модели достаточно знать, что он есть, а лишний
    раз гонять личные данные в запросах незачем.
    """
    parts = []
    name = " ".join(known.get(k, "") for k in ("last_name", "first_name", "middle_name")).strip()
    if name:
        parts.append(f"ФИО — {name}")
    if known.get("phone"):
        parts.append("номер телефона известен, не спрашивай")
    place = ", ".join(known[k] for k in ("district_name", "settlement", "address") if known.get(k))
    if place:
        parts.append(f"место — {place}")
    if known.get("pin"):
        parts.append("житель отправил точку на карте — улицу и дом не переспрашивай")
    return "; ".join(parts)


def forget_ticket(session) -> None:
    """
    Новая заявка — тема, место и исполнитель начинаются заново.

    ФИО и телефон — сведения о самом жителе, их оставляем: переспрашивать
    у человека имя в каждой заявке незачем.
    """
    session.known = {k: session.known[k] for k in CITIZEN_KEYS if k in session.known}
    session.candidates = []


def _topic_history(history: list[dict]) -> list[dict]:
    """История с начала текущей темы (после разделения заявки — только новая тема)."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("topic_start"):
            return history[i:]
    return history


def _mark_topic_start(history: list[dict], crm_id: int | None) -> None:
    """Пометить сообщение жителя, с которого началась новая тема."""
    for item in reversed(history):
        if item.get("role") == "user" and (crm_id is None or item.get("crm_id") == crm_id):
            item["topic_start"] = True
            return


def _conversation(history: list[dict]) -> str:
    """Переписка текстом для модели: последние реплики текущей темы, без фото."""
    lines = []
    for item in _topic_history(history)[-EXTRACT_MESSAGES:]:
        who = "Житель" if item.get("role") == "user" else "Бот"
        content = " ".join(str(item.get("content") or "").split())
        if content:
            lines.append(f"{who}: {content[:800]}")
    return "\n".join(lines)[-6000:]


def _last_citizen_text(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def _needs_more(session) -> bool:
    """Есть ли смысл ещё раз перечитывать переписку."""
    known = session.known
    if known.get("extract_runs", 0) >= MAX_EXTRACT_RUNS:
        return False
    if known.get("kind") != "appeal" or not all(known.get(k) for k in _APPEAL_FIELDS):
        return True            # тема или данные ещё могут проясниться
    # Обращение заполнено. Перечитываем, только если житель написал что-то
    # содержательное — вдруг это уже другая проблема (см. new_topic).
    return len(_last_citizen_text(session.history).strip()) >= SHORT_REPLY_CHARS


def _splittable(known: dict) -> bool:
    """Можно ли заводить отдельную заявку: текущая — уже понятное обращение."""
    return known.get("kind") == "appeal" and bool(known.get("summary"))


async def _extract_once(crm, session, provider_name: str) -> None:
    ticket_id = session.ticket_id
    # Сообщение, по которому сейчас судим о теме. Фиксируем ДО запроса к
    # модели: пока она думает, житель может прислать ещё одно.
    split_from = session.last_message_id
    categories = remote.categories or FALLBACK_CATEGORIES
    districts = remote.districts
    candidates = session.candidates[:6]
    prompt = EXTRACT_PROMPT.format(
        categories=", ".join(f'{c["slug"]} ({c["name"]})' for c in categories),
        districts=", ".join(f'{d["slug"]} ({d["name"]})' for d in districts) or "нет данных",
        candidates="; ".join(f'{c["id"]} — {c["name"]}' for c in candidates) or "нет",
    )
    conversation = _conversation(session.history)
    if not conversation:
        return
    known = session.known
    header = []
    already = describe_known({k: v for k, v in known.items() if k != "pin"})
    if already:
        header.append(f"Уже известно: {already}.")
    if _splittable(known):
        header.append(f"Текущее обращение: {known['summary']}")
    if header:
        conversation = "\n".join(header) + "\n\nПереписка:\n" + conversation
    known["extract_runs"] = known.get("extract_runs", 0) + 1
    try:
        provider = get_provider(provider_name)
        raw = await asyncio.wait_for(
            provider.ask(system=prompt,
                         history=[{"role": "user", "content": conversation}],
                         max_tokens=500, detailed=False),
            timeout=45,
        )
    except (ProviderError, asyncio.TimeoutError) as e:
        logger.info("Разбор переписки не удался (%s) — пропуск", e)
        return

    result = parse_extract(raw, {c["slug"] for c in categories},
                           {d["slug"] for d in districts},
                           {c["id"] for c in candidates})
    if result is None or session.ticket_id != ticket_id:
        return   # пока модель думала, заявка сменилась — этот разбор уже не про неё

    # Сведения о жителе запоминаем в любом случае: они не зависят от заявки.
    for key in ("last_name", "first_name", "middle_name"):
        if result[key]:
            known[key] = result[key]
    if result["phone"] and not known.get("phone"):
        known["phone"], known["phone_source"] = result["phone"], "stated"

    if result["new_topic"] and _splittable(known) and split_from:
        await _split(crm, session, ticket_id, split_from, result)
        return

    # Запоминаем для следующих ответов модели: что уже названо, повторно
    # не спрашиваем. Пустое не затирает известное.
    names = {d["slug"]: d["name"] for d in districts}
    for key in ("kind", "summary", "settlement", "address", "executor"):
        if result[key]:
            known[key] = result[key]
    if result["district"]:
        known["district"] = result["district"]
        known["district_name"] = names.get(result["district"], "")

    payload = _payload(result, known)
    if payload:
        await crm.classify(ticket_id, payload)


def _payload(result: dict, known: dict) -> dict:
    """Что отправить в панель. Панель сама решает, что из этого можно записать."""
    payload = {k: result[k] for k in (
        "kind", "district", "settlement", "address", "last_name", "first_name",
        "middle_name", "phone", "executor") if result[k]}
    meaningful = result["kind"] in ("appeal", "question")
    # Тему и категорию — только когда суть ясна: иначе на первое «здравствуйте»
    # заявка навсегда получала бы «Прочее» и заголовок-приветствие (панель
    # заполняет эти поля один раз). Заголовок шлём однажды за заявку.
    if meaningful and result["category"]:
        payload["category"] = result["category"]
    if meaningful and result["title"] and not known.get("title_sent"):
        payload["title"] = result["title"]
        known["title_sent"] = True
    if result["summary"]:
        payload["description"] = result["summary"]
    # Номер из канала (WhatsApp, кнопка Telegram) надёжнее продиктованного.
    if known.get("phone") and known.get("phone_source") in ("channel", "shared"):
        payload["phone"] = known["phone"]
    return payload


async def _split(crm, session, ticket_id: int, split_from: int, result: dict) -> None:
    """
    Житель заговорил о другой проблеме: новая заявка с этого сообщения.

    Под замком сессии: пока идёт перенос, бот не должен писать ответ
    в старую заявку.
    """
    async with session.lock:
        if session.ticket_id != ticket_id:
            return
        record = await crm.split(ticket_id, split_from, title=result["title"])
        if not record or not record.get("ticket_id"):
            return
        logger.info("Заявка %s: житель сменил тему — новая заявка %s",
                    ticket_id, record.get("number"))
        session.ticket_id = record["ticket_id"]
        forget_ticket(session)
        _mark_topic_start(session.history, split_from)


async def update_ticket(crm, session, provider_name: str) -> None:
    """
    Фоновая задача: перечитать переписку и дозаполнить карточку.

    Если разбор этой заявки уже идёт, повторно не запускаем, а помечаем,
    что после него нужен ещё один проход — по свежей переписке. Если по
    ходу разбора заявка разделилась, тут же разбираем новую. Никогда не
    задерживает ответ жителю и не падает наружу.
    """
    ticket_id = session.ticket_id
    if ticket_id is None:
        return
    if ticket_id in _RUNNING:
        _DIRTY.add(ticket_id)
        return
    me = asyncio.current_task()
    _RUNNING[ticket_id] = me
    try:
        while _needs_more(session):
            _DIRTY.discard(ticket_id)
            try:
                await _extract_once(crm, session, provider_name)
            except Exception:
                logger.exception("Неожиданная ошибка разбора переписки")
                return
            if session.ticket_id != ticket_id:
                # Заявка разделилась (или сменилась) — дальше разбираем новую.
                new_id = session.ticket_id
                if new_id is None or new_id in _RUNNING:
                    return
                _RUNNING.pop(ticket_id, None)
                _DIRTY.discard(ticket_id)
                ticket_id = new_id
                _RUNNING[ticket_id] = me
                continue
            if ticket_id not in _DIRTY:
                return
    finally:
        if _RUNNING.get(ticket_id) is me:
            _RUNNING.pop(ticket_id, None)
        _DIRTY.discard(ticket_id)
