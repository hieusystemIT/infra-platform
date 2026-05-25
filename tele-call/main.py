import os
import yaml
import random
import logging
from datetime import datetime
from fastapi import FastAPI, Request
from telethon import TelegramClient
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
# CONFIG - đọc từ environment variable, không hardcode
# ============================================================
API_ID        = int(os.environ["API_ID"])        # api_id từ my.telegram.org
API_HASH      = os.environ["API_HASH"]            # api_hash từ my.telegram.org
SESSION       = os.environ.get("SESSION_PATH", "./alert")   # path tới file alert.session
TIMEZONE      = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
ONCALL_CONFIG = os.environ.get("ONCALL_CONFIG", "./oncall.yaml")

# Khởi tạo Telethon client dùng user account thật
client = TelegramClient(SESSION, API_ID, API_HASH)


# ============================================================
# XÁC ĐỊNH NGƯỜI ĐANG TRỰC
# ============================================================
def parse_time(t: str):
    # Convert "08:30" -> 510 (phút) để so sánh dễ hơn
    h, m = map(int, t.split(":"))
    return h * 60 + m


def get_oncall_user():
    # Đọc file oncall.yaml
    with open(ONCALL_CONFIG) as f:
        schedule = yaml.safe_load(f)["schedule"]

    tz      = pytz.timezone(TIMEZONE)
    now     = datetime.now(tz)
    current = now.hour * 60 + now.minute  # thời điểm hiện tại tính bằng phút

    # Convert weekday: Python 0=Mon -> mình dùng 2=Thứ2 ... 8=Chủ nhật
    weekday = now.weekday() + 2
    if weekday > 8:
        weekday = weekday - 7

    for person in schedule:
        # Bỏ qua nếu hôm nay không phải ngày trực của người này
        if weekday not in person.get("days", list(range(2, 9))):
            continue
        for slot in person["hours"]:
            s = parse_time(str(slot["start"]))
            e = parse_time(str(slot["end"]))
            # Xử lý ca đêm qua ngày (vd: 23:00 -> 08:00)
            in_range = (s <= current < e) if s < e else (current >= s or current < e)
            if in_range:
                return person  # trả về người đang trực
    return None  # không có ai trực trong khung giờ này


# ============================================================
# TẠO NỘI DUNG TIN NHẮN TỪ ALERT
# ============================================================
def build_message(alerts: list, oncall_name: str) -> str:
    lines = [f"ALERT - On-call: {oncall_name}\n"]
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
# WEBHOOK - nhận alert từ Alertmanager
# ============================================================
@app.post("/webhook")
async def alertmanager_webhook(request: Request):
    body   = await request.json()

    # Chỉ xử lý alert đang firing, bỏ qua resolved
    alerts = [a for a in body.get("alerts", []) if a["status"] == "firing"]

    if not alerts:
        return {"status": "no_firing_alerts"}

    # Xác định người đang trực theo giờ hiện tại
    oncall = get_oncall_user()
    if not oncall:
        logger.warning("No on-call user matched current time slot")
        return {"status": "no_oncall_matched"}

    message    = build_message(alerts, oncall["name"])
    target     = oncall["telegram"]
    severities = {a["labels"].get("severity", "") for a in alerts}

    logger.info(f"Alerting {oncall['name']} ({target})")

    try:
        entity = await client.get_input_entity(target)

        # Gửi tin nhắn Telegram
        await client.send_message(entity, message)
        logger.info("Message sent")

        # Nếu có alert critical -> gọi điện thêm
        if "critical" in severities:
            await client(RequestCallRequest(
                user_id=entity,
                random_id=random.randint(1, 0x7FFFFFFF),
                g_a_hash=bytes(256),
                protocol=PhoneCallProtocol(
                    udp_p2p=True,
                    udp_reflector=True,
                    min_layer=65,
                    max_layer=92,
                    library_versions=["3.0.0"],
                ),
            ))
            logger.info("Call initiated")

    except Exception as e:
        logger.error(f"Failed to alert: {e}")
        return {"status": "error", "detail": str(e)}

    return {
        "status": "ok",
        "alerted": oncall["name"],
        "alerts_count": len(alerts),
        "called": "critical" in severities,
    }


# ============================================================
# HEALTH CHECK - Kubernetes dùng để check app còn sống không
# ============================================================
@app.get("/healthz")
async def health():
    return {"status": "ok"}


# ============================================================
# STARTUP / SHUTDOWN - kết nối Telethon khi app khởi động
# ============================================================
@app.on_event("startup")
async def startup():
    await client.connect()
    logger.info("Telethon client connected")


@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()