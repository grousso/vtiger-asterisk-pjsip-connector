#!/usr/bin/env python3
"""
vtiger_pbx_monitor.py

Single AMI listener for the vtiger PBXManager integration:
  - DialBegin (an agent extension starts ringing)  -> "ringing" event   -> live popup in vtiger
  - Cdr (end of call, ANSWERED)                     -> "completed" event -> call history
  - Cdr (NO ANSWER / BUSY / FAILED)                 -> "missed" event    -> missed-call logging

Features:
  1. Reconnect loop with exponential backoff around the AMI connection.
  2. Uses POST instead of GET (call details never end up in access logs / URLs).
  3. Shared-secret header for basic authentication towards the CRM endpoint.
  4. Retry + local dead-letter queue (JSONL) when delivery to the CRM fails,
     so that no call is silently lost.
  5. Warning (not a silent failure) when an extension is missing from EXTENSION_MAP.
  6. call_id (Asterisk Linkedid/uniqueid) in every payload, so the PHP endpoint can
     upsert (ringing -> completed/missed) the same record instead of creating duplicates.
  7. Proper logging (logging module) to a rotating log file and stdout.

All settings are read from environment variables (see ../pbx-connector.env.example).
"""

import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

import requests
from asterisk.manager import Manager

# ------ AMI settings ------
AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "")
AMI_PASS = os.environ.get("AMI_PASS", "")

# ------ CRM endpoint ------
# Full URL of crm-endpoint/pbx.php on your vtiger server.
API_BASE_URL = os.environ.get("PBX_API_URL", "https://your-vtiger-domain/pbx.php")
# Shared secret, so the PHP endpoint can reject requests without the correct header.
# Must be identical to PBX_API_SECRET in crm-endpoint/config.php.
API_SECRET = os.environ.get("PBX_API_SECRET", "")

# ------ Extension mapping (optional) ------
# Maps a PBX extension to a vtiger user id (webservice format "19xNN").
# Given as JSON in the PBX_EXTENSION_MAP environment variable, e.g.
#   PBX_EXTENSION_MAP='{"101": "19x5", "102": "19x8"}'
# Extensions not in the map are sent as-is; the PHP endpoint then looks up the
# vtiger user whose "CRM Phone Extension" (phone_crm_extension) field matches.
try:
    EXTENSION_MAP = json.loads(os.environ.get("PBX_EXTENSION_MAP", "{}"))
except json.JSONDecodeError:
    EXTENSION_MAP = {}

# ------ Local files (dead-letter queue + logs) ------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("PBX_DATA_DIR", BASE_DIR)
QUEUE_FILE = os.path.join(DATA_DIR, "pbx_failed_events.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "pbx_monitor.log")

# Avoid sending a duplicate "ringing" event for the same call leg.
_seen_ringing = set()
_seen_lock = threading.Lock()

# Ring groups / queues: an incoming call to a ring group with N extensions produces
# N separate Cdr events (one ANSWERED, the rest NO ANSWER), all linked by a common
# "Linkedid". We collect them for a few seconds and send ONE event to vtiger.
GROUP_WINDOW_SECONDS = int(os.environ.get("PBX_GROUP_WINDOW_SECONDS", "3"))
_pending_groups = {}  # linkedid -> {"legs": [dict, ...], "timer": threading.Timer}
_pending_lock = threading.Lock()

# ---------------------------------------------------------------- logging --
logger = logging.getLogger("pbx_monitor")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.addHandler(logging.StreamHandler())  # also to stdout, useful with journalctl


# ------------------------------------------------------------- HTTP layer --
_session = requests.Session()


def _send(payload: dict) -> bool:
    """Returns True if the event was delivered successfully, False otherwise."""
    try:
        resp = _session.post(
            API_BASE_URL,
            json=payload,
            headers={"X-PBX-Secret": API_SECRET},
            timeout=5,
        )
        if resp.status_code >= 400:
            logger.warning("CRM rejected event (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to send event to CRM: %s", exc)
        return False


def _queue_for_retry(payload: dict) -> None:
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def post_call_event(payload: dict) -> None:
    logger.info(
        "%s call: from=%s to=%s user=%s call_id=%s",
        payload.get("disposition", "?").upper(),
        payload.get("from"),
        payload.get("to"),
        payload.get("answered_by"),
        payload.get("call_id"),
    )
    if not _send(payload):
        logger.warning("Queued for retry: call_id=%s", payload.get("call_id"))
        _queue_for_retry(payload)


def retry_worker(interval_seconds: int = 30) -> None:
    """Background thread: every N seconds, try again to send the pending events."""
    while True:
        time.sleep(interval_seconds)
        if not os.path.exists(QUEUE_FILE):
            continue
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            continue

        still_pending = []
        sent = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if _send(payload):
                sent += 1
            else:
                still_pending.append(line)

        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(still_pending) + ("\n" if still_pending else ""))

        if sent:
            logger.info("Retry worker: sent %d pending events, %d remaining", sent, len(still_pending))


# ------------------------------------------------------------- helpers -----
def is_internal(number) -> bool:
    """Internal extensions are short numeric strings (up to 4 digits)."""
    return bool(number) and str(number).isdigit() and len(str(number)) <= 4


def map_user(ext) -> str:
    ext = str(ext)
    if ext not in EXTENSION_MAP:
        logger.debug("Extension '%s' not in EXTENSION_MAP - sent as-is (endpoint will look it up)", ext)
        return ext
    return EXTENSION_MAP[ext]


def extract_extension(channel_str):
    if not channel_str:
        return None
    try:
        # Takes "120" from "PJSIP/120-00000..."
        return channel_str.split("/")[1].split("-")[0]
    except Exception:
        return None


def resolve_direction(chan: str, dchan: str, src: str, dst: str):
    """Direction detection: Channel -> DestinationChannel -> fallback to src/dst."""
    ext_chan = extract_extension(chan)
    ext_dchan = extract_extension(dchan)

    if is_internal(ext_chan):
        return "outbound", dst, ext_chan, ext_chan
    if is_internal(ext_dchan):
        return "inbound", src, ext_dchan, ext_dchan
    if is_internal(src):
        return "outbound", src, dst, src
    return "inbound", src, dst, dst


# --------------------------------------------------------------- handlers --
def handle_ringing(event, manager):
    """
    Listens to DialBegin instead of Newstate/Newchannel: for every member of a ring
    group (e.g. 4 extensions in group 600) there is ONE DialBegin event with a clean
    separation of Channel (caller) / DestChannel (extension that is ringing) - without
    the ambiguity of Newchannel/Newstate, where the agent channel itself looked
    "internal" and was wrongly classified as outbound.
    """
    headers = dict(event.headers) if hasattr(event, "headers") else vars(event)
    if headers.get("Event") != "DialBegin":
        return

    dest_channel = headers.get("DestChannel", "")
    dest_ext = extract_extension(dest_channel)

    # We only care when one of our internal (agent) extensions is ringing.
    if not is_internal(dest_ext):
        return

    caller = headers.get("CallerIDNum") or headers.get("ConnectedLineNum")

    # Internal-to-internal call (e.g. a colleague calling a colleague) -
    # no customer popup needed in the CRM.
    if is_internal(caller):
        return

    dest_uniqueid = headers.get("DestUniqueid") or headers.get("DestUniqueID")
    if not dest_uniqueid:
        return

    with _seen_lock:
        if dest_uniqueid in _seen_ringing:
            return
        _seen_ringing.add(dest_uniqueid)

    payload = {
        "call_id": headers.get("Linkedid") or dest_uniqueid,
        "from": caller,
        "to": dest_ext,
        "direction": "inbound",
        "answered_by": map_user(dest_ext),
        "disposition": "ringing",
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    post_call_event(payload)


def handle_cdr(event, manager):
    """
    Does not send directly to the CRM. Collects the legs of the same logical call
    (common Linkedid - e.g. a ring group with 4 extensions) and, after
    GROUP_WINDOW_SECONDS, calls finalize_group() to send ONE event.
    """
    headers = dict(event.headers) if hasattr(event, "headers") else vars(event)
    if headers.get("Event") != "Cdr":
        return

    uniqueid = headers.get("UniqueID") or headers.get("Uniqueid")

    with _seen_lock:
        _seen_ringing.discard(uniqueid)  # the call has ended, clean up the dedupe set

    # Linkedid links all legs of the same call (e.g. the 4 legs of a ring group).
    # Fall back to uniqueid if it is missing for some reason (e.g. very old Asterisk).
    linkedid = headers.get("Linkedid") or headers.get("LinkedID") or uniqueid

    with _pending_lock:
        group = _pending_groups.setdefault(linkedid, {"legs": [], "timer": None})
        group["legs"].append(headers)

        if group["timer"] is None:
            timer = threading.Timer(GROUP_WINDOW_SECONDS, finalize_group, args=(linkedid,))
            timer.daemon = True
            group["timer"] = timer
            timer.start()


def finalize_group(linkedid):
    with _pending_lock:
        group = _pending_groups.pop(linkedid, None)
    if not group or not group["legs"]:
        return

    legs = group["legs"]

    # If at least one leg was answered, the call as a whole counts as "completed".
    answered_leg = next((l for l in legs if l.get("Disposition") == "ANSWERED"), None)
    chosen = answered_leg or legs[0]
    disposition = "completed" if answered_leg else "missed"

    src = chosen.get("Source")
    dst = chosen.get("Destination")
    chan = chosen.get("Channel", "")
    dchan = chosen.get("DestinationChannel", "")

    direction, real_from, real_to, ext_for_user = resolve_direction(chan, dchan, src, dst)

    if len(legs) > 1:
        logger.info(
            "Ring group call: linkedid=%s, %d legs, disposition=%s (used leg from %s)",
            linkedid, len(legs), disposition, chosen.get("Channel"),
        )

    payload = {
        "call_id": linkedid,
        "from": real_from,
        "to": real_to,
        "direction": direction,
        "answered_by": map_user(ext_for_user),
        "duration": chosen.get("Duration"),
        "disposition": disposition,
        "start_time": chosen.get("StartTime"),
        "answer_time": chosen.get("AnswerTime"),
        "end_time": chosen.get("EndTime"),
    }
    post_call_event(payload)


# ------------------------------------------------------------------- main --
def run():
    if not AMI_USER or not AMI_PASS:
        logger.error("AMI_USER / AMI_PASS are not set - see pbx-connector.env.example")
        return
    if not API_SECRET:
        logger.warning("PBX_API_SECRET is empty - the CRM endpoint will reject all events")

    backoff = 2
    while True:
        manager = Manager()
        try:
            manager.connect(AMI_HOST, AMI_PORT)
            manager.login(AMI_USER, AMI_PASS)
            manager.register_event("Cdr", handle_cdr)
            manager.register_event("DialBegin", handle_ringing)

            logger.info("Connected to AMI %s:%s - monitoring...", AMI_HOST, AMI_PORT)
            backoff = 2  # reset after a successful connection

            # pyst2 does not raise an exception here when the socket drops,
            # so check the connection state explicitly to trigger a reconnect.
            while manager.connected():
                time.sleep(1)
            raise ConnectionError("AMI connection lost")

        except KeyboardInterrupt:
            logger.info("Stopped by user...")
            manager.close()
            return

        except Exception as exc:
            logger.error("AMI connection error: %s - reconnecting in %ss", exc, backoff)
            try:
                manager.close()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    threading.Thread(target=retry_worker, daemon=True).start()
    run()
