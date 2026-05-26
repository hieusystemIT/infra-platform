import os
import yaml
import random
import hashlib
import logging
from datetime import datetime
from fastapi import FastAPI, Request
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
import pytz

# ============================================================
# SETUP LOGGING + FASTAPI
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================================================
# CONFIG
# ============================================================
API_ID        = int(os.environ["API_ID"])
API_HASH      = os.environ["API_HASH"]
TIMEZONE      = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
ONCALL_CONFIG = os.environ.get("ONCALL_CONFIG", "./oncall.yaml")

SESSION_STRING = os.environ.get("SESSION_STRING", "")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============================================================
# XÁC ĐỊNH NGƯỜI ĐANG TRỰC
# ============================================================
def parse_time(t: str):
    h, m = map(int, t.split(":"))
    return h * 60 + m


def load_config():
    with open(ONCALL_CONFIG) as f:
        return yaml.safe_load(f)


def get_oncall_users() -> list:
    config  = load_config()
    schedule = config["schedule"]

    tz      = pytz.timezone(TIMEZONE)
    now     = datetime.now(tz)
    current = now.hour * 60 + now.minute

    weekday = now.weekday() + 2
    if weekday > 8:
        weekday = weekday - 7

    matched = []
    for person in schedule:
        if weekday not in person.get("days", list(range(2, 9))):
            continue
        for slot in person["hours"]:
            s = parse_time(str(slot["start"]))
            e = parse_time(str(slot["end"]))
            in_range = (s <= current < e) if s < e else (current >= s or current < e)
            if in_range:
                matched.append(person)
                break
    return matched


# ============================================================
# TẠO NỘI DUNG TIN NHẮN
# ============================================================
def build_message(alerts: list) -> str:
    lines = ["🚨 DEVOPS ALERT\n"]
    for alert in alerts:
        name     = alert["labels"].get("alertname", "Unknown")
        severity = alert["labels"].get("severity", "unknown").upper()
        ns       = alert["labels"].get("namespace", "-")
        pod      = alert["labels"].get("pod", "-")
        summary  = alert["annotations"].get("summary", "No summary")
        lines.append(
            f"[{severity}] {name}\n"
            f"Namespace: {ns}\n"
            f"Pod: {pod}\n"
            f"{summary}"
        )
    return "\n\n".join(lines)


# ============================================================
# WEBHOOK
# ============================================================
@app.post("/webhook")
async def alertmanager_webhook(request: Request):
    body   = await request.json()
    alerts = [a for a in body.get("alerts", []) if a["status"] == "firing"]

    if not alerts:
        return {"status": "no_firing_alerts"}

    oncall_users = get_oncall_users()
    if not oncall_users:
        logger.warning("No on-call user matched current time slot")
        return {"status": "no_oncall_matched"}

    config       = load_config()
    group_chat_id = config.get("group_chat_id")
    message      = build_message(alerts)
    severities   = {a["labels"].get("severity", "") for a in alerts}
    alerted      = []

    try:
        # Gửi message vào GROUP
        if group_chat_id:
            group_entity = await client.get_input_entity(int(group_chat_id))
            await client.send_message(group_entity, message)
            logger.info(f"Message sent to group {group_chat_id}")

        # Gọi điện cho từng người trực nếu critical
        if "critical" in severities:
            for oncall in oncall_users:
                target = oncall["telegram"]
                try:
                    entity   = await client.get_input_entity(target)
                    g_a_hash = hashlib.sha256(os.urandom(256)).digest()
                    await client(RequestCallRequest(
                        user_id=entity,
                        random_id=random.randint(1, 0x7FFFFFFF),
                        g_a_hash=g_a_hash,
                        protocol=PhoneCallProtocol(
                            udp_p2p=True,
                            udp_reflector=True,
                            min_layer=65,
                            max_layer=92,
                            library_versions=["3.0.0"],
                        ),
                    ))
                    logger.info(f"Call initiated to {oncall['name']}")
                    alerted.append(oncall["name"])
                except Exception as e:
                    logger.error(f"Failed to call {oncall['name']}: {e}")

    except Exception as e:
        logger.error(f"Failed to send group message: {e}")
        return {"status": "error", "detail": str(e)}

    return {
        "status": "ok",
        "group_notified": bool(group_chat_id),
        "called": alerted,
        "alerts_count": len(alerts),
    }


# ============================================================
# HEALTH CHECK
# ============================================================
@app.get("/healthz")
async def health():
    return {"status": "ok"}


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================
@app.on_event("startup")
async def startup():
    await client.connect()
    logger.info("Telethon client connected")


@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()