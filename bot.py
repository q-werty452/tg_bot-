"""
bot.py — точка входа. Здесь живёт вся логика Telegram.

Как это работает в двух словах:
1. Библиотека aiogram постоянно спрашивает у Telegram «есть новые сообщения?»
   (это называется long polling).
2. Когда сообщение приходит, aiogram смотрит на фильтры (@dp.message(...))
   и вызывает подходящую функцию-обработчик (handler).
3. Обработчик берёт сессию чата, вызывает нужного ИИ-провайдера
   и отправляет ответ обратно.

Весь код асинхронный (async/await): пока бот ждёт ответ от модели
для одного пользователя, он спокойно обслуживает остальных.
"""

import asyncio
import contextlib
import logging
import logging.handlers
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from classify import classify_ticket
from config import settings
from crm import crm
from icons import ICONS, clean_text
from prompts import MAX_TOKENS, build_system
from remote_config import remote
from providers import (
    ProviderError,
    available_providers,
    fallback_chain,
    get_provider,
    mark_unavailable,
    warm_up,
)
from storage import MODE_TITLES, PROVIDER_TITLES, Mode, Storage
from utils import split_text

logger = logging.getLogger(__name__)

# Dispatcher — «маршрутизатор»: решает, какая функция обработает какое сообщение.
dp = Dispatcher()

# Хранилище сессий (см. storage.py).
storage = Storage(default_provider=settings.default_provider)

# Куда складывать скачанные из Telegram фотографии до отправки в панель.
from pathlib import Path
MEDIA_DIR = Path(__file__).parent / "incoming_media"

# Как часто убирать из памяти давно неактивные чаты.
CLEANUP_INTERVAL = 60 * 60  # раз в час

# Периоды фоновых циклов, секунды.
OUTBOX_INTERVAL = 2.5     # разбор исходящих из панели
CONFIG_INTERVAL = 30      # опрос конфигурации
HEARTBEAT_INTERVAL = 60   # сигнал «я жив»

# Кнопки «Помогло / Не помогло» под ответами бота. Пока выключены по просьбе
# заказчика — жителю они мешают. Код оценок никуда не делся: обработчик
# нажатий, запись в карточку и показ в панели на месте, включить обратно —
# поставить True.
RATING_BUTTONS_ENABLED = False

# Счётчики для сердцебиения: панель показывает их на странице настроек.
COUNTERS = {"messages": 0, "answers": 0, "quick_answers": 0, "errors": 0}

# Токен, на котором сейчас идёт опрос (нужен для перезапуска при смене).
_active_token: str = ""


# ---------------------------------------------------------------- клавиатуры

def mode_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора режима под сообщением.

    callback_data — это строка, которая прилетит боту при нажатии кнопки.
    Формат придумываем сами; здесь это "mode:detailed" / "mode:chat".
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=MODE_TITLES[Mode.DETAILED], callback_data="mode:detailed")],
            [InlineKeyboardButton(text=MODE_TITLES[Mode.CHAT], callback_data="mode:chat")],
        ]
    )


def provider_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора модели — показываем только те, для которых есть ключ."""
    rows = [
        [InlineKeyboardButton(text=PROVIDER_TITLES[name], callback_data=f"provider:{name}")]
        for name in available_providers()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rating_keyboard(crm_message_id: int) -> InlineKeyboardMarkup:
    """Кнопки «помогло / не помогло» под ответом ИИ (без эмодзи, по стилю)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Помогло",
                             callback_data=f"rate:{crm_message_id}:up"),
        InlineKeyboardButton(text="Не помогло",
                             callback_data=f"rate:{crm_message_id}:down"),
    ]])


def _profile(message: Message) -> dict:
    """Данные жителя для панели."""
    user = message.from_user
    return {
        "tg_user_id": user.id if user else message.chat.id,
        "chat_id": message.chat.id,
        "first_name": (user.first_name if user else "") or "",
        "last_name": (user.last_name if user else "") or "",
        "username": (user.username if user else "") or "",
    }


# ------------------------------------------------------------ вспомогательное

@contextlib.asynccontextmanager
async def typing(bot: Bot, chat_id: int):
    """
    Показывает статус «печатает…», пока внутри блока идёт работа.

    Telegram гасит этот статус через ~5 секунд, поэтому в фоне крутится
    задача, которая обновляет его каждые 4 секунды.
    Используется так:  async with typing(bot, chat_id): ...долгая работа...
    """

    async def loop():
        while True:
            # Статус — дело третьестепенное: если он не отправился
            # (человек заблокировал бота, моргнула сеть), молча идём дальше.
            # Иначе сбой статуса уронил бы ответ, который уже почти готов.
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    # Пропускаем ход планировщику: без этого задача не успеет отправить
    # первый статус, если ответ придёт очень быстро (например, из кэша).
    await asyncio.sleep(0)
    try:
        yield
    finally:
        task.cancel()  # обязательно гасим фоновую задачу, что бы ни случилось
        with contextlib.suppress(BaseException):
            await task


async def send_long(message: Message, text: str, reply_markup=None) -> None:
    """
    Отправить текст, автоматически разрезав его на части по лимиту Telegram.

    reply_markup (кнопки оценки) вешается только на последний кусок —
    оценка относится к ответу целиком.
    """
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await _send_with_retry(message, chunk,
                               reply_markup=reply_markup if is_last else None)


async def _send_with_retry(message: Message, text: str, attempts: int = 3,
                           reply_markup=None) -> None:
    """
    Отправить кусок текста, пережив троттлинг Telegram.

    При частых сообщениях Telegram отвечает «подожди N секунд»
    (TelegramRetryAfter). Ждём столько, сколько просят, и повторяем.
    """
    for attempt in range(attempts):
        try:
            await message.answer(text, reply_markup=reply_markup)
            return
        except TelegramRetryAfter as e:
            if attempt == attempts - 1:
                raise
            logger.warning("Telegram просит подождать %s с", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)


async def safe_edit(callback: CallbackQuery, text: str) -> None:
    """
    Заменить текст сообщения с кнопками.

    Сообщение может быть недоступно (слишком старое) или уже содержать
    такой же текст — Telegram в обоих случаях вернёт ошибку. Для нас это
    не повод падать: просто отправим ответ новым сообщением.
    """
    target = callback.message
    if target is None:
        return
    try:
        await target.edit_text(text)
    except TelegramBadRequest:
        with contextlib.suppress(Exception):
            await target.answer(text)


# -------------------------------------------------------------------- команды

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    session = storage.get(message.chat.id)
    await crm.subscription(message.chat.id, True)
    await message.answer(
        "Привет! Я мост между тобой и ИИ.\n\n"
        f"{ICONS.bullet} Модель: {PROVIDER_TITLES[session.provider]}\n"
        f"{ICONS.bullet} Режим: {MODE_TITLES[session.mode]}\n\n"
        "Просто напиши мне вопрос — и я отвечу.\n\n"
        "Команды:\n"
        f"{ICONS.dot} /mode — переключить режим ответа\n"
        f"{ICONS.dot} /model — переключить ИИ\n"
        f"{ICONS.dot} /reset — забыть историю диалога\n"
        f"{ICONS.dot} /status — что сейчас включено\n"
        f"{ICONS.dot} /help — подробная справка"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться\n\n"
        "Два режима ответа:\n\n"
        f"{MODE_TITLES[Mode.DETAILED]}\nПолный структурированный ответ "
        "с объяснениями и примерами. Для «объясни тему», «напиши код», «разбери задачу».\n\n"
        f"{MODE_TITLES[Mode.CHAT]}\nЖивой диалог: короткие реплики на 1-3 предложения, "
        "встречные вопросы и комментарии. Для обсуждения и брейншторма.\n\n"
        "Бот помнит контекст переписки, поэтому можно задавать уточняющие вопросы "
        "вроде «а подробнее?». Чтобы начать тему с нуля — /reset.\n\n"
        "Команды: /mode, /model, /reset, /status"
    )


@dp.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    session = storage.get(message.chat.id)
    await message.answer(
        f"Текущий режим: {MODE_TITLES[session.mode]}\nВыбери новый:",
        reply_markup=mode_keyboard(),
    )


@dp.message(Command("model"))
async def cmd_model(message: Message) -> None:
    session = storage.get(message.chat.id)
    names = available_providers()
    if len(names) < 2:
        # Ключ только один — переключать не на что, кнопки были бы обманом.
        await message.answer(
            f"Сейчас доступна одна модель: {PROVIDER_TITLES[session.provider]}\n"
            "Чтобы появился выбор, добавь второй ключ в файл .env."
        )
        return
    await message.answer(
        f"Текущая модель: {PROVIDER_TITLES[session.provider]}\nВыбери другую:",
        reply_markup=provider_keyboard(),
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    storage.get(message.chat.id).clear()
    # Открытая карточка в панели закрывается: следующее сообщение — новая тема.
    await crm.close_chat(message.chat.id)
    await message.answer(f"{ICONS.reset}  История очищена. Начинаем с чистого листа.")


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    session = storage.get(message.chat.id)
    model_names = {
        "claude": settings.anthropic_model,
        "openai": settings.openai_model,
        "gemini": settings.google_model,
    }
    model_name = model_names.get(session.provider, "?")
    await message.answer(
        f"{ICONS.status}  Текущие настройки\n\n"
        f"{ICONS.bullet} Модель: {PROVIDER_TITLES[session.provider]}\n"
        f"{ICONS.dot} {model_name}\n"
        f"{ICONS.bullet} Режим: {MODE_TITLES[session.mode]}\n"
        f"{ICONS.bullet} Сообщений в памяти: {len(session.history)} из {settings.history_limit}"
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    """Отписаться от рассылок мэрии."""
    await crm.subscription(message.chat.id, False)
    await message.answer(
        f"{ICONS.ok}  Рассылки отключены. Включить обратно: /start"
    )


# --------------------------------------------------------- нажатия на кнопки

@dp.callback_query(F.data.startswith("mode:"))
async def switch_mode(callback: CallbackQuery) -> None:
    # F.data.startswith(...) — фильтр: обрабатываем только кнопки режима.
    if callback.message is None:
        await callback.answer()
        return

    value = callback.data.split(":", 1)[1]
    try:
        mode = Mode(value)
    except ValueError:
        # Кнопка из старой версии бота — тихо игнорируем, но пишем в лог.
        logger.warning("Неизвестный режим в кнопке: %s", value)
        await callback.answer("Кнопка устарела, вызови /mode заново.", show_alert=True)
        return

    session = storage.get(callback.message.chat.id)
    session.mode = mode
    await safe_edit(callback, f"{ICONS.ok}  Режим переключён: {MODE_TITLES[mode]}")
    # answer() обязателен: убирает «часики» на кнопке в интерфейсе Telegram.
    await callback.answer()


@dp.callback_query(F.data.startswith("provider:"))
async def switch_provider(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    name = callback.data.split(":", 1)[1]
    if name not in available_providers():
        # Ключ убрали из .env уже после того, как кнопка была показана.
        await callback.answer("Эта модель сейчас недоступна.", show_alert=True)
        return

    session = storage.get(callback.message.chat.id)
    session.provider = name
    await safe_edit(
        callback,
        f"{ICONS.ok}  Модель переключена: {PROVIDER_TITLES[name]}\n"
        "История диалога сохранена — можно продолжать.",
    )
    await callback.answer()


# ------------------------------------------------- запрос к ИИ с подстраховкой

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


# --------------------------------------------------------- основной обработчик

@dp.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    """Любое текстовое сообщение, не являющееся командой, идёт в обработку."""
    user_text = (message.text or "").strip()
    if user_text:
        await respond(message, bot, user_text)


@dp.message(F.photo | F.document)
async def handle_media(message: Message, bot: Bot) -> None:
    """
    Фотография или документ от жителя.

    Файл скачивается и уходит в панель вместе с подписью — отдел увидит его
    в карточке. Модель картинку не видит, поэтому для ответа ИИ подпись
    дополняется пометкой, что приложено фото.
    """
    caption = (message.caption or "").strip()

    if message.photo:
        tg_file = message.photo[-1]  # последний размер — самый большой
        filename = f"{message.chat.id}_{message.message_id}.jpg"
    else:
        doc = message.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.answer(
                f"{ICONS.error}  Файл больше 20 МБ — Telegram не даёт ботам "
                "скачивать такие. Пришли, пожалуйста, поменьше."
            )
            return
        tg_file = doc
        filename = f"{message.chat.id}_{message.message_id}_{doc.file_name or 'file'}"

    MEDIA_DIR.mkdir(exist_ok=True)
    path = MEDIA_DIR / filename
    try:
        await bot.download(tg_file, destination=path)
    except Exception:
        logger.exception("Не удалось скачать файл из Telegram")
        await message.answer(
            f"{ICONS.error}  Не получилось принять файл. Попробуй ещё раз."
        )
        return

    model_text = caption or "Житель прислал фотографию без подписи."
    if caption:
        model_text = f"[Житель приложил фотографию] {caption}"
    await respond(message, bot, model_text,
                  file_paths=[str(path)], citizen_text=caption)


async def respond(message: Message, bot: Bot, user_text: str,
                  file_paths: list[str] | None = None,
                  citizen_text: str | None = None) -> None:
    """
    Общий путь любого обращения: панель -> (готовый ответ | модель) -> житель.

    citizen_text — что записать в карточку как сообщение жителя, если оно
    отличается от текста для модели (случай фотографии).
    """
    session = storage.get(message.chat.id)
    COUNTERS["messages"] += 1

    # 1. Бот выключен в панели — вежливый текст техработ и тишина.
    if not remote.bot_enabled:
        await message.answer(f"{ICONS.info}  {remote.maintenance_text}")
        return

    if not available_providers():
        await message.answer(
            f"{ICONS.error}  Не настроена ни одна модель. Проверь ключи в файле .env"
        )
        return

    # 2. Если для этого чата уже идёт ответ — предупреждаем и встаём в очередь.
    #    Замок гарантирует, что сообщения обрабатываются по одному и история
    #    не перемешается (см. Session.lock в storage.py).
    if session.lock.locked():
        with contextlib.suppress(Exception):
            await message.answer(f"{ICONS.info}  Секунду, дописываю предыдущий ответ…")

    async with session.lock:
        # 3. После перезапуска бот ничего не помнит — поднимаем хвост
        #    переписки из панели, чтобы диалог продолжился, а не начался заново.
        await _restore_history(session, message.chat.id)

        # 4. Сообщение жителя уходит в карточку. Панель отвечает, кто ведёт
        #    диалог: ИИ или сотрудник. Панель лежит — обращение в очереди
        #    на диске, а житель всё равно получит ответ ИИ.
        record = await crm.incoming(
            _profile(message),
            citizen_text if citizen_text is not None else user_text,
            tg_message_id=message.message_id,
            file_paths=file_paths,
        )
        if record:
            session.ticket_id = record.get("ticket_id")
            if record.get("created") and session.ticket_id:
                # Новая карточка — фоново определяем тему. Ответа не ждём.
                asyncio.create_task(classify_ticket(
                    crm, session.ticket_id, user_text, session.provider))
            if record.get("answer_mode") == "staff":
                # Разговор перехватил сотрудник: ИИ молчит, ответ придёт
                # через очередь исходящих. Историю ИИ не трогаем.
                return

        # 5. Готовый ответ из панели — отдаём дословно, модель не зовём.
        quick = remote.match_quick_answer(user_text)
        if quick:
            COUNTERS["quick_answers"] += 1
            answer = quick["answer"]
            session.add("user", user_text, settings.history_limit)
            session.add("assistant", answer, settings.history_limit)
            if quick.get("id"):
                asyncio.create_task(crm.answer_hit(quick["id"]))
            await _deliver_answer(message, session, answer)
            return

        # 6. Кладём вопрос в историю и спрашиваем модель. Промпт собирается
        #    из данных панели: тексты, справочник контактов, режим фактов.
        session.add("user", user_text, settings.history_limit)

        detailed = session.mode is Mode.DETAILED
        system = build_system(
            session.mode,
            facts=remote.facts,
            invent=remote.invent_facts,
            override=remote.prompt_override(detailed),
        )
        max_tokens = MAX_TOKENS[session.mode]

        try:
            async with typing(bot, message.chat.id):
                answer, answered_by = await ask_with_fallback(
                    preferred=session.provider,
                    system=system,
                    history=session.history,
                    max_tokens=max_tokens,
                    detailed=detailed,
                )
        except ProviderError as e:
            # Ожидаемая ошибка (нет ключа, лимит, нет сети) — показываем текст
            # пользователю. Вопрос убираем из истории, чтобы не «залип».
            session.drop_last()
            COUNTERS["errors"] += 1
            logger.info("Провайдер %s вернул ошибку: %s", session.provider, e)
            await message.answer(f"{ICONS.error}  {e}")
            return
        except asyncio.CancelledError:
            session.drop_last()
            raise
        except Exception:
            session.drop_last()
            COUNTERS["errors"] += 1
            logger.exception("Ошибка при запросе к модели %s", session.provider)
            await message.answer(f"{ICONS.error}  Что-то пошло не так. Попробуй ещё раз.")
            return

        # 7. Если ответила запасная модель — переключаем на неё и этот чат.
        if answered_by != session.provider:
            logger.info("Чат %s переключён с %s на %s",
                        message.chat.id, session.provider, answered_by)
            session.provider = answered_by

        # 8. Чистим эмодзи, которые модель могла вставить вопреки промпту.
        answer = clean_text(answer)
        if not answer:
            session.drop_last()
            await message.answer(
                f"{ICONS.error}  Модель прислала пустой ответ. Попробуй переформулировать."
            )
            return

        session.add("assistant", answer, settings.history_limit)
        COUNTERS["answers"] += 1
        await _deliver_answer(message, session, answer)


async def _deliver_answer(message: Message, session, answer: str) -> None:
    """
    Отправить ответ жителю и записать его в карточку.

    Если панель приняла запись, под ответом появляются кнопки оценки —
    они привязаны к сообщению в карточке.
    """
    crm_message_id = None
    if session.ticket_id:
        crm_message_id = await crm.ai_message(session.ticket_id, answer)
    markup = (rating_keyboard(crm_message_id)
              if crm_message_id and RATING_BUTTONS_ENABLED else None)
    await send_long(message, answer, reply_markup=markup)


async def _restore_history(session, chat_id: int) -> None:
    """Один раз за жизнь сессии подтянуть контекст диалога из панели."""
    if session.restored or session.history or not crm.enabled:
        session.restored = True
        return
    session.restored = True
    context = await crm.context(chat_id)
    if not context:
        return
    if context.get("is_open"):
        session.ticket_id = context.get("ticket_id")
    for item in context.get("messages") or []:
        role = "user" if item.get("author") == "citizen" else "assistant"
        text = (item.get("text") or "").strip()
        if text:
            session.add(role, text, settings.history_limit)
    if session.history:
        logger.info("Чат %s: восстановлено %s сообщений из панели",
                    chat_id, len(session.history))


@dp.callback_query(F.data.startswith("rate:"))
async def rate_answer(callback: CallbackQuery) -> None:
    """Житель оценил ответ. Оценка уходит в карточку, кнопки убираются."""
    try:
        _, mid, value = callback.data.split(":", 2)
        message_id = int(mid)
        assert value in ("up", "down")
    except (ValueError, AssertionError):
        await callback.answer()
        return
    ok = await crm.rating(message_id, value)
    if callback.message is not None:
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(
        "Спасибо за оценку." if ok else "Не удалось сохранить оценку, но спасибо."
    )


@dp.message()
async def handle_other(message: Message) -> None:
    """Стикеры, голосовые и прочее — бот работает с текстом, фото и файлами."""
    await message.answer(
        f"{ICONS.info}  Я понимаю текст, фотографии и документы. "
        "Голосовые и стикеры пока не разбираю."
    )


# ------------------------------------------------------- обработка сбоев

@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    """
    Последний рубеж: сюда попадает всё, что не поймали обработчики.

    Главное здесь — вернуть True. Это говорит aiogram «ошибка обработана»,
    и опрос Telegram продолжается. Без этого одна неожиданная ошибка
    в одном чате могла бы остановить бота для всех.
    """
    if isinstance(event.exception, TelegramForbiddenError):
        # Человек заблокировал бота или удалил чат — это нормальная ситуация.
        logger.info("Чат недоступен (бот заблокирован): %s", event.exception)
        return True
    logger.exception("Необработанная ошибка", exc_info=event.exception)
    return True


# ------------------------------------------------------------------- запуск

def setup_logging() -> None:
    """Логи одновременно в консоль и в файл bot.log (с ротацией)."""
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # maxBytes/backupCount — чтобы файл логов не съел диск за полгода работы.
    file_handler = logging.handlers.RotatingFileHandler(
        "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(settings.log_level if settings.log_level in
                  ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO")
    root.handlers = [console, file_handler]

    # aiohttp на уровне DEBUG заваливает лог служебными запросами.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def set_commands(bot: Bot) -> None:
    """Список команд в меню Telegram (кнопка «/» рядом с полем ввода)."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="mode", description="Режим ответа"),
        BotCommand(command="model", description="Выбор ИИ"),
        BotCommand(command="reset", description="Очистить историю диалога"),
        BotCommand(command="status", description="Текущие настройки"),
        BotCommand(command="help", description="Справка"),
    ])


async def outbox_loop(bot: Bot) -> None:
    """
    Разбор очереди исходящих из панели: ответы сотрудников, рассылки,
    уведомления. Каждая строка отправляется и помечается; житель,
    заблокировавший бота, помечается в панели и выпадает из рассылок.
    """
    while True:
        await asyncio.sleep(OUTBOX_INTERVAL)
        try:
            items = await crm.outbox_pending()
            for row in items:
                try:
                    for chunk in split_text(row["text"]):
                        await bot.send_message(row["chat_id"], chunk)
                        # Пауза между кусками: у Telegram общий лимит ~30/с.
                        await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await crm.outbox_failed(row["id"], "житель заблокировал бота",
                                            blocked=True)
                except TelegramRetryAfter as e:
                    # Telegram просит подождать — строка остаётся в очереди,
                    # возьмём её в следующем проходе.
                    logger.warning("Рассылка: Telegram просит подождать %s с",
                                   e.retry_after)
                    await asyncio.sleep(e.retry_after + 1)
                    break
                except Exception as e:
                    await crm.outbox_failed(row["id"], str(e))
                else:
                    await crm.outbox_sent(row["id"])
            # Заодно досылаем обращения, не доставленные в панель ранее.
            await crm.flush_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой цикла исходящих")


async def config_loop() -> None:
    """
    Опрос конфигурации панели. Смена токена бота — единственное, что нельзя
    применить на лету внутри aiogram, поэтому в этом случае останавливаем
    опрос Telegram: main() заметит и перезапустится с новым токеном.
    """
    while True:
        try:
            changed = await remote.refresh(crm)
            if changed:
                desired = remote.bot_token or settings.telegram_token
                if _active_token and desired != _active_token:
                    logger.warning("В панели сменили токен бота — перезапускаю опрос")
                    await dp.stop_polling()
                if remote.default_provider:
                    storage._default_provider = remote.default_provider
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой опроса конфигурации")
        await asyncio.sleep(CONFIG_INTERVAL)


async def heartbeat_loop() -> None:
    """Раз в минуту сообщаем панели, что бот жив и какие модели доступны."""
    while True:
        try:
            from providers import is_available
            await crm.health(
                providers={name: is_available(name) for name in available_providers()},
                counters=dict(COUNTERS),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой сердцебиения")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def cleanup_loop() -> None:
    """Фоновая уборка давно неактивных сессий (см. Storage.cleanup)."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        removed = storage.cleanup()
        if removed:
            logger.info("Убрано неактивных сессий: %s (осталось %s)", removed, len(storage))


async def main() -> None:
    global _active_token
    setup_logging()

    # Кэш конфигурации с прошлого запуска: ключи и модели панели
    # применяются ещё до первого ответа сети.
    remote.apply_cached()

    # Внешний цикл нужен для одного случая: в панели сменили токен бота.
    # Тогда опрос Telegram останавливается и начинается заново с новым.
    while True:
        token = remote.bot_token or settings.telegram_token
        _active_token = token
        bot = Bot(token=token)

        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            await bot.session.close()
            if token != settings.telegram_token:
                # Токен из панели битый — откатываемся на токен из .env.
                logger.error("Telegram отклонил токен из панели — возвращаюсь "
                             "на токен из .env")
                remote.data["bot_token"] = None
                continue
            logger.error(
                "Telegram отклонил TELEGRAM_BOT_TOKEN. Проверь токен в .env "
                "(получить заново: @BotFather -> /mybots -> API Token)."
            )
            return
        except TelegramNetworkError as e:
            await bot.session.close()
            logger.error("Нет связи с Telegram: %s. Проверь интернет и доступ "
                         "к api.telegram.org.", e)
            return

        logger.info("Бот запущен: @%s (id %s)", me.username, me.id)

        # Забираем конфигурацию до первого сообщения: ключи и модели могут
        # храниться только в панели, и тогда без этого запроса бот ответил бы
        # первому жителю «не настроена ни одна модель».
        if crm.enabled:
            with contextlib.suppress(Exception):
                await remote.refresh(crm)

        ready = warm_up()  # заранее проверяем ключи, чтобы узнать о проблеме сразу
        logger.info("Провайдер по умолчанию: %s", settings.default_provider)
        logger.info("Доступные провайдеры: %s", ", ".join(ready) or "нет")
        logger.info("Панель управления: %s",
                    settings.crm_url if crm.enabled else "не подключена")

        with contextlib.suppress(Exception):
            await set_commands(bot)

        tasks = [
            asyncio.create_task(cleanup_loop()),
            asyncio.create_task(outbox_loop(bot)),
            asyncio.create_task(config_loop()),
            asyncio.create_task(heartbeat_loop()),
        ]
        try:
            # drop_pending_updates=True — не отвечать на сообщения,
            # которые пришли, пока бот был выключен.
            await dp.start_polling(bot, drop_pending_updates=True)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            await bot.session.close()
            await crm.close()

        desired = remote.bot_token or settings.telegram_token
        if desired == token:
            break  # остановка штатная (Ctrl+C), выходим совсем
        logger.info("Перезапускаюсь с новым токеном из панели…")

    logging.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
