"""
remote_config.py — настройки бота, приходящие из панели управления.

Бот раз в 30 секунд спрашивает панель «конфигурация изменилась?» (по номеру
версии — дёшево). Изменилась — забирает целиком, применяет и сохраняет на
диск. Панель упала — бот работает на последней сохранённой копии; копии нет —
на встроенных значениях из prompts.py и ключах из .env. То есть панель может
лежать сколько угодно, а бот отвечает всегда.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "crm_config_cache.json"


class RemoteConfig:
    def __init__(self, cache_file: Path | None = None) -> None:
        self.data: dict = {}
        self.version: int = -1
        # Параметризуемо: whatsapp_bot.py использует свой файл кэша, чтобы
        # два процесса не перетирали друг другу конфигурацию раз в 30 секунд.
        self._cache_file = cache_file or CACHE_FILE
        self._load_cache()

    # ------------------------------------------------------------ свойства

    @property
    def received(self) -> bool:
        """Получали ли мы конфигурацию хоть раз (из сети или с диска)."""
        return bool(self.data)

    @property
    def bot_enabled(self) -> bool:
        return bool(self.data.get("enabled", True))

    @property
    def maintenance_text(self) -> str:
        return self.data.get("maintenance_text") or (
            "Бот временно недоступен: идут технические работы. "
            "Пожалуйста, напишите позже."
        )

    @property
    def invent_facts(self) -> bool | None:
        value = self.data.get("invent_facts")
        return None if value is None else bool(value)

    @property
    def facts(self) -> str:
        return self.data.get("facts") or ""

    def prompt_override(self, detailed: bool) -> str:
        key = "prompt_detailed" if detailed else "prompt_chat"
        return self.data.get(key) or ""

    @property
    def default_provider(self) -> str | None:
        return self.data.get("default_provider") or None

    @property
    def providers(self) -> list[dict]:
        return self.data.get("providers") or []

    @property
    def bot_token(self) -> str | None:
        return self.data.get("bot_token") or None

    @property
    def categories(self) -> list[dict]:
        return self.data.get("categories") or []

    @property
    def districts(self) -> list[dict]:
        return self.data.get("districts") or []

    def match_quick_answer(self, text: str) -> dict | None:
        """Подобрать готовый ответ: вхождение любой фразы, без учёта регистра."""
        lowered = text.lower()
        for qa in self.data.get("quick_answers") or []:
            for trigger in qa.get("triggers") or []:
                if trigger and trigger in lowered:
                    return qa
        return None

    # ------------------------------------------------------------ обновление

    async def refresh(self, crm) -> bool:
        """Спросить панель. Вернёт True, если конфигурация изменилась."""
        result = await crm.fetch_config(self.version)
        if not result or not result.get("changed"):
            return False
        self.data = result
        self.version = int(result.get("version") or 0)
        self._save_cache()
        self._apply()
        logger.info("Получена конфигурация панели, версия %s", self.version)
        return True

    def _apply(self) -> None:
        """Применить ключи и модели к провайдерам — без перезапуска бота."""
        import providers as providers_module

        overrides = {
            row["provider"]: {"key": row.get("key") or "",
                              "model": row.get("model") or ""}
            for row in self.providers
            if row.get("provider") and row.get("key")
        }
        providers_module.apply_overrides(overrides)

    # ------------------------------------------------------------ кэш

    def _save_cache(self) -> None:
        try:
            self._cache_file.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            logger.exception("Не удалось сохранить кэш конфигурации")

    def _load_cache(self) -> None:
        if not self._cache_file.exists():
            return
        try:
            self.data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            self.version = int(self.data.get("version") or 0)
        except (OSError, ValueError):
            logger.warning("Кэш конфигурации повреждён — начинаем с чистого")
            self.data, self.version = {}, -1

    def apply_cached(self) -> None:
        """Применить кэш при старте (до первого ответа панели)."""
        if self.data:
            self._apply()


remote = RemoteConfig()
