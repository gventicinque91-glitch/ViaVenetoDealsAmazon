import asyncio
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app_v5

app = app_v5.base


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

        sources = await app_v5.v4.v3.find_source_chats()
        print("SOURCE_CHATS=OK " + " | ".join(name for name, _ in sources))

        now = datetime.now(app.ROME)
        messages = await app_v5.v4.v3.day_messages(now)
        print(f"TODAY_MESSAGES=OK count={len(messages)}")

        sample_message = None
        sample_url = ""
        for message in messages:
            for url in app_v5.v2.extract_urls(message):
                if "B0D6ZJ276V" in url.upper():
                    sample_message, sample_url = message, url
                    break
            if sample_message:
                break
        if not sample_message:
            raise RuntimeError("Offerta SCONTALO B0D6ZJ276V non trovata oggi")

        segment = app_v5.offer_segment(sample_message, sample_url, "B0D6ZJ276V")
        price = app.extract_offer_price(segment)
        print(f"SAMPLE_SEGMENT={segment}")
        print(f"SAMPLE_PRICE={price}")
        if abs(float(price or 0) - 8.94) > 0.001:
            raise RuntimeError(f"Prezzo SCONTALO errato: atteso 8.94, letto {price}")

        state = await app.vv.get_state()
        print(f"VIA_VENETO=OK revision={app.vv.revision} generation={app.vv.generation}")

        exact = app.vv.lookup(state, "8720181460043")
        if not exact:
            raise RuntimeError("EAN di test 8720181460043 non trovato in Via Veneto")
        print(f"TEST_EAN=OK description={exact.get('description', '')}")

        aliases = app.load_aliases()
        cache = await app.cache_store.load({})
        resolved = await app_v5.resolve_offer(sample_message, sample_url, state, cache, aliases)
        if not resolved or resolved.get("status") != "matched":
            raise RuntimeError(f"Offerta Dove non risolta: {resolved}")
        if resolved.get("ean") != "8720181460043":
            raise RuntimeError(f"EAN Dove errato: {resolved.get('ean')}")
        if abs(float(resolved.get("amazon_total") or 0) - 8.94) > 0.001:
            raise RuntimeError(f"Totale Amazon errato: {resolved.get('amazon_total')}")
        if abs(float(resolved.get("amazon_unit") or 0) - 1.49) > 0.011:
            raise RuntimeError(f"Prezzo unitario Amazon errato: {resolved.get('amazon_unit')}")
        print(f"DOVE_RESOLVE=OK ean={resolved['ean']} total={resolved['amazon_total']:.2f} unit={resolved['amazon_unit']:.2f}")

        web_codes, web_sources = await asyncio.wait_for(
            app_v5.v4.web_identifiers(
                "B0D6ZJ276V",
                "Dove Bagnoschiuma Dolce Nutrimento 6 Pezzi da 225 ml",
                state,
                cache,
            ),
            timeout=45,
        )
        print(f"WEB_GTIN_FALLBACK={'OK' if web_codes else 'NO_MATCH_BUT_SAFE'} codes={web_codes} sources={web_sources[:3]}")

        bot = await bot_call("getMe", {})
        print(f"BOT=OK username=@{bot.get('username', '')}")

        report = await asyncio.wait_for(app.build_report(now), timeout=180)
        print("REPORT_BUILD=OK")
        for line in report.splitlines()[:24]:
            print(line)

        chat_id = str(getattr(me, "id", ""))
        await bot_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "✅ Aggiornamento completato: Caccia allo SCONTO + SCONTALO, mapping EAN verificati e prezzo SCONTALO associato alla singola offerta. Report di test aggiornato in arrivo.",
                "disable_web_page_preview": True,
            },
        )
        for part in app.chunks(report):
            await bot_call("sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
        print("TELEGRAM_REPORT_SEND=OK")
    finally:
        await client.disconnect()
        await app.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
