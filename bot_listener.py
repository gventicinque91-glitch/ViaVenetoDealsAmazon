import json
import os
import time
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
LISTENER_WORKFLOW = os.getenv("LISTENER_WORKFLOW", "bot-listener.yml").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
LISTEN_SECONDS = int(os.getenv("LISTEN_SECONDS", "19200"))  # 5h20m
LONG_POLL_SECONDS = 50


def request_json(url: str, *, method: str = "GET", payload=None, headers=None, timeout=65):
    body = None
    req_headers = {"User-Agent": "ViaVenetoDealsAmazon-Listener/1.0"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc


def telegram(method: str, payload: dict, *, timeout=65):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_DEALS_BOT_TOKEN non configurato")
    data = request_json(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        method="POST",
        payload=payload,
        timeout=timeout,
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
        timeout=35,
    )


def workflow_runs(workflow: str, per_page: int = 10):
    data = github_api(
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/{workflow}/runs?per_page={per_page}"
    )
    return list((data or {}).get("workflow_runs") or [])


def active_report():
    for run in workflow_runs(REPORT_WORKFLOW):
        if run.get("status") in {"queued", "in_progress"}:
            return run
    return None


def dispatch_report(chat_id: str, status_message_id: int, requested_at: datetime):
    requested_iso = requested_at.astimezone(timezone.utc).isoformat()
    github_api(
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/{REPORT_WORKFLOW}/dispatches",
        method="POST",
        payload={
            "ref": GITHUB_REF_NAME,
            "inputs": {
                "chat_id": str(chat_id),
                "status_message_id": str(status_message_id),
                "cutoff": requested_iso,
                "requested_at": requested_iso,
                "origin": "manual",
            },
        },
    )


def dispatch_successor():
    github_api(
        f"/repos/{GITHUB_REPOSITORY}/actions/workflows/{LISTENER_WORKFLOW}/dispatches",
        method="POST",
        payload={"ref": GITHUB_REF_NAME},
    )


def fmt_time(value: str | datetime) -> str:
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = value
        return dt.astimezone(ROME).strftime("%d/%m %H:%M:%S")
    except Exception:
        return str(value or "?")


def handle_status(chat_id: str):
    runs = workflow_runs(REPORT_WORKFLOW)
    active = next((r for r in runs if r.get("status") in {"queued", "in_progress"}), None)
    if active:
        label = "in coda" if active.get("status") == "queued" else "in elaborazione"
        send_message(
            chat_id,
            "🟡 REPORT IN CORSO\n"
            f"Stato: {label}\n"
            f"GitHub run creato: {fmt_time(active.get('created_at', ''))}",
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


def requested_datetime(message: dict) -> datetime:
    raw = message.get("date")
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def handle_command(message: dict):
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return

    command = text.split()[0].split("@")[0].lower()

    if chat.get("type") != "private":
        send_message(chat_id, "⛔ Questo bot accetta comandi solo in chat privata.")
        return

    if OWNER_CHAT_ID and chat_id != OWNER_CHAT_ID:
        send_message(chat_id, "⛔ Questo bot è privato.")
        return

    if command == "/start":
        send_message(
            chat_id,
            "Via Veneto Deals attivo.\n\n"
            "/sconti — genera il report di oggi\n"
            "/report — stesso report\n"
            "/status — stato dell'elaborazione\n\n"
            "Il listener è sempre attivo: normalmente il comando viene preso in carico in pochi secondi. "
            "Il report automatico parte alle 20:00 Europe/Rome.",
        )
        return

    if command == "/status":
        handle_status(chat_id)
        return

    if command not in {"/sconti", "/report"}:
        return

    requested_at = requested_datetime(message)
    running = active_report()
    if running:
        label = "in coda" if running.get("status") == "queued" else "in elaborazione"
        send_message(
            chat_id,
            f"⏳ C'è già un report {label}.\n"
            f"Richiesta ricevuta alle {fmt_time(requested_at).split()[-1]}.\n"
            "Usa /status per seguirlo.",
        )
        return

    status = send_message(
        chat_id,
        "🟡 Report preso in carico.\n"
        f"Comando ricevuto alle {fmt_time(requested_at).split()[-1]}.\n"
        "Avvio subito l'analisi; il tempo finale includerà anche l'eventuale attesa GitHub.",
    )
    status_message_id = int((status or {}).get("message_id") or 0)
    try:
        dispatch_report(chat_id, status_message_id, requested_at)
    except Exception as exc:
        send_message(chat_id, f"❌ Non sono riuscito ad accodare il report: {str(exc)[:700]}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_DEALS_BOT_TOKEN non configurato")

    # getUpdates and webhooks are mutually exclusive.
    telegram("deleteWebhook", {"drop_pending_updates": False})

    deadline = time.monotonic() + LISTEN_SECONDS
    offset = None
    print(
        f"LISTENER_START duration={LISTEN_SECONDS}s "
        f"repo={GITHUB_REPOSITORY} ref={GITHUB_REF_NAME}"
    )

    try:
        while time.monotonic() < deadline:
            payload = {
                "timeout": LONG_POLL_SECONDS,
                "limit": 100,
                "allowed_updates": ["message", "edited_message"],
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = telegram(
                    "getUpdates",
                    payload,
                    timeout=LONG_POLL_SECONDS + 15,
                ) or []
            except Exception as exc:
                print(f"GET_UPDATES_WARNING {type(exc).__name__}: {exc}")
                time.sleep(3)
                continue

            for update in updates:
                update_id = int(update.get("update_id") or 0)
                offset = max(offset or 0, update_id + 1)
                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    continue
                try:
                    handle_command(message)
                except Exception as exc:
                    chat_id = str((message.get("chat") or {}).get("id") or "")
                    print(
                        f"COMMAND_ERROR update={update_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if chat_id:
                        try:
                            send_message(
                                chat_id,
                                f"❌ Errore nel comando: {type(exc).__name__}: {str(exc)[:600]}",
                            )
                        except Exception:
                            pass
    finally:
        # Start a replacement listener before this runner exits. The workflow
        # concurrency group keeps only one listener active at a time.
        try:
            dispatch_successor()
            print("SUCCESSOR_DISPATCHED")
        except Exception as exc:
            print(f"SUCCESSOR_WARNING {type(exc).__name__}: {exc}")

    print("LISTENER_STOP")


if __name__ == "__main__":
    main()
