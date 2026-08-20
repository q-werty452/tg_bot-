"""
providers/openrouter.py — OpenRouter.

OpenRouter — это шлюз к чужим моделям: один ключ, а за ним десятки моделей
разных компаний (OpenAI, Google, Anthropic, Meta и другие). Протокол он
повторяет за OpenAI один в один, поэтому весь код запроса берём у соседа
и меняем только адрес и название модели.

Названия моделей здесь составные, с указанием владельца:
  openai/gpt-4o-mini, google/gemini-2.0-flash-exp, anthropic/claude-3.5-haiku
Выбирать модель удобнее в панели: она покажет список, доступный ключу.
"""

from openai import AsyncOpenAI

from config import settings
from providers.base import ProviderError
from providers.openai_provider import OpenAIProvider

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"
    title = "OpenRouter"

    def __init__(self) -> None:
        # Своя сборка клиента вместо родительской: адрес другой, а
        # предупреждение об отсутствующем ключе должно называть свою строку.
        from providers import override_for

        override = override_for("openrouter")
        api_key = override.get("key") or settings.openrouter_api_key
        model = override.get("model") or settings.openrouter_model
        if not api_key:
            raise ProviderError(
                "Не задан ключ OpenRouter. Добавь его в панели управления "
                "(раздел «Ключи и токены») или строкой OPENROUTER_API_KEY в .env"
            )
        if not model:
            raise ProviderError(
                "Не выбрана модель OpenRouter. У него их сотни, поэтому нужную "
                "нужно выбрать в панели: «Ключи и токены» -> «Проверить»."
            )

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            timeout=settings.request_timeout,
            max_retries=2,
        )
        self._model = model
        self._legacy_token_param = False
