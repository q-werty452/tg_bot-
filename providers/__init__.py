"""
providers/__init__.py — «фабрика» провайдеров.

Наличие файла __init__.py превращает папку в пакет Python,
и мы можем писать `from providers import get_provider`.

Здесь же лежит кэш созданных клиентов: клиент к API создаём один раз
и переиспользуем, а не на каждое сообщение (иначе тратим время
на установку соединений).
"""

from config import settings
from providers.base import LLMProvider, ProviderError
from providers.claude import ClaudeProvider
from providers.gemini import GeminiProvider
from providers.openai_provider import OpenAIProvider

# Реестр: имя -> класс. Добавить нового провайдера = добавить строчку сюда.
_REGISTRY: dict[str, type[LLMProvider]] = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}

# Кэш уже созданных экземпляров.
_CACHE: dict[str, LLMProvider] = {}


def available_providers() -> list[str]:
    """Список провайдеров, для которых реально есть API-ключ."""
    result = []
    if settings.anthropic_api_key:
        result.append("claude")
    if settings.openai_api_key:
        result.append("openai")
    if settings.google_api_key:
        result.append("gemini")
    return result


def get_provider(name: str) -> LLMProvider:
    """Вернуть готовый объект провайдера по имени."""
    if name not in _REGISTRY:
        raise ProviderError(f"Неизвестный провайдер: {name}")
    if name not in _CACHE:
        _CACHE[name] = _REGISTRY[name]()  # создаём при первом обращении
    return _CACHE[name]


__all__ = ["LLMProvider", "ProviderError", "get_provider", "available_providers"]
