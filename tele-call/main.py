import os
import yaml
import random
import hashlib
import logging
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.phone import RequestCallRequest, DiscardCallRequest
from telethon.tl.functions.messages import GetMessageReadParticipantsRequest
from telethon.tl.types import PhoneCallProtocol, PhoneCallDiscardReasonHangup
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
API_ID           = int(os.environ["API_ID"])
API_HASH         = os.environ["API_HASH"]
TIMEZONE         = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
ONCALL_CONFIG    = os.environ.get("ONCALL_CONFIG", "./oncall.yaml")
WAIT_BEFORE_CALL = int(os.environ.get("WAIT_BEFORE_CALL", "120"))
CALL_TIMEOUT     = int(os.environ.get("CALL_TIMEOUT", "60"))
MAX_RETRIES      = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY      = int(os.environ.get("RETRY_DELAY", "5"))

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
    config   = load_config()
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
# TẠO NỘI DUNG TIN NHẮN CÓ TAG NGƯỜI TRỰC
# ============================================================
def build_message(alerts: list, oncall_users: list) -> str:
    lines = ["DEVOPS ALERT\n"]
    tz = pytz.timezone("Asia/Ho_Chi_Minh")

    for alert in alerts:
        name     = alert["labels"].get("alertname", "Unknown")
        severity = alert["labels"].get("severity", "unknown").upper()
        ns       = alert["labels"].get("namespace")
        pod      = alert["labels"].get("pod")
        node     = alert["labels"].get("node")

        # Convert UTC sang giờ VN
        starts_raw = alert.get("startsAt", "")
        starts = ""
        if starts_raw and starts_raw != "0001-01-01T00:00:00Z":
            try:
                dt     = datetime.strptime(starts_raw[:19], "%Y-%m-%dT%H:%M:%S")
                dt     = pytz.utc.localize(dt).astimezone(tz)
                starts = dt.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                starts = ""

        detail = f"[{severity}] {name}\n"
        if ns:
            detail += f"Namespace: {ns}\n"
        if pod:
            detail += f"Pod: {pod}\n"
        if node:
            detail += f"Node: {node}\n"
        if starts:
            detail += f"Time: {starts}"

        lines.append(detail)

    mentions = " ".join([f"@{p['telegram'].lstrip('@')}" for p in oncall_users])
    lines.append(f"\n{mentions} vui lòng xem alert này!")

    return "\n\n".join(lines)


# ============================================================
# GỌI ĐIỆN VỚI RETRY
# ============================================================
async def call_with_retry(entity, name: str):
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"[CALL] {name} — attempt {attempt}/{MAX_RETRIES} starting")
        responded = False

        try:
            g_a_hash = hashlib.sha256(os.urandom(256)).digest()
            result   = await client(RequestCallRequest(
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
            logger.info(f"[CALL] {name} — ringing...")
            call_start    = asyncio.get_event_loop().time()
            discard_event = asyncio.Event()

            @client.on(events.Raw(types.UpdatePhoneCall))
            async def call_handler(event):
                if hasattr(event.phone_call, 'reason'):
                    discard_event.set()

            try:
                await asyncio.wait_for(discard_event.wait(), timeout=CALL_TIMEOUT)
                elapsed = asyncio.get_event_loop().time() - call_start
                logger.info(f"[CALL] {name} — call ended after {elapsed:.0f}s")

                if elapsed < 30:
                    logger.info(f"[CALL] {name} — DECLINED (< 30s) → stopping retry")
                    responded = True
                else:
                    logger.info(f"[CALL] {name} — TIMEOUT (>= 30s) → will retry")
                    responded = False

            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - call_start
                logger.info(f"[CALL] {name} — no answer after {elapsed:.0f}s → hanging up → will retry")
                try:
                    await client(DiscardCallRequest(
                        peer=result.phone_call,
                        duration=0,
                        reason=PhoneCallDiscardReasonHangup(),
                        connection_id=0,
                    ))
                except Exception:
                    pass
                responded = False

            finally:
                client.remove_event_handler(call_handler)

        except Exception as e:
            logger.error(f"[CALL] {name} — attempt {attempt} failed: {e}")
            responded = True

        if responded:
            logger.info(f"[CALL] {name} — stopping retry")
            break

        if attempt < MAX_RETRIES:
            logger.info(f"[CALL] {name} — waiting {RETRY_DELAY}s before retry {attempt + 1}...")
            await asyncio.sleep(RETRY_DELAY)

    logger.info(f"[CALL] {name} — done")


# ============================================================
# CHECK ĐÚNG NGƯỜI TRỰC ĐÃ ĐỌC CHƯA (read receipt)
# ============================================================
async def is_oncall_read(group_entity, msg_id: int, oncall_users: list) -> bool:
    try:
        result = await client(GetMessageReadParticipantsRequest(
            peer=group_entity,
            msg_id=msg_id,
        ))
        read_user_ids   = {r.user_id for r in result}
        oncall_user_ids = {person["user_id"] for person in oncall_users}
        logger.info(f"[READ] Message {msg_id} read by: {read_user_ids} | on-call: {oncall_user_ids}")

        overlap = read_user_ids & oncall_user_ids
        if overlap:
            logger.info(f"[READ] On-call user(s) {overlap} already read → skipping call")
            return True

        logger.info(f"[READ] No on-call user has read yet → will call")
        return False

    except Exception as e:
        logger.error(f"[READ] Failed to check read status: {e}")
        return False


# ============================================================
# XỬ LÝ ALERT: CHỜ 120S → CHECK ĐỌC → GỌI NẾU CHƯA ĐỌC
# ============================================================
async def handle_alert(alerts: list, group_entity, msg_id: int, oncall_users: list):
    logger.info(f"[ALERT] Waiting {WAIT_BEFORE_CALL}s before checking read status...")
    await asyncio.sleep(WAIT_BEFORE_CALL)

    read = await is_oncall_read(group_entity, msg_id, oncall_users)
    if read:
        logger.info("[ALERT] On-call user already read — skipping call")
        return

    logger.info("[ALERT] On-call user has not read — initiating calls")
    for oncall in oncall_users:
        target = oncall["telegram"]
        try:
            entity = await client.get_input_entity(target)
            asyncio.create_task(call_with_retry(entity, oncall["name"]))
            logger.info(f"[ALERT] Call task created for {oncall['name']}")
        except Exception as e:
            logger.error(f"[ALERT] Failed to get entity for {oncall['name']}: {e}")


# ============================================================
# WEBHOOK
# ============================================================
@app.post("/webhook")
async def alertmanager_webhook(request: Request):
    body   = await request.json()
    logger.info(f"[WEBHOOK] Raw payload: {body}")

    alerts = [a for a in body.get("alerts", []) if a["status"] == "firing"]

    if not alerts:
        return {"status": "no_firing_alerts"}

    config         = load_config()
    call_on_alerts = config.get("call_on_alerts", [])

    # Chỉ xử lý alert có trong call_on_alerts — bỏ qua hoàn toàn nếu không có
    alerts_to_process = [
        a for a in alerts
        if a["labels"].get("alertname") in call_on_alerts
    ]

    if not alerts_to_process:
        logger.info(f"[WEBHOOK] No alerts in call_on_alerts — ignoring")
        return {"status": "ignored"}

    oncall_users = get_oncall_users()
    if not oncall_users:
        logger.warning("[WEBHOOK] No on-call user matched current time slot")
        return {"status": "no_oncall_matched"}

    group_chat_id = config.get("group_chat_id")
    message       = build_message(alerts_to_process, oncall_users)
    alert_names   = [a["labels"].get("alertname") for a in alerts_to_process]

    try:
        if group_chat_id:
            group_entity = await client.get_entity(int(group_chat_id))
            sent_msg     = await client.send_message(group_entity, message)
            logger.info(f"[WEBHOOK] Message sent to group (msg_id={sent_msg.id})")

            asyncio.create_task(
                handle_alert(alerts_to_process, group_entity, sent_msg.id, oncall_users)
            )
            logger.info(f"[WEBHOOK] Will call in {WAIT_BEFORE_CALL}s if unread — alerts: {alert_names}")

    except Exception as e:
        logger.error(f"[WEBHOOK] Error: {e}")
        return {"status": "error", "detail": str(e)}

    return {
        "status": "ok",
        "group_notified": bool(group_chat_id),
        "will_call_in": f"{WAIT_BEFORE_CALL}s if unread",
        "oncall": [u["name"] for u in oncall_users],
        "alerts_processed": alert_names,
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