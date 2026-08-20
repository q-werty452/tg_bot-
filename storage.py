"""
storage.py — «память» бота.

Telegram-бот сам по себе ничего не помнит: каждое сообщение прилетает
отдельно. И API моделей тоже без памяти — историю переписки нужно
присылать целиком при каждом запросе.

Поэтому здесь мы храним для каждого чата:
  - какой провайдер выбран (claude / openai);
  - какой режим ответа выбран (подробный / диалоговый);
  - историю сообщений.

ВАЖНО: хранение в оперативной памяти (обычный словарь). Перезапустил бота —
всё забылось. Для учебного проекта это нормально. Когда захочешь постоянную
память — заменяй этот файл на SQLite/Postgres, остальной код менять не придётся.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from icons import ICONS


class Mode(str, Enum):
    """Два режима ответа. str в родителях — чтобы значение легко клалось в callback-кнопки."""

    DETAILED = "detailed"  # развёрнутый ответ, как обычно отвечает ИИ
    CHAT = "chat"          # живое обсуждение: коротко, с вопросами и комментариями


# Подписи собираются из реестра иконок (см. icons.py), а не из эмодзи.
MODE_TITLES = {
    Mode.DETAILED: f"{ICONS.mode_detailed}  Развёрнутый",
    Mode.CHAT: f"{ICONS.mode_chat}  Обсуждение",
}

PROVIDER_TITLES = {
    "claude": f"{ICONS.claude}  Claude (Anthropic)",
    "openai": f"{ICONS.openai}  GPT (OpenAI)",
    "gemini": f"{ICONS.gemini}  Gemini (Google)",
    "openrouter": f"{ICONS.openrouter}  OpenRouter",
}


@dataclass
class Session:
    """Состояние одного чата."""

    provider: str
    mode: Mode = Mode.DETAILED
    # История в общем формате: [{"role": "user"|"assistant", "content": "текст"}]
    # Такой формат понимают все провайдеры, поэтому переключение модели
    # не ломает диалог.
    history: list[dict] = field(default_factory=list)

    # Замок: пока идёт ответ на одно сообщение этого чата, следующее ждёт.
    # Без него два быстрых сообщения подряд перемешали бы историю
    # (получилось бы user, user, assistant, assistant — а API ждёт чередования).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Время последней активности — по нему чистим давно неактивные чаты.
    last_seen: float = field(default_factory=time.monotonic)

    # Номер карточки в панели управления (None — панель ещё не отвечала).
    ticket_id: int | None = None

    # Подтягивали ли мы историю из панели после перезапуска бота.
    restored: bool = False

    def add(self, role: str, content: str, limit: int) -> None:
        """Добавить сообщение в историю и обрезать её, если стала слишком длинной."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > limit:
            # Оставляем только последние `limit` сообщений.
            # Зачем: длинная история = больше токенов = дороже и медленнее.
            self.history = self.history[-limit:]
            # История обязана начинаться с сообщения пользователя,
            # иначе Anthropic вернёт ошибку.
            while self.history and self.history[0]["role"] != "user":
                self.history.pop(0)

    def clear(self) -> None:
        self.history.clear()
        self.ticket_id = None

    def drop_last(self) -> None:
        """Убрать последнее сообщение (используем, когда ответ не получился)."""
        if self.history:
            self.history.pop()


# Через сколько секунд бездействия сессия считается брошенной.
# 12 часов: человек, вернувшийся на следующий день, всё равно начинает новую тему.
SESSION_TTL = 12 * 60 * 60


class Storage:
    """Простое хранилище сессий: ключ — id чата в Telegram."""

    def __init__(self, default_provider: str) -> None:
        self._default_provider = default_provider
        self._sessions: dict[int, Session] = {}

    def get(self, chat_id: int) -> Session:
        """Вернуть сессию чата, создав её при первом обращении."""
        session = self._sessions.get(chat_id)
        if session is None:
            session = Session(provider=self._default_provider)
            self._sessions[chat_id] = session
        session.last_seen = time.monotonic()
        return session

    def __len__(self) -> int:
        return len(self._sessions)

    def cleanup(self, ttl: float = SESSION_TTL) -> int:
        """
        Удалить сессии, в которых давно ничего не происходило.

        Зачем: бот работает месяцами, а каждая сессия держит в памяти
        историю переписки. Без уборки память растёт вместе с числом
        обратившихся жителей. Занятые (отвечающие прямо сейчас) не трогаем.
        Возвращает количество удалённых.
        """
        now = time.monotonic()
        stale = [
            chat_id
            for chat_id, session in self._sessions.items()
            if now - session.last_seen > ttl and not session.lock.locked()
        ]
        for chat_id in stale:
            del self._sessions[chat_id]
        return len(stale)
