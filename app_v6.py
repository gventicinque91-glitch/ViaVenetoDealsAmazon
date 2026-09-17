'use strict'

import asyncio
import re
import time
from datetime import datetime

import app_v5 as v5

v4 = v5.v4
v3 = v4.v3
v2 = v5.v2
base = v5.base

UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/search"
UPC_MIN_INTERVAL_SECONDS = 15.5
UPC_DAILY_BUDGET = 20
_upc_lock = asyncio.Lock()
_upc_last_call = 0.0
_upc_day = ""
_upc_calls_today = 0


def _tokens_ordered(text: str) -> list[str]:
    stop = {
        "della", "delle", "dello", "dalla", "degli", "dell", "senza", "formula", "pelle",
        "uomo", "donna", "con", "per", "una", "uno", "gli", "che", "pezzi", "pezzo",
        "consegna", "ore", "protezione", "formula", "avanzata", "prodotto", "set",
    }
    return [
        t for t in re.findall(r"[a-zà-ÿ0-9]+", (text or "").casefold())
        if len(t) >= 3 and t not in stop
    ]


def _measurements(text: str) -> set[str]:
    out: set[str] = set()
    low = (text or "").casefold().replace(",", ".")
    for value, unit in re.findall(r"\b(\d+(?:\.\d+)?)\s*(ml|cl|l|g|kg)\b", low):
        out.add(f"{value}{unit}")
    return out


def _pack_count_from_title(text: str) -> int:
    low = text or ""
    patterns = (
        re.compile(r"\b(\d{1,2})\s*[x×]\s*\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|kg)\b", re.I),
        re.compile(r"\b(\d{1,2})\s*(?:pezzi|pz|tubi|flaconi|bottiglie|confezioni)\b", re.I),
        re.compile(r"\b(?:set|pack|confezione)\s+(?:di|da)?\s*(\d{1,2})\b", re.I),
    )
    for pattern in patterns:
        m = pattern.search(low)
        if m:
            try:
                n = int(m.group(1))
                if 1 < n <= 48:
                    return n
            except Exception:
                pass
    return 1


def _title_score(query: str, candidate: str) -> float:
    q = _tokens_ordered(query)
    c = _tokens_ordered(candidate)
    if not q or not c:
        return 0.0
    qs, cs = set(q), set(c)
    overlap = len(qs & cs)
    ratio = overlap / max(1, min(len(qs), len(cs)))
    score = overlap + 4.0 * ratio

    # Il primo token significativo è normalmente il marchio. Se coincide, forte bonus.
    if q[0] in cs:
        score += 2.0
    else:
        score -= 2.0

    qm = _measurements(query)
    cm = _measurements(candidate)
    if qm and cm:
        if qm & cm:
            score += 2.0
        else:
            score -= 2.0

    qp = _pack_count_from_title(query)
    cp = _pack_count_from_title(candidate)
    if qp > 1 and cp > 1:
        score += 1.5 if qp == cp else -1.5
    return score


def _contained_ean13(gtin14: str) -> str:
    digits = base.normalize_gtin(gtin14)
    if len(digits) != 14 or not base.valid_gtin(digits):
        return ""
    body12 = digits[1:13]
    total = 0
    weight = 3
    for ch in reversed(body12):
        total += int(ch) * weight
        weight = 1 if weight == 3 else 3
    check = (10 - (total % 10)) % 10
    candidate = body12 + str(check)
    return candidate if base.valid_gtin(candidate) else ""


def _identifier_candidates(item: dict) -> list[str]:
    out: list[str] = []
    for field in ("ean", "upc", "gtin"):
        raw = base.normalize_gtin(item.get(field))
        if not raw:
            continue
        for code in base.ean_candidates(raw):
            if code not in out:
                out.append(code)
        if len(raw) == 14:
            contained = _contained_ean13(raw)
            if contained and contained not in out:
                out.append(contained)
    return out


def _compact_query(hint: str) -> str:
    value = re.sub(r"\([^)]*(?:consegna|spedizione)[^)]*\)", " ", hint or "", flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -,")
    return value[:180]


async def _upc_search(query: str) -> list[dict]:
    global _upc_last_call, _upc_day, _upc_calls_today
    today = datetime.now(base.ROME).date().isoformat()
    async with _upc_lock:
        if _upc_day != today:
            _upc_day = today
            _upc_calls_today = 0
            _upc_last_call = 0.0
        if _upc_calls_today >= UPC_DAILY_BUDGET:
            return []

        wait = UPC_MIN_INTERVAL_SECONDS - (time.monotonic() - _upc_last_call)
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            response = await base.http.get(
                UPCITEMDB_URL,
                params={"s": query},
                headers={"Accept": "application/json", "Content-Type": "application/json", **v2.SEARCH_HEADERS},
                timeout=12,
            )
            _upc_last_call = time.monotonic()
            _upc_calls_today += 1
            if response.status_code == 429:
                base.LOG.warning("UPCitemdb rate limit raggiunto")
                return []
            if not response.is_success:
                base.LOG.warning("UPCitemdb HTTP %s", response.status_code)
                return []
            payload = response.json()
            return [x for x in (payload.get("items") or []) if isinstance(x, dict)]
        except Exception as exc:
            _upc_last_call = time.monotonic()
            _upc_calls_today += 1
            base.LOG.warning("UPCitemdb search fallita: %s", exc)
            return []


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    """Resolve product extended name -> EAN/GTIN, then validate only by exact Via Veneto EAN.

    No DB description/fuzzy match is used to identify a product. The name is used solely to
    query external product-code data. A candidate is accepted only if its checksum is valid,
    its external title matches the Telegram product title, and that exact identifier exists
    in Via Veneto.
    """
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    if cached.get("name_lookup_done"):
        return (
            list(cached.get("name_identifiers") or []),
            dict(cached.get("name_identifier_modes") or {}),
            list(cached.get("name_sources") or []),
        )

    query = _compact_query(hint)
    if not query:
        return [], {}, []

    items = await _upc_search(query)
    scored: list[tuple[float, str, str, str]] = []
    amazon_pack = v2.pack_count(hint)

    for item in items:
        external_title = " ".join(
            x for x in (str(item.get("brand") or "").strip(), str(item.get("title") or "").strip()) if x
        ).strip()
        score = _title_score(query, external_title)
        if score < 5.0:
            continue

        external_pack = _pack_count_from_title(external_title)
        for code in _identifier_candidates(item):
            if not base.vv.first_match(state, [code]):
                continue
            # If the external title explicitly describes the same multipack, its EAN is the
            # package EAN. If it describes a single item while Amazon is a multipack, it is a
            # unit EAN and the Amazon total must be divided by the pack count.
            if amazon_pack > 1 and external_pack == amazon_pack:
                mode = "package"
            elif amazon_pack > 1 and external_pack == 1:
                mode = "unit"
            else:
                mode = "package"
            scored.append((score, code, mode, external_title))

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
    entry["name_lookup_done"] = True
    entry["name_checked_at"] = datetime.now(base.ROME).isoformat()
    entry["name_query"] = query
    entry["name_identifiers"] = accepted
    entry["name_identifier_modes"] = modes
    entry["name_titles"] = titles
    entry["name_sources"] = ["UPCitemdb"] if items else []
    if accepted:
        entry["identifiers"] = list(dict.fromkeys(list(entry.get("identifiers") or []) + accepted))
        if hint and not entry.get("title"):
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info("Name->EAN ASIN=%s query=%r accepted=%s modes=%s", asin, query, accepted, modes)
    return accepted, modes, entry["name_sources"]


async def resolve_offer(message, amazon_url: str, state: dict, cache: dict, aliases: dict):
    canonical = await base.canonical_amazon_url(amazon_url)
    asin = base.asin_from_url(canonical)
    if not asin:
        return None

    segment = v5.offer_segment(message, amazon_url, asin)
    if not v2.relevant_offer(segment):
        return None

    price = base.extract_offer_price(segment)
    if price is None:
        return {"asin": asin, "status": "no_price"}

    hint = v2.product_hint(segment)
    units = 1
    identifier_source = ""
    match = None
    verified_pack = False
    pack_mode = ""

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

    # Core requested flow: extended product name -> EAN/GTIN -> exact EAN lookup in Via Veneto.
    if not match:
        identifiers, modes, sources = await name_identifiers(asin, hint, state, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            ean_candidate = match[0]
            pack_mode = modes.get(ean_candidate, "")
            identifier_source = "nome→EAN" + (f" ({', '.join(sources)})" if sources else "")

    if not match:
        identifiers, sources = await v4.web_identifiers(asin, hint, state, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            identifier_source = "web verificato" + (f" ({', '.join(sources[:2])})" if sources else "")

    if not match:
        return {"asin": asin, "status": "unresolved", "url": canonical, "title": hint or title}

    detected_pack = v2.pack_count(segment)
    if detected_pack > 1 and not verified_pack:
        if pack_mode == "unit":
            units = detected_pack
        elif pack_mode == "package":
            units = 1
        else:
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


v2.resolve_offer = resolve_offer
base.resolve_offer = resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
