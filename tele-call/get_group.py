from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = 30416016
API_HASH = '12b42faaeb9fd08fc16910ec442fa8eb'
SESSION  = '1BVtsOJkBu6vov0IEXOzQD5Js4KM8U5PK8cZqkfXLykWsuYAFrGrjuBsbGPgvnhdRlGAKOJK5r_Z_8FZpY4wMFc2FD_8yjhU4v6aLwwEpNdtadhGm0raa3DeAvc_2P9VKaOizm02U6JBo_qBER1z8uXH34DUOeckUVMwQejL4VRWyqrYaoaWZra8PVLb64_HMlL8kXXXBeipvUo1oCTZYGb2TzvOBTm_GogjCohq7lMozkdeb2cK101PdvUaxFpzT2taZoGLWwBa-Pr45wnTacDFgGYecX60EZ2x_nLOi2GjrVr_ju_JUjd6K6HZFBAx7WwsBmUp4V06VcPTor1NjdiG6pP7UL2Y='

with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        if "Devops" in dialog.name:
            print(f"Name: {dialog.name}")
            print(f"ID: {dialog.id}")
            print(f"Type: {type(dialog.entity).__name__}")
            print(f"Input: {dialog.input_entity}")
