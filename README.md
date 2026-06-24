# mimir-display-smartTV-ultra

Mimir push adapter for the **GEEKMAGIC SmallTV Ultra** — a 240×240 color LCD desktop display built on ESP32. No custom firmware required; images are pushed over WiFi using the device's built-in HTTP API.

## How it works

The adapter runs as a small Python service alongside the Mimir stack:

1. Calls a Mimir channel's `request-image` endpoint at 240×240
2. Uploads the JPEG to the SmallTV Ultra via `POST /doUpload?dir=/image/`
3. Waits for `PUSH_INTERVAL` seconds, then repeats

The device stays in **Photo Album** mode, cycling to the newest image automatically.

---

## Device

**Model:** GEEKMAGIC SmallTV Ultra (firmware V9.0.40+)  
**Firmware source:** https://github.com/GeekMagicClock/smalltv-ultra  
**Display:** 240×240 color LCD  
**Connection:** WiFi only (USB is charge-only on this unit)

### Discovered API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/set?theme=3` | GET | Switch to Photo Album mode |
| `/set?autoplay=1&i_i=<secs>` | GET | Enable autoplay at interval |
| `/doUpload?dir=/image/` | POST | Upload a file (field: `file`) |
| `/delete?file=/image/<name>` | GET | Delete a file |
| `/filelist?dir=/image/` | GET | List files |
| `/set?clear=image` | GET | Delete all images |
| `/update` | GET/POST | OTA firmware update |
| `/v.json` | GET | Model and firmware version |
| `/app.json` | GET | Current theme |
| `/album.json` | GET | Autoplay settings |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Mimir URL, channel ID, and device IP
```

### Run

```bash
# Load config from .env (or set env vars directly)
export $(cat .env | xargs)
python push.py
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MIMIR_API` | `http://localhost:8000` | Mimir server base URL |
| `CHANNEL_ID` | *(required)* | Mimir channel plugin ID, e.g. `com.metmuseum.art` |
| `SUBCHANNEL_ID` | *(empty)* | Sub-channel / gallery ID (optional) |
| `DEVICE_IP` | *(required)* | SmallTV Ultra IP address |
| `PUSH_INTERVAL` | `300` | Seconds between image pushes |
| `FILENAME` | `mimir.jpg` | Filename stored on the device |

---

## Connecting to the device

The USB port on this unit is **charge-only** — no serial connection is available. All communication is over WiFi.

Access the device's built-in web UI at `http://<DEVICE_IP>/` to configure WiFi, themes, and brightness.

### WSL2 note

WSL2 can reach devices on the local network directly if mirrored networking is enabled. If `ping 192.168.1.x` works in WSL, no extra setup is needed. For USB serial access to other ESP32 boards, see [`mimir-display-esp32/README.md`](../mimir-display-esp32/README.md).

---

## Limitations vs. full Mimir display client

| Feature | This adapter | Full client (MagTag, Electron) |
|---|---|---|
| Image display | Yes | Yes |
| Push on scene change | No — periodic poll | Yes (MQTT) |
| Scene assignment via UI | No | Yes |
| Registration / display tracking | No | Yes |
| Heartbeat / online status | No | Yes |

The periodic push is fine for content that changes on a schedule (art, weather, news). For instant-on-scene-change behavior, the device would need custom firmware implementing the Mimir MQTT protocol.
