import asyncio
from datetime import datetime

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

import app_v12

app = app_v12.base


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
        for url in app_v12.v2.extract_urls(message):
            canonical_asin = app.asin_from_url(url)
            if canonical_asin == asin or asin in url.upper():
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

        sources = await app_v12.v3.find_source_chats()
        print("SOURCE_CHATS=OK " + " | ".join(name for name, _ in sources))
        now = datetime.now(app.ROME)
        messages = await app_v12.v3.day_messages(now)
        print(f"TODAY_MESSAGES=OK count={len(messages)}")

        state = await app.vv.get_state()
        print(f"VIA_VENETO=OK revision={app.vv.revision} generation={app.vv.generation}")

        # Regression for channels that hide Amazon behind a CTA text-link.
        tesori_message = next(
            (
                m for m in messages
                if "tesori d'oriente" in str(m.message or "").casefold()
                and "hammam" in str(m.message or "").casefold()
            ),
            None,
        )
        if tesori_message:
            tesori_urls = app_v12.v2.extract_urls(tesori_message)
            print(f"TESORI_URL_CANDIDATES={tesori_urls}")
            if not tesori_urls:
                raise RuntimeError("Tesori Hammam trovato ma nessun link Amazon/affiliate estratto")

            tesori_resolved = None
            for tesori_url in tesori_urls:
                canonical = await app.canonical_amazon_url(tesori_url)
                asin = app.asin_from_url(canonical)
                print(f"TESORI_URL raw={tesori_url} canonical={canonical} asin={asin}")
                if not asin:
                    continue
                result = await app_v12.v11.resolve_offer(
                    tesori_message,
                    tesori_url,
                    state,
                    await app.cache_store.load({}),
                    app.load_aliases(),
                )
                print(f"TESORI_RESOLVE={result}")
                if result and result.get("status") == "matched":
                    tesori_resolved = result
                    break

            if not tesori_resolved:
                raise RuntimeError("Tesori Hammam non risolto come offerta detergenza/igiene")
            if tesori_resolved.get("ean") != "8008970005591":
                raise RuntimeError(f"Tesori Hammam EAN inatteso: {tesori_resolved.get('ean')}")
            print(
                f"TESORI_MATCH_OK ean={tesori_resolved['ean']} "
                f"asin={tesori_resolved['asin']} unit={tesori_resolved['amazon_unit']:.2f}"
            )
        else:
            print("TESORI_TEST_SKIPPED message_not_found_today")

        aliases = app.load_aliases()
        cache = await app.cache_store.load({})

        dove_message, dove_url = find_offer(messages, "B0D6ZJ276V")
        if not dove_message:
            raise RuntimeError("Offerta Dove di test non trovata")
        dove = await app_v12.v11.resolve_offer(dove_message, dove_url, state, cache, aliases)
        if not dove or dove.get("status") != "matched" or dove.get("ean") != "8720181460043":
            raise RuntimeError(f"Regressione Dove: {dove}")
        print(f"DOVE_RESOLVE=OK ean={dove['ean']} total={dove['amazon_total']:.2f} unit={dove['amazon_unit']:.2f}")

        elmex_asin = "B0BZ58TBGD"
        elmex_message, elmex_url = find_offer(messages, elmex_asin)
        if not elmex_message:
            raise RuntimeError("Offerta Elmex B0BZ58TBGD non trovata oggi")
        elmex_segment = app_v12.v5.offer_segment(elmex_message, elmex_url, elmex_asin)
        elmex_hint = app_v12.v2.product_hint(elmex_segment)
        print(f"ELMEX_SEGMENT={elmex_segment}")
        print(f"ELMEX_HINT={elmex_hint}")
        print(f"ELMEX_CATALOG_QUERY={app_v12.v8.catalog_query(elmex_hint)}")
        print(f"ELMEX_UNIT_QUERY={app_v12.v8.unit_query(elmex_hint)}")

        elmex = await asyncio.wait_for(
            app_v12.v11.resolve_offer(elmex_message, elmex_url, state, cache, aliases), timeout=180
        )
        print(f"ELMEX_RESOLVE={elmex}")

        cached = cache.get(elmex_asin) if isinstance(cache.get(elmex_asin), dict) else {}
        print(
            "ELMEX_NAME_EAN "
            f"codes={cached.get('name_identifiers', [])} "
            f"modes={cached.get('name_identifier_modes', {})} "
            f"sources={cached.get('name_sources', [])}"
        )

        if not elmex or elmex.get("status") != "matched":
            raise RuntimeError(f"Elmex non risolto dal nuovo flusso nome→EAN→Via Veneto: {elmex}")
        if elmex.get("ean") != "8718951545632":
            raise RuntimeError(f"Elmex EAN inatteso: {elmex.get('ean')}")
        if int(elmex.get("units") or 0) != 4:
            raise RuntimeError(f"Elmex multipack non normalizzato a 4 unità: {elmex.get('units')}")
        if not str(elmex.get("identifier_source") or "").startswith("nome→EAN"):
            raise RuntimeError(f"Elmex non risolto tramite nome→EAN: {elmex.get('identifier_source')}")
        print(f"ELMEX_MATCH_OK ean={elmex['ean']} total={elmex['amazon_total']:.2f} unit={elmex['amazon_unit']:.4f} source={elmex['identifier_source']}")

        bot = await bot_call("getMe", {})
        print(f"BOT=OK username=@{bot.get('username', '')}")
        chat_id = str(getattr(me, "id", ""))
        await bot_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"✅ Matching nome esteso → EAN → Via Veneto verificato su Elmex: EAN {elmex['ean']}, Amazon €{elmex['amazon_total']:.2f}/4 = €{elmex['amazon_unit']:.2f} per tubo.",
                "disable_web_page_preview": True,
            },
        )
        print("TELEGRAM_DIAGNOSTIC_SEND=OK")
    finally:
        await client.disconnect()
        await app.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
