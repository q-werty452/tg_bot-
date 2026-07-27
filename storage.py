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
}


@dataclass
class Session:
    """Состояние одного чата."""

    provider: str
    mode: Mode = Mode.DETAILED
    # История в общем формате: [{"role": "user"|"assistant", "content": "текст"}]
    # Такой формат понимают оба провайдера, поэтому переключение модели
    # не ломает диалог.
    history: list[dict] = field(default_factory=list)

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


class Storage:
    """Простое хранилище сессий: ключ — id чата в Telegram."""

    def __init__(self, default_provider: str) -> None:
        self._default_provider = default_provider
        self._sessions: dict[int, Session] = {}

    def get(self, chat_id: int) -> Session:
        """Вернуть сессию чата, создав её при первом обращении."""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = Session(provider=self._default_provider)
        return self._sessions[chat_id]
