import asyncio
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app_v2

app = app_v2.base


async def bot_call(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{app.BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload)
        data = response.json()
        if not response.is_success or not data.get("ok"):
            raise RuntimeError(f"Bot API {method} fallita: {data.get('description') or response.status_code}")
        return data.get("result")


async def main():
    app.validate_config()

    client = TelegramClient(StringSession(app.USER_SESSION), app.API_ID, app.API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("StringSession Telegram non autorizzata")
        app.user_client = client

        me = await client.get_me()
        print(f"TELEGRAM_USER=OK id={getattr(me, 'id', '?')}")

        sources = await app_v2.find_source_chats()
        print("SOURCE_CHATS=OK " + " | ".join(name for name, _ in sources))

        now = datetime.now(app.ROME)
        messages = await app_v2.day_messages(now)
        print(f"TODAY_MESSAGES=OK count={len(messages)}")

        state = await app.vv.get_state()
        print(f"VIA_VENETO=OK revision={app.vv.revision} generation={app.vv.generation}")

        exact = app.vv.lookup(state, "8720181460043")
        if not exact:
            raise RuntimeError("EAN di test 8720181460043 non trovato in Via Veneto")
        print(f"TEST_EAN=OK description={exact.get('description', '')}")

        cache = await app.cache_store.load({})
        web_codes, web_sources = await asyncio.wait_for(
            app_v2.web_identifiers(
                "B0D6ZJ276V",
                "Dove Bagnoschiuma Dolce Nutrimento 6 Pezzi da 225 ml",
                state,
                cache,
            ),
            timeout=150,
        )
        print(f"WEB_GTIN_FALLBACK={'OK' if '8720181460043' in web_codes else 'NO_MATCH'} codes={web_codes} sources={web_sources[:3]}")

        bot = await bot_call("getMe", {})
        print(f"BOT=OK username=@{bot.get('username', '')}")

        report = await asyncio.wait_for(app.build_report(now), timeout=300)
        print("REPORT_BUILD=OK")
        for line in report.splitlines()[:14]:
            print(line)

        chat_id = str(getattr(me, "id", ""))
        if not chat_id:
            raise RuntimeError("Impossibile determinare il chat_id Telegram")

        await bot_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "✅ Via Veneto Deals aggiornato: ora monitoro Caccia allo SCONTO + SCONTALO e uso anche il resolver web ASIN→EAN. Invio il report aggiornato.",
                "disable_web_page_preview": True,
            },
        )
        for part in app.chunks(report):
            await bot_call(
                "sendMessage",
                {"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
            )
        print("TELEGRAM_REPORT_SEND=OK")
    finally:
        await client.disconnect()
        await app.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
