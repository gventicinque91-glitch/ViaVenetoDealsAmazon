'use strict'

import asyncio
import html as html_lib
import re
from datetime import datetime
from urllib.parse import urlparse

import app_v8 as v8

v7 = v8.v7
v6 = v8.v6
v5 = v8.v5
v4 = v8.v4
v3 = v8.v3
v2 = v8.v2
base = v8.base


def _norm_measure(value: str, unit: str) -> str:
    return f"{value.replace(',', '.').lower()}{unit.lower()}"


def _measure_set(text: str) -> set[str]:
    return {
        _norm_measure(value, unit)
        for value, unit in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*(ml|cl|l|g|kg)\b", text or "", flags=re.I)
    }


def _significant_tokens(text: str) -> list[str]:
    stop = {
        "della", "delle", "dello", "degli", "dell", "alla", "allo", "agli", "alle",
        "con", "senza", "per", "pezzi", "pezzo", "pack", "set", "ml", "prodotto",
        "formula", "protezione", "consegna", "ore", "nuovo", "nuova", "originale",
    }
    return [
        t for t in re.findall(r"[a-zà-ÿ0-9]+", (text or "").casefold())
        if len(t) >= 3 and t not in stop and not t.isdigit()
    ]


def _context_score(query: str, context: str) -> float:
    q = _significant_tokens(query)
    if not q:
        return 0.0
    low = (context or "").casefold()
    matched = [t for t in dict.fromkeys(q) if t in low]
    score = float(len(matched))

    # First meaningful token is normally the brand. It must be present.
    if q[0] not in low:
        return 0.0
    score += 3.0

    q_measures = _measure_set(query)
    c_measures = _measure_set(context)
    if q_measures:
        if q_measures & c_measures:
            score += 3.0
        else:
            score -= 2.0
    return score


def _valid_db_codes_from_text(raw: str, query: str, state: dict) -> list[tuple[str, float]]:
    """Extract EANs from external evidence and validate product context.

    Crucially, this never compares the query with Via Veneto descriptions. The text
    similarity is evaluated only against the external search/page context; Via Veneto
    is queried afterwards using the exact EAN/GTIN.
    """
    clean = v2._clean_text(raw) if hasattr(v2, "_clean_text") else re.sub(r"<[^>]+>", " ", html_lib.unescape(raw or ""))
    found: dict[str, float] = {}
    for m in re.finditer(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)", clean):
        raw_code = m.group(1)
        if not base.valid_gtin(raw_code):
            continue
        lo, hi = max(0, m.start() - 240), min(len(clean), m.end() + 240)
        context = clean[lo:hi]
        score = _context_score(query, context)
        if score < 6.0:
            continue
        for code in base.ean_candidates(raw_code):
            if base.vv.first_match(state, [code]):
                found[code] = max(found.get(code, 0.0), score)
    return sorted(found.items(), key=lambda x: x[1], reverse=True)


async def search_engine_eans(query: str, state: dict) -> tuple[list[str], list[str]]:
    """Discover EANs from public search snippets/pages using the product name.

    Search text is external evidence only. A result is accepted only when:
    - GTIN checksum is valid;
    - nearby external text strongly matches brand/product/size;
    - the exact EAN exists in Via Veneto.
    """
    queries = [
        f'"{query}" EAN',
        f'"{query}" GTIN',
        f'"{query}" "codice a barre"',
    ]
    endpoints = (
        ("https://www.google.com/search", lambda q: {"q": q, "num": "10", "hl": "it"}, "google"),
        ("https://www.bing.com/search", lambda q: {"q": q, "format": "rss", "setlang": "it-IT"}, "bing"),
        ("https://html.duckduckgo.com/html/", lambda q: {"q": q}, "duckduckgo"),
    )

    evidence: dict[str, set[str]] = {}
    best_score: dict[str, float] = {}

    async def one(endpoint, params, provider):
        try:
            r = await base.http.get(endpoint, params=params, headers=v2.SEARCH_HEADERS, timeout=10)
            if not r.is_success:
                return provider, ""
            return provider, r.text[:800000]
        except Exception:
            return provider, ""

    tasks = []
    for q in queries:
        for endpoint, make_params, provider in endpoints:
            tasks.append(one(endpoint, make_params(q), provider))
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    except asyncio.TimeoutError:
        results = []

    for provider, body in results:
        if not body:
            continue
        for code, score in _valid_db_codes_from_text(body, query, state):
            evidence.setdefault(code, set()).add(provider)
            best_score[code] = max(best_score.get(code, 0.0), score)

    # A very strong context match from one provider is sufficient; otherwise require
    # corroboration by at least two independent search providers.
    accepted = [
        code for code, providers in evidence.items()
        if best_score.get(code, 0.0) >= 9.0 or len(providers) >= 2
    ]
    accepted.sort(key=lambda c: (best_score.get(c, 0.0), len(evidence.get(c, set()))), reverse=True)
    sources = sorted({p for code in accepted for p in evidence.get(code, set())})
    base.LOG.info("Search-name->EAN query=%r accepted=%s sources=%s", query, accepted, sources)
    return accepted, sources


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    if version >= 4 and cached.get("name_lookup_done"):
        return (
            list(cached.get("name_identifiers") or []),
            dict(cached.get("name_identifier_modes") or {}),
            list(cached.get("name_sources") or []),
        )

    full_query = v8.catalog_query(hint)
    single_query = v8.unit_query(hint)
    amazon_pack = v2.pack_count(hint)
    accepted: list[str] = []
    modes: dict[str, str] = {}
    sources: list[str] = []

    # Structured barcode catalogue first.
    for query, forced_mode in ((full_query, ""), (single_query, "unit" if amazon_pack > 1 else "")):
        if not query or (query == single_query and query == full_query):
            continue
        items = await v6._upc_search(query)
        scored = v7._score_items(items, query, hint, state, forced_mode=forced_mode) if items else []
        if scored and "UPCitemdb" not in sources:
            sources.append("UPCitemdb")
        for _, code, mode, _ in sorted(scored, key=lambda x: x[0], reverse=True):
            if code not in accepted:
                accepted.append(code)
                modes[code] = mode
        if accepted:
            break

    # Generic public-web discovery from the extended/normalized product name.
    if not accepted:
        queries = [full_query]
        if amazon_pack > 1 and single_query and single_query.casefold() != full_query.casefold():
            queries.append(single_query)
        for idx, query in enumerate(queries):
            codes, web_sources = await search_engine_eans(query, state)
            if codes:
                for code in codes:
                    if code not in accepted:
                        accepted.append(code)
                        modes[code] = "unit" if amazon_pack > 1 and idx > 0 else "package"
                for source in web_sources:
                    if source not in sources:
                        sources.append(source)
                break

    entry = dict(cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": 4,
        "name_checked_at": datetime.now(base.ROME).isoformat(),
        "name_query": full_query,
        "name_unit_query": single_query,
        "name_identifiers": accepted,
        "name_identifier_modes": modes,
        "name_sources": sources,
    })
    if accepted:
        entry["identifiers"] = list(dict.fromkeys(list(entry.get("identifiers") or []) + accepted))
        if hint and not entry.get("title"):
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info(
        "Name->EAN v4 ASIN=%s full=%r unit=%r accepted=%s modes=%s sources=%s",
        asin, full_query, single_query, accepted, modes, sources,
    )
    return accepted, modes, sources


v6.name_identifiers = name_identifiers
v7.name_identifiers = name_identifiers
v8.name_identifiers = name_identifiers
v2.resolve_offer = v6.resolve_offer
base.resolve_offer = v6.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
