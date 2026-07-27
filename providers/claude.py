"""
providers/claude.py — работа с Claude через официальный SDK anthropic.

Ключевые особенности Anthropic API (важно запомнить):
1. Системный промпт передаётся ОТДЕЛЬНЫМ параметром `system`,
   а не внутри списка messages (в отличие от OpenAI).
2. `max_tokens` — обязательный параметр.
3. Ответ приходит списком блоков (content), а не одной строкой:
   там могут быть блоки "text" и блоки "thinking" (размышления).
   Нам нужны только "text".
4. У новых моделей (Opus 4.8) НЕЛЬЗЯ передавать temperature/top_p —
   запрос вернёт ошибку 400. Стиль ответа настраивается промптом.
5. Режим «думать перед ответом» включается параметром
   thinking={"type": "adaptive"} — модель сама решает, сколько думать.
"""

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from config import settings
from providers.base import LLMProvider, ProviderError


class ClaudeProvider(LLMProvider):
    name = "claude"
    title = "Claude (Anthropic)"

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise ProviderError("Не задан ANTHROPIC_API_KEY в файле .env")
        # Async-клиент: не блокирует бота, пока ждём ответ модели.
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        # Собираем параметры запроса.
        params: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,      # <- отдельным полем, не в messages
            "messages": history,
        }

        if detailed:
            # Развёрнутый режим: разрешаем модели подумать перед ответом.
            # "adaptive" = модель сама решает, нужны ли размышления.
            params["thinking"] = {"type": "adaptive"}
            # effort управляет «усердием»: low / medium / high / xhigh / max.
            # medium — разумный баланс скорости, цены и качества для чата.
            params["output_config"] = {"effort": "medium"}
        else:
            # Диалоговый режим: думать не нужно, важна скорость и краткость.
            params["thinking"] = {"type": "disabled"}
            params["output_config"] = {"effort": "low"}

        try:
            response = await self._client.messages.create(**params)
        except AuthenticationError:
            raise ProviderError("Claude: неверный API-ключ (проверь ANTHROPIC_API_KEY).")
        except RateLimitError:
            raise ProviderError("Claude: слишком много запросов, подожди немного.")
        except APIConnectionError:
            raise ProviderError("Claude: нет связи с сервером. Проверь интернет/VPN.")
        except APIStatusError as e:
            raise ProviderError(f"Claude вернул ошибку {e.status_code}: {e.message}")

        # Модель могла отказаться отвечать по соображениям безопасности.
        if response.stop_reason == "refusal":
            raise ProviderError("Claude отказался отвечать на этот запрос.")

        # Достаём только текстовые блоки: в content могут лежать и блоки размышлений.
        parts = [block.text for block in response.content if block.type == "text"]
        text = "\n".join(parts).strip()

        if not text:
            raise ProviderError("Claude вернул пустой ответ. Попробуй переформулировать.")
        return text
