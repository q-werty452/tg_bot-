"""
providers/gemini.py — работа с Gemini через официальный SDK google-genai.

Это третий провайдер. Обрати внимание: чтобы его добавить, я НЕ трогал
bot.py вообще. Понадобилось только:
  1) написать этот файл;
  2) добавить одну строку в providers/__init__.py.
Ради этого и нужен был базовый класс LLMProvider — см. providers/base.py.

Особенности Google API (третий вариант того же самого, сравни с соседями):
1. Системный промпт передаётся в объекте config -> system_instruction.
   (У Anthropic это отдельный параметр system, у OpenAI — первое сообщение.)
2. Роли называются "user" и "model". Слова "assistant" тут НЕТ —
   историю приходится конвертировать.
3. Сообщение — это не просто строка, а объект с частями (parts).
   Так сделано, чтобы в одном сообщении могли лежать текст, картинки, файлы.
4. Асинхронный вызов живёт в client.aio — то есть client.aio.models.generate_content().
5. Готовый текст ответа лежит в response.text.
"""

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import settings
from providers.base import LLMProvider, ProviderError


class GeminiProvider(LLMProvider):
    name = "gemini"
    title = "Gemini (Google)"

    def __init__(self) -> None:
        if not settings.google_api_key:
            raise ProviderError("Не задан GOOGLE_API_KEY в файле .env")
        self._client = genai.Client(api_key=settings.google_api_key)
        self._model = settings.google_model

    @staticmethod
    def _to_gemini_history(history: list[dict]) -> list[types.Content]:
        """
        Переводит нашу общую историю в формат Google.

        Было:  {"role": "assistant", "content": "текст"}
        Стало: Content(role="model", parts=[Part(text="текст")])

        Именно поэтому мы храним историю в нейтральном формате (см. storage.py):
        каждый провайдер конвертирует её под себя, и переключение модели
        посреди диалога ничего не ломает.
        """
        return [
            types.Content(
                role="model" if item["role"] == "assistant" else "user",
                parts=[types.Part(text=item["content"])],
            )
            for item in history
        ]

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,   # <- системный промпт живёт здесь
            max_output_tokens=max_tokens,
        )

        if not detailed:
            # Диалоговый режим: у Gemini по умолчанию включено «мышление»,
            # оно замедляет ответ и съедает лимит max_output_tokens.
            # Для коротких реплик оно не нужно — ставим минимум.
            #
            # Осторожно, тут API менялось между поколениями моделей:
            #   Gemini 3.x  -> thinking_level (MINIMAL / LOW / MEDIUM / HIGH)
            #   Gemini 2.5  -> thinking_budget (число токенов, 0 = выключить)
            # На модели 3.x параметр thinking_budget=0 вызывает ошибку 400.
            config.thinking_config = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL
            )
        # В развёрнутом режиме thinking_config не задаём вообще —
        # модель сама решит, сколько думать (это её поведение по умолчанию).

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._to_gemini_history(history),
                config=config,
            )
        except genai_errors.ClientError as e:
            # 4xx — виноват запрос или ключ.
            if e.code == 401 or e.code == 403:
                raise ProviderError("Gemini: неверный API-ключ (проверь GOOGLE_API_KEY).")
            if e.code == 429:
                raise ProviderError("Gemini: превышен лимит запросов, подожди минуту.")
            raise ProviderError(f"Gemini вернул ошибку {e.code}: {e.message}")
        except genai_errors.ServerError as e:
            # 5xx — проблема на стороне Google.
            raise ProviderError(f"Gemini: сервер недоступен ({e.code}). Попробуй позже.")
        except genai_errors.APIError as e:
            raise ProviderError(f"Gemini: ошибка API — {e}")

        text = (response.text or "").strip()
        if not text:
            # Пустой ответ обычно значит, что сработал фильтр безопасности
            # либо ответ упёрся в лимит токенов ещё на стадии размышлений.
            raise ProviderError(
                "Gemini вернул пустой ответ (возможно, сработал фильтр). "
                "Попробуй переформулировать."
            )
        return text
