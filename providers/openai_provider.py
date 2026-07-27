"""
providers/openai_provider.py — работа с GPT через официальный SDK openai.

Отличия от Anthropic (полезно видеть рядом):
1. Системный промпт — это ПЕРВОЕ сообщение в списке messages
   с ролью "system", отдельного параметра нет.
2. Ответ лежит в response.choices[0].message.content — сразу строкой.
3. Лимит длины передаём как max_completion_tokens (современное имя;
   старое max_tokens считается устаревшим).
4. temperature не передаём: часть новых «рассуждающих» моделей его не принимает.
   Стиль задаём промптом — так надёжнее.

Файл называется openai_provider.py, а не openai.py, специально:
иначе Python при import openai нашёл бы наш файл вместо библиотеки.
"""

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from config import settings
from providers.base import LLMProvider, ProviderError


class OpenAIProvider(LLMProvider):
    name = "openai"
    title = "GPT (OpenAI)"

    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise ProviderError("Не задан OPENAI_API_KEY в файле .env")
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        # Системный промпт идёт первым сообщением, дальше — вся история.
        messages = [{"role": "system", "content": system}, *history]

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_completion_tokens=max_tokens,
            )
        except AuthenticationError:
            raise ProviderError("OpenAI: неверный API-ключ (проверь OPENAI_API_KEY).")
        except RateLimitError:
            raise ProviderError(
                "OpenAI: лимит запросов или закончились средства на балансе."
            )
        except APIConnectionError:
            raise ProviderError("OpenAI: нет связи с сервером. Проверь интернет/VPN.")
        except APIStatusError as e:
            raise ProviderError(f"OpenAI вернул ошибку {e.status_code}: {e.message}")

        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise ProviderError("GPT вернул пустой ответ. Попробуй переформулировать.")
        return text
