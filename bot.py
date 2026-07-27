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

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from icons import ICONS, clean_text
from prompts import MAX_TOKENS, SYSTEM_PROMPTS
from providers import ProviderError, available_providers, get_provider
from storage import MODE_TITLES, PROVIDER_TITLES, Mode, Storage
from utils import split_text

logger = logging.getLogger(__name__)

# Dispatcher — «маршрутизатор»: решает, какая функция обработает какое сообщение.
dp = Dispatcher()

# Хранилище сессий (см. storage.py).
storage = Storage(default_provider=settings.default_provider)


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
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()  # обязательно гасим фоновую задачу, что бы ни случилось
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def send_long(message: Message, text: str) -> None:
    """Отправить текст, автоматически разрезав его на части по лимиту Telegram."""
    for chunk in split_text(text):
        await message.answer(chunk)


# -------------------------------------------------------------------- команды

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    session = storage.get(message.chat.id)
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
    await message.answer(
        f"Текущая модель: {PROVIDER_TITLES[session.provider]}\nВыбери другую:",
        reply_markup=provider_keyboard(),
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    storage.get(message.chat.id).clear()
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


# --------------------------------------------------------- нажатия на кнопки

@dp.callback_query(F.data.startswith("mode:"))
async def switch_mode(callback: CallbackQuery) -> None:
    # F.data.startswith(...) — фильтр: обрабатываем только кнопки режима.
    session = storage.get(callback.message.chat.id)
    session.mode = Mode(callback.data.split(":", 1)[1])

    await callback.message.edit_text(
        f"{ICONS.ok}  Режим переключён: {MODE_TITLES[session.mode]}"
    )
    # answer() обязателен: убирает «часики» на кнопке в интерфейсе Telegram.
    await callback.answer()


@dp.callback_query(F.data.startswith("provider:"))
async def switch_provider(callback: CallbackQuery) -> None:
    session = storage.get(callback.message.chat.id)
    session.provider = callback.data.split(":", 1)[1]

    await callback.message.edit_text(
        f"{ICONS.ok}  Модель переключена: {PROVIDER_TITLES[session.provider]}\n"
        "История диалога сохранена — можно продолжать."
    )
    await callback.answer()


# --------------------------------------------------------- основной обработчик

@dp.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    """Любое текстовое сообщение, не являющееся командой, идёт в ИИ."""
    session = storage.get(message.chat.id)
    user_text = message.text.strip()
    if not user_text:
        return

    # 1. Кладём вопрос пользователя в историю.
    session.add("user", user_text, settings.history_limit)

    # 2. Выбираем провайдера и параметры под текущий режим.
    try:
        provider = get_provider(session.provider)
    except ProviderError as e:
        await message.answer(f"{ICONS.error}  {e}")
        return

    system = SYSTEM_PROMPTS[session.mode]
    max_tokens = MAX_TOKENS[session.mode]
    detailed = session.mode is Mode.DETAILED

    # 3. Спрашиваем модель, показывая «печатает…».
    try:
        async with typing(bot, message.chat.id):
            answer = await provider.ask(
                system=system,
                history=session.history,
                max_tokens=max_tokens,
                detailed=detailed,
            )
    except ProviderError as e:
        # Ожидаемая ошибка (нет ключа, лимит, нет сети) — показываем текст пользователю.
        # Вопрос убираем из истории, чтобы он не «залип» без ответа.
        session.history.pop()
        await message.answer(f"{ICONS.error}  {e}")
        return
    except Exception:
        # Неожиданная ошибка: пишем полный traceback в консоль, пользователю — вежливо.
        session.history.pop()
        logger.exception("Ошибка при запросе к модели")
        await message.answer(f"{ICONS.error}  Что-то пошло не так. Попробуй ещё раз.")
        return

    # 4. Приводим ответ к нашему стилю: вырезаем эмодзи, которые модель
    #    могла вставить вопреки промпту (см. icons.py).
    answer = clean_text(answer)

    # 5. Запоминаем ответ (чтобы модель помнила контекст) и отправляем его.
    session.add("assistant", answer, settings.history_limit)
    await send_long(message, answer)


@dp.message()
async def handle_other(message: Message) -> None:
    """Фото, стикеры, голосовые и прочее — этот бот работает только с текстом."""
    await message.answer(
        f"{ICONS.info}  Я пока понимаю только текст. Напиши сообщение словами."
    )


# ------------------------------------------------------------------- запуск

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    bot = Bot(token=settings.telegram_token)

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)
    logger.info("Провайдер по умолчанию: %s", settings.default_provider)
    logger.info("Доступные провайдеры: %s", ", ".join(available_providers()))

    # drop_pending_updates=True — не отвечать на сообщения,
    # которые пришли, пока бот был выключен.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
