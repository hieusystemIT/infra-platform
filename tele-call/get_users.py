from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = 31705467
API_HASH = '18a57dad4333404a859150f52f2f5190'
SESSION  = '1BVtsOL0Bu13Fds8qChOH9SSCU479_vCDEfUs-RAP7LD3CiXI0uMNQxPRstvKjwexMQJ4ck_scrskBTGwJxg5HfriWbcuMG6ihh1E8-Q8G5qmuTTM7ZjOtxU99jJ-OrB5E9kvUWM6kTFrSRx_M_He3WDzJ3i1WravBZ1siDOsZLec0by_CKr_YDLUGxV5W0fqI-BWEqANNvDZlrPPPwgXhQ7gvK3mxpbUKrk7ZWY2DEf4NDkqefsZ_3MvZYyJ-Kist0_GtoWKXUMjPfzqsjMOsVcaywQBUZhVNeBZYU3Pr_WxC42hXePr02sRA0UsTXjWuIX5y5Nd6kx7Rrd7Y6IPpVCZGI7Zl_U='

USERS = [
    "@duchieu246",
    "@xuanquynhqbf",
    "@tuanphi2408",
    # add new usernames here
]

with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
    for username in USERS:
        user = client.get_entity(username)
        print(f"{user.id} | {username}")
