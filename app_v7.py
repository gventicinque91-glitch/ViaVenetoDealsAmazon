'use strict'

import asyncio
import re
from datetime import datetime

import app_v6 as v6

v5 = v6.v5
v4 = v6.v4
v3 = v6.v3
v2 = v6.v2
base = v6.base

_original_web_identifiers = v4.web_identifiers


def unit_query(hint: str) -> str:
    """Turn a multipack title into the single retail item title without losing identity.

    Example: 'Elmex Dentifricio Anticarie 4x75ml (...)' ->
             'Elmex Dentifricio Anticarie 75ml ...'
    The resulting string is used only to discover an EAN externally; Via Veneto is
    still matched strictly by the returned EAN, never by description.
    """
    value = re.sub(r"\([^)]*(?:consegna|spedizione)[^)]*\)", " ", hint or "", flags=re.I)
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
    return re.sub(r"\s+", " ", value).strip(" -,:")[:180]


def _score_items(items: list[dict], query: str, original_hint: str, state: dict, forced_mode: str = ""):
    amazon_pack = v2.pack_count(original_hint)
    scored: list[tuple[float, str, str, str]] = []
    for item in items:
        external_title = " ".join(
            x for x in (str(item.get("brand") or "").strip(), str(item.get("title") or "").strip()) if x
        ).strip()
        score = v6._title_score(query, external_title)
        if score < 5.0:
            continue
        external_pack = v6._pack_count_from_title(external_title)
        for code in v6._identifier_candidates(item):
            # This is the only Via Veneto identity check: exact EAN/GTIN lookup.
            if not base.vv.first_match(state, [code]):
                continue
            if forced_mode:
                mode = forced_mode
            elif amazon_pack > 1 and external_pack == amazon_pack:
                mode = "package"
            elif amazon_pack > 1 and external_pack == 1:
                mode = "unit"
            else:
                mode = "package"
            scored.append((score, code, mode, external_title))
    return scored


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    if version >= 2 and cached.get("name_lookup_done"):
        return (
            list(cached.get("name_identifiers") or []),
            dict(cached.get("name_identifier_modes") or {}),
            list(cached.get("name_sources") or []),
        )

    full_query = v6._compact_query(hint)
    if not full_query:
        return [], {}, []

    scored: list[tuple[float, str, str, str]] = []
    sources: list[str] = []

    full_items = await v6._upc_search(full_query)
    if full_items:
        sources.append("UPCitemdb")
        scored.extend(_score_items(full_items, full_query, hint, state))

    # For multipacks, Via Veneto normally contains the sellable unit EAN. If the
    # full multipack title did not yield an exact listino EAN, search the normalized
    # single-unit title and mark any exact EAN as a unit EAN.
    amazon_pack = v2.pack_count(hint)
    single_query = unit_query(hint)
    if amazon_pack > 1 and not scored and single_query and single_query.casefold() != full_query.casefold():
        single_items = await v6._upc_search(single_query)
        if single_items and "UPCitemdb" not in sources:
            sources.append("UPCitemdb")
        scored.extend(_score_items(single_items, single_query, hint, state, forced_mode="unit"))

    scored.sort(key=lambda x: x[0], reverse=True)
    accepted: list[str] = []
    modes: dict[str, str] = {}
    titles: dict[str, str] = {}
    for score, code, mode, external_title in scored:
        if code not in accepted:
            accepted.append(code)
            modes[code] = mode
            titles[code] = external_title

    entry = dict(cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": 2,
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
        "Name->EAN v2 ASIN=%s full=%r unit=%r accepted=%s modes=%s",
        asin, full_query, single_query, accepted, modes,
    )
    return accepted, modes, sources


async def web_identifiers(asin: str, hint: str, state: dict, cache: dict):
    codes, sources = await _original_web_identifiers(asin, hint, state, cache)
    if codes:
        return codes, sources
    # Search engines often index the unit EAN rather than the Amazon multipack EAN.
    if v2.pack_count(hint) > 1:
        single = unit_query(hint)
        if single and single.casefold() != (hint or "").casefold():
            return await _original_web_identifiers(asin, single, state, cache)
    return codes, sources


# Patch the resolver hooks used by app_v6.resolve_offer and the report builder.
v6.name_identifiers = name_identifiers
v4.web_identifiers = web_identifiers
v2.resolve_offer = v6.resolve_offer
base.resolve_offer = v6.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
