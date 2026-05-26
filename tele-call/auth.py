import os
from telethon.sync import TelegramClient

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = "alert"

print("=== Telegram Session Setup ===\n")

with TelegramClient(SESSION, API_ID, API_HASH) as client:
    me = client.get_me()
    print(me.first_name)
    print(me.username)
