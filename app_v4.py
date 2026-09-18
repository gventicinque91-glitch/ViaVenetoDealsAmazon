'use strict'

import asyncio
import html as html_lib
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import app_v3 as v3

v2 = v3.v2
base = v3.base


async def _provider_search(endpoint: str, params: dict) -> tuple[list[str], str]:
    try:
        r = await base.http.get(endpoint, params=params, headers=v2.SEARCH_HEADERS, timeout=6)
        if not r.is_success:
            return [], ""
        hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", r.text, re.I)
        hrefs += re.findall(r"<link>(https?://[^<]+)</link>", r.text, re.I)
        urls: list[str] = []
        for href in hrefs:
            url = v3._normalize_link(href)
            if not url:
                continue
            host = (urlparse(url).hostname or "").lower()
            if any(x in host for x in ("google.", "bing.com", "microsoft.com", "duckduckgo.com", "gstatic.com")):
                continue
            if url not in urls:
                urls.append(url)
            if len(urls) >= 8:
                break
        return urls, r.text[:500000]
    except Exception:
        return [], ""


async def fast_search_urls(query: str) -> tuple[list[str], list[str]]:
    providers = (
        ("https://www.google.com/search", {"q": query, "num": "10", "hl": "it"}),
        ("https://www.bing.com/search", {"q": query, "format": "rss", "setlang": "it-IT"}),
        ("https://html.duckduckgo.com/html/", {"q": query}),
    )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_provider_search(endpoint, params) for endpoint, params in providers)),
            timeout=8,
        )
    except asyncio.TimeoutError:
        return [], []
    urls: list[str] = []
    pages: list[str] = []
    for provider_urls, body in results:
        for url in provider_urls:
            if url not in urls:
                urls.append(url)
        if body:
            pages.append(body)
    return urls[:10], pages


async def _fetch_evidence_fast(url: str, asin: str, hint_tokens: set[str]):
    try:
        r = await base.http.get(url, headers=v2.SEARCH_HEADERS, timeout=6)
        if not r.is_success:
            return "", [], False, 0
        ctype = (r.headers.get("content-type") or "").lower()
        if not any(x in ctype for x in ("text", "html", "json", "xml")):
            return "", [], False, 0
        body = r.text[:500000]
        clean = v2._clean_text(body)
        codes = v2._marked_gtins(body)
        low = clean.casefold()
        has_asin = asin.casefold() in low
        overlap = sum(1 for token in hint_tokens if token in low)
        host = (urlparse(str(r.url)).hostname or urlparse(url).hostname or "").lower()
        return host, codes, has_asin, overlap
    except Exception:
        return "", [], False, 0


async def web_identifiers(asin: str, hint: str, state: dict, cache: dict):
    if not v2.WEB_GTIN_FALLBACK:
        return [], []
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    if cached.get("web_verified") and cached.get("web_identifiers"):
        return list(cached.get("web_identifiers") or []), list(cached.get("web_sources") or [])

    variants = v3._hint_variants(hint)
    compact = variants[0] if variants else hint
    queries = [f'"{asin}" EAN GTIN', f'"{compact}" EAN GTIN']

    urls: list[str] = []
    search_pages: list[str] = []
    search_results = await asyncio.gather(*(fast_search_urls(q) for q in queries))
    for found_urls, bodies in search_results:
        for url in found_urls:
            if url not in urls:
                urls.append(url)
        search_pages.extend(bodies)

    hint_tokens = v3._tokens(compact)
    evidence: dict[str, set[str]] = defaultdict(set)
    direct: set[str] = set()
    overlap_score: dict[str, int] = defaultdict(int)

    # Anche lo snippet del motore può contenere un EAN/GTIN esplicito.
    for idx, body in enumerate(search_pages):
        clean = v2._clean_text(body)
        low = clean.casefold()
        overlap = sum(1 for token in hint_tokens if token in low)
        for code in v2._marked_gtins(body):
            if base.vv.first_match(state, [code]):
                evidence[code].add(f"search-{idx}")
                overlap_score[code] = max(overlap_score[code], overlap)
                if asin.casefold() in low:
                    direct.add(code)

    semaphore = asyncio.Semaphore(8)

    async def one(url: str):
        async with semaphore:
            return await _fetch_evidence_fast(url, asin, hint_tokens)

    try:
        fetched = await asyncio.wait_for(
            asyncio.gather(*(one(u) for u in urls[:10])),
            timeout=12,
        ) if urls else []
    except asyncio.TimeoutError:
        fetched = []

    for host, codes, has_asin, overlap in fetched:
        if not host:
            continue
        for code in codes:
            if not base.vv.first_match(state, [code]):
                continue
            evidence[code].add(host)
            overlap_score[code] = max(overlap_score[code], overlap)
            if has_asin:
                direct.add(code)

    accepted = [
        code for code, hosts in evidence.items()
        if code in direct or len(hosts) >= 2 or overlap_score.get(code, 0) >= 3
    ]
    sources = sorted({host for code in accepted for host in evidence.get(code, set()) if not host.startswith("search-")})

    entry = dict(cached)
    entry["web_checked_at"] = datetime.now(base.ROME).isoformat()
    entry["web_identifiers"] = accepted
    entry["web_sources"] = sources
    entry["web_verified"] = bool(accepted)
    if accepted:
        entry["identifiers"] = list(dict.fromkeys(list(entry.get("identifiers") or []) + accepted))
        if hint and not entry.get("title"):
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info("Fast web resolver ASIN=%s urls=%s accepted=%s sources=%s", asin, len(urls), accepted, sources[:3])
    return accepted, sources


async def build_report(until: datetime | None = None) -> str:
    async with base.report_lock:
        until = (until or datetime.now(base.ROME)).astimezone(base.ROME)
        state = await base.vv.get_state()
        messages = await v3.day_messages(until)
        cache = await base.cache_store.load({})
        aliases = base.load_aliases()

        candidates: list[tuple[object, str]] = []
        for message in messages:
            for url in v2.extract_urls(message):
                candidates.append((message, url))

        semaphore = asyncio.Semaphore(5)

        async def one(message, url):
            async with semaphore:
                try:
                    return await v2.resolve_offer(message, url, state, cache, aliases)
                except Exception as exc:
                    base.LOG.warning("Offerta non processata %s: %s", url, exc)
                    return None

        results = await asyncio.gather(*(one(message, url) for message, url in candidates)) if candidates else []

        no_price = 0
        unresolved: dict[str, dict] = {}
        best_by_asin: dict[str, dict] = {}
        relevant = 0
        for result in results:
            if not result:
                continue
            relevant += 1
            asin = result.get("asin") or ""
            if result.get("status") == "no_price":
                no_price += 1
                continue
            if result.get("status") == "unresolved":
                unresolved[asin] = result
                continue
            previous = best_by_asin.get(asin)
            if not previous or float(result["amazon_unit"]) < float(previous["amazon_unit"]):
                best_by_asin[asin] = result

        matched = list(best_by_asin.values())
        deals = [x for x in matched if float(x.get("savings") or 0) > 0]
        deals.sort(key=lambda x: float(x.get("savings_pct") or 0), reverse=True)

        lines = [
            "🛒 VIA VENETO · AMAZON DEALS",
            f"📅 {until.strftime('%d/%m/%Y')} · aggiornato alle {until.strftime('%H:%M')}",
            f"📣 Fonti: {' + '.join(v2.SOURCE_CHAT_TITLES)}",
            "",
            f"Messaggi letti: {len(messages)}",
            f"Link Amazon totali: {len(candidates)}",
            f"Offerte detergenza/igiene analizzate: {relevant}",
            f"Prodotti Via Veneto riconosciuti: {len(matched)}",
            f"Amazon più conveniente: {len(deals)}",
            f"Non identificati dopo ricerca EAN web: {len(unresolved)}",
        ]

        if deals:
            lines += ["", "🔥 OFFERTE CONVENIENTI"]
            for item in deals[:25]:
                via = item["via"]
                description = via.get("description") or item.get("title") or item["asin"]
                units = int(item.get("units") or 1)
                ref = float(via["reference_price"])
                unit = float(item["amazon_unit"])
                lines += [
                    "",
                    f"• {description}",
                    f"EAN: {item['ean']} · ASIN: {item['asin']}",
                    (f"Amazon: €{item['amazon_total']:.2f} confezione / {units} = €{unit:.2f} per pezzo" if units > 1 else f"Amazon: €{unit:.2f}"),
                    f"Via Veneto ({via['reference_label']}): €{ref:.2f}" + (f" · {via.get('purchase_date') or via.get('list_date')}" if (via.get('purchase_date') or via.get('list_date')) else ""),
                    f"Risparmio: €{item['savings']:.2f} · {item['savings_pct']:.1f}%" + (" · ⚠️ prezzo condizionato" if item.get("conditional") else ""),
                    item["url"],
                ]
        else:
            lines += ["", "Nessuna offerta Amazon identificata con prezzo inferiore al riferimento Via Veneto."]

        if unresolved:
            lines += ["", f"❓ Ancora senza EAN verificato: {len(unresolved)} prodotti."]
            for item in list(unresolved.values())[:5]:
                lines.append(f"• {item.get('title') or item.get('asin')} · {item.get('asin')}")
        if no_price:
            lines.append(f"⚠️ {no_price} offerte ignorate perché il prezzo non era ricavabile.")
        return "\n".join(lines)


v2.web_identifiers = web_identifiers
base.build_report = build_report


if __name__ == "__main__":
    asyncio.run(base.main())
