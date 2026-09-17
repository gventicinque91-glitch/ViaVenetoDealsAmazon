'use strict'

import asyncio
import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import app_v9 as v9

v8 = v9.v8
v7 = v9.v7
v6 = v9.v6
v5 = v9.v5
v4 = v9.v4
v3 = v9.v3
v2 = v9.v2
base = v9.base


def query_variants(query: str) -> list[str]:
    value = re.sub(r"\s+", " ", query or "").strip(" -,:;")
    if not value:
        return []
    out = [value]
    plain = re.sub(r"[-–—,:;]+", " ", value)
    plain = re.sub(r"\s+", " ", plain).strip()
    if plain and plain not in out:
        out.append(plain)
    # Generic catalogue synonym: product titles frequently alternate between
    # "anticarie" and "protezione carie". This is used only for web discovery.
    anti = re.sub(r"\banticarie\b", "protezione carie", plain, flags=re.I)
    if anti and anti.casefold() not in {x.casefold() for x in out}:
        out.append(anti)
    return out[:3]


def normalize_result_link(href: str) -> str:
    href = (href or "").replace("&amp;", "&").strip()
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/url?"):
        q = parse_qs(urlparse(href).query).get("q") or parse_qs(urlparse(href).query).get("url")
        return q[0] if q else ""
    if href.startswith("/l/?"):
        href = "https://duckduckgo.com" + href
    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()
    if "duckduckgo.com" in host:
        uddg = parse_qs(parsed.query).get("uddg")
        return unquote(uddg[0]) if uddg else ""
    if "google." in host and parsed.path == "/url":
        q = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
        return q[0] if q else ""
    return href if href.startswith("http") else ""


async def search_result_urls(query: str) -> list[str]:
    endpoints = (
        ("https://www.google.com/search", {"q": f'"{query}" EAN', "num": "10", "hl": "it"}),
        ("https://www.bing.com/search", {"q": f'"{query}" EAN', "format": "rss", "setlang": "it-IT"}),
        ("https://html.duckduckgo.com/html/", {"q": f'"{query}" EAN'}),
    )

    async def one(endpoint: str, params: dict):
        try:
            r = await base.http.get(endpoint, params=params, headers=v2.SEARCH_HEADERS, timeout=10)
            if not r.is_success:
                return []
            body = r.text
            hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", body, re.I)
            hrefs += re.findall(r"<link>(https?://[^<]+)</link>", body, re.I)
            urls: list[str] = []
            for href in hrefs:
                url = normalize_result_link(href)
                if not url:
                    continue
                host = (urlparse(url).hostname or "").lower()
                if any(x in host for x in ("google.", "bing.com", "microsoft.com", "duckduckgo.com", "gstatic.com")):
                    continue
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 10:
                    break
            return urls
        except Exception:
            return []

    results = await asyncio.gather(*(one(endpoint, params) for endpoint, params in endpoints))
    out: list[str] = []
    for urls in results:
        for url in urls:
            if url not in out:
                out.append(url)
            if len(out) >= 18:
                return out
    return out


async def page_evidence(query: str, state: dict) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    for variant in query_variants(query):
        for url in await search_result_urls(variant):
            if url not in urls:
                urls.append(url)
        if len(urls) >= 18:
            break

    semaphore = asyncio.Semaphore(6)

    async def one(url: str):
        async with semaphore:
            try:
                r = await base.http.get(url, headers=v2.SEARCH_HEADERS, timeout=10)
                if not r.is_success:
                    return "", []
                ctype = (r.headers.get("content-type") or "").lower()
                if not any(x in ctype for x in ("text", "html", "json", "xml")):
                    return "", []
                host = (urlparse(str(r.url)).hostname or urlparse(url).hostname or "").lower()
                body = r.text[:900000]
                # Evaluate the EAN only against the external page context, never the DB description.
                found = v9._valid_db_codes_from_text(body, query, state)
                return host, found
            except Exception:
                return "", []

    try:
        fetched = await asyncio.wait_for(asyncio.gather(*(one(url) for url in urls[:18])), timeout=20)
    except asyncio.TimeoutError:
        fetched = []

    evidence: dict[str, set[str]] = {}
    best_score: dict[str, float] = {}
    for host, pairs in fetched:
        if not host:
            continue
        for code, score in pairs:
            evidence.setdefault(code, set()).add(host)
            best_score[code] = max(best_score.get(code, 0.0), score)

    accepted = [
        code for code, hosts in evidence.items()
        if best_score.get(code, 0.0) >= 8.0 or len(hosts) >= 2
    ]
    accepted.sort(key=lambda c: (best_score.get(c, 0.0), len(evidence.get(c, set()))), reverse=True)
    sources = sorted({host for code in accepted for host in evidence.get(code, set())})
    base.LOG.info("Page-name->EAN query=%r urls=%d accepted=%s sources=%s", query, len(urls), accepted, sources[:4])
    return accepted, sources


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    if version >= 5 and cached.get("name_lookup_done"):
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

    # 1) Structured public barcode catalogue.
    for query, mode in ((full_query, "package"), (single_query, "unit" if amazon_pack > 1 else "package")):
        if not query:
            continue
        if query == single_query and query == full_query:
            continue
        items = await v6._upc_search(query)
        scored = v7._score_items(items, query, hint, state, forced_mode=mode) if items else []
        if scored:
            sources.append("UPCitemdb")
            for _, code, resolved_mode, _ in sorted(scored, key=lambda x: x[0], reverse=True):
                if code not in accepted:
                    accepted.append(code)
                    modes[code] = resolved_mode
            break

    # 2) Follow actual product pages returned by public search providers. For multipacks,
    # prefer the normalized single-unit title because Via Veneto stores the retail unit EAN.
    if not accepted:
        search_plan = []
        if amazon_pack > 1 and single_query:
            search_plan.append((single_query, "unit"))
        if full_query and full_query.casefold() != (single_query or "").casefold():
            search_plan.append((full_query, "package"))
        for query, mode in search_plan:
            codes, page_sources = await page_evidence(query, state)
            if codes:
                for code in codes:
                    if code not in accepted:
                        accepted.append(code)
                        modes[code] = mode
                for source in page_sources:
                    if source not in sources:
                        sources.append(source)
                break

    # 3) Search-result snippets as final safe fallback.
    if not accepted:
        search_plan = [(single_query, "unit" if amazon_pack > 1 else "package"), (full_query, "package")]
        for query, mode in search_plan:
            if not query:
                continue
            codes, snippet_sources = await v9.search_engine_eans(query, state)
            if codes:
                for code in codes:
                    if code not in accepted:
                        accepted.append(code)
                        modes[code] = mode
                for source in snippet_sources:
                    if source not in sources:
                        sources.append(source)
                break

    entry = dict(cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": 5,
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
    base.LOG.info("Name->EAN v5 ASIN=%s accepted=%s modes=%s sources=%s", asin, accepted, modes, sources[:4])
    return accepted, modes, sources


v6.name_identifiers = name_identifiers
v7.name_identifiers = name_identifiers
v8.name_identifiers = name_identifiers
v9.name_identifiers = name_identifiers
v2.resolve_offer = v6.resolve_offer
base.resolve_offer = v6.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
