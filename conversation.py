"""
conversation.py — общая для Telegram и WhatsApp логика запроса к ИИ и
восстановления истории после перезапуска.

Вынесено из bot.py: это единственные два куска логики обработки сообщения,
которые не зависят ни от aiogram, ни от какого-либо другого транспорта —
whatsapp_bot.py использует их без изменений. Остальная оркестрация
(respond(), запись в панель, доставка ответа, обработка ошибок конкретного
транспорта) намеренно НЕ обобщается и живёт отдельно в каждом боте — см.
комментарий в whatsapp_bot.py о том, почему это осознанный выбор.
"""

import asyncio
import logging
import re

from prompts import (
    NO_SEARCH_RULES,
    WEB_SEARCH_RULES,
    compose_user_turn,
    parse_search_request,
)
from providers import (
    ProviderError,
    fallback_chain,
    get_provider,
    has_key,
    is_available,
    mark_unavailable,
)

logger = logging.getLogger(__name__)


async def ask_with_fallback(
    preferred: str, system: str, history: list[dict], max_tokens: int, detailed: bool
) -> tuple[str, str]:
    """
    Спросить модель, а если она подвела — незаметно переспросить у соседней.

    Зачем: у бесплатных тарифов есть лимит запросов. Упереться в него посреди
    показа или рабочего дня нельзя, поэтому при ошибке самого провайдера
    (лимит, сеть, битый ключ) бот молча берёт следующую доступную модель.
    Человек видит просто ответ, а в лог пишется, кто именно ответил.

    Ошибки другого рода (сработал фильтр, ответ не влез в лимит длины)
    не переигрываем: соседняя модель ответит так же (см. ProviderError.retryable).

    Возвращает пару: текст ответа и имя модели, которая ответила.
    """
    chain = fallback_chain(preferred)
    if not chain:
        raise ProviderError("Не настроена ни одна модель. Проверь ключи в .env")

    last_error: ProviderError | None = None

    for name in chain:
        try:
            provider = get_provider(name)
        except ProviderError as e:
            # Нет ключа или не установлена библиотека — просто идём дальше.
            last_error = e
            continue

        try:
            answer = await provider.ask(
                system=system, history=history, max_tokens=max_tokens, detailed=detailed
            )
        except ProviderError as e:
            if not e.retryable:
                raise  # виноват вопрос, а не провайдер — переспрашивать незачем
            last_error = e
            mark_unavailable(name)  # отставим на пару минут, чтобы не спотыкаться
            logger.warning("Провайдер %s подвёл (%s), пробую следующего", name, e)
            continue

        if name != preferred:
            logger.info("Ответ получен через запасную модель %s (вместо %s)", name, preferred)
        return answer, name

    # Все модели по очереди отказали.
    raise last_error or ProviderError("Ни одна модель не ответила. Попробуй позже.")


# Поиск в интернете тратит токены на прочитанные страницы, поэтому ответ ему
# даём длиннее обычного диалогового: иначе ссылка на источник не влезает.
SEARCH_MAX_TOKENS = 900

# Последняя страховка, если модель упорно просит поиск, а его нет.
NO_DATA_TEXT = ("Точных сведений об этом у меня сейчас нет. Уточните, пожалуйста, "
                "в аппарате полномочного представителя Президента КР "
                "в Джалал-Абадской области.")


def with_context(history: list[dict], *, knowledge: str = "",
                 citizen: str = "") -> list[dict]:
    """
    Копия истории, где к последнему сообщению жителя приклеены справочные
    блоки (см. prompts.compose_user_turn). Сама история сессии не меняется:
    блоки нужны только для текущего запроса.
    """
    if not history or history[-1].get("role") != "user" or not (knowledge or citizen):
        return history
    last = dict(history[-1])
    last["content"] = compose_user_turn(last.get("content") or "",
                                        knowledge=knowledge, citizen=citizen)
    return [*history[:-1], last]


async def ask_assistant(
    preferred: str, system: str, history: list[dict], max_tokens: int, detailed: bool
) -> tuple[str, str]:
    """
    Ответ модели с поиском в интернете по требованию.

    Модель отвечает как обычно. Если в справочных данных нужного факта не
    нашлось, она вместо ответа пишет «ПОИСК: запрос» — тогда делаем второй
    запрос, уже с поиском (он есть только у OpenAI). Нет ключа OpenAI или
    поиск сломался — переспрашиваем без поиска, чтобы модель честно сказала,
    что данных нет. Служебную строку житель не видит ни при каком исходе.

    Возвращает текст и имя модели, ответившей на ПЕРВЫЙ запрос: по нему
    bot.py решает, не пора ли переключить чат на запасную модель, и поиск
    через OpenAI не должен уводить с модели, которую выбрал житель.
    """
    answer, answered_by = await ask_with_fallback(
        preferred, system, history, max_tokens, detailed)
    query = parse_search_request(answer)
    if query is None:
        return answer, answered_by

    query = query or _last_citizen_text(history) or "вопрос жителя"
    logger.info("Справочник не помог, ищу в интернете: %s", query)
    if has_key("openai") and is_available("openai"):
        try:
            provider = get_provider("openai")
            found = await provider.search_web(
                system + WEB_SEARCH_RULES.format(query=query), history,
                max(max_tokens, SEARCH_MAX_TOKENS))
            if parse_search_request(found) is None:
                return found, answered_by
            logger.warning("Ответ с поиском снова просит поиск — отвечаю без него")
        except ProviderError as e:
            logger.warning("Поиск в интернете не удался: %s", e)

    try:
        answer, _ = await ask_with_fallback(
            preferred, system + NO_SEARCH_RULES, history, max_tokens, detailed)
    except ProviderError as e:
        # Первый ответ уже был — значит, модели в целом работают. Не
        # показываем жителю ошибку из-за второго, служебного запроса.
        logger.warning("Повторный запрос без поиска не удался: %s", e)
        return NO_DATA_TEXT, answered_by
    if parse_search_request(answer) is not None:
        answer = NO_DATA_TEXT
    return answer, answered_by


def _last_citizen_text(history: list[dict]) -> str:
    """Последнее сообщение жителя без служебных блоков — запасной запрос."""
    for item in reversed(history):
        if item.get("role") == "user":
            return strip_service(item.get("content") or "")[:200]
    return ""


# Служебные пометки в квадратных скобках в начале текста: «[Голосовое ...]»,
# «[Житель приложил фотографию]» и блоки справочника. Для поиска это шум.
_SERVICE_BLOCK_RE = re.compile(r"^\s*\[[^\]]*\]\s*")


def strip_service(text: str) -> str:
    """Текст жителя без служебных пометок в начале (и без блоков справочника)."""
    marker = "[Сообщение жителя]"
    if marker in text:
        text = text.split(marker, 1)[1]
    while True:
        cleaned = _SERVICE_BLOCK_RE.sub("", text, count=1)
        if cleaned == text:
            return cleaned.strip()
        text = cleaned


# ---------------------------------------------------------------- фоновые задачи

# asyncio держит на задачу только слабую ссылку: задачу без собственной
# ссылки сборщик мусора может прибить на полпути (см. документацию
# asyncio.create_task). Поэтому фоновые задачи бота живут в этом множестве.
_BACKGROUND: set[asyncio.Task] = set()


def background(coro) -> asyncio.Task:
    """Запустить корутину фоном, не теряя на неё ссылку."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


# ------------------------------------------------------------------ справочник
#
# Справочник организаций области (knowledge.py) целиком — около 47 тысяч
# токенов: слать его с каждым вопросом дорого и бессмысленно. Поэтому на
# каждое сообщение ищем 3-5 подходящих записей (адреса, телефоны, куда
# обращаться) и подставляем только их — примерно 300-600 токенов.

# Поиск со смысловым слоем ходит в сеть за эмбеддингом вопроса. Дольше этого
# жителя не держим — ищем только по словам.
LOOKUP_TIMEOUT = 8.0

# Сколько символов справочных данных подставлять к вопросу. В развёрнутом
# режиме ответ подробнее — и данных ему можно дать чуть больше.
KNOWLEDGE_MAX_CHARS = {"chat": 1800, "detailed": 2600}

# Короткое сообщение вроде «а телефон?» без предыдущего вопроса ничего не
# найдёт — к такому подклеиваем прошлое сообщение жителя.
SHORT_QUERY_CHARS = 40

_knowledge = None   # KnowledgeBase или None, если справочник не подключён
_embed = None       # функция эмбеддингов или None (тогда поиск только по словам)
_initialized = False


def init_knowledge():
    """
    Загрузить справочник (один раз на процесс) и фоном досчитать эмбеддинги.

    Нет папки со справочником — бот работает как раньше, без него: это
    штатный режим, а не ошибка (данные лежат в отдельном приватном репо).
    Повторный вызов (бот перезапустил опрос с новым токеном) ничего не делает.
    """
    global _knowledge, _embed, _initialized
    if _initialized:
        return _knowledge
    _initialized = True
    from knowledge import get_knowledge_base, make_embedder

    kb = get_knowledge_base()
    _knowledge = kb if kb.enabled else None
    if _knowledge is None:
        logger.warning("Справочник не найден — бот отвечает без него")
        return None
    _embed = make_embedder()
    if _embed is not None:
        background(kb.prepare(_embed))
    logger.info("Справочник загружен: %s", kb.stats())
    return kb


def search_query(history: list[dict]) -> str:
    """Текст для поиска: последнее сообщение жителя (и предыдущее, если оно короткое)."""
    texts: list[str] = []
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        text = strip_service(item.get("content") or "")
        if text:
            texts.append(text)
        if len(texts) == 2 or (texts and len(texts[0]) >= SHORT_QUERY_CHARS):
            break
    return " ".join(reversed(texts))[:500]


def _merge_candidates(found: list[dict], previous: list[dict], limit: int = 8) -> list[dict]:
    """Свежие кандидаты в исполнители — первыми, без повторов."""
    merged, seen = [], set()
    for item in [*found, *previous]:
        if item.get("id") and item["id"] not in seen:
            seen.add(item["id"])
            merged.append(item)
    return merged[:limit]


async def knowledge_context(session, mode: str) -> str:
    """
    Найти в справочнике записи к последнему сообщению жителя.

    Возвращает готовый блок для prompts.compose_user_turn ("" — ничего не
    нашлось) и попутно запоминает в сессии организации-кандидаты: из них
    classify.update_ticket выбирает исполнителя заявки. Никогда не падает:
    без справочника житель всё равно получит ответ.
    """
    kb = _knowledge
    if kb is None:
        return ""
    query = search_query(session.history)
    if not query:
        return ""
    hint = ", ".join(session.known[k] for k in ("district_name", "settlement")
                     if session.known.get(k))
    try:
        try:
            hits, arts = await asyncio.wait_for(
                _lookup(kb, query, hint, _embed), LOOKUP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Поиск по справочнику дольше %.0f с — ищу только по словам",
                           LOOKUP_TIMEOUT)
            hits, arts = await _lookup(kb, query, hint, None)
        found = kb.candidate_organizations(hits)
        if found:
            session.candidates = _merge_candidates(found, session.candidates)
        return kb.format_context(hits, arts, max_chars=KNOWLEDGE_MAX_CHARS.get(mode, 1800))
    except Exception:
        logger.exception("Поиск по справочнику упал — отвечаю без него")
        return ""


async def _lookup(kb, query: str, hint: str, embed):
    hits = await kb.search(query, embed=embed, territory_hint=hint, limit=5)
    arts = await kb.search_constitution(query, embed=embed, limit=2)
    return hits, arts

async def restore_history(session, chat_id: int, crm, history_limit: int,
                          channel: str = "telegram") -> None:
    """Один раз за жизнь сессии подтянуть контекст диалога из панели."""
    if session.restored or session.history or not crm.enabled:
        session.restored = True
        return
    session.restored = True
    context = await crm.context(chat_id, channel=channel)
    if not context:
        return
    if context.get("is_open"):
        session.ticket_id = context.get("ticket_id")
    for item in context.get("messages") or []:
        role = "user" if item.get("author") == "citizen" else "assistant"
        text = (item.get("text") or "").strip()
        if text:
            session.add(role, text, history_limit)
    if session.history:
        logger.info("Чат %s: восстановлено %s сообщений из панели",
                    chat_id, len(session.history))
