"""
crm.py — клиент к панели управления (manas-crm).

Правило номер один: панель НИКОГДА не должна ломать разговор с жителем.
Любая сетевая ошибка здесь гасится: метод возвращает None (или пустой
список), пишет в лог — и бот продолжает отвечать. Обращения, которые
не удалось доставить, копятся в файле очереди и досылаются позже.

Все методы асинхронные, все ходят с заголовком X-Bot-Token.
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Очередь недоставленных обращений: по строке JSON на обращение.
QUEUE_FILE = Path(__file__).parent / "crm_queue.jsonl"


class CRM:
    def __init__(self) -> None:
        self.base = (settings.crm_url or "").rstrip("/")
        self.token = settings.crm_bot_token or ""
        self.enabled = bool(self.base and self.token)
        self._client: httpx.AsyncClient | None = None
        self._flush_lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base + "/api/v1",
                headers={"X-Bot-Token": self.token},
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, **kwargs):
        """Один запрос к панели. Ошибка сети или 5xx — None, без исключений."""
        if not self.enabled:
            return None
        try:
            response = await self._http().request(method, url, **kwargs)
        except httpx.HTTPError as e:
            logger.warning("Панель недоступна (%s %s): %s", method, url, e)
            return None
        if response.status_code >= 500:
            logger.warning("Панель вернула %s на %s", response.status_code, url)
            return None
        if response.status_code >= 400:
            logger.warning("Панель отклонила %s %s: %s",
                           method, url, response.text[:200])
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ------------------------------------------------------------ обращения

    async def incoming(self, profile: dict, text: str,
                       tg_message_id: int | None = None,
                       file_paths: list[str] | None = None) -> dict | None:
        """
        Отправить сообщение жителя. Вернёт {ticket_id, number, answer_mode,
        created} или None, если панель недоступна (тогда обращение — в очередь).
        """
        await self.flush_queue()

        data = {**profile, "text": text}
        if tg_message_id:
            data["tg_message_id"] = str(tg_message_id)
        files = []
        handles = []
        try:
            for path in file_paths or []:
                p = Path(path)
                if p.exists():
                    fh = p.open("rb")
                    handles.append(fh)
                    files.append(("files", (p.name, fh)))
            result = await self._request(
                "POST", "/tickets/incoming/",
                data={k: str(v) for k, v in data.items()},
                files=files or None,
            )
        finally:
            for fh in handles:
                fh.close()

        if result is None and self.enabled:
            self._enqueue({"profile": profile, "text": text,
                           "tg_message_id": tg_message_id,
                           "files": file_paths or []})
        return result

    async def ai_message(self, ticket_id: int, text: str) -> int | None:
        """Записать ответ ИИ в карточку. Вернёт id сообщения (для оценки)."""
        result = await self._request(
            "POST", f"/tickets/{ticket_id}/messages/", json={"text": text})
        return result.get("message_id") if result else None

    async def classify(self, ticket_id: int, data: dict) -> None:
        await self._request("POST", f"/tickets/{ticket_id}/classify/", json=data)

    async def retitle(self, ticket_id: int, title: str) -> bool:
        """Разовое уточнение темы заявки по всей переписке (см. classify.py)."""
        result = await self._request(
            "POST", f"/tickets/{ticket_id}/retitle/", json={"title": title})
        return bool(result and result.get("applied"))

    async def history(self, ticket_id: int, limit: int = 30) -> dict | None:
        return await self._request(
            "GET", f"/tickets/{ticket_id}/history/", params={"limit": limit})

    async def rating(self, message_id: int, value: str) -> bool:
        result = await self._request(
            "POST", f"/messages/{message_id}/rating/", json={"rating": value})
        return result is not None

    async def context(self, chat_id: int, channel: str = "telegram") -> dict | None:
        """История диалога после перезапуска бота."""
        return await self._request(
            "GET", f"/chats/{chat_id}/context/", params={"channel": channel})

    async def close_chat(self, chat_id: int, channel: str = "telegram") -> None:
        await self._request(
            "POST", f"/chats/{chat_id}/close/", json={"channel": channel})

    async def subscription(self, chat_id: int, subscribed: bool,
                           channel: str = "telegram") -> None:
        await self._request("POST", "/citizens/subscription/",
                            json={"chat_id": chat_id, "subscribed": subscribed,
                                  "channel": channel})

    async def answer_hit(self, answer_id: int) -> None:
        await self._request("POST", f"/answers/{answer_id}/hit/")

    # ------------------------------------------------------------ исходящие

    async def outbox_pending(self, channel: str, limit: int = 50) -> list[dict]:
        result = await self._request(
            "GET", "/outbox/", params={"limit": limit, "channel": channel})
        return (result or {}).get("items", [])

    async def outbox_sent(self, row_id: int) -> None:
        await self._request("POST", f"/outbox/{row_id}/sent/")

    async def outbox_failed(self, row_id: int, error: str,
                            blocked: bool = False) -> None:
        await self._request("POST", f"/outbox/{row_id}/failed/",
                            json={"error": error[:300], "blocked": blocked})

    # ------------------------------------------------------- конфигурация

    async def fetch_config(self, version: int) -> dict | None:
        return await self._request("GET", "/config/", params={"version": version})

    async def health(self, providers: dict, counters: dict) -> None:
        await self._request("POST", "/health/",
                            json={"providers": providers, "counters": counters})

    # ------------------------------------------------------ очередь на диске

    def _enqueue(self, item: dict) -> None:
        """Обращение не доставлено — дописываем в файл, дошлём позже."""
        try:
            with QUEUE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            logger.info("Обращение отложено в очередь (%s)", QUEUE_FILE.name)
        except OSError:
            logger.exception("Не удалось записать очередь на диск")

    async def flush_queue(self) -> int:
        """Дослать накопившееся. Вернёт число доставленных."""
        if not self.enabled or not QUEUE_FILE.exists():
            return 0
        async with self._flush_lock:
            try:
                lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            if not lines:
                QUEUE_FILE.unlink(missing_ok=True)
                return 0

            remaining, delivered = [], 0
            for line in lines:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue  # битую строку выбрасываем
                data = {**item["profile"], "text": item["text"]}
                if item.get("tg_message_id"):
                    data["tg_message_id"] = str(item["tg_message_id"])
                files, handles = [], []
                try:
                    for path in item.get("files") or []:
                        p = Path(path)
                        if p.exists():
                            fh = p.open("rb")
                            handles.append(fh)
                            files.append(("files", (p.name, fh)))
                    result = await self._request(
                        "POST", "/tickets/incoming/",
                        data={k: str(v) for k, v in data.items()},
                        files=files or None,
                    )
                finally:
                    for fh in handles:
                        fh.close()
                if result is None:
                    remaining.append(line)  # панель всё ещё лежит
                else:
                    delivered += 1

            if remaining:
                QUEUE_FILE.write_text("\n".join(remaining) + "\n", encoding="utf-8")
            else:
                QUEUE_FILE.unlink(missing_ok=True)
            if delivered:
                logger.info("Дослано обращений из очереди: %s", delivered)
            return delivered


# Один клиент на всё приложение.
crm = CRM()
