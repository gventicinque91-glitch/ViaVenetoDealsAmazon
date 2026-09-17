'use strict'

import asyncio
import html
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

ROME = ZoneInfo("Europe/Rome")
LOG = logging.getLogger("via-veneto-deals")

AMAZON_HOST_RE = re.compile(r"(^|\.)(amazon\.(it|com|de|fr|es|co\.uk)|amzn\.(to|eu))$", re.I)
URL_RE = re.compile(r"https?://[^\s<>\]\[()]+", re.I)
ASIN_PATTERNS = [
    re.compile(r"/(?:dp|gp/product|product)/([A-Z0-9]{10})(?:[/?]|$)", re.I),
    re.compile(r"/(?:dp|gp/product|product)/([A-Z0-9]{10})", re.I),
]
CONDITIONAL_WORDS = ("coupon", "buono", "prime", "iscriviti e risparmia", "abbonati", "codice sconto", "con codice")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


API_ID = int(env("TELEGRAM_API_ID", "0") or "0")
API_HASH = env("TELEGRAM_API_HASH")
USER_SESSION = env("TELEGRAM_USER_SESSION")
BOT_TOKEN = env("TELEGRAM_DEALS_BOT_TOKEN")
REPORT_CHAT_ID = env("TELEGRAM_REPORT_CHAT_ID")
SOURCE_CHAT_TITLE = env("SOURCE_CHAT_TITLE", "Caccia allo SCONTO 🎯")
VIA_VENETO_API_URL = env("VIA_VENETO_API_URL", "https://prezzi-mamma-api.gventicinque91.workers.dev").rstrip("/")
VIA_VENETO_PIN = env("VIA_VENETO_PIN")
KEEPA_API_KEY = env("KEEPA_API_KEY")
AUTO_REGISTER_CHAT = env("AUTO_REGISTER_CHAT", "true").lower() not in {"0", "false", "no"}
STATE_DIR = Path(env("STATE_DIR", "./data"))
STATE_FILE = STATE_DIR / "runtime_state.json"
CACHE_FILE = STATE_DIR / "asin_cache.json"
VV_CACHE_FILE = STATE_DIR / "via_veneto_cache.json"
ALIASES_FILE = Path(env("ALIASES_FILE", "aliases.json"))


@dataclass
class Offer:
    asin: str
    amazon_url: str
    amazon_total: float
    units_per_pack: int
    amazon_unit: float
    ean: str
    title: str
    via_description: str
    purchase_price: float | None
    purchase_date: str
    list_price: float | None
    list_date: str
    reference_price: float
    reference_label: str
    savings: float
    savings_pct: float
    conditional: bool
    identifier_source: str


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self, default: Any):
        async with self._lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return default

    async def save(self, value: Any):
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)


runtime_store = JsonStore(STATE_FILE)
cache_store = JsonStore(CACHE_FILE)
vv_cache_store = JsonStore(VV_CACHE_FILE)
report_lock = asyncio.Lock()
http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True, headers={"User-Agent": "ViaVenetoDealsAmazon/1.0"})
user_client: TelegramClient | None = None
bot_app: Application | None = None


def normalize_gtin(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def valid_gtin(value: str) -> bool:
    digits = normalize_gtin(value)
    if len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = 0
    weight = 3
    for ch in reversed(body):
        total += int(ch) * weight
        weight = 1 if weight == 3 else 3
    return (10 - (total % 10)) % 10 == check


def ean_candidates(value: Any) -> list[str]:
    e = normalize_gtin(value)
    if not valid_gtin(e):
        return []
    out = [e]
    if len(e) == 12:
        candidate = "0" + e
        if valid_gtin(candidate):
            out.append(candidate)
    elif len(e) == 13 and e.startswith("0"):
        candidate = e[1:]
        if valid_gtin(candidate):
            out.append(candidate)
    return list(dict.fromkeys(out))


def text_gtins(text: str) -> list[str]:
    found: list[str] = []
    for raw in re.findall(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)", text or ""):
        for candidate in ean_candidates(raw):
            if candidate not in found:
                found.append(candidate)
    return found


def extract_urls(message) -> list[str]:
    text = message.message or ""
    urls = [m.group(0).rstrip(".,;:!?") for m in URL_RE.finditer(text)]
    for entity in message.entities or []:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            urls.append(entity.url)
    out: list[str] = []
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            continue
        if AMAZON_HOST_RE.search(host) and url not in out:
            out.append(url)
    return out


async def canonical_amazon_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("amzn.to") or host.endswith("amzn.eu"):
        try:
            response = await http.get(url)
            return str(response.url)
        except Exception as exc:
            LOG.warning("Short URL non risolto %s: %s", url, exc)
    return url


def asin_from_url(url: str) -> str:
    parsed = urlparse(url)
    for pattern in ASIN_PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            return match.group(1).upper()
    query = parse_qs(parsed.query)
    for key in ("asin", "ASIN"):
        value = query.get(key)
        if value and re.fullmatch(r"[A-Z0-9]{10}", value[0], re.I):
            return value[0].upper()
    match = re.search(r"\b(B0[A-Z0-9]{8})\b", url, re.I)
    return match.group(1).upper() if match else ""


def extract_offer_price(text: str) -> float | None:
    candidates: list[tuple[float, int, str]] = []
    patterns = [
        re.compile(r"€\s*(\d{1,4}(?:[.,]\d{1,2})?)", re.I),
        re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|eur)\b?", re.I),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            try:
                value = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            if not (0.05 <= value <= 5000):
                continue
            lo, hi = max(0, match.start() - 28), min(len(text), match.end() + 28)
            context = text[lo:hi].lower()
            # Non scambiare l'importo di un coupon/buono per il prezzo del prodotto.
            if any(word in context for word in ("coupon da", "buono da", "coupon di", "buono di")):
                continue
            penalty = 0
            if any(word in context for word in ("anziché", "invece di", "prima ", "listino", "prezzo precedente")):
                penalty += 5
            if any(word in context for word in ("ora", "solo", "offerta", "prezzo", " a ")):
                penalty -= 1
            candidates.append((value, penalty, context))
    if not candidates:
        return None
    # Prima privilegia i candidati senza segnali da prezzo vecchio, poi il più basso.
    candidates.sort(key=lambda x: (x[1], x[0]))
    best_penalty = candidates[0][1]
    same_quality = [x for x in candidates if x[1] == best_penalty]
    return min(x[0] for x in same_quality)


def conditional_price(text: str) -> bool:
    low = (text or "").lower()
    return any(word in low for word in CONDITIONAL_WORDS)


def load_aliases() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        return {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}


class ViaVeneto:
    def __init__(self):
        self.state: dict[str, Any] | None = None
        self.revision = -1
        self.generation = ""

    async def _request(self, path: str) -> dict[str, Any]:
        if not VIA_VENETO_PIN:
            raise RuntimeError("VIA_VENETO_PIN non configurato")
        response = await http.get(VIA_VENETO_API_URL + path, headers={"X-App-Pin": VIA_VENETO_PIN})
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "Errore API Via Veneto")
        return payload

    async def get_state(self) -> dict[str, Any]:
        revision = await self._request("/revision")
        remote_rev = int(revision.get("revision") or 0)
        remote_generation = str(revision.get("generation") or "")
        if self.state is not None and self.revision == remote_rev and self.generation == remote_generation:
            return self.state
        cached = await vv_cache_store.load({})
        if cached.get("revision") == remote_rev and cached.get("generation") == remote_generation and isinstance(cached.get("state"), dict):
            self.revision = remote_rev
            self.generation = remote_generation
            self.state = cached["state"]
            return self.state
        bootstrap = await self._request("/bootstrap")
        self.revision = int(bootstrap.get("revision") or remote_rev)
        self.generation = str(bootstrap.get("generation") or remote_generation)
        self.state = bootstrap.get("state") or {}
        await vv_cache_store.save({"revision": self.revision, "generation": self.generation, "state": self.state})
        return self.state

    @staticmethod
    def _matches(value: Any, candidates: set[str]) -> bool:
        return any(x in candidates for x in ean_candidates(value))

    def lookup(self, state: dict[str, Any], ean: str) -> dict[str, Any] | None:
        cands = set(ean_candidates(ean))
        if not cands:
            return None
        products = state.get("products") or {}
        product = None
        matched_ean = ""
        for candidate in cands:
            if candidate in products:
                product = products[candidate]
                matched_ean = candidate
                break
        if product is None:
            for key, p in products.items():
                if self._matches(key, cands) or self._matches((p or {}).get("ean"), cands):
                    product, matched_ean = p, normalize_gtin(key)
                    break
        if product is None:
            return None

        docs = {str(d.get("id")): d for d in (state.get("documents") or []) if isinstance(d, dict)}
        history = []
        for row in state.get("priceHistory") or []:
            if not isinstance(row, dict) or not self._matches(row.get("ean"), cands):
                continue
            try:
                cost = float(row.get("costGross"))
            except (TypeError, ValueError):
                continue
            if cost <= 0:
                continue
            doc = docs.get(str(row.get("documentId")))
            if doc and str(doc.get("docType") or "").upper() == "NOTA_CREDITO":
                continue
            history.append((str(row.get("documentDate") or ""), str(row.get("confirmedAt") or ""), cost))
        history.sort(key=lambda x: (x[0], x[1]), reverse=True)
        purchase_price = history[0][2] if history else None
        purchase_date = history[0][0] or history[0][1][:10] if history else ""

        list_price = None
        list_date = ""
        orders = sorted(
            [o for o in (state.get("orders") or []) if isinstance(o, dict)],
            key=lambda o: (str(o.get("validFrom") or ""), str(o.get("importedAt") or "")),
            reverse=True,
        )
        for order in orders:
            line = next((l for l in (order.get("lines") or []) if isinstance(l, dict) and self._matches(l.get("ean"), cands)), None)
            if line:
                try:
                    value = float(line.get("expectedGross"))
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    list_price = value
                    list_date = str(order.get("validFrom") or order.get("importedAt") or "")[:10]
                    break
        if list_price is None:
            try:
                fallback = float((product or {}).get("expectedGross"))
            except (TypeError, ValueError):
                fallback = 0
            if fallback > 0:
                list_price = fallback
                list_date = str((product or {}).get("lastCatalogAt") or "")[:10]

        reference_price = purchase_price if purchase_price is not None else list_price
        if reference_price is None:
            return None
        return {
            "ean": matched_ean or ean,
            "description": str((product or {}).get("description") or (product or {}).get("name") or "Prodotto Via Veneto"),
            "purchase_price": purchase_price,
            "purchase_date": purchase_date,
            "list_price": list_price,
            "list_date": list_date,
            "reference_price": reference_price,
            "reference_label": "ultimo acquisto" if purchase_price is not None else "ultimo listino",
        }

    def first_match(self, state: dict[str, Any], identifiers: list[str]) -> tuple[str, dict[str, Any]] | None:
        for identifier in identifiers:
            for candidate in ean_candidates(identifier):
                result = self.lookup(state, candidate)
                if result:
                    return candidate, result
        return None


vv = ViaVeneto()


async def keepa_identifiers(asin: str, cache: dict[str, Any]) -> tuple[list[str], str]:
    cached = cache.get(asin) if isinstance(cache.get(asin), dict) else None
    if cached:
        return list(cached.get("identifiers") or []), str(cached.get("title") or "")
    if not KEEPA_API_KEY:
        return [], ""
    try:
        response = await http.get("https://api.keepa.com/product", params={"key": KEEPA_API_KEY, "domain": 8, "asin": asin})
        response.raise_for_status()
        payload = response.json()
        products = payload.get("products") or []
        product = products[0] if products else {}
        identifiers: list[str] = []
        for field in ("eanList", "upcList", "gtinList"):
            for code in product.get(field) or []:
                for candidate in ean_candidates(code):
                    if candidate not in identifiers:
                        identifiers.append(candidate)
        title = str(product.get("title") or "")
        cache[asin] = {
            "identifiers": identifiers,
            "title": title,
            "checked_at": datetime.now(ROME).isoformat(),
        }
        await cache_store.save(cache)
        return identifiers, title
    except Exception as exc:
        LOG.warning("Keepa %s: %s", asin, exc)
        return [], ""


async def find_source_chat():
    assert user_client is not None
    exact = None
    partial = None
    wanted = SOURCE_CHAT_TITLE.casefold()
    async for dialog in user_client.iter_dialogs():
        name = str(dialog.name or "")
        if name.casefold() == wanted:
            exact = dialog.entity
            break
        if wanted in name.casefold() or name.casefold() in wanted:
            partial = partial or dialog.entity
    entity = exact or partial
    if entity is None:
        raise RuntimeError(f"Canale Telegram non trovato: {SOURCE_CHAT_TITLE}")
    return entity


async def day_messages(until: datetime) -> list[Any]:
    assert user_client is not None
    entity = await find_source_chat()
    start = datetime.combine(until.date(), time.min, tzinfo=ROME)
    messages = []
    async for message in user_client.iter_messages(entity):
        if not message.date:
            continue
        local_dt = message.date.astimezone(ROME)
        if local_dt < start:
            break
        if local_dt <= until:
            messages.append(message)
    return messages


async def resolve_offer(message, amazon_url: str, state: dict[str, Any], cache: dict[str, Any], aliases: dict[str, Any]) -> dict[str, Any] | None:
    canonical = await canonical_amazon_url(amazon_url)
    asin = asin_from_url(canonical)
    if not asin:
        return None
    text = message.message or ""
    price = extract_offer_price(text)
    if price is None:
        return {"asin": asin, "status": "no_price"}

    title = ""
    units = 1
    identifier_source = ""
    match = None

    alias = aliases.get(asin)
    if alias:
        alias_ean = normalize_gtin(alias.get("ean"))
        if valid_gtin(alias_ean):
            match = vv.first_match(state, [alias_ean])
            if match:
                units = max(1, int(alias.get("units_per_pack") or 1))
                identifier_source = "alias verificato"

    if not match:
        # Accettiamo un GTIN scritto nel post solo se trova davvero un prodotto Via Veneto.
        match = vv.first_match(state, text_gtins(text))
        if match:
            identifier_source = "GTIN nel messaggio"

    if not match:
        identifiers, title = await keepa_identifiers(asin, cache)
        match = vv.first_match(state, identifiers)
        if match:
            identifier_source = "Keepa"

    if not match:
        return {"asin": asin, "status": "unresolved", "url": canonical, "title": title}

    ean, via = match
    amazon_unit = round(price / units, 4)
    reference = float(via["reference_price"])
    savings = reference - amazon_unit
    return {
        "asin": asin,
        "status": "matched",
        "url": canonical,
        "title": title,
        "ean": ean,
        "units": units,
        "amazon_total": price,
        "amazon_unit": amazon_unit,
        "via": via,
        "conditional": conditional_price(text),
        "identifier_source": identifier_source,
        "savings": savings,
        "savings_pct": (savings / reference * 100) if reference else 0,
    }


async def build_report(until: datetime | None = None) -> str:
    async with report_lock:
        until = (until or datetime.now(ROME)).astimezone(ROME)
        state = await vv.get_state()
        messages = await day_messages(until)
        cache = await cache_store.load({})
        aliases = load_aliases()

        amazon_links = 0
        no_price = 0
        unresolved: dict[str, dict[str, Any]] = {}
        best_by_asin: dict[str, dict[str, Any]] = {}

        for message in messages:
            for url in extract_urls(message):
                amazon_links += 1
                result = await resolve_offer(message, url, state, cache, aliases)
                if not result:
                    continue
                asin = result.get("asin") or ""
                if result.get("status") == "no_price":
                    no_price += 1
                    continue
                if result.get("status") == "unresolved":
                    unresolved[asin] = result
                    continue
                previous = best_by_asin.get(asin)
                if not previous or float(result["amazon_unit"]) < float(previous["amazon_unit"]):
                    best_by_asin[asin] = result

        matched = list(best_by_asin.values())
        deals = [x for x in matched if float(x.get("savings") or 0) > 0]
        deals.sort(key=lambda x: float(x.get("savings_pct") or 0), reverse=True)

        lines = [
            f"🛒 VIA VENETO · AMAZON DEALS",
            f"📅 {until.strftime('%d/%m/%Y')} · aggiornato alle {until.strftime('%H:%M')}",
            f"📣 Fonte: {SOURCE_CHAT_TITLE}",
            "",
            f"Messaggi letti: {len(messages)}",
            f"Link Amazon: {amazon_links}",
            f"Prodotti Via Veneto riconosciuti: {len(matched)}",
            f"Amazon più conveniente: {len(deals)}",
            f"Non identificati con codice esatto: {len(unresolved)}",
        ]

        if deals:
            lines += ["", "🔥 OFFERTE CONVENIENTI"]
            for item in deals[:25]:
                via = item["via"]
                description = via.get("description") or item.get("title") or item["asin"]
                units = int(item.get("units") or 1)
                ref = float(via["reference_price"])
                unit = float(item["amazon_unit"])
                lines += [
                    "",
                    f"• {description}",
                    f"EAN: {item['ean']} · ASIN: {item['asin']}",
                    (f"Amazon: €{item['amazon_total']:.2f} confezione / {units} = €{unit:.2f} per pezzo" if units > 1 else f"Amazon: €{unit:.2f}"),
                    f"Via Veneto ({via['reference_label']}): €{ref:.2f}" + (f" · {via.get('purchase_date') or via.get('list_date')}" if (via.get('purchase_date') or via.get('list_date')) else ""),
                    f"Risparmio: €{item['savings']:.2f} · {item['savings_pct']:.1f}%" + (" · ⚠️ prezzo condizionato" if item.get("conditional") else ""),
                    item["url"],
                ]
        else:
            lines += ["", "Nessuna offerta Amazon identificata con prezzo inferiore al riferimento Via Veneto."]

        if unresolved:
            lines += ["", f"❓ Da identificare: {len(unresolved)} ASIN (nessuna associazione EAN/GTIN esatta trovata)."]
            for item in list(unresolved.values())[:8]:
                title = item.get("title") or item.get("asin")
                lines.append(f"• {title} · {item.get('asin')}")

        if no_price:
            lines.append(f"⚠️ {no_price} link Amazon ignorati perché il prezzo non era ricavabile dal messaggio.")
        return "\n".join(lines)


def chunks(text: str, limit: int = 3900) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > limit and current:
            parts.append(current.rstrip())
            current = ""
        current += line
    if current:
        parts.append(current.rstrip())
    return parts


async def report_chat_id() -> str:
    if REPORT_CHAT_ID:
        return REPORT_CHAT_ID
    state = await runtime_store.load({})
    return str(state.get("report_chat_id") or "")


async def send_report_to(chat_id: str, until: datetime | None = None):
    assert bot_app is not None
    report = await build_report(until)
    for part in chunks(report):
        await bot_app.bot.send_message(chat_id=chat_id, text=part, disable_web_page_preview=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    if AUTO_REGISTER_CHAT:
        state = await runtime_store.load({})
        state["report_chat_id"] = str(chat.id)
        state["registered_at"] = datetime.now(ROME).isoformat()
        await runtime_store.save(state)
    await update.message.reply_text(
        "Via Veneto Deals attivo.\n\n"
        "• /sconti — report da mezzanotte fino ad ora\n"
        "• /report — stesso report\n"
        "• /status — verifica configurazione\n\n"
        "Il report automatico parte ogni giorno alle 20:00 (Europe/Rome)."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    registered = await report_chat_id()
    checks = {
        "Telegram account": bool(API_ID and API_HASH and USER_SESSION),
        "Bot report": bool(BOT_TOKEN),
        "Chat report": bool(registered),
        "Via Veneto": bool(VIA_VENETO_PIN),
        "Keepa ASIN→EAN": bool(KEEPA_API_KEY),
    }
    text = "⚙️ Stato Via Veneto Deals\n" + "\n".join(f"{'✅' if ok else '⚠️'} {name}" for name, ok in checks.items())
    await update.message.reply_text(text)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    if AUTO_REGISTER_CHAT:
        state = await runtime_store.load({})
        state["report_chat_id"] = str(chat.id)
        await runtime_store.save(state)
    await update.message.reply_text("⏳ Analizzo i messaggi di oggi e confronto i prezzi con Via Veneto…")
    try:
        await send_report_to(str(chat.id), datetime.now(ROME))
    except Exception as exc:
        LOG.exception("Report manuale fallito")
        await update.message.reply_text(f"❌ Report non completato: {exc}")


async def scheduled_report():
    chat_id = await report_chat_id()
    if not chat_id:
        LOG.warning("Report delle 20:00 saltato: chat non registrata")
        return
    try:
        await send_report_to(chat_id, datetime.now(ROME))
    except Exception:
        LOG.exception("Report schedulato fallito")


def validate_config():
    missing = []
    if not API_ID:
        missing.append("TELEGRAM_API_ID")
    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not USER_SESSION:
        missing.append("TELEGRAM_USER_SESSION")
    if not BOT_TOKEN:
        missing.append("TELEGRAM_DEALS_BOT_TOKEN")
    if not VIA_VENETO_PIN:
        missing.append("VIA_VENETO_PIN")
    if missing:
        raise RuntimeError("Secret mancanti: " + ", ".join(missing))


async def main():
    global user_client, bot_app
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    user_client = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH)
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError("TELEGRAM_USER_SESSION non autorizzata: rigenerarla con generate_session.py")

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("sconti", cmd_report))
    bot_app.add_handler(CommandHandler("report", cmd_report))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    await bot_app.initialize()
    await bot_app.start()
    if bot_app.updater is None:
        raise RuntimeError("Updater Telegram Bot non disponibile")
    await bot_app.updater.start_polling(drop_pending_updates=False)

    scheduler = AsyncIOScheduler(timezone=ROME)
    scheduler.add_job(scheduled_report, CronTrigger(hour=20, minute=0, timezone=ROME), id="daily-20", max_instances=1, coalesce=True)
    scheduler.start()

    me = await user_client.get_me()
    LOG.info("Servizio attivo come account Telegram id=%s; fonte=%s; report 20:00 Europe/Rome", getattr(me, "id", "?"), SOURCE_CHAT_TITLE)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    scheduler.shutdown(wait=False)
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()
    await user_client.disconnect()
    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
