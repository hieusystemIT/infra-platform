# tele-call

Hệ thống alert on-call tự động tích hợp với `kube-prometheus-stack` trên GKE.  
Khi có alert, app sẽ gửi message vào group Telegram và gọi điện cho người đang trực theo lịch.

---

## Kiến trúc

```
Prometheus → Alertmanager → alert-caller (FastAPI + Telethon)
                                  ↓
                         Gửi message vào group Telegram (tag người trực)
                                  ↓
                         Chờ 120s — check người trực đã đọc chưa
                                  ↓
                    Chưa đọc → Gọi điện (tối đa 3 lần)
                    Đã đọc  → Không gọi
```

---

## Tính năng

- Gửi message vào group Telegram, tag đúng người đang trực
- Chờ 120s check read receipt — nếu người trực đã đọc thì không gọi
- Gọi điện Telegram cho người trực nếu chưa đọc
- Retry tối đa 3 lần, chờ 5s giữa mỗi lần
- Tắt máy sớm (< 30s) → dừng retry
- Không nghe (timeout 60s) → gọi lại
- On-call rotation theo khung giờ và thứ trong tuần
- Hỗ trợ nhiều người trực cùng ca

---

## Cấu trúc project

```
tele-call/
├── main.py              # FastAPI + Telethon app
├── auth.py              # Tạo session file (chạy local 1 lần)
├── get_users.py         # Lấy Telegram user_id của từng thành viên
├── requirements.txt     # Python dependencies
├── Dockerfile           # Build image
└── helm/
    └── alert-caller/
        ├── Chart.yaml
        ├── values.yaml          # Config chính: image, oncall schedule
        └── templates/
            ├── namespace.yaml
            ├── configmap.yaml
            ├── deployment.yaml
            └── service.yaml
```

---

## Yêu cầu

- GKE cluster đang chạy
- `kube-prometheus-stack` đã deploy
- Docker + kubectl + helm đã cài
- Python 3.12+ + venv
- Tài khoản Telegram riêng (không dùng số cá nhân chính)

---

## Bước 1 — Lấy Telegram API credentials

1. Truy cập https://my.telegram.org
2. Đăng nhập bằng số điện thoại Telegram
3. Vào **API development tools** → tạo app mới
4. Lưu lại `api_id` và `api_hash`

---

## Bước 2 — Tạo session string (chạy local 1 lần)

```bash
# Tạo venv
python3 -m venv ~/venv-alert
source ~/venv-alert/bin/activate
pip install telethon

# Tạo session string và upload thẳng lên GKE
API_ID=YOUR_API_ID API_HASH=YOUR_API_HASH python3 -c "
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os, subprocess
client = TelegramClient(StringSession(), int(os.environ['API_ID']), os.environ['API_HASH'])
client.start()
session_str = client.session.save()
client.disconnect()
subprocess.run(['kubectl', 'create', 'secret', 'generic', 'telegram-session-string',
  '--from-literal=session_string=' + session_str,
  '-n', 'alert-caller'])
print('Done! Secret created.')
"
```

---

## Bước 3 — Lấy user_id của từng thành viên

```bash
source ~/venv-alert/bin/activate
python3 get_users.py
```

Nội dung `get_users.py`:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = YOUR_API_ID
API_HASH = "YOUR_API_HASH"
SESSION  = "YOUR_SESSION_STRING"

USERS = [
    "@username1",
    "@username2",
    "@username3",
    # thêm user mới vào đây
]

with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
    for username in USERS:
        user = client.get_entity(username)
        print(f"{user.id} | {username}")
```

---

## Bước 4 — Lấy chat_id của group Telegram

```bash
source ~/venv-alert/bin/activate
python3 -c "
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = YOUR_API_ID
API_HASH = 'YOUR_API_HASH'
SESSION  = 'YOUR_SESSION'

with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        print(dialog.id, '|', dialog.name)
"
```

Tìm tên group → lấy ID (thường bắt đầu bằng `-100` với supergroup).

> **Lưu ý:** Group phải là **supergroup** để hỗ trợ read receipt.  
> Cách convert: thêm `@GroupAnonymousBot` vào group → Telegram tự convert.

---

## Bước 5 — Cấu hình values.yaml

```yaml
image:
  repository: YOUR_REGISTRY/alert-caller
  tag: "v2"
  pullPolicy: Always

oncall:
  group_chat_id: "-1003940071694"   # chat_id của group (supergroup)
  schedule:
    - name: "Nguyen Van A"
      telegram: "@username_a"
      user_id: 123456789            # lấy từ get_users.py
      days: [2, 3, 4, 5, 6]        # 2=Thứ2 ... 8=Chủ nhật
      hours:
        - start: "08:30"
          end: "17:30"

    - name: "Tran Thi B"
      telegram: "@username_b"
      user_id: 987654321
      days: [2, 3, 4, 5, 6]
      hours:
        - start: "17:30"
          end: "23:30"
```

---

## Bước 6 — Upload secrets lên GKE

```bash
# Tạo namespace
kubectl create namespace alert-caller

# Label namespace cho Helm quản lý
kubectl label namespace alert-caller app.kubernetes.io/managed-by=Helm
kubectl annotate namespace alert-caller meta.helm.sh/release-name=alert-caller
kubectl annotate namespace alert-caller meta.helm.sh/release-namespace=alert-caller

# Telegram API credentials
kubectl create secret generic telegram-credentials \
  --from-literal=api_id=YOUR_API_ID \
  --from-literal=api_hash=YOUR_API_HASH \
  -n alert-caller
```

---

## Bước 7 — Build và push Docker image

```bash
# Build
docker build --no-cache -t YOUR_REGISTRY/alert-caller:v2 .

# Push
docker push YOUR_REGISTRY/alert-caller:v2
```

---

## Bước 8 — Deploy bằng Helm

```bash
helm upgrade --install alert-caller ./helm/alert-caller -n alert-caller
```

Kiểm tra pod:

```bash
kubectl get pod -n alert-caller -w
```

---

## Bước 9 — Cấu hình Alertmanager

Thêm vào file values của `kube-prometheus-stack`:

```yaml
alertmanager:
  enabled: true
  config:
    global:
      resolve_timeout: 5m
    route:
      group_by: ['alertname', 'namespace']
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 1h
      receiver: 'oncall-webhook'
    receivers:
      - name: 'oncall-webhook'
        webhook_configs:
          - url: 'http://alert-caller-svc.alert-caller.svc.cluster.local:8000/webhook'
            send_resolved: false
```

Apply:

```bash
helm upgrade kube-prometheus-stack ./kube-prometheus-stack \
  -n monitoring \
  -f monitor-values.yaml
```

---

## Test

```bash
# Port-forward
kubectl port-forward svc/alert-caller-svc 8000:8000 -n alert-caller

# Gửi fake alert
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "TestAlert",
        "severity": "critical",
        "namespace": "production",
        "pod": "api-server-xyz"
      },
      "annotations": {
        "summary": "Day la test alert"
      }
    }]
  }'
```

---

## Khi cần thêm/sửa người trực

```bash
# 1. Lấy user_id của người mới
python3 get_users.py

# 2. Sửa values.yaml thêm người mới vào oncall.schedule

# 3. Deploy lại (không cần build Docker)
helm upgrade --install alert-caller ./helm/alert-caller -n alert-caller
```

---

## Khi sửa code main.py

```bash
docker build --no-cache -t YOUR_REGISTRY/alert-caller:v2 .
docker push YOUR_REGISTRY/alert-caller:v2
kubectl rollout restart deployment alert-caller -n alert-caller
```

---

## Environment variables

| Variable | Default | Mô tả |
|---|---|---|
| `API_ID` | bắt buộc | Telegram API ID |
| `API_HASH` | bắt buộc | Telegram API Hash |
| `SESSION_STRING` | bắt buộc | Telegram session string |
| `TIMEZONE` | `Asia/Ho_Chi_Minh` | Timezone |
| `ONCALL_CONFIG` | `./oncall.yaml` | Path tới config file |
| `WAIT_BEFORE_CALL` | `120` | Giây chờ trước khi gọi |
| `CALL_TIMEOUT` | `60` | Giây chờ nghe máy |
| `MAX_RETRIES` | `3` | Số lần gọi tối đa |
| `RETRY_DELAY` | `5` | Giây chờ giữa các lần gọi |

---

## Lưu ý bảo mật

- **Không push** `alert.session` lên git — thêm vào `.gitignore`
- **Không hardcode** `api_id`, `api_hash`, `session_string` trong code
- Dùng **số điện thoại riêng** cho Telegram account này
- Revoke `api_hash` ngay nếu bị lộ tại https://my.telegram.org