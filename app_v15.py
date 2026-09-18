'use strict'

import asyncio
import html as html_lib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import app_v14 as v14

v12 = v14.v12
v11 = v14.v11
v10 = v14.v10
v9 = v14.v9
v8 = v14.v8
v7 = v14.v7
v6 = v14.v6
v5 = v14.v5
v4 = v14.v4
v3 = v14.v3
v2 = v14.v2
base = v14.base

RESOLVER_VERSION = 9
TITLE_ALIASES_FILE = Path("verified_title_aliases.json")


def _norm_title(value: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", (value or "").casefold()).strip()


def _load_title_aliases() -> list[dict]:
    try:
        payload = json.loads(TITLE_ALIASES_FILE.read_text(encoding="utf-8"))
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
    except Exception:
        return []


def verified_title_alias(hint: str) -> dict | None:
    norm = _norm_title(hint)
    for row in _load_title_aliases():
        needles = [_norm_title(x) for x in (row.get("contains") or []) if str(x).strip()]
        if needles and all(token in norm for token in needles):
            return row
    return None


def _all_valid_gtins(raw: str) -> list[str]:
    text = html_lib.unescape(raw or "").replace("\\/", "/")
    out: list[str] = []
    patterns = (
        r'(?i)(?:ean|gtin(?:-?8|-?12|-?13|-?14)?|barcode|codice\s+(?:a\s+)?barre|riferimento)[^0-9]{0,140}(\d{8}|\d{12,14})',
        r'(?i)(\d{8}|\d{12,14})[^A-Za-z0-9]{0,100}(?:ean|gtin|barcode|codice\s+(?:a\s+)?barre)',
        r'(?i)[\"\']gtin(?:8|12|13|14)?[\"\']\s*:\s*[\"\']?(\d{8}|\d{12,14})',
    )
    for pattern in patterns:
        for raw_code in re.findall(pattern, text):
            code = base.normalize_gtin(raw_code)
            if not base.valid_gtin(code):
                continue
            for candidate in base.ean_candidates(code):
                if candidate not in out:
                    out.append(candidate)
    for code in v2._marked_gtins(text):
        if code not in out:
            out.append(code)
    return out


def _contextual_codes(raw: str, query: str) -> list[tuple[str, float]]:
    clean = v2._clean_text(raw)
    found: dict[str, float] = {}
    for match in re.finditer(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)", clean):
        raw_code = match.group(1)
        if not base.valid_gtin(raw_code):
            continue
        lo, hi = max(0, match.start() - 320), min(len(clean), match.end() + 320)
        context = clean[lo:hi]
        score = max(v6._title_score(query, context), v9._context_score(query, context))
        if score < 6.0:
            continue
        for code in base.ean_candidates(raw_code):
            found[code] = max(found.get(code, 0.0), score)
    return sorted(found.items(), key=lambda item: item[1], reverse=True)


async def dm_any_eans(query: str) -> tuple[list[str], list[str]]:
    """Return externally verified EANs from dm without consulting Via Veneto."""
    if not query:
        return [], []
    try:
        response = await base.http.get(
            v12.DM_SEARCH_URL,
            params={"query": query},
            headers={"Accept": "application/json", **v2.SEARCH_HEADERS},
            timeout=12,
        )
        if not response.is_success:
            return [], []
        payload = response.json()
    except Exception as exc:
        base.LOG.debug("dm unrestricted lookup failed %r: %s", query, exc)
        return [], []

    scored: list[tuple[float, str]] = []
    for row in v12._walk_rows(payload):
        codes = v12._codes_from_row(row)
        if not codes:
            continue
        external_text = v12._dict_text(row)
        score = v6._title_score(query, external_text)
        if score < 5.0:
            continue
        for code in codes:
            if base.valid_gtin(code):
                scored.append((score, code))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return [], []

    best = scored[0][0]
    # Keep only candidates close to the best external-title match.
    accepted: list[str] = []
    for score, code in scored:
        if score + 1.5 < best:
            continue
        if code not in accepted:
            accepted.append(code)
    return accepted[:4], ["dm"]


async def broad_external_eans(query: str, asin: str = "") -> tuple[list[str], list[str]]:
    """Search the Internet for EANs first; Via Veneto is deliberately not consulted here."""
    urls, search_bodies = await v14._search_web(query, asin=asin)
    evidence: dict[str, set[str]] = {}
    best_score: dict[str, float] = {}

    for provider, body in search_bodies:
        for code, score in _contextual_codes(body, query):
            evidence.setdefault(code, set()).add(provider)
            best_score[code] = max(best_score.get(code, 0.0), score)

    semaphore = asyncio.Semaphore(8)

    async def one(url: str):
        async with semaphore:
            try:
                response = await base.http.get(url, headers=v2.SEARCH_HEADERS, timeout=10)
                if not response.is_success:
                    return "", [], 0.0
                ctype = (response.headers.get("content-type") or "").lower()
                if not any(x in ctype for x in ("text", "html", "json", "xml")):
                    return "", [], 0.0
                body = response.text[:1200000]
                host = (urlparse(str(response.url)).hostname or urlparse(url).hostname or "").lower()
                title = v14._page_title(body)
                title_score = max(v6._title_score(query, title), v9._context_score(query, title))
                has_asin = bool(asin and asin.casefold() in body.casefold())
                codes = {code for code, _ in _contextual_codes(body, query)}
                if title_score >= 5.5 or has_asin:
                    codes.update(_all_valid_gtins(body))
                return host, sorted(codes), (12.0 if has_asin else title_score)
            except Exception:
                return "", [], 0.0

    try:
        pages = await asyncio.wait_for(
            asyncio.gather(*(one(url) for url in urls[:20])),
            timeout=26,
        ) if urls else []
    except asyncio.TimeoutError:
        pages = []

    for host, codes, score in pages:
        if not host:
            continue
        for code in codes:
            evidence.setdefault(code, set()).add(host)
            best_score[code] = max(best_score.get(code, 0.0), score)

    accepted = [
        code
        for code, providers in evidence.items()
        if best_score.get(code, 0.0) >= 6.5 or len(providers) >= 2
    ]
    accepted.sort(
        key=lambda code: (best_score.get(code, 0.0), len(evidence.get(code, set()))),
        reverse=True,
    )
    sources = sorted({source for code in accepted for source in evidence.get(code, set())})
    base.LOG.info(
        "External-name->EAN query=%r accepted=%s sources=%s",
        query, accepted[:5], sources[:5],
    )
    return accepted[:5], sources


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    """Extended product name -> external EAN first; exact Via Veneto lookup happens later."""
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
        ids = list(cached.get("name_identifiers") or [])
        if ids:
            return ids, dict(cached.get("name_identifier_modes") or {}), list(cached.get("name_sources") or [])
        try:
            checked = datetime.fromisoformat(str(cached.get("name_checked_at") or ""))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=base.ROME)
            age = (datetime.now(base.ROME) - checked.astimezone(base.ROME)).total_seconds()
        except Exception:
            age = 10**9
        if age < 1800:
            return [], {}, list(cached.get("name_sources") or [])

    accepted: list[str] = []
    modes: dict[str, str] = {}
    sources: list[str] = []
    pack_units: dict[str, int] = {}

    plan: list[tuple[str, str]] = []
    if amazon_pack > 1 and single_query:
        plan.append((single_query, "unit"))
    if full_query and full_query.casefold() != (single_query or "").casefold():
        plan.append((full_query, "package"))
    elif full_query:
        plan.append((full_query, "package"))

    # Search every relevant identity layer instead of stopping at the first hit.
    # For multipacks, unit EAN candidates are intentionally ordered first; package
    # candidates remain available if Via Veneto stores the bundle barcode instead.
    for query, mode in plan:
        codes, provider_sources = await dm_any_eans(query)
        if not codes:
            codes, provider_sources = await broad_external_eans(
                query,
                asin=(asin if mode == "package" else ""),
            )
        for code in codes:
            if code not in accepted:
                accepted.append(code)
                modes[code] = mode
                if mode == "unit" and amazon_pack > 1:
                    pack_units[code] = amazon_pack
        for source in provider_sources:
            if source not in sources:
                sources.append(source)

    # Exact ASIN evidence is a strong package-level fallback, especially for affiliate
    # pages that publish ASIN + EAN explicitly.
    if asin:
        asin_codes, asin_sources = await broad_external_eans(full_query or hint, asin=asin)
        for code in asin_codes:
            if code not in accepted:
                accepted.append(code)
                modes[code] = "package"
        for source in asin_sources:
            if source not in sources:
                sources.append(source)

    # Aggregate wash-count bundles, e.g. 123 washes -> 3 x 41, only when one factor wins.
    if not accepted and amazon_pack == 1 and full_query:
        inferred: list[tuple[str, int, list[str]]] = []
        for unit_candidate, factor in v12._wash_bundle_queries(full_query):
            codes, provider_sources = await dm_any_eans(unit_candidate)
            if not codes:
                codes, provider_sources = await broad_external_eans(unit_candidate)
            for code in codes:
                inferred.append((code, factor, provider_sources))
        unique = {(code, factor) for code, factor, _ in inferred}
        factors = {factor for _, factor in unique}
        if unique and len(factors) == 1:
            factor = next(iter(factors))
            for code, candidate_factor, provider_sources in inferred:
                if candidate_factor != factor or code in accepted:
                    continue
                accepted.append(code)
                modes[code] = "unit"
                pack_units[code] = factor
                for source in provider_sources:
                    if source not in sources:
                        sources.append(source)

    entry = dict(cached)
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
        entry["identifiers"] = list(dict.fromkeys(list(entry.get("identifiers") or []) + accepted))
        entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    return accepted, modes, sources


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
    resolved_pack_units = 1
    external_ids: list[str] = []
    title = ""

    title_alias = verified_title_alias(hint)
    if title_alias:
        verified_codes = [
            base.normalize_gtin(code)
            for code in (title_alias.get("eans") or [])
            if base.valid_gtin(base.normalize_gtin(code))
        ]
        if verified_codes:
            external_ids = list(dict.fromkeys(external_ids + verified_codes))
            match = base.vv.first_match(state, verified_codes)
            if match:
                units = max(1, int(title_alias.get("units_per_pack") or 1))
                verified_pack = True
                identifier_source = "nome→EAN verificato"

    alias = aliases.get(asin)
    if alias and not match:
        alias_ean = base.normalize_gtin(alias.get("ean"))
        if base.valid_gtin(alias_ean):
            external_ids = list(dict.fromkeys(external_ids + [alias_ean]))
            match = base.vv.first_match(state, [alias_ean])
            if match:
                units = max(1, int(alias.get("units_per_pack") or 1))
                verified_pack = True
                identifier_source = "alias verificato"

    if not match:
        message_ids = base.text_gtins(segment)
        if message_ids:
            external_ids = list(message_ids)
            match = base.vv.first_match(state, external_ids)
            if match:
                identifier_source = "GTIN nel messaggio"

    if not match:
        identifiers, modes, sources = await name_identifiers(asin, hint, state, cache)
        if identifiers:
            external_ids = list(identifiers)
            match = base.vv.first_match(state, external_ids)
            if match:
                pack_mode = modes.get(match[0], "")
                refreshed = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
                try:
                    resolved_pack_units = max(
                        1,
                        int((refreshed.get("name_pack_units") or {}).get(match[0]) or 1),
                    )
                except Exception:
                    resolved_pack_units = 1
                identifier_source = "nome→EAN" + (f" ({', '.join(sources[:2])})" if sources else "")

    if not match and base.KEEPA_API_KEY:
        identifiers, title = await base.keepa_identifiers(asin, cache)
        if identifiers:
            external_ids = list(dict.fromkeys(external_ids + list(identifiers)))
            match = base.vv.first_match(state, identifiers)
            if match:
                identifier_source = "Keepa"

    if not match:
        if external_ids:
            return {
                "asin": asin,
                "status": "not_in_db",
                "url": canonical,
                "title": hint or title,
                "eans": external_ids[:5],
            }
        return {
            "asin": asin,
            "status": "unresolved",
            "url": canonical,
            "title": hint or title,
        }

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
    elif detected_pack == 1 and pack_mode == "unit" and resolved_pack_units > 1:
        units = resolved_pack_units

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


# Report builder resolves offers through v2.resolve_offer.
v10.name_identifiers = name_identifiers
v2.resolve_offer = resolve_offer
base.resolve_offer = resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
