"""
whatsapp_bot.py — точка входа для WhatsApp (официальный Meta Cloud API).

Отдельный процесс от bot.py (Telegram): свой aiohttp-сервер для приёма
вебхуков Meta, своя in-memory сессия жителей, своя очередь исходящих.
Общее с Telegram-ботом — только то, что канало-независимо: crm.py,
config.py, remote_config.py, classify.py, prompts.py, providers,
conversation.py (запрос к ИИ и восстановление истории).

Почему respond()-подобная оркестрация здесь ПРОДУБЛИРОВАНА, а не вынесена
в общий модуль вместе с ask_with_fallback/restore_history: остальная часть
respond() в bot.py плотно завязана на aiogram (Message, типы ошибок
Telegram, статус «печатает», разбиение через message.answer). Обобщать её
ради одного второго канала означало бы трогать функцию, на которой стоят
161 зелёная проверка bot.py, ради сомнительной выгоды — таким образом риск
сломать существующий Telegram-бот выше, чем цена дублирования полутора
экранов оркестрации.

Фото и документы принимаются (см. download_whatsapp_media): скачиваются по
двухшаговому протоколу Meta (media_id -> временная ссылка -> файл) и уходят
в панель как вложение — аналогично bot.py:handle_media() для Telegram. Голос,
видео, стикеры, локация и т.п. — фиксированный ответ, обращение не заводится.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web

from classify import classify_ticket, refine_ticket
from config import settings
from conversation import ask_with_fallback, restore_history
from crm import crm
from icons import clean_text
from logging_setup import setup_logging
from prompts import MAX_TOKENS, build_system
from providers import ProviderError, available_providers, is_available, warm_up
from remote_config import RemoteConfig
from storage import Mode, Storage
from utils import split_text

logger = logging.getLogger(__name__)

# Свой экземпляр — отдельный процесс, отдельная память сессий.
storage = Storage(default_provider=settings.default_provider)

# Свой кэш конфигурации: два процесса не должны перетирать один файл.
remote = RemoteConfig(cache_file=Path(__file__).parent / "crm_config_cache_whatsapp.json")

CLEANUP_INTERVAL = 60 * 60
OUTBOX_INTERVAL = 2.5
CONFIG_INTERVAL = 30
HEARTBEAT_INTERVAL = 60

# Куда складывать скачанные из WhatsApp фото и документы до отправки в панель
# (тот же принцип, что MEDIA_DIR в bot.py для Telegram).
MEDIA_DIR = Path(__file__).parent / "incoming_media"
# Тот же лимит, что bot.py применяет к документам из Telegram (bot.py:381-386);
# для WhatsApp он же экономит трафик и место на диске.
WHATSAPP_MEDIA_LIMIT = 20 * 1024 * 1024

COUNTERS = {"messages": 0, "answers": 0, "quick_answers": 0, "errors": 0}


# ------------------------------------------------------------- разбор Meta

def verify_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Проверка X-Hub-Signature-256: подпись тела запроса секретом приложения."""
    if not app_secret or not header_value:
        return False
    prefix = "sha256="
    if not header_value.startswith(prefix):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len(prefix):])


def parse_webhook_payload(payload: dict) -> list[dict]:
    """
    Разобрать тело вебхука Meta в список сообщений.

    Meta присылает не только сообщения, но и статус-колбэки (доставлено,
    прочитано) — у них нет ключа "messages", такие записи просто пропускаем.
    """
    messages: list[dict] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {}
            for contact in value.get("contacts") or []:
                wa_id = contact.get("wa_id")
                name = (contact.get("profile") or {}).get("name") or ""
                if wa_id:
                    names[wa_id] = name
            for m in value.get("messages") or []:
                phone = m.get("from")
                if not phone:
                    continue
                mtype = m.get("type") or "unknown"
                text = ""
                media_id = media_mime = media_filename = None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif mtype in ("image", "document"):
                    media = m.get(mtype) or {}
                    media_id = media.get("id")
                    media_mime = media.get("mime_type", "")
                    media_filename = media.get("filename", "")
                    text = media.get("caption", "") or ""
                messages.append({
                    "phone": phone,
                    "type": mtype,
                    "text": text,
                    "wa_message_id": m.get("id"),
                    "name": names.get(phone, ""),
                    "media_id": media_id,
                    "media_mime": media_mime,
                    "media_filename": media_filename,
                })
    return messages


def build_profile(phone: str, name: str) -> dict:
    """Данные жителя для панели — аналог _profile() в bot.py, но для WhatsApp."""
    return {
        "channel": "whatsapp",
        "chat_id": int(phone),
        "first_name": name or "",
        "last_name": "",
        "username": "",
        "phone": phone,
    }


# --------------------------------------------------------------- отправка

async def send_whatsapp_text(phone: str, text: str) -> tuple[bool, str]:
    """
    Отправить текст через WhatsApp Cloud API.

    Возвращает (успех, текст_ошибки). Длинные ответы режем тем же
    split_text(), что и Telegram-бот, — лимит WhatsApp (4096) чуть больше
    нашего запаса (4000), так что кусок точно пройдёт.
    """
    url = (f"https://graph.facebook.com/{settings.meta_graph_api_version}/"
           f"{settings.meta_phone_number_id}/messages")
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        for chunk in split_text(text):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as e:
                return False, str(e)
            if response.status_code >= 400:
                return False, response.text[:300]
            await asyncio.sleep(0.05)
    return True, ""


async def download_whatsapp_media(media_id: str, mime: str, filename: str) -> Path | None:
    """
    Скачать медиа-файл по media_id (двухшаговый протокол Meta):
    сперва запрашиваем временную ссылку (~5 минут), потом скачиваем по ней —
    оба запроса требуют того же Bearer-токена, что и отправка сообщений.

    Возвращает путь к сохранённому файлу или None (не удалось узнать ссылку,
    файл больше WHATSAPP_MEDIA_LIMIT, сеть подвела) — respond() в этом случае
    сообщает жителю, что файл не принят, вместо того чтобы создавать заявку
    без вложения.
    """
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            meta_resp = await client.get(
                f"https://graph.facebook.com/{settings.meta_graph_api_version}/{media_id}",
                headers=headers)
        except httpx.HTTPError:
            logger.exception("WhatsApp: не удалось получить метаданные медиа %s", media_id)
            return None
        if meta_resp.status_code >= 400:
            logger.warning("WhatsApp: метаданные медиа %s отклонены: %s",
                          media_id, meta_resp.text[:200])
            return None
        info = meta_resp.json()
        url = info.get("url")
        size = int(info.get("file_size") or 0)
        if not url:
            return None
        if size and size > WHATSAPP_MEDIA_LIMIT:
            logger.info("WhatsApp: файл %s больше лимита (%s байт) — пропущен", media_id, size)
            return None

        try:
            file_resp = await client.get(url, headers=headers)
        except httpx.HTTPError:
            logger.exception("WhatsApp: не удалось скачать медиа %s", media_id)
            return None
        if file_resp.status_code >= 400:
            logger.warning("WhatsApp: скачивание медиа %s отклонено: %s",
                          media_id, file_resp.status_code)
            return None
        content = file_resp.content
        if len(content) > WHATSAPP_MEDIA_LIMIT:
            return None

    MEDIA_DIR.mkdir(exist_ok=True)
    ext = mimetypes.guess_extension(mime or "") or ""
    safe_name = "".join(c for c in (filename or "") if c.isalnum() or c in "._-")
    path = MEDIA_DIR / f"{media_id}_{safe_name or ('file' + ext)}"
    path.write_bytes(content)
    return path


# ------------------------------------------------------- основной обработчик

async def handle_incoming_message(msg: dict) -> None:
    """
    Разбор входящего сообщения WhatsApp по типу — аналог handle_text()/
    handle_media() из bot.py, только для обоих типов на входе одного вебхука.
    """
    phone = msg["phone"]

    if msg["type"] == "text":
        user_text = (msg.get("text") or "").strip()
        if not user_text:
            return
        await respond(msg, phone, user_text)
        return

    if msg["type"] in ("image", "document"):
        caption = (msg.get("text") or "").strip()
        media_id = msg.get("media_id")
        path = await download_whatsapp_media(
            media_id, msg.get("media_mime") or "", msg.get("media_filename") or "",
        ) if media_id else None
        if path is None:
            await send_whatsapp_text(
                phone, "Не получилось принять файл. Попробуйте отправить ещё раз "
                      "или опишите обращение текстом.")
            return
        label = "фотографию" if msg["type"] == "image" else "документ"
        model_text = caption or f"Житель прислал {label} без подписи."
        if caption:
            model_text = f"[Житель приложил {label}] {caption}"
        await respond(msg, phone, model_text, file_paths=[str(path)], citizen_text=caption)
        return

    await send_whatsapp_text(
        phone, "Пока я могу обрабатывать только текст, фото и документы. "
              "Опишите обращение текстом, пожалуйста.")


async def respond(msg: dict, phone: str, user_text: str,
                  file_paths: list[str] | None = None,
                  citizen_text: str | None = None) -> None:
    """
    Общий путь любого обращения WhatsApp — аналог respond() из bot.py.

    citizen_text — что записать в карточку как сообщение жителя, если оно
    отличается от текста для модели (случай фото/документа с подписью или без).
    """
    chat_id = int(phone)
    session = storage.get(chat_id)
    COUNTERS["messages"] += 1

    if not remote.bot_enabled:
        await send_whatsapp_text(phone, remote.maintenance_text)
        return

    if not available_providers():
        logger.error("WhatsApp: нет доступной модели, сообщение от %s не обработано", phone)
        return

    async with session.lock:
        await restore_history(session, chat_id, crm, settings.history_limit,
                              channel="whatsapp")

        record = await crm.incoming(
            build_profile(phone, msg.get("name", "")),
            citizen_text if citizen_text is not None else user_text,
            tg_message_id=None, file_paths=file_paths,
        )
        if record:
            session.ticket_id = record.get("ticket_id")
            if record.get("created") and session.ticket_id:
                asyncio.create_task(classify_ticket(
                    crm, session.ticket_id, user_text, session.provider))
                asyncio.create_task(refine_ticket(
                    crm, session.ticket_id, session.provider))
            if record.get("answer_mode") == "staff":
                # Разговор перехватил сотрудник — ответ уйдёт через очередь
                # исходящих (см. outbox_send_loop), ИИ здесь молчит.
                return

        quick = remote.match_quick_answer(user_text)
        if quick:
            COUNTERS["quick_answers"] += 1
            answer = quick["answer"]
            session.add("user", user_text, settings.history_limit)
            session.add("assistant", answer, settings.history_limit)
            if quick.get("id"):
                asyncio.create_task(crm.answer_hit(quick["id"]))
            await _deliver_answer(phone, session, answer)
            return

        session.add("user", user_text, settings.history_limit)

        detailed = session.mode is Mode.DETAILED
        system = build_system(
            session.mode,
            facts=remote.facts,
            invent=remote.invent_facts,
            override=remote.prompt_override(detailed),
        )
        max_tokens = MAX_TOKENS[session.mode]

        try:
            answer, answered_by = await ask_with_fallback(
                preferred=session.provider,
                system=system,
                history=session.history,
                max_tokens=max_tokens,
                detailed=detailed,
            )
        except ProviderError as e:
            session.drop_last()
            COUNTERS["errors"] += 1
            logger.info("Провайдер %s вернул ошибку: %s", session.provider, e)
            await send_whatsapp_text(phone, str(e))
            return
        except asyncio.CancelledError:
            session.drop_last()
            raise
        except Exception:
            session.drop_last()
            COUNTERS["errors"] += 1
            logger.exception("Ошибка при запросе к модели %s", session.provider)
            await send_whatsapp_text(phone, "Что-то пошло не так. Попробуйте ещё раз.")
            return

        if answered_by != session.provider:
            session.provider = answered_by

        answer = clean_text(answer)
        if not answer:
            session.drop_last()
            await send_whatsapp_text(
                phone, "Модель прислала пустой ответ. Попробуйте переформулировать.")
            return

        session.add("assistant", answer, settings.history_limit)
        COUNTERS["answers"] += 1
        await _deliver_answer(phone, session, answer)


async def _deliver_answer(phone: str, session, answer: str) -> None:
    if session.ticket_id:
        await crm.ai_message(session.ticket_id, answer)
    await send_whatsapp_text(phone, answer)


# ------------------------------------------------------------- вебхук Meta

async def verify_webhook(request: web.Request) -> web.Response:
    """GET /webhook — подтверждение адреса при подключении в Meta for Developers."""
    mode = request.query.get("hub.mode")
    token = request.query.get("hub.verify_token")
    challenge = request.query.get("hub.challenge", "")
    if (mode == "subscribe" and settings.meta_verify_token
            and token == settings.meta_verify_token):
        return web.Response(text=challenge)
    return web.Response(status=403)


async def receive_webhook(request: web.Request) -> web.Response:
    """POST /webhook — входящие сообщения. Meta ждёт быстрый 200: обработка фоном."""
    raw = await request.read()
    if not verify_signature(settings.meta_app_secret, raw,
                            request.headers.get("X-Hub-Signature-256")):
        return web.Response(status=403)
    try:
        payload: dict[str, Any] = json.loads(raw)
    except ValueError:
        return web.Response(status=200)  # мусор от Meta — не наша забота, но и не 500
    for msg in parse_webhook_payload(payload):
        asyncio.create_task(handle_incoming_message(msg))
    return web.Response(status=200)


# --------------------------------------------------------------- фоновые циклы

async def outbox_send_loop() -> None:
    """Аналог outbox_loop() из bot.py, но для очереди WhatsApp."""
    while True:
        await asyncio.sleep(OUTBOX_INTERVAL)
        try:
            items = await crm.outbox_pending(channel="whatsapp")
            for row in items:
                phone = str(row["chat_id"])
                ok, error = await send_whatsapp_text(phone, row["text"])
                if ok:
                    await crm.outbox_sent(row["id"])
                else:
                    blocked = "131026" in error or "470" in error  # номер недоступен
                    await crm.outbox_failed(row["id"], error, blocked=blocked)
            await crm.flush_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой цикла исходящих WhatsApp")


async def config_loop() -> None:
    while True:
        try:
            await remote.refresh(crm)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой опроса конфигурации")
        await asyncio.sleep(CONFIG_INTERVAL)


async def heartbeat_loop() -> None:
    while True:
        try:
            await crm.health(
                providers={name: is_available(name) for name in available_providers()},
                counters={**COUNTERS, "channel": "whatsapp"},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой сердцебиения")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        removed = storage.cleanup()
        if removed:
            logger.info("WhatsApp: убрано неактивных сессий: %s (осталось %s)",
                        removed, len(storage))


# ------------------------------------------------------------------- запуск

def _check_configured() -> None:
    missing = [name for name, value in (
        ("META_VERIFY_TOKEN", settings.meta_verify_token),
        ("META_ACCESS_TOKEN", settings.meta_access_token),
        ("META_APP_SECRET", settings.meta_app_secret),
        ("META_PHONE_NUMBER_ID", settings.meta_phone_number_id),
    ) if not value]
    if missing:
        raise SystemExit(
            "WhatsApp не настроен — не заданы: " + ", ".join(missing) + ". "
            "Заполните их в .env (см. .env.example) или не запускайте этот процесс, "
            "если WhatsApp пока не нужен."
        )


async def main() -> None:
    setup_logging(log_file="whatsapp_bot.log")
    _check_configured()

    remote.apply_cached()
    if crm.enabled:
        with contextlib.suppress(Exception):
            await remote.refresh(crm)

    ready = warm_up()
    logger.info("WhatsApp-бот запускается на порту %s", settings.whatsapp_port)
    logger.info("Доступные провайдеры: %s", ", ".join(ready) or "нет")

    tasks = [
        asyncio.create_task(cleanup_loop()),
        asyncio.create_task(outbox_send_loop()),
        asyncio.create_task(config_loop()),
        asyncio.create_task(heartbeat_loop()),
    ]

    app = web.Application()
    app.router.add_get("/webhook", verify_webhook)
    app.router.add_post("/webhook", receive_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.whatsapp_port)
    await site.start()
    logger.info("WhatsApp-вебхук слушает :%s/webhook", settings.whatsapp_port)

    try:
        await asyncio.Event().wait()  # работаем, пока не остановят (Ctrl+C)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        await runner.cleanup()
        await crm.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        if e.code:
            print(f"\n{e.code}")
    except KeyboardInterrupt:
        print("\nWhatsApp-бот остановлен.")
