# ViaVenetoDealsAmazon

Servizio separato per monitorare le offerte Amazon pubblicate nei canali Telegram dell'account autorizzato, identificare i prodotti tramite codici GTIN/EAN/UPC verificati, confrontarli con il database Via Veneto e inviare un report Telegram.

## Funzioni

- Legge la cronologia del giorno delle fonti Telegram configurate.
- Report automatico ogni giorno alle **20:00 Europe/Rome**.
- Report manuale con `/sconti` o `/report`, dalle 00:00 fino all'istante del comando.
- `/status` mostra se il report è in coda, in elaborazione, completato o fallito.
- Il messaggio finale distingue **tempo totale dalla richiesta**, **attesa avvio GitHub** ed **elaborazione effettiva**.
- Identificazione rigorosa: alias verificato -> GTIN/EAN nel messaggio -> cache ASIN -> resolver esterni verificati.
- Nessun fuzzy match verso Via Veneto per decidere l'identità del prodotto: il confronto finale avviene per codice esatto.
- Confronto Via Veneto: **ultimo prezzo di acquisto confermato**; se assente, **ultimo listino/ordine**.
- Multipack normalizzati solo quando il rapporto confezione/unità è verificato.

## Architettura attiva

La configurazione principale non richiede un server esterno sempre acceso né secret Cloudflare aggiuntivi.

1. `.github/workflows/bot-listener.yml` mantiene un listener Telegram attivo per diverse ore su un runner GitHub.
2. `bot_listener.py` usa il long polling Telegram e normalmente riceve `/start`, `/sconti`, `/report` e `/status` in pochi secondi.
3. Prima di terminare, il listener lancia automaticamente il proprio successore; uno schedule periodico funge da watchdog.
4. Per `/sconti`, il listener passa a `.github/workflows/report.yml` **l'orario reale del comando Telegram**. Il cut-off del report non dipende quindi dall'eventuale coda GitHub.
5. `.github/workflows/daily-report-dispatch.yml` avvia il report automatico quando sono le 20:00 in `Europe/Rome`, gestendo automaticamente ora legale/solare.
6. `run_report_once.py` legge Telegram con Telethon, genera il report e aggiorna su Telegram il tempo totale e il tempo di elaborazione.

Il vecchio poller cron ogni 5 minuti è stato disattivato perché gli schedule GitHub possono partire con ritardo e rendevano la risposta ai comandi imprevedibile.

I file Cloudflare Worker restano nel repository come alternativa futura per una modalità webhook, ma non sono necessari per la configurazione GitHub-only.

## Secret richiesti in GitHub Actions

Non inserire mai credenziali nei file del repository.

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_USER_SESSION`
- `TELEGRAM_DEALS_BOT_TOKEN`
- `VIA_VENETO_PIN`

Opzionali:

- `TELEGRAM_OWNER_CHAT_ID` — limita i comandi del bot a una sola chat privata.
- `TELEGRAM_REPORT_CHAT_ID` — destinazione esplicita per il report automatico; in assenza viene usato l'account Telegram autorizzato.
- `KEEPA_API_KEY` — fonte aggiuntiva per ASIN→EAN.
- `SOURCE_CHAT_TITLE`, `VIA_VENETO_API_URL`, `STATE_DIR`, `AUTO_REGISTER_CHAT`.

## Prima autenticazione Telegram

La StringSession Telethon si genera una sola volta con:

```bash
python generate_session.py
```

Il programma chiede numero, codice Telegram ed eventuale password 2FA direttamente nel terminale. Non salvare OTP, password o StringSession nel repository: la StringSession va messa direttamente nel secret `TELEGRAM_USER_SESSION`.

## Alias multipack verificati

Un alias manuale va inserito solo dopo verifica dell'identità del prodotto:

```json
{
  "B0XXXXXXXX": {
    "ean": "8000000000000",
    "units_per_pack": 6,
    "note": "verificato manualmente"
  }
}
```

Il prezzo Amazon viene diviso per `units_per_pack` prima del confronto con il prezzo unitario Via Veneto.
