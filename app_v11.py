'use strict'

import asyncio

import app_v10 as v10

v9 = v10.v9
v8 = v10.v8
v7 = v10.v7
v6 = v10.v6
v5 = v10.v5
v4 = v10.v4
v3 = v10.v3
v2 = v10.v2
base = v10.base


async def resolve_offer(message, amazon_url: str, state: dict, cache: dict, aliases: dict):
    """Resolve an offer with name→EAN metadata preserved end-to-end.

    Identity flow:
      Telegram extended product title -> external EAN/GTIN evidence -> exact Via Veneto EAN lookup.

    The Via Veneto description is never used for fuzzy identity matching. For multipacks,
    the resolver also preserves whether the discovered EAN is the unit barcode or the
    package barcode so the Amazon price is normalized correctly.
    """
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
    title = ""

    # 1) Explicit, manually verified ASIN→EAN aliases remain the strongest evidence.
    alias = aliases.get(asin)
    if alias:
        alias_ean = base.normalize_gtin(alias.get("ean"))
        if base.valid_gtin(alias_ean):
            match = base.vv.first_match(state, [alias_ean])
            if match:
                units = max(1, int(alias.get("units_per_pack") or 1))
                verified_pack = True
                identifier_source = "alias verificato"

    # 2) If Telegram itself contains a valid GTIN, use exact barcode lookup.
    if not match:
        match = base.vv.first_match(state, base.text_gtins(segment))
        if match:
            identifier_source = "GTIN nel messaggio"

    # 3) Reuse a previously verified name→EAN association WITH its pack metadata.
    #    This fixes the previous bug where the generic cache returned the EAN through
    #    keepa_identifiers(), losing the unit/package relation for multipacks.
    if not match:
        cached = cache.get(asin) if isinstance(cache.get(asin), dict) else {}
        cached_name_ids = list(cached.get("name_identifiers") or [])
        cached_modes = dict(cached.get("name_identifier_modes") or {})
        if cached_name_ids:
            match = base.vv.first_match(state, cached_name_ids)
            if match:
                pack_mode = cached_modes.get(match[0], "")
                sources = list(cached.get("name_sources") or [])
                identifier_source = "nome→EAN cache" + (f" ({', '.join(sources[:2])})" if sources else "")

    # 4) Core requested flow: extended product name -> external EAN -> exact Via Veneto EAN.
    if not match:
        identifiers, modes, sources = await v10.name_identifiers(asin, hint, state, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            pack_mode = modes.get(match[0], "")
            identifier_source = "nome→EAN" + (f" ({', '.join(sources[:2])})" if sources else "")

    # 5) Optional Keepa fallback. Only call it when a Keepa key is actually configured;
    #    otherwise the legacy generic cache could masquerade as Keepa data.
    if not match and base.KEEPA_API_KEY:
        identifiers, title = await base.keepa_identifiers(asin, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            identifier_source = "Keepa"

    # 6) Conservative web fallback for single products. For a multipack, a code without
    #    unit/package metadata is intentionally not enough to normalize the price.
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


# All report code ultimately calls v2.resolve_offer. Patch both references so manual
# commands and the scheduled report use the same corrected identity path.
v2.resolve_offer = resolve_offer
base.resolve_offer = resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
