# vtiger ↔ Asterisk (PJSIP) connector

A lightweight replacement for the official vtiger CRM Asterisk connector, built for
**Asterisk / FreePBX installations that use PJSIP** (e.g. SIP trunks that are PJSIP-only).

The official vtiger connectors are outdated and parse `SIP/` channel names, so they
break with `PJSIP/` channels. This project talks to Asterisk directly through the
**Asterisk Manager Interface (AMI)** and to vtiger through its standard
**webservice API**. No vtiger core files need to be patched.

Tested with **vtiger CRM 7.2** (PBXManager module) and FreePBX/Asterisk with PJSIP.

## Features

- **Incoming-call popup** in vtiger while the agent's phone is ringing.
- **Call history**: every answered, missed and outbound call is logged in PBXManager and
  linked to the matching Contact (looked up by mobile number).
- **Ring groups / queues**: all legs of a ring-group call are merged into **one** record.
- **Click-to-call** from vtiger: the agent's phone rings first, then the customer is dialed.
- **No lost calls**: if the CRM is unreachable, events are queued on disk and retried.
- Automatic AMI reconnect with exponential backoff.

## How it works

```
Call logging & popup:
  Asterisk ──AMI events──▶ vtiger_pbx_monitor.py ──HTTPS POST + secret──▶ pbx.php ──webservice──▶ vtiger

Click-to-call:
  vtiger ──/makecall?from=&to=&secret=──▶ click2call.py ──AMI Originate──▶ Asterisk
```

| Component | Runs on | What it does |
|---|---|---|
| `pbx-monitor/vtiger_pbx_monitor.py` | PBX server | Listens to AMI events and sends `ringing` / `completed` / `missed` events to the CRM endpoint. |
| `crm-endpoint/pbx.php` | vtiger server | Receives the events and creates or updates the PBXManager record (same record from ringing → completed/missed, keyed by the Asterisk Linkedid). |
| `click2call/click2call.py` | PBX server | Small Flask service that vtiger calls for click-to-call; it issues an AMI `Originate`. |

## Requirements

- Asterisk 13+ (FreePBX works) with PJSIP extensions, AMI enabled and CDR events sent to AMI.
- Python 3.8+ on the PBX server.
- vtiger CRM 7.x with the PBXManager module enabled, PHP with the `curl` extension.

## Installation

### 1. Create an AMI user on the PBX

On FreePBX, add this to `/etc/asterisk/manager_custom.conf` (on plain Asterisk use `manager.conf`):

```ini
[vtiger]
secret = a-long-random-password
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = call,cdr
write = call,originate
```

Make sure CDR events reach AMI (`/etc/asterisk/cdr_manager.conf` → `enabled = yes`; FreePBX enables it by default). Then reload:

```bash
asterisk -rx "manager reload"
asterisk -rx "module reload cdr_manager.so"
```

### 2. Install the Python services on the PBX

```bash
git clone https://github.com/grousso/vtiger-asterisk-pjsip-connector /opt/vtiger-asterisk-pjsip-connector
cd /opt/vtiger-asterisk-pjsip-connector
python3 -m venv venv
venv/bin/pip install -r requirements.txt

mkdir -p /var/lib/pbx-connector
cp pbx-connector.env.example /etc/pbx-connector.env
chmod 600 /etc/pbx-connector.env
nano /etc/pbx-connector.env        # fill in AMI_PASS, PBX_API_URL, PBX_API_SECRET, CLICK2CALL_SECRET ...
```

Generate the secrets with `openssl rand -hex 32`.

### 3. Install the CRM endpoint on the vtiger server

Copy the `crm-endpoint/` folder to a directory served by your web server, for example
`/var/www/html/pbx-endpoint/`, then:

```bash
cd /var/www/html/pbx-endpoint
cp config.example.php config.php
nano config.php                     # api_secret, vtiger_ws_url, vtiger_user, vtiger_access_key
```

- `api_secret` must be **identical** to `PBX_API_SECRET` on the PBX.
- The access key is in vtiger: *My Preferences → User Advanced Options → Access Key*.
- The included `.htaccess` (Apache) lets the web server serve **only** `pbx.php`, so
  `config.php` and the log file cannot be downloaded. On Nginx, add an equivalent rule,
  or set `log_file` to a path outside the web root.
- The web server user must be able to write the log file.

Test it from the PBX (a `401 unauthorized` response means the URL works but the secret is wrong):

```bash
curl -s -X POST "$PBX_API_URL" -H "X-PBX-Secret: $PBX_API_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"call_id":"test-1","from":"6900000000","to":"101","direction":"inbound","answered_by":"101","disposition":"missed"}'
```

### 4. Configure vtiger

1. **Settings → PBXManager** (Integration):
   - *Asterisk Server IP*: the click-to-call URL, e.g. `http://pbx.example.com:5000`
   - *Vtiger Secret Key*: the same value as `CLICK2CALL_SECRET`
   - *Outbound Context*: `from-internal` (FreePBX)
2. For every user, set **CRM Phone Extension** (*My Preferences*) to their PJSIP extension.
   This is how calls are matched to users and how the popup knows whom to show.
   (Alternatively, set `PBX_EXTENSION_MAP` in the env file.)
3. Contacts are matched by the **Mobile** field, so store numbers the way the
   PBX presents them (e.g. `6912345678`, or with the country code if your trunk sends one).

### 5. Start the services

```bash
cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vtiger-pbx-monitor vtiger-click2call
journalctl -u vtiger-pbx-monitor -f
```

Make a test call: the popup should appear in vtiger while the phone rings, and the call
should show up in PBXManager after hang-up.

## Security notes

- **Restrict access to the click-to-call port (5000).** Anyone who can reach it and knows
  the secret can place calls through your PBX. Allow only your vtiger server's IP in the
  firewall, or set `CLICK2CALL_LISTEN_HOST=127.0.0.1` and put a reverse proxy with HTTPS in front.
  The service warns at startup if `CLICK2CALL_SECRET` is empty.
- Use **HTTPS** for `PBX_API_URL` when the PBX and the CRM are on different servers.
- `berliCRM_WS_Curl_Class.php` (third-party) disables TLS certificate checks for its
  requests to `webservice.php`. This is harmless with the default local URL
  (`http://127.0.0.1/...`); if you point `vtiger_ws_url` to a remote HTTPS server, be aware of it.
- Never commit `config.php` or the `.env` file - both are in `.gitignore`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Popup shows the caller as **HIDDEN** | The PBXManager record has `customer` set without `customertype`. vtiger's permission check in `searchIncomingCalls()` needs both. `pbx.php` always sets them together. |
| No popup at all | Check that the user's *CRM Phone Extension* matches the PJSIP extension, and `journalctl -u vtiger-pbx-monitor` shows `RINGING call`. |
| Endpoint returns **500 "customernumber does not have a value"** in a loop | Old events without a caller number are stuck in `pbx_failed_events.jsonl`. Empty the file: `> /var/lib/pbx-connector/pbx_failed_events.jsonl`. |
| Endpoint returns **401** | `PBX_API_SECRET` (PBX) and `api_secret` (config.php) differ, or a proxy strips the `X-PBX-Secret` header. |
| Duplicate records for ring-group calls | Increase `PBX_GROUP_WINDOW_SECONDS`. |
| Click-to-call does nothing | Check `journalctl -u vtiger-click2call`; verify the *Vtiger Secret Key*, the firewall, and that the AMI user has `write = originate`. |

## License

MIT - see [LICENSE](LICENSE).

`crm-endpoint/berliCRM_WS_Curl_Class.php` is a third-party file by crm-now GmbH,
included **unmodified** under the **GNU LGPL v2.1 or later**
(see [crm-endpoint/LICENSE-LGPL-2.1.txt](crm-endpoint/LICENSE-LGPL-2.1.txt)).
