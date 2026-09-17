import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")

BOT_TOKEN = os.getenv("TELEGRAM_DEALS_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = (
    os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
    or os.getenv("TELEGRAM_REPORT_CHAT_ID", "").strip()
)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
REPORT_WORKFLOW = os.getenv("REPORT_WORKFLOW", "report.yml").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"


def request_json(url: str, *, method: str = "GET", payload=None, headers=None):
    body = None
    req_headers = {"User-Agent": "ViaVenetoDealsAmazon-Poller/1.0"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc


def telegram(method: str, payload: dict):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_DEALS_BOT_TOKEN non configurato")
    data = request_json(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        method="POST",
        payload=payload,
    )
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {(data or {}).get('description', 'errore sconosciuto')}")
    return data.get("result")


def send_message(chat_id: str, text: str):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def github_api(path: str, *, method: str = "GET", payload=None):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY non disponibili")
    return request_json(
        f"https://api.github.com{path}",
        method=method,
        payload=payload,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def report_runs():
    data = github_api(
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/{REPORT_WORKFLOW}/runs?per_page=10"
    )
    return list((data or {}).get("workflow_runs") or [])


def active_report():
    for run in report_runs():
        if run.get("status") in {"queued", "in_progress"}:
            return run
    return None


def dispatch_report(chat_id: str, status_message_id: int):
    github_api(
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/{REPORT_WORKFLOW}/dispatches",
        method="POST",
        payload={
            "ref": GITHUB_REF_NAME,
            "inputs": {
                "chat_id": str(chat_id),
                "status_message_id": str(status_message_id),
                "cutoff": datetime.now(timezone.utc).isoformat(),
                "origin": "manual",
            },
        },
    )


def fmt_time(value: str) -> str:
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(ROME).strftime("%d/%m %H:%M")
    except Exception:
        return value


def handle_status(chat_id: str):
    runs = report_runs()
    active = next((r for r in runs if r.get("status") in {"queued", "in_progress"}), None)
    if active:
        label = "in coda" if active.get("status") == "queued" else "in elaborazione"
        send_message(
            chat_id,
            "🟡 REPORT IN CORSO\n"
            f"Stato: {label}\n"
            f"Avvio: {fmt_time(active.get('created_at', ''))}",
        )
        return

    last = next((r for r in runs if r.get("status") == "completed"), None)
    if not last:
        send_message(chat_id, "ℹ️ Nessun report ancora registrato.")
        return

    ok = last.get("conclusion") == "success"
    send_message(
        chat_id,
        ("✅" if ok else "❌")
        + f" Ultimo report {'completato' if ok else 'terminato con errore'}\n"
        + f"Conclusione: {last.get('conclusion') or 'sconosciuta'}\n"
        + f"Avvio: {fmt_time(last.get('created_at', ''))}\n"
        + f"Fine: {fmt_time(last.get('updated_at', ''))}",
    )


def handle_command(message: dict, dispatched_this_run: bool) -> bool:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return dispatched_this_run

    command = text.split()[0].split("@")[0].lower()

    if chat.get("type") != "private":
        send_message(chat_id, "⛔ Questo bot accetta comandi solo in chat privata.")
        return dispatched_this_run

    if OWNER_CHAT_ID and chat_id != OWNER_CHAT_ID:
        send_message(chat_id, "⛔ Questo bot è privato.")
        return dispatched_this_run

    if command == "/start":
        send_message(
            chat_id,
            "Via Veneto Deals attivo.\n\n"
            "/sconti — genera il report di oggi\n"
            "/report — stesso report\n"
            "/status — stato dell'elaborazione\n\n"
            "I comandi vengono raccolti automaticamente ogni pochi minuti; "
            "il report automatico parte alle 20:00 Europe/Rome.",
        )
        return dispatched_this_run

    if command == "/status":
        handle_status(chat_id)
        return dispatched_this_run

    if command not in {"/sconti", "/report"}:
        return dispatched_this_run

    if dispatched_this_run:
        send_message(chat_id, "⏳ Ho già accodato un report da un comando appena ricevuto. Usa /status.")
        return True

    running = active_report()
    if running:
        label = "in coda" if running.get("status") == "queued" else "in elaborazione"
        send_message(chat_id, f"⏳ C'è già un report {label}. Usa /status per seguirlo.")
        return True

    status = send_message(
        chat_id,
        "🟡 Report avviato.\n"
        "Analizzo i messaggi da oggi 00:00 fino ad ora e confronto i prezzi con Via Veneto.",
    )
    status_message_id = int((status or {}).get("message_id") or 0)
    try:
        dispatch_report(chat_id, status_message_id)
    except Exception as exc:
        send_message(chat_id, f"❌ Non sono riuscito ad accodare il report: {str(exc)[:700]}")
        return dispatched_this_run
    return True


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_DEALS_BOT_TOKEN non configurato")

    # This architecture intentionally uses getUpdates, so remove any old webhook
    # without discarding pending Telegram commands.
    try:
        telegram("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        print(f"DELETE_WEBHOOK_WARNING: {exc}")

    updates = telegram(
        "getUpdates",
        {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": ["message", "edited_message"],
        },
    ) or []

    if not updates:
        print("NO_UPDATES")
        return

    max_update_id = max(int(u.get("update_id") or 0) for u in updates)
    dispatched_this_run = False

    for update in updates:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        try:
            dispatched_this_run = handle_command(message, dispatched_this_run)
        except Exception as exc:
            chat_id = str((message.get("chat") or {}).get("id") or "")
            print(f"COMMAND_ERROR update={update.get('update_id')}: {type(exc).__name__}: {exc}")
            if chat_id:
                try:
                    send_message(chat_id, f"❌ Errore nel comando: {type(exc).__name__}: {str(exc)[:600]}")
                except Exception:
                    pass

    # Acknowledge every update from this batch only after processing it.
    telegram(
        "getUpdates",
        {
            "offset": max_update_id + 1,
            "timeout": 0,
            "limit": 1,
            "allowed_updates": ["message", "edited_message"],
        },
    )
    print(f"PROCESSED_UPDATES={len(updates)} LAST_UPDATE_ID={max_update_id}")


if __name__ == "__main__":
    main()
