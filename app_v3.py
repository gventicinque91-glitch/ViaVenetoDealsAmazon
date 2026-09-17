'use strict'

import asyncio
import html as html_lib
import re
from collections import defaultdict
from datetime import datetime, time
from urllib.parse import parse_qs, unquote, urlparse

import app_v2 as v2

base = v2.base

STOPWORDS = {
    "della", "delle", "dello", "dalla", "dalla", "degli", "dell", "dopo", "senza", "pezzi",
    "formula", "pelle", "uomo", "donna", "con", "per", "una", "uno", "gli", "che", "ml",
}


async def find_source_chats() -> list[tuple[str, object]]:
    assert base.user_client is not None
    dialogs: list[tuple[str, object]] = []
    async for dialog in base.user_client.iter_dialogs():
        name = str(dialog.name or "").strip()
        if name:
            dialogs.append((name, dialog.entity))

    found: list[tuple[str, object]] = []
    used_ids: set[str] = set()
    missing: list[str] = []
    for wanted in v2.SOURCE_CHAT_TITLES:
        w = wanted.casefold().strip()
        exact = [d for d in dialogs if d[0].casefold() == w]
        partial = [d for d in dialogs if w in d[0].casefold() or (d[0].casefold() in w and len(d[0]) >= 5)]
        candidates = exact or sorted(partial, key=lambda x: (abs(len(x[0]) - len(wanted)), len(x[0])))
        if not candidates:
            missing.append(wanted)
            continue
        name, entity = candidates[0]
        entity_id = str(getattr(entity, "id", id(entity)))
        if entity_id not in used_ids:
            found.append((name, entity))
            used_ids.add(entity_id)

    if not found:
        raise RuntimeError("Nessuna fonte Telegram trovata: " + ", ".join(v2.SOURCE_CHAT_TITLES))
    if missing:
        base.LOG.warning("Fonti Telegram non trovate: %s", ", ".join(missing))
    return found


async def find_source_chat():
    return (await find_source_chats())[0][1]


async def day_messages(until: datetime) -> list[object]:
    assert base.user_client is not None
    start = datetime.combine(until.date(), time.min, tzinfo=base.ROME)
    messages: list[object] = []
    seen: set[tuple[str, int]] = set()
    for _, entity in await find_source_chats():
        entity_id = str(getattr(entity, "id", ""))
        async for message in base.user_client.iter_messages(entity):
            if not message.date:
                continue
            local_dt = message.date.astimezone(base.ROME)
            if local_dt < start:
                break
            if local_dt > until:
                continue
            key = (entity_id, int(getattr(message, "id", 0) or 0))
            if key not in seen:
                messages.append(message)
                seen.add(key)
    messages.sort(key=lambda m: m.date or datetime.min.replace(tzinfo=base.ROME))
    return messages


def _normalize_link(href: str) -> str:
    href = html_lib.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/url?"):
        q = parse_qs(urlparse(href).query).get("q")
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


async def _search_urls(query: str) -> list[str]:
    urls: list[str] = []
    endpoints = (
        ("https://www.google.com/search", {"q": query, "num": "10", "hl": "it"}),
        ("https://www.bing.com/search", {"q": query, "format": "rss", "setlang": "it-IT"}),
        ("https://html.duckduckgo.com/html/", {"q": query}),
    )
    for endpoint, params in endpoints:
        try:
            r = await base.http.get(endpoint, params=params, headers=v2.SEARCH_HEADERS, timeout=12)
            if not r.is_success:
                continue
            hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", r.text, re.I)
            hrefs += re.findall(r"<link>(https?://[^<]+)</link>", r.text, re.I)
            for href in hrefs:
                url = _normalize_link(href)
                if not url:
                    continue
                host = (urlparse(url).hostname or "").lower()
                if any(x in host for x in ("google.", "bing.com", "microsoft.com", "duckduckgo.com", "gstatic.com")):
                    continue
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 16:
                    return urls
        except Exception as exc:
            base.LOG.debug("Search provider failed %s: %s", endpoint, exc)
    return urls


def _hint_variants(hint: str) -> list[str]:
    hint = re.sub(r"\s+", " ", hint or "").strip()
    if not hint:
        return []
    first = hint.split(",", 1)[0].strip()
    single = re.sub(r"\b\d{1,2}\s*(?:pezzi|pz|flaconi|bottiglie|confezioni)\s+da\s+", "", hint, flags=re.I)
    size = ""
    sizes = re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg)\b", hint, flags=re.I)
    if sizes:
        size = sizes[-1]
    compact = (first + (" " + size if size and size.casefold() not in first.casefold() else "")).strip()
    out = []
    for value in (compact, single[:170], hint[:170]):
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in out:
            out.append(value)
    return out


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-zà-ÿ0-9]+", (text or "").casefold())
        if len(t) >= 4 and t not in STOPWORDS
    }


async def _fetch_evidence(url: str, asin: str, hint_tokens: set[str]):
    try:
        r = await base.http.get(url, headers=v2.SEARCH_HEADERS, timeout=10)
        if not r.is_success:
            return "", [], False, 0
        ctype = (r.headers.get("content-type") or "").lower()
        if not any(x in ctype for x in ("text", "html", "json", "xml")):
            return "", [], False, 0
        body = r.text[:700000]
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

    variants = _hint_variants(hint)
    queries = [f'"{asin}" EAN GTIN barcode']
    for variant in variants[:2]:
        queries.append(f'"{variant}" EAN GTIN')
        queries.append(f'"{variant}" "codice a barre"')

    urls: list[str] = []
    for query in queries:
        for url in await _search_urls(query):
            if url not in urls:
                urls.append(url)
        if len(urls) >= 16:
            break

    hint_tokens = _tokens(variants[0] if variants else hint)
    semaphore = asyncio.Semaphore(5)

    async def one(url: str):
        async with semaphore:
            return await _fetch_evidence(url, asin, hint_tokens)

    evidence: dict[str, set[str]] = defaultdict(set)
    direct: set[str] = set()
    overlap_score: dict[str, int] = defaultdict(int)
    results = await asyncio.gather(*(one(u) for u in urls[:16])) if urls else []
    for host, codes, has_asin, overlap in results:
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
    sources = sorted({host for code in accepted for host in evidence.get(code, set())})

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
    base.LOG.info("Web resolver ASIN=%s urls=%s accepted=%s sources=%s", asin, len(urls), accepted, sources[:3])
    return accepted, sources


# Sovrascrive le funzioni v2 usate dinamicamente da resolve_offer.
v2.find_source_chats = find_source_chats
v2.find_source_chat = find_source_chat
v2.day_messages = day_messages
v2.web_identifiers = web_identifiers
base.find_source_chat = find_source_chat
base.day_messages = day_messages


if __name__ == "__main__":
    asyncio.run(base.main())
