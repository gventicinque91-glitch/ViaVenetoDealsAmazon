import asyncio
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app


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

        source = await app.find_source_chat()
        source_title = getattr(source, "title", None) or getattr(source, "username", None) or app.SOURCE_CHAT_TITLE
        print(f"SOURCE_CHAT=OK title={source_title}")

        now = datetime.now(app.ROME)
        messages = await app.day_messages(now)
        print(f"TODAY_MESSAGES=OK count={len(messages)}")

        revision = await app.vv._request("/revision")
        print(f"VIA_VENETO=OK revision={revision.get('revision')} generation={revision.get('generation')}")

        bot = await bot_call("getMe", {})
        print(f"BOT=OK username=@{bot.get('username', '')}")

        report = await asyncio.wait_for(app.build_report(now), timeout=240)
        print("REPORT_BUILD=OK")
        for line in report.splitlines()[:10]:
            print(line)

        # Nelle chat private Telegram il chat_id coincide con l'ID dell'utente.
        # L'invio riesce solo se l'utente ha gia' avviato il bot: e' quindi anche
        # un test reale della destinazione del report.
        chat_id = str(getattr(me, "id", ""))
        if not chat_id:
            raise RuntimeError("Impossibile determinare il chat_id Telegram")

        await bot_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "✅ Via Veneto Deals: collegamenti Telegram e database verificati. Invio ora il report di test aggiornato.",
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
