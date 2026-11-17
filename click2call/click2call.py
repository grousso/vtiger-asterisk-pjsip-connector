#!/usr/bin/env python3
"""
click2call.py

Small Flask service that receives vtiger PBXManager "click-to-call" requests and
asks Asterisk (via AMI Originate) to ring the agent's PJSIP extension first and
then dial the customer's number.

vtiger calls:  <Asterisk Server IP>/makecall?event=OutgoingCall&secret=...&from=<extension>&to=<number>&context=...

All settings are read from environment variables (see ../pbx-connector.env.example).
"""

import hmac
import logging
import os
import re
import threading

from asterisk.manager import Manager
from flask import Flask, jsonify, request

app = Flask(__name__)

# Flask's built-in access log prints the full request URL, which contains the
# secret (vtiger sends it in the query string). Keep only warnings/errors.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ------ AMI settings ------
AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "")
AMI_PASS = os.environ.get("AMI_PASS", "")

# ------ Service settings ------
# Must match "Vtiger Secret Key" in vtiger: Settings -> PBXManager.
CLICK2CALL_SECRET = os.environ.get("CLICK2CALL_SECRET", "")
# Dialplan context used for the outgoing leg (FreePBX default: from-internal).
DIAL_CONTEXT = os.environ.get("CLICK2CALL_CONTEXT", "from-internal")
LISTEN_HOST = os.environ.get("CLICK2CALL_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("CLICK2CALL_LISTEN_PORT", "5000"))

mgr = Manager()
_mgr_lock = threading.Lock()


def connect_ami():
    try:
        mgr.connect(AMI_HOST, port=AMI_PORT)
        mgr.login(AMI_USER, AMI_PASS)
        print("[+] Connected to Asterisk AMI")
    except Exception as e:
        print(f"[-] AMI Connection Error: {e}")


def ensure_ami():
    """Reconnect if the AMI connection dropped (e.g. after an Asterisk restart)."""
    if not mgr.connected():
        print("[*] AMI not connected - reconnecting...")
        connect_ami()
    return mgr.connected()


def param(name):
    """vtiger may send the parameters in the query string or as form data."""
    return request.args.get(name) or request.form.get(name)


@app.route("/makecall", methods=["GET", "POST"])  # vtiger may use either method
def make_call():
    # Parameter names as sent by vtiger's PBXManager connector.
    number_to_call = param("to")
    extension = param("from")
    secret = param("secret") or ""

    if CLICK2CALL_SECRET and not hmac.compare_digest(secret, CLICK2CALL_SECRET):
        print("[-] Rejected request: bad or missing secret")
        return jsonify({"success": False, "message": "unauthorized"}), 401

    # Keep only characters that are valid in a dial string / extension.
    number_to_call = re.sub(r"[^0-9+*#]", "", number_to_call or "")
    extension = re.sub(r"[^0-9]", "", extension or "")

    print(f"[*] Request received: From {extension} to {number_to_call}")

    if not number_to_call or not extension:
        return jsonify({"success": False, "message": "Missing 'to' or 'from'"}), 400

    try:
        with _mgr_lock:
            if not ensure_ami():
                return jsonify({"success": False, "message": "AMI not connected"}), 503
            display_name = f"Vtiger: {number_to_call}"
            mgr.originate(
                channel=f"PJSIP/{extension}",
                exten=number_to_call,
                context=DIAL_CONTEXT,
                priority="1",
                caller_id=display_name,  # caller ID shown on the agent's phone
            )
        return jsonify({"success": True, "message": "Call initiated"}), 200
    except Exception as e:
        print(f"[-] Originate Error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == "__main__":
    if not CLICK2CALL_SECRET:
        print("[!] WARNING: CLICK2CALL_SECRET is empty - anyone who can reach this port can place calls.")
    connect_ami()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
