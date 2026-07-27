"""
config.py — все настройки в одном месте.

Зачем отдельный файл: чтобы ключи и названия моделей не были разбросаны
по коду. Код читает настройки только отсюда, а сюда они приходят из файла
.env (через библиотеку python-dotenv).

Правило: секреты (токены, ключи) живут в .env, а не в коде.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Читает файл .env из папки проекта и кладёт его строки в переменные окружения.
# После этого os.getenv("ИМЯ") умеет их доставать.
load_dotenv()


@dataclass(frozen=True)  # frozen=True => настройки нельзя случайно изменить в рантайме
class Settings:
    telegram_token: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    google_api_key: str | None
    default_provider: str
    anthropic_model: str
    openai_model: str
    google_model: str
    history_limit: int


def load_settings() -> Settings:
    """Собирает объект настроек и сразу проверяет самое критичное."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создай файл .env (см. .env.example) "
            "и вставь туда токен от @BotFather."
        )

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    openai_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    google_key = os.getenv("GOOGLE_API_KEY", "").strip() or None

    # Какие провайдеры реально можно использовать (есть ключ).
    # Порядок важен: если провайдер по умолчанию недоступен,
    # берём первый из этого списка.
    keys = {"claude": anthropic_key, "openai": openai_key, "gemini": google_key}
    usable = [name for name, key in keys.items() if key]

    if not usable:
        raise RuntimeError(
            "Не задан ни один ключ ИИ (ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "GOOGLE_API_KEY). Нужен хотя бы один, иначе боту нечем отвечать."
        )

    default_provider = os.getenv("DEFAULT_PROVIDER", "claude").strip().lower()
    if default_provider not in usable:
        default_provider = usable[0]

    return Settings(
        telegram_token=token,
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
        google_api_key=google_key,
        default_provider=default_provider,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o").strip(),
        google_model=os.getenv("GOOGLE_MODEL", "gemini-3.6-flash").strip(),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
    )


# Один общий объект настроек на всё приложение.
settings = load_settings()
