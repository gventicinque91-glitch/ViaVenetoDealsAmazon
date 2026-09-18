'use strict'

import asyncio

from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityTextUrl

import app_v4 as v4

v3 = v4.v3
v2 = v4.v2
base = v4.base


def _is_cta_line(value: str) -> bool:
    low = (value or "").casefold()
    return any(token in low for token in ("amazon", "apri", "guarda", "offerta", "vai al", "acquista"))


def _context_block_around(text_surrogate: str, start: int, end: int) -> str:
    """Return the product block around a Telegram text-link.

    Many channels put the Amazon CTA on its own line. In that layout the old code
    returned only "APRI SU AMAZON", losing product name and price. Prefer the
    paragraph containing the CTA; if the post has no blank-line separators, include
    a bounded number of preceding lines.
    """
    para_start = text_surrogate.rfind("\n\n", 0, start)
    para_start = para_start + 2 if para_start >= 0 else -1
    para_end = text_surrogate.find("\n\n", end)
    if para_end < 0:
        para_end = len(text_surrogate)

    if para_start < 0:
        # Fall back to at most 8 preceding lines so a CTA-only anchor still carries
        # product title, price and pack information without swallowing the whole post.
        cursor = start
        for _ in range(8):
            prev = text_surrogate.rfind("\n", 0, cursor)
            if prev < 0:
                cursor = 0
                break
            cursor = prev
        para_start = cursor + (1 if cursor > 0 else 0)

    block = del_surrogate(text_surrogate[para_start:para_end]).strip()
    return block


def offer_segment(message, amazon_url: str, asin: str) -> str:
    """Return the product block associated with this Telegram offer URL."""
    text = message.message or ""
    if not text:
        return ""

    surrogate = add_surrogate(text)
    for entity in message.entities or []:
        if not isinstance(entity, MessageEntityTextUrl) or not entity.url:
            continue
        if entity.url != amazon_url:
            continue

        start = int(entity.offset)
        end = start + int(entity.length)
        line_start = surrogate.rfind("\n", 0, start) + 1
        line_end = surrogate.find("\n", end)
        if line_end < 0:
            line_end = len(surrogate)
        line = del_surrogate(surrogate[line_start:line_end]).strip()

        if line and not _is_cta_line(line):
            return line

        block = _context_block_around(surrogate, start, end)
        if block:
            return block

    # For inline URLs, use the legacy local context. For button-only affiliate links,
    # the URL is not present in message text, so use the full post; category filtering
    # and price parsing will still discard unrelated posts.
    fallback = v2.offer_context(text, asin, amazon_url)
    if fallback == text and amazon_url not in text:
        return text
    return fallback


async def resolve_offer(message, amazon_url: str, state: dict, cache: dict, aliases: dict):
    canonical = await base.canonical_amazon_url(amazon_url)
    asin = base.asin_from_url(canonical)
    if not asin:
        return None

    segment = offer_segment(message, amazon_url, asin)
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
        identifiers, sources = await v4.web_identifiers(asin, hint, state, cache)
        match = base.vv.first_match(state, identifiers)
        if match:
            identifier_source = "web verificato" + (f" ({', '.join(sources[:2])})" if sources else "")

    if not match:
        return {"asin": asin, "status": "unresolved", "url": canonical, "title": hint or title}

    detected_pack = v2.pack_count(segment)
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


v2.resolve_offer = resolve_offer
base.resolve_offer = resolve_offer


if __name__ == "__main__":
    asyncio.run(base.main())
