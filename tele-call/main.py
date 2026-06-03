import os
import re
import yaml
import random
import hashlib
import logging
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
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
ONCALL_CONFIG    = os.environ["ONCALL_CONFIG"]
WAIT_BEFORE_CALL = int(os.environ["WAIT_BEFORE_CALL"])
CALL_TIMEOUT     = int(os.environ["CALL_TIMEOUT"])
MAX_RETRIES      = int(os.environ["MAX_RETRIES"])
RETRY_DELAY      = int(os.environ["RETRY_DELAY"])
WEBHOOK_TOKEN    = os.environ.get("WEBHOOK_TOKEN", "")

SESSION_STRING = os.environ.get("SESSION_STRING", "")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Track người đang được gọi để tránh duplicate
_calling_users: set = set()


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
# VERIFY BEARER TOKEN
# ============================================================
def verify_token(request: Request):
    if not WEBHOOK_TOKEN:
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warning("[AUTH] Missing or invalid Authorization header")
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth_header.removeprefix("Bearer ").strip()
    if token != WEBHOOK_TOKEN:
        logger.warning("[AUTH] Invalid token")
        raise HTTPException(status_code=401, detail="Unauthorized")


# ============================================================
# CHECK ALERT CÓ CẦN XỬ LÝ KHÔNG
# ============================================================
def should_process(alert: dict, receiver: str, config: dict) -> bool:
    call_on_severities         = config.get("call_on_severities", [])
    call_on_receivers          = config.get("call_on_receivers", [])
    call_on_namespace_patterns = config.get("call_on_namespace_patterns", [])

    severity = alert["labels"].get("severity", "")
    if severity not in call_on_severities:
        logger.debug(f"[FILTER] Skip — severity '{severity}' not in call_on_severities")
        return False

    if receiver not in call_on_receivers:
        logger.debug(f"[FILTER] Skip — receiver '{receiver}' not in call_on_receivers")
        return False

    namespace = alert["labels"].get("namespace")
    if namespace:
        matched = any(re.match(p, namespace) for p in call_on_namespace_patterns)
        if not matched:
            logger.debug(f"[FILTER] Skip — namespace '{namespace}' not match patterns")
        return matched
    else:
        return True


# ============================================================
# TẠO NỘI DUNG TIN NHẮN CÓ TAG NGƯỜI TRỰC
# ============================================================
def build_message(alerts: list, oncall_users: list) -> str:
    lines = ["🚨 DEVOPS ALERT"]
    tz = pytz.timezone("Asia/Ho_Chi_Minh")

    important_labels = [
        "namespace", "pod", "node",
        "instance_name", "instance", "nodename", "ip",
        "service", "cluster", "datacenter",
        "rabbitmq_node", "container",
    ]

    for alert in alerts:
        name     = alert["labels"].get("alertname", "Unknown")
        severity = alert["labels"].get("severity", "unknown").upper()
        summary  = alert["annotations"].get("summary") or alert["annotations"].get("description")

        starts_raw = alert.get("startsAt", "")
        starts = ""
        if starts_raw and starts_raw != "0001-01-01T00:00:00Z":
            try:
                dt     = datetime.strptime(starts_raw[:19], "%Y-%m-%dT%H:%M:%S")
                dt     = pytz.utc.localize(dt).astimezone(tz)
                starts = dt.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                starts = ""

        detail = f"\n[{severity}] {name}"
        for label in important_labels:
            val = alert["labels"].get(label)
            if val:
                label_display = label.replace("_", " ").title()
                detail += f"\n{label_display}: {val}"
        if summary:
            detail += f"\nSummary: {summary}"
        if starts:
            detail += f"\nTime: {starts}"

        lines.append(detail)

    mentions = " ".join([f"@{p['telegram'].lstrip('@')}" for p in oncall_users])
    lines.append(f"\n{mentions} vui lòng xem alert này!")

    return "\n".join(lines)


# ============================================================
# CHECK NGƯỜI TRONG DANH SÁCH ĐÃ ĐỌC MESSAGE CHƯA
# ============================================================
async def is_read_by(group_entity, msg_id: int, user_ids: set) -> bool:
    try:
        result = await client(GetMessageReadParticipantsRequest(
            peer=group_entity,
            msg_id=msg_id,
        ))
        read_user_ids = {r.user_id for r in result}
        logger.info(f"[READ] Message {msg_id} read by: {read_user_ids} | checking: {user_ids}")

        overlap = read_user_ids & user_ids
        if overlap:
            logger.info(f"[READ] User(s) {overlap} already read → stopping call")
            return True

        logger.info(f"[READ] No one in list has read yet → continue calling")
        return False

    except Exception as e:
        logger.error(f"[READ] Failed to check read status: {e}")
        return False


# ============================================================
# CHECK ĐÚNG NGƯỜI TRỰC ĐÃ ĐỌC CHƯA (dùng trước khi gọi)
# ============================================================
async def is_oncall_read(group_entity, msg_id: int, oncall_users: list) -> bool:
    oncall_user_ids = {person["user_id"] for person in oncall_users}
    return await is_read_by(group_entity, msg_id, oncall_user_ids)


# ============================================================
# GỌI ĐIỆN VỚI RETRY + CHECK ĐỌC SAU MỖI LẦN TIMEOUT
# ============================================================
async def call_with_retry(entity, name: str, group_entity, msg_id: int, check_user_ids: set) -> bool:
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

            async def call_handler(event):
                if hasattr(event.phone_call, 'reason'):
                    discard_event.set()

            client.add_event_handler(call_handler, events.Raw(types.UpdatePhoneCall))

            try:
                await asyncio.wait_for(discard_event.wait(), timeout=CALL_TIMEOUT)
                elapsed = asyncio.get_event_loop().time() - call_start
                logger.info(f"[CALL] {name} — call ended after {elapsed:.0f}s")

                if elapsed < 30:
                    logger.info(f"[CALL] {name} — DECLINED (< 30s) → stopping retry")
                else:
                    logger.info(f"[CALL] {name} — ANSWERED (>= 30s) → stopping retry")
                responded = True

            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - call_start
                logger.info(f"[CALL] {name} — no answer after {elapsed:.0f}s → hanging up")
                try:
                    await client(DiscardCallRequest(
                        peer=result.phone_call,
                        duration=0,
                        reason=PhoneCallDiscardReasonHangup(),
                        connection_id=0,
                    ))
                except Exception:
                    pass

                # Check đọc sau mỗi lần timeout
                if await is_read_by(group_entity, msg_id, check_user_ids):
                    logger.info(f"[CALL] {name} — message read after timeout → stopping retry")
                    return True

                responded = False

            finally:
                client.remove_event_handler(call_handler)

        except Exception as e:
            logger.error(f"[CALL] {name} — attempt {attempt} failed: {e}")
            responded = True

        if responded:
            logger.info(f"[CALL] {name} — stopping retry")
            return True

        if attempt < MAX_RETRIES:
            logger.info(f"[CALL] {name} — waiting {RETRY_DELAY}s before retry {attempt + 1}...")
            await asyncio.sleep(RETRY_DELAY)

    logger.info(f"[CALL] {name} — done, no response after {MAX_RETRIES} attempts")
    return False


# ============================================================
# GỌI ĐIỆN CHO 1 NGƯỜI TRỰC + ESCALATION SANG BACKUP LIST
# Deduplication: nếu đang gọi người này rồi thì skip
# ============================================================
async def call_person_with_escalation(oncall: dict, group_entity, msg_id: int):
    name           = oncall["name"]
    target         = oncall["telegram"]
    oncall_user_id = oncall["user_id"]

    # Deduplication — nếu đang gọi người này rồi thì skip
    if name in _calling_users:
        logger.info(f"[CALL] {name} — already being called → skip duplicate")
        return

    _calling_users.add(name)
    logger.info(f"[CALL] {name} — added to calling list")

    try:
        # Check trực đọc trước khi gọi
        if await is_read_by(group_entity, msg_id, {oncall_user_id}):
            logger.info(f"[CALL] {name} — already read before calling → skip")
            return

        try:
            entity = await client.get_input_entity(target)
        except Exception as e:
            logger.error(f"[CALL] Failed to get entity for {name}: {e}")
            return

        # Gọi người trực — chỉ check người trực đọc
        responded = await call_with_retry(
            entity, name, group_entity, msg_id,
            check_user_ids={oncall_user_id}
        )

        if responded:
            logger.info(f"[ESCALATION] {name} responded — no escalation needed")
            return

        # Người trực không phản hồi → escalate qua từng backup trong list
        backup_list = oncall.get("backup", [])
        if not backup_list:
            logger.info(f"[ESCALATION] {name} no response and no backup configured — stopping")
            return

        valid_backups = [b for b in backup_list if b and b.get("telegram")]
        if not valid_backups:
            logger.info(f"[ESCALATION] {name} no valid backup configured — stopping")
            return

        for backup in valid_backups:
            backup_name = backup.get("name", "backup")
            backup_id   = backup.get("user_id")

            logger.info(f"[ESCALATION] {name} no response → escalating to backup: {backup_name}")

            check_ids = {oncall_user_id}
            if backup_id:
                check_ids.add(backup_id)

            if await is_read_by(group_entity, msg_id, check_ids):
                logger.info(f"[ESCALATION] Message already read before calling backup {backup_name} → stop")
                return

            try:
                backup_entity = await client.get_input_entity(backup["telegram"])
            except Exception as e:
                logger.error(f"[ESCALATION] Failed to get entity for backup {backup_name}: {e}")
                continue

            responded = await call_with_retry(
                backup_entity, backup_name, group_entity, msg_id,
                check_user_ids=check_ids
            )

            if responded:
                logger.info(f"[ESCALATION] Backup {backup_name} responded — stopping escalation")
                return

        logger.info(f"[ESCALATION] All backups exhausted for {name} — stopping")

    finally:
        _calling_users.discard(name)
        logger.info(f"[CALL] {name} — removed from calling list")


# ============================================================
# XỬ LÝ ALERT: CHỜ → CHECK ĐỌC → GỌI NẾU CHƯA ĐỌC
# ============================================================
async def handle_alert(group_entity, msg_id: int, oncall_users: list):
    logger.info(f"[ALERT] Waiting {WAIT_BEFORE_CALL}s before checking read status...")
    await asyncio.sleep(WAIT_BEFORE_CALL)

    read = await is_oncall_read(group_entity, msg_id, oncall_users)
    if read:
        logger.info("[ALERT] On-call user already read — skipping call")
        return

    logger.info("[ALERT] On-call user has not read — initiating calls")
    for oncall in oncall_users:
        asyncio.create_task(call_person_with_escalation(oncall, group_entity, msg_id))
        logger.info(f"[ALERT] Call task created for {oncall['name']}")


# ============================================================
# WEBHOOK
# ============================================================
@app.post("/webhook")
async def alertmanager_webhook(request: Request):
    verify_token(request)

    body     = await request.json()
    receiver = body.get("receiver", "")
    logger.info(f"[WEBHOOK] Receiver: {receiver} | Raw payload: {body}")

    alerts = [a for a in body.get("alerts", []) if a["status"] == "firing"]

    if not alerts:
        return {"status": "no_firing_alerts"}

    config = load_config()

    alerts_to_process = [a for a in alerts if should_process(a, receiver, config)]

    if not alerts_to_process:
        logger.info(f"[WEBHOOK] No alerts passed filter — receiver: {receiver}")
        return {"status": "ignored", "receiver": receiver}

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
                handle_alert(group_entity, sent_msg.id, oncall_users)
            )
            logger.info(f"[WEBHOOK] Will call in {WAIT_BEFORE_CALL}s if unread — alerts: {alert_names}")

    except Exception as e:
        logger.error(f"[WEBHOOK] Error: {e}")
        return {"status": "error", "detail": str(e)}

    return {
        "status": "ok",
        "receiver": receiver,
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
    logger.info("Loading dialogs...")
    await client.get_dialogs()
    logger.info("Dialogs loaded")
    logger.info("Telethon client connected")


@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()