'use strict'

import asyncio
import re

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
    """Return a bounded block before/around a CTA-only Telegram link.

    Deal channels often put blank lines between title, promo text, price and
    "APRI SU AMAZON". A paragraph-only slice therefore loses the product name.
    Walk backwards by lines instead, stopping at strong post separators.
    """
    line_end = text_surrogate.find("\n", end)
    if line_end < 0:
        line_end = len(text_surrogate)

    cursor = start
    line_starts = [text_surrogate.rfind("\n", 0, cursor) + 1]
    # Include enough preceding lines to retain title + price even with blank lines.
    for _ in range(14):
        prev_nl = text_surrogate.rfind("\n", 0, max(0, line_starts[-1] - 1))
        if prev_nl < 0:
            line_starts.append(0)
            break
        candidate_start = prev_nl + 1
        candidate_end = line_starts[-1] - 1
        candidate = del_surrogate(text_surrogate[candidate_start:candidate_end]).strip()
        # A previous CTA is itself a hard product boundary in multi-offer posts.
        # Without this, the second/third offer can inherit the title and category
        # of the product immediately above it.
        if candidate and _is_cta_line(candidate):
            break
        # Strong separators used by multi-offer posts: do not bleed into prior products.
        if re.fullmatch(r"[\s➖━─—_-]{4,}", candidate or ""):
            break
        line_starts.append(candidate_start)

    block_start = line_starts[-1]
    block = del_surrogate(text_surrogate[block_start:line_end]).strip()

    # Remove generic channel promo/greeting lines before the actual product title.
    # This keeps the extended product name clean for name→EAN resolution.
    lines = block.splitlines()
    first_relevant = next(
        (idx for idx, value in enumerate(lines) if v2.relevant_offer(value)),
        None,
    )
    if first_relevant is not None:
        block = "\n".join(lines[first_relevant:]).strip()
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

        if line:
            # A common channel layout puts the product description, price and CTA
            # on the same line. The previous logic treated any line containing
            # "Amazon"/"Apri" as CTA-only and then walked backwards, which could
            # attach the link to a previous product in the same multi-offer post.
            line_surrogate = surrogate[line_start:line_end]
            rel_start = max(0, start - line_start)
            rel_end = max(rel_start, min(len(line_surrogate), end - line_start))
            before = del_surrogate(line_surrogate[:rel_start]).strip()
            after = del_surrogate(line_surrogate[rel_end:]).strip()
            substantive = re.sub(
                r"^[\\s👉➡️•·|–—-]+|[\\s👉➡️•·|–—-]+$",
                "",
                f"{before} {after}",
            ).strip()

            # If removing the linked CTA leaves a category-relevant offer, this
            # exact line is the correct identity context for this URL.
            if substantive and v2.relevant_offer(substantive):
                return substantive

            if not _is_cta_line(line):
                return line

        block = _context_block_around(surrogate, start, end)
        if block:
            return block

    # Generic multi-offer fallback: when the URL comes from a Telegram button (or an
    # entity representation that does not compare byte-for-byte with amazon_url),
    # bind URLs and CTA lines by their order in the post. This prevents a later
    # fashion/household link from inheriting the previous detergent product block.
    try:
        ordered_urls = v2.extract_urls(message)
        target_index = ordered_urls.index(amazon_url)
    except Exception:
        target_index = -1

    if target_index >= 0:
        lines = text.splitlines(True)
        ctas: list[tuple[int, int]] = []
        pos = 0
        for raw_line in lines:
            line_end = pos + len(raw_line)
            plain = raw_line.strip()
            if plain and _is_cta_line(plain):
                ctas.append((pos, line_end))
            pos = line_end

        if target_index < len(ctas):
            current_start, current_end = ctas[target_index]
            previous_end = ctas[target_index - 1][1] if target_index > 0 else 0
            block = text[previous_end:current_end].strip()
            # Remove the CTA line itself, retaining the product title and price.
            block_lines = block.splitlines()
            while block_lines and _is_cta_line(block_lines[-1].strip()):
                block_lines.pop()
            block = "\n".join(block_lines).strip()
            if block:
                return block

    # For inline URLs, use the legacy local context. For button-only affiliate links,
    # the URL is not present in message text, so use the full post only as a last resort.
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
