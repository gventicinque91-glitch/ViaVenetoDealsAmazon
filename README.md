# ViaVenetoDealsAmazon

Servizio separato per monitorare le offerte Amazon pubblicate nei canali Telegram dell'account autorizzato, identificare i prodotti tramite codici GTIN/EAN/UPC verificati, confrontarli con il database Via Veneto e inviare un report Telegram.

## Funzioni

- Legge **solo quando richiesto** la cronologia del giorno del canale configurato (inizialmente `Caccia allo SCONTO 🎯`).
- Report automatico ogni giorno alle **20:00 Europe/Rome**.
- Report manuale con `/sconti` o `/report`, dalle 00:00 fino al momento del comando.
- Identificazione rigorosa: alias manuale verificato -> GTIN/EAN nel messaggio -> cache ASIN -> Keepa (se configurato).
- Nessun fuzzy match per decidere l'identità del prodotto.
- Confronto Via Veneto: **ultimo prezzo di acquisto confermato**; se assente, **ultimo listino/ordine**.
- Multipack gestiti solo tramite relazione verificata `ASIN -> EAN unitario + units_per_pack`.

## Secret / variabili richieste

Non inserire mai credenziali nei file del repository.

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_USER_SESSION` (StringSession Telethon, generata una sola volta con `generate_session.py`)
- `TELEGRAM_DEALS_BOT_TOKEN`
- `VIA_VENETO_PIN`

Consigliate:

- `TELEGRAM_REPORT_CHAT_ID` — chat privata in cui inviare il report automatico.
- `KEEPA_API_KEY` — usata solo per ASIN non ancora presenti in cache; Amazon.it usa Keepa domain `8`.

Variabili opzionali:

- `SOURCE_CHAT_TITLE` (default `Caccia allo SCONTO 🎯`)
- `VIA_VENETO_API_URL` (default `https://prezzi-mamma-api.gventicinque91.workers.dev`)
- `STATE_DIR` (default `./data`)
- `AUTO_REGISTER_CHAT` (default `true`)

## Avvio

```bash
python -m pip install -r requirements.txt
python app.py
```

Oppure con Docker:

```bash
docker build -t via-veneto-deals .
docker run --env-file .env via-veneto-deals
```

## Prima autenticazione Telegram

Eseguire localmente:

```bash
python generate_session.py
```

Il programma chiede il numero e il codice Telegram direttamente nel terminale e stampa una `StringSession`. **Non inviare il codice di login o la StringSession in chat.** Salvare la StringSession direttamente come secret `TELEGRAM_USER_SESSION` nel servizio che esegue il bot.

## Alias multipack verificati

Copiare `aliases.example.json` in `aliases.json` soltanto nell'ambiente di esecuzione oppure montarlo come file persistente. Un alias deve essere inserito solo dopo verifica manuale dell'identità:

```json
{
  "B0XXXXXXXX": {
    "ean": "8000000000000",
    "units_per_pack": 6,
    "note": "verificato manualmente"
  }
}
```

Il prezzo Amazon sarà diviso per `units_per_pack` prima del confronto con il prezzo unitario Via Veneto.
