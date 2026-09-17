import asyncio
import os
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app_v12

base = app_v12.base


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def parse_cutoff(raw: str) -> datetime:
    if not raw:
        return datetime.now(base.ROME)
    value = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=base.ROME)
    return dt.astimezone(base.ROME)


def elapsed(started: datetime) -> str:
    seconds = max(0, int((datetime.now(base.ROME) - started).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"


async def bot_call(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{base.BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload)
        data = response.json()
        if not response.is_success or not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description') or response.status_code}")
        return data.get("result")


async def send_message(chat_id: str, text: str):
    return await bot_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


async def edit_message(chat_id: str, message_id: int, text: str):
    try:
        return await bot_call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
    except Exception as exc:
        print(f"STATUS_EDIT_WARNING {type(exc).__name__}: {exc}")
        return None


async def heartbeat(chat_id: str, message_id: int, started: datetime, cutoff: datetime, stop: asyncio.Event):
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
            return
        except asyncio.TimeoutError:
            await edit_message(
                chat_id,
                message_id,
                "⏳ Report ancora in corso…\n"
                f"Tempo trascorso: {elapsed(started)}\n"
                f"Dati fino alle {cutoff.strftime('%H:%M')}.\n"
                "Sto leggendo Telegram, risolvendo EAN e confrontando Via Veneto.\n"
                "Puoi usare /status in qualsiasi momento.",
            )


async def main():
    base.validate_config()
    cutoff = parse_cutoff(env("REPORT_CUTOFF"))
    origin = env("REPORT_ORIGIN", "manual")
    chat_id = env("REPORT_CHAT_ID")
    raw_status_message_id = env("STATUS_MESSAGE_ID")
    status_message_id = int(raw_status_message_id) if raw_status_message_id.isdigit() else 0

    client = TelegramClient(StringSession(base.USER_SESSION), base.API_ID, base.API_HASH)
    await client.connect()
    base.user_client = client
    started = datetime.now(base.ROME)
    stop = asyncio.Event()
    hb_task = None

    try:
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_USER_SESSION non autorizzata")

        if not chat_id:
            me = await client.get_me()
            chat_id = str(getattr(me, "id", "") or "")
        if not chat_id:
            raise RuntimeError("Chat Telegram di destinazione non determinabile")

        if not status_message_id:
            text = (
                "🕗 Report automatico delle 20:00 avviato.\n"
                "Sto leggendo Telegram e confrontando i prezzi con Via Veneto.\n"
                "Puoi usare /status durante l'elaborazione."
                if origin == "scheduled"
                else
                "🟡 Report avviato.\nSto elaborando i messaggi di oggi."
            )
            status = await send_message(chat_id, text)
            status_message_id = int(status.get("message_id") or 0)

        hb_task = asyncio.create_task(heartbeat(chat_id, status_message_id, started, cutoff, stop))

        print(f"REPORT_START origin={origin} chat={chat_id} cutoff={cutoff.isoformat()}")
        report = await base.build_report(cutoff)
        print(f"REPORT_BUILT chars={len(report)} elapsed={elapsed(started)}")

        for part in base.chunks(report):
            await send_message(chat_id, part)

        await edit_message(
            chat_id,
            status_message_id,
            "✅ Report completato.\n"
            f"Durata: {elapsed(started)}\n"
            f"Dati analizzati fino alle {cutoff.strftime('%H:%M')}.",
        )
        print(f"REPORT_SUCCESS elapsed={elapsed(started)}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"REPORT_ERROR {error}")
        if chat_id and status_message_id:
            await edit_message(
                chat_id,
                status_message_id,
                "❌ Report non completato.\n"
                f"Dopo: {elapsed(started)}\n"
                f"Errore: {error[:700]}\n\n"
                "Usa /status per vedere lo stato GitHub dell'esecuzione.",
            )
        raise
    finally:
        stop.set()
        if hb_task:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
        await client.disconnect()
        await base.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
