'use strict'

import asyncio
import html as html_lib
import os
import re
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityTextUrl

import app as base
from telegram import Update
from telegram.ext import ContextTypes


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


SOURCE_CHAT_TITLES = [
    x.strip()
    for x in env("SOURCE_CHAT_TITLES", "Caccia allo SCONTO 🎯|SCONTALO|Coupons Italia|Offerte Flash|PROBABILI ERRORI|HomeZone").split("|")
    if x.strip()
]
base.SOURCE_CHAT_TITLE = " + ".join(SOURCE_CHAT_TITLES)
base.ALIASES_FILE = Path(env("ALIASES_FILE", "verified_aliases.json"))
WEB_GTIN_FALLBACK = env("WEB_GTIN_FALLBACK", "true").lower() not in {"0", "false", "no"}

CATEGORY_KEYWORDS = (
    "detersiv", "lavatrice", "lavastoviglie", "ammorbidente", "smacchiatore", "igienizzante",
    "sgrassatore", "candeggina", "pulitore", "pavimenti", "wc", "detergente", "sapone",
    "bagnoschiuma", "bagno schiuma", "bagno crema", "bagnodoccia", "docciaschiuma", "gel doccia", "shower gel", "shampoo", "balsamo", "deodorante",
    "antitraspirante", "dentifricio", "collutorio", "spazzolino", "crema corpo", "crema mani",
    "rasoio", "rasatura", "depil", "assorbent", "igiene intima", "salviett", "pannolin",
    "micellare", "struccante", "gel doccia", "body wash", "shower gel", "cura persona",
)

PACK_PATTERNS = (
    re.compile(r"^\s*(\d{1,2})\s*[x×]\s*(?=[A-Za-zÀ-ÿ])", re.I),
    re.compile(r"\b(\d{1,2})\s*(?:pezzi|pz|flaconi|bottiglie|confezioni)\s+da\b", re.I),
    re.compile(r"\bconfezione\s+da\s+(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\s*[x×]\s*\d", re.I),
)

GTIN_MARKERS = ("ean", "gtin", "barcode", "codice a barre", "codice barre")
SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
}

_ORIGINAL_EXTRACT_URLS = base.extract_urls
_ORIGINAL_CANONICAL_AMAZON_URL = base.canonical_amazon_url


def relevant_offer(text: str) -> bool:
    low = (text or "").casefold()
    return any(keyword in low for keyword in CATEGORY_KEYWORDS)


def pack_count(text: str) -> int:
    for pattern in PACK_PATTERNS:
        m = pattern.search(text or "")
        if m:
            try:
                n = int(m.group(1))
                if 1 < n <= 48:
                    return n
            except Exception:
                pass
    return 1


def offer_context(text: str, asin: str, url: str) -> str:
    text = text or ""
    pos = text.upper().find(asin.upper()) if asin else -1
    if pos < 0:
        pos = text.find(url)
    if pos < 0:
        return text

    start = text.rfind("📌", 0, pos)
    if start < 0:
        start = max(0, text.rfind("\n\n", 0, pos))
    next_pin = text.find("📌", pos + 1)
    next_sep = text.find("➖➖", pos + 1)
    ends = [x for x in (next_pin, next_sep) if x >= 0]
    end = min(ends) if ends else min(len(text), pos + 900)
    return text[start:end].strip() or text


def product_hint(segment: str) -> str:
    value = re.sub(r"https?://\S+", " ", segment or "")
    value = re.sub(r"\bB0[A-Z0-9]{8}\b", " ", value, flags=re.I)
    value = re.sub(r"[⭐✅❌🛒➡️ℹ️📌‼️🔥⚡]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    for marker in (
        " invece di ", " a soli ", " passa da ", " venduto ", " recensioni",
        " minimo storico", " apri link amazon", " apri su amazon", " guarda offerta",
        " #amazon", " €", " eur",
    ):
        idx = value.casefold().find(marker)
        if idx > 18:
            value = value[:idx]
    return value[:220]


def _entity_text(message, entity) -> str:
    try:
        text = add_surrogate(message.message or "")
        start = int(entity.offset)
        end = start + int(entity.length)
        return del_surrogate(text[start:end]).strip()
    except Exception:
        return ""


def _looks_like_offer_cta(label: str) -> bool:
    low = (label or "").casefold()
    if "amazon" in low:
        return True
    return any(token in low for token in ("apri", "guarda", "offerta", "vai al", "acquista"))


def extract_urls(message) -> list[str]:
    """Collect direct Amazon URLs plus affiliate/redirect links explicitly presented as offer CTAs.

    Several deal channels hide the Amazon destination behind a Telegram text-link such as
    "APRI SU AMAZON". The previous extractor discarded those URLs before they could be
    resolved, which meant relevant products were never even classified.
    """
    out = list(_ORIGINAL_EXTRACT_URLS(message))
    text = message.message or ""
    message_relevant = relevant_offer(text)

    for entity in message.entities or []:
        if not isinstance(entity, MessageEntityTextUrl) or not entity.url:
            continue
        try:
            host = (urlparse(entity.url).hostname or "").lower()
        except Exception:
            host = ""
        label = _entity_text(message, entity)
        if (
            base.AMAZON_HOST_RE.search(host)
            or "amazon" in label.casefold()
            or (message_relevant and _looks_like_offer_cta(label))
        ):
            if entity.url not in out:
                out.append(entity.url)

    try:
        for row in message.buttons or []:
            for button in row:
                url = getattr(button, "url", None)
                if not url:
                    continue
                try:
                    host = (urlparse(url).hostname or "").lower()
                except Exception:
                    host = ""
                label = str(getattr(button, "text", "") or "")
                if (
                    base.AMAZON_HOST_RE.search(host)
                    or "amazon" in label.casefold()
                    or (message_relevant and _looks_like_offer_cta(label))
                ):
                    if url not in out:
                        out.append(url)
    except Exception:
        pass
    return out


async def canonical_amazon_url(url: str) -> str:
    """Resolve direct, short and affiliate offer URLs to an Amazon product URL."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""

    if base.AMAZON_HOST_RE.search(host):
        return await _ORIGINAL_CANONICAL_AMAZON_URL(url)

    try:
        response = await base.http.get(url, headers=SEARCH_HEADERS, timeout=12)
        final_url = str(response.url)
        final_host = (urlparse(final_url).hostname or "").lower()
        if base.AMAZON_HOST_RE.search(final_host):
            return await _ORIGINAL_CANONICAL_AMAZON_URL(final_url)

        # Some affiliate gateways return an HTML interstitial instead of an HTTP redirect.
        body = html_lib.unescape(response.text[:500000]).replace("\\/", "/")
        for found in re.findall(r"https?://[^\\s\"'<>]+", body, flags=re.I):
            try:
                found_host = (urlparse(found).hostname or "").lower()
            except Exception:
                continue
            if base.AMAZON_HOST_RE.search(found_host) and base.asin_from_url(found):
                return await _ORIGINAL_CANONICAL_AMAZON_URL(found)
    except Exception as exc:
        base.LOG.debug("Affiliate Amazon URL non risolto %s: %s", url, exc)

    return url


async def find_source_chats() -> list[tuple[str, object]]:
    assert base.user_client is not None
    dialogs: list[tuple[str, object]] = []
    async for dialog in base.user_client.iter_dialogs():
        dialogs.append((str(dialog.name or ""), dialog.entity))

    found: list[tuple[str, object]] = []
    used_ids: set[str] = set()
    missing: list[str] = []
    for wanted in SOURCE_CHAT_TITLES:
        w = wanted.casefold()
        exact = [d for d in dialogs if d[0].casefold() == w]
        partial = [d for d in dialogs if w in d[0].casefold() or d[0].casefold() in w]
        candidates = exact or sorted(partial, key=lambda x: len(x[0]))
        if not candidates:
            missing.append(wanted)
            continue
        name, entity = candidates[0]
        entity_id = str(getattr(entity, "id", id(entity)))
        if entity_id not in used_ids:
            found.append((name, entity))
            used_ids.add(entity_id)

    if not found:
        raise RuntimeError("Nessuna fonte Telegram trovata: " + ", ".join(SOURCE_CHAT_TITLES))
    if missing:
        base.LOG.warning("Fonti Telegram non trovate: %s", ", ".join(missing))
    return found


async def find_source_chat():
    chats = await find_source_chats()
    return chats[0][1]


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


def _clean_text(raw: str) -> str:
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw or "", flags=re.I | re.S)
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_lib.unescape(raw)).strip()


def _marked_gtins(raw: str) -> list[str]:
    text = _clean_text(raw)
    found: list[str] = []
    for match in re.finditer(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)", text):
        code = match.group(1)
        if not base.valid_gtin(code):
            continue
        lo, hi = max(0, match.start() - 110), min(len(text), match.end() + 110)
        ctx = text[lo:hi].casefold()
        if any(marker in ctx for marker in GTIN_MARKERS):
            for candidate in base.ean_candidates(code):
                if candidate not in found:
                    found.append(candidate)
    return found


def _normalize_result_link(href: str) -> str:
    href = html_lib.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/url?"):
        q = parse_qs(urlparse(href).query).get("q")
        return q[0] if q else ""
    if href.startswith("/l/?"):
        href = "https://duckduckgo.com" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or ""):
        uddg = parse_qs(parsed.query).get("uddg")
        return unquote(uddg[0]) if uddg else ""
    return href if href.startswith("http") else ""


async def _search_urls(query: str) -> list[str]:
    urls: list[str] = []
    endpoints = (
        ("https://html.duckduckgo.com/html/", {"q": query}),
        ("https://www.bing.com/search", {"q": query}),
    )
    for endpoint, params in endpoints:
        try:
            r = await base.http.get(endpoint, params=params, headers=SEARCH_HEADERS, timeout=12)
            if not r.is_success:
                continue
            for href in re.findall(r"href=[\"']([^\"']+)[\"']", r.text, re.I):
                url = _normalize_result_link(href)
                if not url:
                    continue
                host = (urlparse(url).hostname or "").lower()
                if any(x in host for x in ("duckduckgo.com", "bing.com", "microsoft.com")):
                    continue
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 12:
                    return urls
        except Exception as exc:
            base.LOG.debug("Web search fallback %s: %s", endpoint, exc)
    return urls


async def _fetch_web_evidence(url: str, asin: str) -> tuple[str, list[str], bool]:
    try:
        r = await base.http.get(url, headers=SEARCH_HEADERS, timeout=10)
        if not r.is_success:
            return "", [], False
        ctype = (r.headers.get("content-type") or "").lower()
        if not any(x in ctype for x in ("text", "html", "json", "xml")):
            return "", [], False
        body = r.text[:700000]
        codes = _marked_gtins(body)
        has_asin = asin.casefold() in _clean_text(body).casefold()
        host = (urlparse(str(r.url)).hostname or urlparse(url).hostname or "").lower()
        return host, codes, has_asin
    except Exception:
        return "", [], False


async def web_identifiers(asin: str, hint: str, state: dict, cache: dict) -> tuple[list[str], list[str]]:
    if not WEB_GTIN_FALLBACK:
        return [], []

    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    if cached.get("web_verified") and cached.get("web_identifiers"):
        return list(cached.get("web_identifiers") or []), list(cached.get("web_sources") or [])

    queries = [f'"{asin}" EAN GTIN barcode']
    if hint:
        short_hint = re.sub(r"\s+", " ", hint).strip()[:150]
        queries += [f'"{short_hint}" EAN GTIN', f'"{short_hint}" "codice a barre"']

    urls: list[str] = []
    for query in queries:
        for url in await _search_urls(query):
            if url not in urls:
                urls.append(url)
        if len(urls) >= 12:
            break

    semaphore = asyncio.Semaphore(4)

    async def one(url: str):
        async with semaphore:
            return await _fetch_web_evidence(url, asin)

    evidence: dict[str, set[str]] = defaultdict(set)
    direct: set[str] = set()
    for host, codes, has_asin in await asyncio.gather(*(one(u) for u in urls[:12])) if urls else []:
        if not host:
            continue
        for code in codes:
            if not base.vv.first_match(state, [code]):
                continue
            evidence[code].add(host)
            if has_asin:
                direct.add(code)

    accepted = [code for code, hosts in evidence.items() if code in direct or len(hosts) >= 2]
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
    return accepted, sources


async def resolve_offer(message, amazon_url: str, state: dict, cache: dict, aliases: dict):
    canonical = await base.canonical_amazon_url(amazon_url)
    asin = base.asin_from_url(canonical)
    if not asin:
        return None

    text = message.message or ""
    segment = offer_context(text, asin, amazon_url)
    if not relevant_offer(segment):
        return None

    price = base.extract_offer_price(segment)
    if price is None:
        return {"asin": asin, "status": "no_price"}

    hint = product_hint(segment)
    units = 1
    identifier_source = ""
    match = None
    verified_pack = False

    alias = aliases.get(asin)
    if alias:
        alias_ean = base.normalize_gtin(alias.get("ean"))
        if base.valid_gtin(alias_ean):
            match = base.vv.first_match(state, [alias_ean])
            if match:
                units = max(1, int(alias.get("units_per_pack") or 1))
                verified_pack = True
                identifier_source = "alias verificato"

    if not match:
        match = base.vv.first_match(state, base.text_gtins(segment))
        if match:
            identifier_source = "GTIN nel messaggio"

    title = ""
    if not match:
        identifiers, title = await base.keepa_identifiers(asin, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            identifier_source = "Keepa/cache"

    if not match:
        identifiers, sources = await web_identifiers(asin, hint, state, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            identifier_source = "web verificato" + (f" ({', '.join(sources[:2])})" if sources else "")

    if not match:
        return {"asin": asin, "status": "unresolved", "url": canonical, "title": hint or title}

    detected_pack = pack_count(segment)
    if detected_pack > 1 and not verified_pack:
        return {
            "asin": asin,
            "status": "unresolved",
            "url": canonical,
            "title": (hint or title) + f" [multipack x{detected_pack}: relazione EAN/unità da verificare]",
        }

    ean, via = match
    amazon_unit = round(price / units, 4)
    reference = float(via["reference_price"])
    savings = reference - amazon_unit
    return {
        "asin": asin,
        "status": "matched",
        "url": canonical,
        "title": hint or title,
        "ean": ean,
        "units": units,
        "amazon_total": price,
        "amazon_unit": amazon_unit,
        "via": via,
        "conditional": base.conditional_price(segment),
        "identifier_source": identifier_source,
        "savings": savings,
        "savings_pct": (savings / reference * 100) if reference else 0,
    }


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registered = await base.report_chat_id()
    checks = {
        "Telegram account": bool(base.API_ID and base.API_HASH and base.USER_SESSION),
        "Bot report": bool(base.BOT_TOKEN),
        "Chat report": bool(registered),
        "Via Veneto": bool(base.VIA_VENETO_PIN),
        "Web ASIN→EAN": WEB_GTIN_FALLBACK,
        "Keepa opzionale": bool(base.KEEPA_API_KEY),
    }
    text = "⚙️ Stato Via Veneto Deals\n" + "\n".join(f"{'✅' if ok else '⚠️'} {name}" for name, ok in checks.items())
    await update.message.reply_text(text)


# Patchiamo il servizio esistente senza duplicarne scheduler, bot e logica Via Veneto.
base.extract_urls = extract_urls
base.canonical_amazon_url = canonical_amazon_url
base.find_source_chat = find_source_chat
base.day_messages = day_messages
base.resolve_offer = resolve_offer
base.cmd_status = cmd_status


if __name__ == "__main__":
    asyncio.run(base.main())
