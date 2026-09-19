'use strict'

import asyncio
import re
from datetime import datetime

import app_v7 as v7

v6 = v7.v6
v5 = v7.v5
v4 = v7.v4
v3 = v7.v3
v2 = v7.v2
base = v7.base


def catalog_query(hint: str) -> str:
    """Build a concise catalogue name from the full Telegram/Amazon title.

    The full title remains the source identity context, but barcode catalogues work
    better with brand + product + variant + size than with promotional prose.
    """
    value = hint or ""
    # Remove invisible Telegram formatting chars before building a catalogue query.
    value = re.sub(r"[\u200b-\u200f\u2060-\u206f\ufeff]", "", value)
    value = re.sub(r"\([^)]*(?:consegna|spedizione)[^)]*\)", " ", value, flags=re.I)
    value = re.sub(r"\s+-\s+\d{1,4}(?:[.,]\d{1,2})?\s*€.*$", "", value, flags=re.I)
    value = re.sub(r"\s+invece\s+di\s+.*$", "", value, flags=re.I)
    value = re.sub(r"^[✅⭐🔥⚡🛒📌🚨❗‼️🔴💰\s]+", "", value)

    # Channels often prepend marketing prose before a multipack title:
    # "Tornaaa, sempre TOP RICHIESTO: 4 x Elmex ...".  The pack expression is a
    # reliable start of the commercial identity, so discard a short preamble.
    pack_anchor = re.search(r"\b\d{1,2}\s*[x×]\s+(?=[A-Za-zÀ-ÿ])", value, flags=re.I)
    if pack_anchor and pack_anchor.start() <= 120:
        value = value[pack_anchor.start():]

    # Stop before price/promo commentary.  This keeps brand/product/variant/size
    # while excluding prose that makes external barcode search unnecessarily noisy.
    value = re.sub(
        r"\s*[🔴❗‼️💰🚨⚡🔥]*\s*(?:al\s+supermercato|qui\s+su\s+amazon|sconto\s*\+?\s*coupon|"
        r"passa\s+da|minimo\s+storico|offerta|coupon|venduto|spedito|"
        r"apri\s+su\s+amazon|apri\s+link\s+amazon).*$",
        "",
        value,
        flags=re.I,
    )
    # Keep the extended commercial identity, including size/variant after commas.
    # Cutting at the first comma loses decisive attributes such as "1,9 L".
    value = re.sub(r"\s+", " ", value).strip(" -,:;")
    return value[:180]


def unit_query(hint: str) -> str:
    value = catalog_query(hint)
    value = re.sub(r"^\s*\d{1,2}\s*[x×]\s+(?=[A-Za-zÀ-ÿ])", "", value, flags=re.I)
    value = re.sub(
        r"\b\d{1,2}\s*[x×]\s*(\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg))\b",
        r"\1",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b\d{1,2}\s*(?:pezzi|pz|tubi|flaconi|bottiglie|confezioni)\s+da\s+",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\b(?:set|pack|confezione)\s+(?:di|da)?\s*\d{1,2}\b", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" -,:;")[:120]


# Make app_v7's web fallback use the same concise single-unit query.
v7.unit_query = unit_query


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    if version >= 3 and cached.get("name_lookup_done"):
        return (
            list(cached.get("name_identifiers") or []),
            dict(cached.get("name_identifier_modes") or {}),
            list(cached.get("name_sources") or []),
        )

    full_query = catalog_query(hint)
    single_query = unit_query(hint)
    amazon_pack = v2.pack_count(hint)
    scored = []
    sources: list[str] = []

    # 1) Search external barcode catalogue by concise product name.
    full_items = await v6._upc_search(full_query) if full_query else []
    if full_items:
        sources.append("UPCitemdb")
        scored.extend(v7._score_items(full_items, full_query, hint, state))

    # 2) For a multipack, retry the exact single sellable unit title. Via Veneto
    # normally stores this unit barcode rather than the Amazon bundle barcode.
    if amazon_pack > 1 and not scored and single_query and single_query.casefold() != full_query.casefold():
        single_items = await v6._upc_search(single_query)
        if single_items and "UPCitemdb" not in sources:
            sources.append("UPCitemdb")
        scored.extend(v7._score_items(single_items, single_query, hint, state, forced_mode="unit"))

    scored.sort(key=lambda x: x[0], reverse=True)
    accepted: list[str] = []
    modes: dict[str, str] = {}
    titles: dict[str, str] = {}
    for score, code, mode, external_title in scored:
        if code not in accepted:
            accepted.append(code)
            modes[code] = mode
            titles[code] = external_title

    # 3) If the catalogue has no hit, query public web product pages using the same
    # concise name. The web resolver only accepts a valid GTIN that exists exactly
    # in Via Veneto; it never compares the Via Veneto description text.
    if not accepted:
        web_codes, web_sources = await v7._original_web_identifiers(asin, full_query, state, cache)
        if not web_codes and amazon_pack > 1 and single_query != full_query:
            web_codes, web_sources = await v7._original_web_identifiers(asin, single_query, state, cache)
        for code in web_codes:
            if code not in accepted:
                accepted.append(code)
                modes[code] = "unit" if amazon_pack > 1 else "package"
        for source in web_sources:
            if source not in sources:
                sources.append(source)

    entry = dict(cache.get(asin) if isinstance(cache.get(asin), dict) else cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": 3,
        "name_checked_at": datetime.now(base.ROME).isoformat(),
        "name_query": full_query,
        "name_unit_query": single_query,
        "name_identifiers": accepted,
        "name_identifier_modes": modes,
        "name_titles": titles,
        "name_sources": sources,
    })
    if accepted:
        entry["identifiers"] = list(dict.fromkeys(list(entry.get("identifiers") or []) + accepted))
        if hint and not entry.get("title"):
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info(
        "Name->EAN v3 ASIN=%s full=%r unit=%r accepted=%s modes=%s sources=%s",
        asin, full_query, single_query, accepted, modes, sources,
    )
    return accepted, modes, sources


# app_v6.resolve_offer resolves these names dynamically from its own module globals.
v6.name_identifiers = name_identifiers
v7.name_identifiers = name_identifiers
v2.resolve_offer = v6.resolve_offer
base.resolve_offer = v6.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
