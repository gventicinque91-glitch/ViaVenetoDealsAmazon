'use strict'

import asyncio
import re
from datetime import datetime

import app_v11 as v11

v10 = v11.v10
v9 = v10.v9
v8 = v10.v8
v7 = v10.v7
v6 = v10.v6
v5 = v10.v5
v4 = v10.v4
v3 = v10.v3
v2 = v10.v2
base = v10.base

_original_name_identifiers = v10.name_identifiers
DM_SEARCH_URL = "https://product-search.services.dmtech.com/it/search"


def _dict_text(row: dict) -> str:
    parts: list[str] = []
    for key, value in row.items():
        k = str(key).casefold()
        if isinstance(value, str) and any(token in k for token in ("name", "title", "brand", "description", "product")):
            parts.append(value)
        elif isinstance(value, dict) and any(token in k for token in ("brand", "product")):
            for child in value.values():
                if isinstance(child, str):
                    parts.append(child)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _codes_from_row(row: dict) -> list[str]:
    out: list[str] = []
    for key, value in row.items():
        k = str(key).casefold()
        if not any(token in k for token in ("gtin", "ean", "barcode")):
            continue
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if isinstance(raw, dict):
                values.extend(raw.values())
                continue
            code = base.normalize_gtin(raw)
            for candidate in base.ean_candidates(code):
                if candidate not in out:
                    out.append(candidate)
    return out


def _walk_rows(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_rows(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_rows(value)


async def dm_name_identifiers(query: str, state: dict) -> tuple[list[str], list[str]]:
    """Search dm's public product catalogue by product name and return exact listino EANs.

    Text similarity is evaluated only against dm catalogue text. Via Veneto is queried
    afterwards exclusively by exact EAN/GTIN.
    """
    if not query:
        return [], []
    try:
        response = await base.http.get(
            DM_SEARCH_URL,
            params={"query": query},
            headers={"Accept": "application/json", **v2.SEARCH_HEADERS},
            timeout=12,
        )
        base.LOG.info("dm catalogue query=%r status=%s", query, response.status_code)
        if not response.is_success:
            return [], []
        payload = response.json()
    except Exception as exc:
        base.LOG.warning("dm catalogue search fallita %r: %s", query, exc)
        return [], []

    scored: list[tuple[float, str]] = []
    for row in _walk_rows(payload):
        codes = _codes_from_row(row)
        if not codes:
            continue
        external_text = _dict_text(row)
        score = v6._title_score(query, external_text)
        if score < 5.0:
            continue
        for code in codes:
            if base.vv.first_match(state, [code]):
                scored.append((score, code))

    scored.sort(key=lambda x: x[0], reverse=True)
    accepted: list[str] = []
    for _, code in scored:
        if code not in accepted:
            accepted.append(code)
    return accepted, ["dm"] if accepted else []


def _wash_bundle_queries(query: str) -> list[tuple[str, int]]:
    """Generate conservative unit-title candidates from aggregate wash counts.

    Example: "123 Lavaggi" can only be split cleanly as 3 x 41 within the normal
    retail-dose range, so the unit query becomes "41 Lavaggi". Ambiguous totals are
    kept as multiple candidates and accepted only when one candidate resolves uniquely.
    """
    match = re.search(r"\b(\d{2,3})\s*(lavaggi|dosi|misurini)\b", query or "", flags=re.I)
    if not match:
        return []
    total = int(match.group(1))
    label = match.group(2)
    out: list[tuple[str, int]] = []
    for factor in (2, 3, 4, 5, 6):
        if total % factor:
            continue
        unit = total // factor
        if not 15 <= unit <= 70:
            continue
        unit_query = (
            (query or "")[: match.start()]
            + f"{unit} {label}"
            + (query or "")[match.end() :]
        )
        unit_query = re.sub(r"\s+", " ", unit_query).strip()
        if unit_query and unit_query.casefold() != (query or "").casefold():
            out.append((unit_query, factor))
    return out


async def name_identifiers(asin: str, hint: str, state: dict, cache: dict):
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
    version = int(cached.get("name_resolver_version") or 0)
    if version >= 7 and cached.get("name_lookup_done"):
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
    pack_units: dict[str, int] = {}

    # dm covers both personal-care and household-cleaning products, which maps well to
    # the Telegram categories monitored by this bot. For multipacks search the retail
    # unit first because Via Veneto stores the sellable unit barcode.
    plan: list[tuple[str, str]] = []
    if amazon_pack > 1 and single_query:
        plan.append((single_query, "unit"))
    if full_query and full_query.casefold() != (single_query or "").casefold():
        plan.append((full_query, "package"))
    if not plan and full_query:
        plan.append((full_query, "package"))

    for query, mode in plan:
        codes, provider_sources = await dm_name_identifiers(query, state)
        if codes:
            for code in codes:
                if code not in accepted:
                    accepted.append(code)
                    modes[code] = mode
            sources.extend(x for x in provider_sources if x not in sources)
            break

    # Some laundry bundles advertise only the aggregate wash count (e.g. 123 washes)
    # instead of "3 x 41". If the normal title did not match, derive conservative
    # unit-count candidates and accept them only when exactly one factor resolves to
    # an exact Via Veneto EAN through an external catalogue/page.
    if not accepted and amazon_pack == 1 and full_query:
        inferred_hits: list[tuple[str, int, list[str]]] = []
        for unit_candidate, factor in _wash_bundle_queries(full_query):
            codes, provider_sources = await dm_name_identifiers(unit_candidate, state)
            candidate_sources = list(provider_sources)
            if not codes:
                try:
                    codes, page_sources = await v10.page_evidence(unit_candidate, state)
                except Exception:
                    codes, page_sources = [], []
                candidate_sources.extend(x for x in page_sources if x not in candidate_sources)
            for code in codes:
                inferred_hits.append((code, factor, candidate_sources))

        unique = {(code, factor) for code, factor, _ in inferred_hits}
        factors = {factor for _, factor in unique}
        if len(unique) == 1 or (len(factors) == 1 and unique):
            chosen_factor = next(iter(factors))
            for code, factor, candidate_sources in inferred_hits:
                if factor != chosen_factor or code in accepted:
                    continue
                accepted.append(code)
                modes[code] = "unit"
                pack_units[code] = factor
                for source in candidate_sources:
                    if source not in sources:
                        sources.append(source)

    # Keep the broader public-web resolver as a fallback for products dm does not stock.
    if not accepted:
        old_codes, old_modes, old_sources = await _original_name_identifiers(asin, hint, state, cache)
        accepted = list(old_codes)
        modes = dict(old_modes)
        sources = list(old_sources)

    if amazon_pack > 1:
        for code, mode in modes.items():
            if mode == "unit" and code not in pack_units:
                pack_units[code] = amazon_pack

    entry = dict(cache.get(asin) if isinstance(cache.get(asin), dict) else cached)
    entry.update({
        "name_lookup_done": True,
        "name_resolver_version": 7,
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
        if hint and not entry.get("title"):
            entry["title"] = hint
    cache[asin] = entry
    await base.cache_store.save(cache)
    base.LOG.info("Name->EAN v7 ASIN=%s accepted=%s modes=%s pack_units=%s sources=%s", asin, accepted, modes, pack_units, sources)
    return accepted, modes, sources


# v11.resolve_offer calls v10.name_identifiers dynamically, so patch that hook.
v10.name_identifiers = name_identifiers
v2.resolve_offer = v11.resolve_offer
base.resolve_offer = v11.resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
