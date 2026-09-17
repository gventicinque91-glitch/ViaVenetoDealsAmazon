'use strict'

import asyncio
from datetime import datetime
from typing import Any

import app_v12 as v12

base = v12.base
v11 = v12.v11
v10 = v12.v10
v9 = v12.v9
v8 = v12.v8
v7 = v12.v7
v6 = v12.v6
v5 = v12.v5
v4 = v12.v4
v3 = v12.v3
v2 = v12.v2


_report_task: asyncio.Task | None = None
_report_started_at: datetime | None = None
_report_owner_chat: str = ""
_report_cutoff: datetime | None = None


def _elapsed_text(started: datetime | None = None) -> str:
    started = started or _report_started_at
    if not started:
        return "0s"
    seconds = max(0, int((datetime.now(base.ROME) - started).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"


async def _persist_run(**changes: Any) -> None:
    state = await base.runtime_store.load({})
    run = dict(state.get("report_run") or {})
    run.update(changes)
    state["report_run"] = run
    await base.runtime_store.save(state)


async def _safe_edit(chat_id: str, message_id: int, text: str) -> None:
    try:
        if base.bot_app is not None:
            await base.bot_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as exc:
        base.LOG.warning("Impossibile aggiornare messaggio stato report: %s", exc)


async def _heartbeat(chat_id: str, message_id: int, stop: asyncio.Event) -> None:
    # Keep the user informed while slow external catalogue lookups are running.
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
            return
        except asyncio.TimeoutError:
            await _safe_edit(
                chat_id,
                message_id,
                "⏳ Report ancora in corso…\n"
                f"Tempo trascorso: {_elapsed_text()}\n"
                "Sto leggendo Telegram, risolvendo EAN e confrontando Via Veneto.\n"
                "Puoi usare /status in qualsiasi momento.",
            )


async def _run_report(chat_id: str, message_id: int, cutoff: datetime, origin: str) -> None:
    global _report_task, _report_started_at, _report_owner_chat, _report_cutoff

    started = datetime.now(base.ROME)
    _report_started_at = started
    _report_owner_chat = chat_id
    _report_cutoff = cutoff
    await _persist_run(
        status="running",
        origin=origin,
        chat_id=chat_id,
        started_at=started.isoformat(),
        cutoff=cutoff.isoformat(),
        finished_at="",
        error="",
    )

    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(chat_id, message_id, heartbeat_stop))
    try:
        await base.send_report_to(chat_id, cutoff)
        elapsed = _elapsed_text(started)
        finished = datetime.now(base.ROME)
        await _persist_run(
            status="success",
            finished_at=finished.isoformat(),
            duration=elapsed,
            error="",
        )
        await _safe_edit(
            chat_id,
            message_id,
            "✅ Report completato.\n"
            f"Durata: {elapsed}\n"
            f"Dati analizzati fino alle {cutoff.strftime('%H:%M')}.",
        )
    except asyncio.CancelledError:
        elapsed = _elapsed_text(started)
        await _persist_run(
            status="cancelled",
            finished_at=datetime.now(base.ROME).isoformat(),
            duration=elapsed,
            error="esecuzione cancellata",
        )
        await _safe_edit(chat_id, message_id, f"⚠️ Report interrotto dopo {elapsed}.")
        raise
    except Exception as exc:
        elapsed = _elapsed_text(started)
        error = f"{type(exc).__name__}: {exc}"
        base.LOG.exception("Report %s fallito", origin)
        await _persist_run(
            status="error",
            finished_at=datetime.now(base.ROME).isoformat(),
            duration=elapsed,
            error=error[:1000],
        )
        await _safe_edit(
            chat_id,
            message_id,
            "❌ Report non completato.\n"
            f"Dopo: {elapsed}\n"
            f"Errore: {error[:700]}\n\n"
            "Usa /status per vedere l'ultimo stato registrato.",
        )
    finally:
        heartbeat_stop.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass
        _report_task = None
        _report_started_at = None
        _report_owner_chat = ""
        _report_cutoff = None


async def cmd_report(update, context):
    global _report_task

    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    if base.AUTO_REGISTER_CHAT:
        state = await base.runtime_store.load({})
        state["report_chat_id"] = str(chat.id)
        state["registered_at"] = datetime.now(base.ROME).isoformat()
        await base.runtime_store.save(state)

    if _report_task is not None and not _report_task.done():
        cutoff_txt = _report_cutoff.strftime('%H:%M') if _report_cutoff else "?"
        await message.reply_text(
            "⏳ C'è già un report in corso.\n"
            f"Avviato da: {_elapsed_text()}\n"
            f"Cut-off: {cutoff_txt}\n"
            "Non ne avvio un secondo per evitare doppie scansioni. Usa /status per seguirlo."
        )
        return

    cutoff = datetime.now(base.ROME)
    status_message = await message.reply_text(
        "🟡 Report avviato.\n"
        f"Analizzo i messaggi dalle 00:00 alle {cutoff.strftime('%H:%M')}.\n"
        "Ti aggiorno qui ogni ~20 secondi; puoi anche usare /status."
    )
    _report_task = asyncio.create_task(
        _run_report(str(chat.id), status_message.message_id, cutoff, "manual")
    )


async def cmd_status(update, context):
    message = update.effective_message
    if not message:
        return

    registered = await base.report_chat_id()
    checks = {
        "Telegram account": bool(base.API_ID and base.API_HASH and base.USER_SESSION),
        "Bot report": bool(base.BOT_TOKEN),
        "Chat report": bool(registered),
        "Via Veneto": bool(base.VIA_VENETO_PIN),
    }

    lines = ["⚙️ Stato Via Veneto Deals"]
    if _report_task is not None and not _report_task.done():
        cutoff_txt = _report_cutoff.strftime('%H:%M') if _report_cutoff else "?"
        lines += [
            "",
            "🟡 REPORT IN CORSO",
            f"Durata: {_elapsed_text()}",
            f"Cut-off: {cutoff_txt}",
        ]
    else:
        state = await base.runtime_store.load({})
        run = dict(state.get("report_run") or {})
        status = str(run.get("status") or "never")
        if status == "success":
            lines += ["", "✅ Ultimo report completato", f"Durata: {run.get('duration') or '?'}"]
        elif status == "error":
            lines += [
                "",
                "❌ Ultimo report fallito",
                f"Durata: {run.get('duration') or '?'}",
                f"Errore: {str(run.get('error') or 'non disponibile')[:700]}",
            ]
        elif status == "cancelled":
            lines += ["", "⚠️ Ultimo report interrotto", f"Durata: {run.get('duration') or '?'}"]
        elif status == "running":
            # A stale 'running' record means the process restarted while a report was active.
            lines += [
                "",
                "⚠️ L'ultimo report risulta interrotto da un riavvio del servizio.",
                f"Avvio registrato: {run.get('started_at') or '?'}",
            ]
        else:
            lines += ["", "ℹ️ Nessun report ancora registrato in questa istanza."]

    lines += ["", "Configurazione:"]
    lines.extend(f"{'✅' if ok else '⚠️'} {name}" for name, ok in checks.items())
    await message.reply_text("\n".join(lines))


async def scheduled_report():
    global _report_task

    chat_id = await base.report_chat_id()
    if not chat_id:
        base.LOG.warning("Report delle 20:00 saltato: chat non registrata")
        return
    if _report_task is not None and not _report_task.done():
        base.LOG.warning("Report delle 20:00 non duplicato: un report è già in corso")
        return

    cutoff = datetime.now(base.ROME)
    try:
        status_message = await base.bot_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "🕗 Report automatico delle 20:00 avviato.\n"
                "Ti aggiorno qui durante l'elaborazione; /status resta disponibile."
            ),
        )
        _report_task = asyncio.create_task(
            _run_report(str(chat_id), status_message.message_id, cutoff, "scheduled")
        )
    except Exception:
        base.LOG.exception("Impossibile avviare report schedulato")


# base.main resolves these names dynamically from the base module when handlers/jobs are registered.
base.cmd_report = cmd_report
base.cmd_status = cmd_status
base.scheduled_report = scheduled_report


if __name__ == "__main__":
    asyncio.run(base.main())
