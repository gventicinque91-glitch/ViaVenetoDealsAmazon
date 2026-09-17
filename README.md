# ViaVenetoDealsAmazon

Servizio separato per monitorare le offerte Amazon pubblicate nei canali Telegram dell'account autorizzato, identificare i prodotti tramite codici GTIN/EAN/UPC verificati, confrontarli con il database Via Veneto e inviare un report Telegram.

## Funzioni

- Legge la cronologia del giorno delle fonti Telegram configurate.
- Report automatico ogni giorno alle **20:00 Europe/Rome**.
- Report manuale con `/sconti` o `/report`, dalle 00:00 fino al momento del comando.
- `/status` mostra lo stato dell'ultimo report.
- Identificazione rigorosa: alias verificato -> GTIN/EAN nel messaggio -> cache ASIN -> resolver esterni verificati.
- Nessun fuzzy match verso Via Veneto per decidere l'identità del prodotto: il confronto finale avviene per codice esatto.
- Confronto Via Veneto: **ultimo prezzo di acquisto confermato**; se assente, **ultimo listino/ordine**.
- Multipack normalizzati solo quando il rapporto confezione/unità è verificato.

## Architettura attiva

La configurazione principale non richiede un server sempre acceso né secret Cloudflare aggiuntivi.

1. `.github/workflows/bot-poller.yml` controlla periodicamente i comandi ricevuti dal bot Telegram.
2. `bot_poller.py` gestisce `/start`, `/sconti`, `/report` e `/status`.
3. Per `/sconti` viene lanciato `.github/workflows/report.yml` tramite il `GITHUB_TOKEN` temporaneo della stessa GitHub Action.
4. `.github/workflows/daily-report-dispatch.yml` avvia il report automatico quando sono le 20:00 in `Europe/Rome`, gestendo automaticamente ora legale/solare.
5. `run_report_once.py` legge Telegram con Telethon, genera il report e lo invia tramite il bot.

Il polling dei comandi è ogni 5 minuti, quindi un comando Telegram può impiegare alcuni minuti a partire. Anche gli schedule GitHub possono avere un piccolo ritardo rispetto all'orario nominale.

I file Cloudflare Worker restano nel repository come alternativa per una futura modalità webhook immediata, ma non sono necessari per la configurazione GitHub-only.

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
