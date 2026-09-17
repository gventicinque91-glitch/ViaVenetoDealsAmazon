import asyncio
import getpass
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    raw_id = os.getenv("TELEGRAM_API_ID", "").strip() or input("Telegram API ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip() or getpass.getpass("Telegram API Hash (non verrà mostrato): ").strip()
    phone = input("Numero Telegram con prefisso internazionale (es. +39...): ").strip()

    client = TelegramClient(StringSession(), int(raw_id), api_hash)
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: input("Codice ricevuto da Telegram: ").strip(),
        password=lambda: getpass.getpass("Password 2FA Telegram (se richiesta): "),
    )
    session = client.session.save()
    me = await client.get_me()
    print("\nAutenticazione riuscita.")
    print(f"Account Telegram ID: {getattr(me, 'id', '')}")
    print("\nCOPIA LA STRINGA SEGUENTE DIRETTAMENTE NEL SECRET TELEGRAM_USER_SESSION.")
    print("NON INVIARLA IN CHAT E NON SALVARLA NEL REPOSITORY.\n")
    print(session)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
