'use strict'

import asyncio
import html as html_lib
import re
from datetime import datetime
from urllib.parse import urlparse

import app_v12 as v12

v11 = v12.v11
v10 = v12.v10
v9 = v12.v9
v8 = v12.v8
v7 = v12.v7
v6 = v12.v6
v5 = v12.v5
v4 = v12.v4
v3 = v12.v3
v2 = v12.v2
base = v12.base

RESOLVER_VERSION = 8


def _page_title(raw: str) -> str:
    parts: list[str] = []
    for pattern in (
        r"<title[^>]*>(.*?)</title>",
        r"<h1[^>]*>(.*?)</h1>",
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
        r"<meta[^>]+name=[\"']twitter:title[\"'][^>]+content=[\"']([^\"']+)",
    ):
        for match in re.findall(pattern, raw or "", flags=re.I | re.S):
            clean = v2._clean_text(match)
            if clean and clean not in parts:
                parts.append(clean)
            if len(parts) >= 4:
                break
    return " | ".join(parts[:4])


def _explicit_gtins(raw: str) -> list[str]:
    """Extract GTINs explicitly labelled as EAN/GTIN/barcode anywhere on a product page."""
    text = html_lib.unescape(raw or "").replace("\\/", "/")
    found: list[str] = []

    patterns = (
        r'(?i)(?:ean|gtin(?:-?8|-?12|-?13|-?14)?|barcode|codice\s+(?:a\s+)?barre)[^0-9]{0,120}(\d{8}|\d{12,14})',
        r'(?i)(\d{8}|\d{12,14})[^A-Za-z0-9]{0,80}(?:ean|gtin|barcode|codice\s+(?:a\s+)?barre)',
        r'(?i)[\"\']gtin(?:8|12|13|14)?[\"\']\s*:\s*[\"\']?(\d{8}|\d{12,14})',
    )
    for pattern in patterns:
        for raw_code in re.findall(pattern, text):
            code = base.normalize_gtin(raw_code)
            if not base.valid_gtin(code):
                continue
            for candidate in base.ean_candidates(code):
                if candidate not in found:
                    found.append(candidate)

    # Reuse the legacy marker-aware extractor as another source.
    for code in v2._marked_gtins(text):
        if code not in found:
            found.append(code)
    return found


def _candidate_urls(raw: str) -> list[str]:
    hrefs = re.findall(r'href=[\"\']([^\"\']+)[\"\']', raw or "", flags=re.I)
    hrefs += re.findall(r"<link>(https?://[^<]+)</link>", raw or "", flags=re.I)
    out: list[str] = []
    for href in hrefs:
        url = v10.normalize_result_link(href)
        if not url:
            url = v3._normalize_link(href)
        if not url or not url.startswith("http"):
            continue
        host = (urlparse(url).hostname or "").lower()
        if any(
            bad in host
            for bad in (
                "google.", "bing.com", "duckduckgo.com", "yahoo.com",
                "gstatic.com", "microsoft.com", "facebook.com", "instagram.com",
                "tiktok.com", "youtube.com",
            )
        ):
            continue
        if url not in out:
            out.append(url)
        if len(out) >= 24:
            break
    return out


async def _search_web(query: str, asin: str = "") -> tuple[list[str], list[tuple[str, str]]]:
    """Search like a user would: ASIN first, then extended product name, then retailer search."""
    q = re.sub(r"\s+", " ", query or "").strip()
    search_queries: list[str] = []
    if asin:
        search_queries += [f'"{asin}" EAN', f'"{asin}" GTIN']
    if q:
        search_queries += [
            f'"{q}" EAN',
            f'{q} EAN',
            f'"{q}" GTIN',
            f'{q} "codice a barre"',
        ]
    # Deduplicate while preserving order.
    search_queries = list(dict.fromkeys(search_queries))[:6]

    tasks = []
    for sq in search_queries:
        tasks += [
            ("google", "https://www.google.com/search", {"q": sq, "num": "10", "hl": "it"}),
            ("bing", "https://www.bing.com/search", {"q": sq, "format": "rss", "setlang": "it-IT"}),
            ("duckduckgo", "https://html.duckduckgo.com/html/", {"q": sq}),
        ]
    if q:
        # Direct retailer search is useful when public search engines throttle GitHub runners.
        tasks += [
            ("ebay", "https://www.ebay.it/sch/i.html", {"_nkw": q}),
        ]

    async def one(provider: str, endpoint: str, params: dict):
        try:
            r = await base.http.get(endpoint, params=params, headers=v2.SEARCH_HEADERS, timeout=10)
            if not r.is_success:
                return provider, ""
            return provider, r.text[:900000]
        except Exception:
            return provider, ""

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(one(provider, endpoint, params) for provider, endpoint, params in tasks)),
            timeout=18,
        )
    except asyncio.TimeoutError:
        results = []

    urls: list[str] = []
    bodies: list[tuple[str, str]] = []
    for provider, body in results:
        if not body:
            continue
        bodies.append((provider, body))
        for url in _candidate_urls(body):
            if url not in urls:
                urls.append(url)
            if len(urls) >= 24:
                break
    return urls[:24], bodies


async def broad_page_evidence(query: str, state: dict, asin: str = "") -> tuple[list[str], list[str]]:
    urls, search_bodies = await _search_web(query, asin)
    evidence: dict[str, set[str]] = {}
    best_score: dict[str, float] = {}

    # Search snippets can already expose an EAN.
    for provider, body in search_bodies:
        for code, score in v9._valid_db_codes_from_text(body, query, state):
            evidence.setdefault(code, set()).add(provider)
            best_score[code] = max(best_score.get(code, 0.0), score)

    semaphore = asyncio.Semaphore(8)

    async def one(url: str):
        async with semaphore:
            try:
                r = await base.http.get(url, headers=v2.SEARCH_HEADERS, timeout=10)
                if not r.is_success:
                    return "", [], 0.0, False
                ctype = (r.headers.get("content-type") or "").lower()
                if not any(x in ctype for x in ("text", "html", "json", "xml")):
                    return "", [], 0.0, False
                body = r.text[:1000000]
                host = (urlparse(str(r.url)).hostname or urlparse(url).hostname or "").lower()
                title = _page_title(body)
                score = max(v6._title_score(query, title), v9._context_score(query, title))
                has_asin = bool(asin and asin.casefold() in body.casefold())

                codes = {code for code, _ in v9._valid_db_codes_from_text(body, query, state)}
                if has_asin or score >= 6.0:
                    for code in _explicit_gtins(body):
                        if base.vv.first_match(state, [code]):
                            codes.add(code)
                return host, sorted(codes), score, has_asin
            except Exception:
                return "", [], 0.0, False

    try:
        pages = await asyncio.wait_for(
            asyncio.gather(*(one(url) for url in urls[:18])),
            timeout=24,
        ) if urls else []
    except asyncio.TimeoutError:
        pages = []

    for host, codes, score, has_asin in pages:
        if not host:
            continue
        for code in codes:
            evidence.setdefault(code, set()).add(host)
            strong = 12.0 if has_asin else score
            best_score[code] = max(best_score.get(code, 0.0), strong)

    accepted = [
        code
        for code, providers in evidence.items()
        if best_score.get(code, 0.0) >= 7.0 or len(providers) >= 2
    ]
    accepted.sort(
        key=lambda code: (best_score.get(code, 0.0), len(evidence.get(code, set()))),
        reverse=True,
    )
    sources = sorted({source for code in accepted for source in evidence.get(code, set())})
    base.LOG.info(
        "Broad-name->EAN query=%r asin=%s urls=%d accepted=%s sources=%s",
        query, asin, len(urls), accepted, sources[:5],
    )
    return accepted, sources


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    full_query = v8.catalog_query(hint)
    single_query = v8.unit_query(hint)
    amazon_pack = v2.pack_count(hint)
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    cached_query = str(cached.get("name_query") or "").strip().casefold()

    if (
        version >= RESOLVER_VERSION
        and cached.get("name_lookup_done")
        and cached_query
        and cached_query == str(full_query or "").strip().casefold()
    ):
        return (
            list(cached.get("name_identifiers") or []),
            dict(cached.get("name_identifier_modes") or {}),
            list(cached.get("name_sources") or []),
        )

    accepted: list[str] = []
    modes: dict[str, str] = {}
    sources: list[str] = []
    pack_units: dict[str, int] = {}

    # 1) Fast structured catalogue.
    plan: list[tuple[str, str]] = []
    if amazon_pack > 1 and single_query:
        plan.append((single_query, "unit"))
    if full_query:
        plan.append((full_query, "package"))
    for query, mode in plan:
        codes, provider_sources = await v12.dm_name_identifiers(query, state)
        if codes:
            for code in codes:
                if code not in accepted:
                    accepted.append(code)
                    modes[code] = mode
                    if mode == "unit" and amazon_pack > 1:
                        pack_units[code] = amazon_pack
            sources.extend(x for x in provider_sources if x not in sources)
            break

    # 2) Broad Internet lookup. This is intentionally the main fallback now:
    # extended name/ASIN -> explicit EAN on an external product page -> exact DB EAN.
    if not accepted:
        for query, mode in plan:
            codes, page_sources = await broad_page_evidence(query, state, asin=asin)
            if codes:
                for code in codes:
                    if code not in accepted:
                        accepted.append(code)
                        modes[code] = mode
                        if mode == "unit" and amazon_pack > 1:
                            pack_units[code] = amazon_pack
                sources.extend(x for x in page_sources if x not in sources)
                break

    # 3) Laundry offers sometimes expose only an aggregate wash count (123 = 3x41).
    if not accepted and amazon_pack == 1 and full_query:
        inferred_hits: list[tuple[str, int, list[str]]] = []
        for unit_candidate, factor in v12._wash_bundle_queries(full_query):
            codes, page_sources = await broad_page_evidence(unit_candidate, state, asin="")
            for code in codes:
                inferred_hits.append((code, factor, page_sources))
        unique = {(code, factor) for code, factor, _ in inferred_hits}
        factors = {factor for _, factor in unique}
        if unique and len(factors) == 1:
            chosen_factor = next(iter(factors))
            for code, factor, page_sources in inferred_hits:
                if factor != chosen_factor or code in accepted:
                    continue
                accepted.append(code)
                modes[code] = "unit"
                pack_units[code] = factor
                sources.extend(x for x in page_sources if x not in sources)

    # 4) Keep the older providers as a final fallback.
    if not accepted:
        old_codes, old_modes, old_sources = await v12._original_name_identifiers(
            asin, hint, state, cache
        )
        accepted = list(old_codes)
        modes = dict(old_modes)
        sources = list(old_sources)
        if amazon_pack > 1:
            for code, mode in modes.items():
                if mode == "unit":
                    pack_units[code] = amazon_pack

    entry = dict(cache.get(asin) if isinstance(cache.get(asin), dict) else cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": RESOLVER_VERSION,
        "name_checked_at": datetime.now(base.ROME).isoformat(),
        "name_query": full_query,
        "name_unit_query": single_query,
        "name_identifiers": accepted,
        "name_identifier_modes": modes,
        "name_pack_units": pack_units,
        "name_sources": sources,
    })
    if accepted:
        entry["identifiers"] = list(
            dict.fromkeys(list(entry.get("identifiers") or []) + accepted)
        )
        if hint:
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info(
        "Name->EAN v8 ASIN=%s query=%r accepted=%s modes=%s pack_units=%s sources=%s",
        asin, full_query, accepted, modes, pack_units, sources[:5],
    )
    return accepted, modes, sources


# app_v11.resolve_offer calls v10.name_identifiers dynamically.
v10.name_identifiers = name_identifiers
v2.resolve_offer = v11.resolve_offer
base.resolve_offer = v11.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
