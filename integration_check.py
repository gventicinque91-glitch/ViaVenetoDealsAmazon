import asyncio
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app_v6

app = app_v6.base


async def bot_call(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{app.BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload)
        data = response.json()
        if not response.is_success or not data.get("ok"):
            raise RuntimeError(f"Bot API {method} fallita: {data.get('description') or response.status_code}")
        return data.get("result")


def find_offer(messages, asin: str):
    for message in messages:
        for url in app_v6.v2.extract_urls(message):
            canonical_asin = app.asin_from_url(url)
            if canonical_asin == asin:
                return message, url
            if asin in url.upper():
                return message, url
    return None, ""


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

        sources = await app_v6.v3.find_source_chats()
        print("SOURCE_CHATS=OK " + " | ".join(name for name, _ in sources))
        now = datetime.now(app.ROME)
        messages = await app_v6.v3.day_messages(now)
        print(f"TODAY_MESSAGES=OK count={len(messages)}")

        state = await app.vv.get_state()
        print(f"VIA_VENETO=OK revision={app.vv.revision} generation={app.vv.generation}")

        # Diagnostic only: show all Elmex entries actually present in Via Veneto.
        elmex_rows = []
        for ean, product in (state.get("products") or {}).items():
            desc = str((product or {}).get("description") or (product or {}).get("name") or "")
            if "ELMEX" in desc.upper():
                elmex_rows.append((str(ean), desc))
        print(f"ELMEX_DB_COUNT={len(elmex_rows)}")
        for ean, desc in elmex_rows[:30]:
            print(f"ELMEX_DB ean={ean} description={desc}")

        # Regression: known Dove mapping remains exact.
        dove_message, dove_url = find_offer(messages, "B0D6ZJ276V")
        if not dove_message:
            raise RuntimeError("Offerta Dove di test non trovata")
        aliases = app.load_aliases()
        cache = await app.cache_store.load({})
        dove = await app_v6.resolve_offer(dove_message, dove_url, state, cache, aliases)
        if not dove or dove.get("status") != "matched" or dove.get("ean") != "8720181460043":
            raise RuntimeError(f"Regressione Dove: {dove}")
        print(f"DOVE_RESOLVE=OK ean={dove['ean']} total={dove['amazon_total']:.2f} unit={dove['amazon_unit']:.2f}")

        # Core test: Telegram extended title -> external EAN -> exact Via Veneto EAN lookup.
        elmex_asin = "B0BZ58TBGD"
        elmex_message, elmex_url = find_offer(messages, elmex_asin)
        if not elmex_message:
            raise RuntimeError("Offerta Elmex B0BZ58TBGD non trovata oggi")
        elmex_segment = app_v6.v5.offer_segment(elmex_message, elmex_url, elmex_asin)
        elmex_hint = app_v6.v2.product_hint(elmex_segment)
        print(f"ELMEX_SEGMENT={elmex_segment}")
        print(f"ELMEX_HINT={elmex_hint}")

        name_codes, modes, sources_used = await asyncio.wait_for(
            app_v6.name_identifiers(elmex_asin, elmex_hint, state, cache), timeout=45
        )
        print(f"ELMEX_NAME_EAN codes={name_codes} modes={modes} sources={sources_used}")
        elmex = await asyncio.wait_for(
            app_v6.resolve_offer(elmex_message, elmex_url, state, cache, aliases), timeout=60
        )
        print(f"ELMEX_RESOLVE={elmex}")

        bot = await bot_call("getMe", {})
        print(f"BOT=OK username=@{bot.get('username', '')}")

        # Do not fail this diagnostic commit solely on an unresolved external catalogue result;
        # the DB dump above tells us the exact EAN family to validate next.
        chat_id = str(getattr(me, "id", ""))
        status = "MATCHED" if elmex and elmex.get("status") == "matched" else "UNRESOLVED"
        await bot_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"🔎 Test nuovo matching nome→EAN→Via Veneto: Elmex {status}. Sto verificando l'EAN esatto presente nel listino, senza fare matching fuzzy sul database.",
                "disable_web_page_preview": True,
            },
        )
        print("TELEGRAM_DIAGNOSTIC_SEND=OK")
    finally:
        await client.disconnect()
        await app.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
