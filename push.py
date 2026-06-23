#!/usr/bin/env python3
"""
Mimir display client for the GEEKMAGIC SmallTV Ultra.

Registers itself as a display in Mimir, then listens for scene assignments
and display_image commands via MQTT. When an image arrives it pushes it to
the SmallTV Ultra over its built-in HTTP API.

Configuration (.env file or environment variables):
  MIMIR_API         Mimir server base URL            [http://localhost:8000]
  MQTT_BROKER       MQTT broker host                  [localhost]
  MQTT_PORT         MQTT broker port                  [1883]
  MQTT_USERNAME     MQTT username                     [optional]
  MQTT_PASSWORD     MQTT password                     [optional]
  DEVICE_IP         SmallTV Ultra IP address          [required]
  DEVICE_NAME       Friendly name shown in Mimir UI   [SmallTV Ultra]
  DEVICE_HOSTNAME   Hostname used for MQTT routing    [smarttv-ultra]
  REGISTRATION_FILE Path to persist registration      [registration.json]
"""

import io
import json
import logging
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [smarttv] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MIMIR_API         = os.getenv("MIMIR_API", "http://localhost:8000")
MQTT_BROKER       = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT         = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME     = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD     = os.getenv("MQTT_PASSWORD") or None
DEVICE_IP         = os.getenv("DEVICE_IP", "")
DEVICE_NAME       = os.getenv("DEVICE_NAME", "SmallTV Ultra")
DEVICE_HOSTNAME   = os.getenv("DEVICE_HOSTNAME", "smarttv-ultra")
REGISTRATION_FILE = os.getenv("REGISTRATION_FILE", "registration.json")

DISPLAY_W = 240
DISPLAY_H = 240
FILENAME  = "mimir.jpg"
HEARTBEAT_INTERVAL = 30  # seconds

CLIENT_VERSION = "1.0.0"

# ── Registration ─────────────────────────────────────────────────────────────

def load_registration() -> dict | None:
    try:
        with open(REGISTRATION_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("Could not load registration: %s", exc)
        return None


def save_registration(data: dict) -> None:
    with open(REGISTRATION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Registration saved to %s", REGISTRATION_FILE)


def register_with_mimir() -> dict:
    """Get a provision token and register as a new display. Returns saved reg dict."""
    log.info("Registering with Mimir at %s", MIMIR_API)

    bundle_resp = requests.get(f"{MIMIR_API}/api/displays/provision-bundle", timeout=10)
    bundle_resp.raise_for_status()
    reg_token = bundle_resp.json()["payload"]["reg_token"]

    reg_resp = requests.post(
        f"{MIMIR_API}/api/displays/provision-register",
        json={
            "reg_token": reg_token,
            "device_id": DEVICE_HOSTNAME,
            "hostname": DEVICE_HOSTNAME,
            "capabilities": {
                "resolution": [DISPLAY_W, DISPLAY_H],
                "native_resolution": [DISPLAY_W, DISPLAY_H],
                "orientation": "square",
                "redis_distribution": False,
                "content_claiming": False,
            },
            "metadata": {
                "name": DEVICE_NAME,
                "client_version": CLIENT_VERSION,
            },
        },
        timeout=10,
    )
    reg_resp.raise_for_status()
    display = reg_resp.json()
    data = {
        "display_id": display["id"],
        "hostname": DEVICE_HOSTNAME,
    }
    save_registration(data)
    log.info("Registered as display '%s' (id=%s)", DEVICE_NAME, display["id"])
    return data


def ensure_registered() -> dict:
    reg = load_registration()
    if reg:
        log.info("Loaded existing registration (id=%s)", reg["display_id"])
        return reg
    return register_with_mimir()


# ── SmallTV HTTP API ──────────────────────────────────────────────────────────

def clear_all_images() -> None:
    requests.get(f"http://{DEVICE_IP}/set?clear=image", timeout=10)


def delete_file(filename: str) -> None:
    requests.get(f"http://{DEVICE_IP}/delete?file=/image/{filename}", timeout=5)


def upload_image(image_bytes: bytes) -> bool:
    url = f"http://{DEVICE_IP}/doUpload?dir=/image/"
    files = {"file": (FILENAME, io.BytesIO(image_bytes), "image/jpeg")}
    r = requests.post(url, files=files, timeout=15)
    return r.status_code == 200


def show_image() -> None:
    """Pin the uploaded file to the display immediately."""
    base = f"http://{DEVICE_IP}"
    requests.get(f"{base}/set?theme=3", timeout=5)
    requests.get(f"{base}/set?img=/image/{FILENAME}", timeout=5)
    requests.get(f"{base}/set?autoplay=0", timeout=5)


def push_image(image_bytes: bytes) -> bool:
    delete_file(FILENAME)
    ok = upload_image(image_bytes)
    if ok:
        show_image()
    return ok


# ── MQTT client ───────────────────────────────────────────────────────────────

class DisplayClient:
    def __init__(self, hostname: str):
        self.hostname = hostname
        self.topic_cmd       = f"mimir/{hostname}/cmd"
        self.topic_status    = f"mimir/{hostname}/status"
        self.topic_heartbeat = f"mimir/{hostname}/heartbeat"
        self.topic_evt       = f"mimir/{hostname}/evt"
        self._mqtt: mqtt.Client | None = None
        self._connected = threading.Event()

    # ── MQTT lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self.hostname,
            protocol=mqtt.MQTTv311,
        )
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message
        client.will_set(
            self.topic_status,
            json.dumps({"device_id": self.hostname, "status": "offline"}),
            retain=True,
        )
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        self._mqtt = client
        client.loop_start()
        self._connected.wait(timeout=10)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            log.error("MQTT connect failed rc=%s", reason_code)
            return
        log.info("MQTT connected to %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(self.topic_cmd, qos=1)
        self._connected.set()
        self._publish_status("online")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        log.warning("MQTT disconnected (rc=%s) — will auto-reconnect", reason_code)
        self._connected.clear()

    # ── Command handling ──────────────────────────────────────────────────────

    def _on_message(self, client, userdata, msg) -> None:
        try:
            obj = json.loads(msg.payload)
        except Exception:
            return
        cmd_type = obj.get("type")
        log.info("Command received: %s", cmd_type)

        if cmd_type == "display_image":
            threading.Thread(target=self._handle_display_image, args=(obj,), daemon=True).start()
        elif cmd_type == "set_scene":
            self._publish_event("ack", what="set_scene", scene_id=obj.get("scene_id", ""))
        elif cmd_type == "ping":
            self._publish_event("ack", echo="ping")
        elif cmd_type == "refresh":
            self._publish_event("ack", what="refresh")
        else:
            log.debug("Unhandled command type: %s", cmd_type)

    def _handle_display_image(self, cmd: dict) -> None:
        image_url    = cmd.get("image_url", "")
        assignment_id = cmd.get("assignment_id", "")
        log.info("Fetching image from %s", image_url)
        try:
            r = requests.get(image_url, timeout=30)
            r.raise_for_status()
            image_bytes = r.content
        except Exception as exc:
            log.error("Image download failed: %s", exc)
            self._publish_event("error", error=str(exc), assignment_id=assignment_id)
            return

        log.info("Pushing %d bytes to SmallTV", len(image_bytes))
        try:
            ok = push_image(image_bytes)
        except Exception as exc:
            log.error("SmallTV push failed: %s", exc)
            self._publish_event("error", error=str(exc), assignment_id=assignment_id)
            return

        if ok:
            log.info("Display updated (assignment=%s)", assignment_id)
            self._publish_event("rendered", what="image", assignment_id=assignment_id)
        else:
            self._publish_event("error", error="upload_failed", assignment_id=assignment_id)

    # ── Presence ──────────────────────────────────────────────────────────────

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _publish_status(self, status: str) -> None:
        payload = {
            "device_id": self.hostname,
            "hostname": self.hostname,
            "status": status,
            "timestamp": self._now_iso(),
            "res": [DISPLAY_W, DISPLAY_H],
            "ori": "square",
            "registration_state": "finalized",
        }
        if self._mqtt:
            self._mqtt.publish(self.topic_status, json.dumps(payload), retain=True)

    def _publish_heartbeat(self) -> None:
        payload = {
            "device_id": self.hostname,
            "hostname": self.hostname,
            "timestamp": self._now_iso(),
            "registration_state": "finalized",
            "cap": {
                "res": [DISPLAY_W, DISPLAY_H],
                "ori": "square",
                "client_version": CLIENT_VERSION,
                "redis_distribution": False,
                "content_claiming": False,
            },
            "res": [DISPLAY_W, DISPLAY_H],
            "rot": 0,
        }
        if self._mqtt:
            self._mqtt.publish(self.topic_heartbeat, json.dumps(payload))

    def _publish_event(self, evt_type: str, **fields) -> None:
        doc = {"type": evt_type, "t": str(int(time.time()))}
        doc.update(fields)
        if self._mqtt:
            self._mqtt.publish(self.topic_evt, json.dumps(doc))

    def heartbeat_loop(self) -> None:
        while True:
            self._publish_status("online")
            self._publish_heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not DEVICE_IP:
        log.error("DEVICE_IP is required")
        sys.exit(1)

    reg = ensure_registered()
    hostname = reg["hostname"]

    log.info("Clearing demo images from device")
    try:
        clear_all_images()
    except Exception as exc:
        log.warning("Could not clear images: %s", exc)

    client = DisplayClient(hostname)
    log.info("Connecting to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
    client.start()

    log.info(
        "Display client running  name='%s'  hostname=%s  device=%s",
        DEVICE_NAME, hostname, DEVICE_IP,
    )
    log.info("Assign a scene to this display in the Mimir Screens view")

    client.heartbeat_loop()  # blocks forever


if __name__ == "__main__":
    main()
