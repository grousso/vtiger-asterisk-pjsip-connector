<?php
// Receives JSON POST requests from vtiger_pbx_monitor.py and creates/updates a record in PBXManager.

ini_set('display_errors', 0); // NO display errors on a production endpoint - logging only
error_reporting(E_ALL);

require_once(__DIR__ . "/berliCRM_WS_Curl_Class.php");

// ------ Configuration (copy config.example.php to config.php and edit it) ------
if (!file_exists(__DIR__ . "/config.php")) {
    http_response_code(500);
    echo json_encode(["status" => "error", "message" => "config.php missing"]);
    exit;
}
$config = require(__DIR__ . "/config.php");

// ------ Log file for diagnostics (separate from PHP errors) ------
$LOG_FILE = $config['log_file'];
function pbx_log($msg) {
    global $LOG_FILE;
    if (!$LOG_FILE) return;
    file_put_contents($LOG_FILE, date("Y-m-d H:i:s") . " " . $msg . "\n", FILE_APPEND);
}

header("Content-Type: application/json");

// ------ Shared secret check (must match PBX_API_SECRET of the Python script) ------
$API_SECRET = (string)$config['api_secret'];

$headers = function_exists('getallheaders') ? getallheaders() : [];
$givenSecret = $headers['X-Pbx-Secret'] ?? $headers['X-PBX-Secret'] ?? ($_SERVER['HTTP_X_PBX_SECRET'] ?? '');

if ($API_SECRET === '' || !hash_equals($API_SECRET, (string)$givenSecret)) {
    http_response_code(401);
    pbx_log("REJECTED: bad or missing secret from " . ($_SERVER['REMOTE_ADDR'] ?? '?'));
    echo json_encode(["status" => "error", "message" => "unauthorized"]);
    exit;
}

// ------ Read the JSON POST body (no longer $_GET) ------
$raw = file_get_contents("php://input");
pbx_log("RAW INPUT: " . $raw);

$data = json_decode($raw, true);
if (!is_array($data)) {
    http_response_code(400);
    pbx_log("REJECTED: invalid JSON body");
    echo json_encode(["status" => "error", "message" => "invalid json"]);
    exit;
}

function getSafeMobile($val) {
    if (!$val) return '';
    $mobile = trim($val);
    $mobile = preg_replace('/[^0-9+]/', '', $mobile);
    return $mobile;
}

$mobile        = getSafeMobile($data['from'] ?? '');
$direction     = $data['direction'] ?? '';
$answered_by   = $data['answered_by'] ?? '';
$duration      = $data['duration'] ?? null;
$disposition   = $data['disposition'] ?? '';   // ringing / completed / missed
$start_time    = $data['start_time'] ?? null;
$answer_time   = $data['answer_time'] ?? null;
$end_time      = $data['end_time'] ?? null;
$call_id       = $data['call_id'] ?? null;     // Asterisk Linkedid/uniqueid - upsert key
$incominglinename = $data['incominglinename'] ?? '-';

// ------ CRM credentials ------
$wsC = new WS_Curl_Class($config['vtiger_ws_url'], $config['vtiger_user'], $config['vtiger_access_key']);

if (!$wsC->login()) {
    pbx_log("LOGIN FAILED: " . $wsC->errorMsg);
    http_response_code(500);
    echo json_encode(["status" => "error", "message" => "crm login failed", "detail" => $wsC->errorMsg]);
    exit;
}


// ------ Valid vtiger user: if it did not arrive as 19xNN, look it up by extension ------
if (!preg_match('/^\d+x\d+$/', $answered_by)) {
    $ext = preg_replace('/[^0-9]/', '', $answered_by);
    $found = null;
    if ($ext !== '') {
        $U = $wsC->operation("query",
            array("query" => "SELECT id FROM Users WHERE phone_crm_extension = '$ext' AND status = 'Active' limit 1;"),
            "GET");
        if (!$wsC->errorMsg && !empty($U[0]['id'])) $found = $U[0]['id'];
    }
    $fallbackUser = $config['fallback_user_id'];
    pbx_log("USER MAP: ext='$answered_by' -> " . ($found ?? "not found, fallback $fallbackUser"));
    $answered_by = $found ?? $fallbackUser;
}


// ------ Look up the Contact by mobile number ------
$contact_id = null;
if ($mobile !== '') {
    // basic escaping of single quotes so the query does not break
    $safeMobile = str_replace("'", "\\'", $mobile);
    $Contact_Details = $wsC->operation(
        "query",
        array("query" => "SELECT id FROM Contacts where mobile = '$safeMobile' limit 1;"),
        "GET"
    );

    if ($wsC->errorMsg) {
        pbx_log("CONTACT QUERY ERROR: " . $wsC->errorMsg);
    } elseif (!empty($Contact_Details) && isset($Contact_Details[0]['id'])) {
        $contact_id = $Contact_Details[0]['id'];
    } else {
        pbx_log("NO CONTACT FOUND for mobile=$mobile - record will be created without a customer link");
    }
}

// ------ Build the element - customertype/customer only if a contact was found ------
$element = array(
    "direction"        => $direction,
    "callstatus"       => $disposition,
    "customernumber"   => ($mobile !== '' ? $mobile : $config['hidden_caller_label']),
    "assigned_user_id" => $answered_by, // always has a value - record ownership
    "totalduration"    => $duration,
    "starttime"        => $start_time,
    "endtime"          => $end_time,
    "incominglinename" => $incominglinename,
    "sourceuuid"       => $call_id, // upsert key - same call_id for ringing & completed/missed
);

// The "user" field records who ANSWERED the call - it stays empty while ringing
// (and for missed calls, since nobody answered); it is filled only on completed.
if ($disposition === 'completed') {
    $element["user"] = $answered_by;
}

if ($contact_id) {
    $element["customertype"] = "Contacts";
    $element["customer"] = $contact_id;
}
// If no contact was found, we do NOT send customertype/customer -
// this avoids a vtiger validation error caused by an empty/invalid reference.
// Note: customertype MUST be set together with customer, otherwise the incoming
// call popup shows the caller as "HIDDEN" (permission check in searchIncomingCalls()).

// ------ Upsert: check whether a record with the same sourceuuid already exists
// (e.g. this agent's ringing record) - if so, UPDATE it instead of a new CREATE,
// so that ringing -> completed/missed is the SAME record. ------
$existing_id = null;
if ($call_id) {
    $safeCallId = str_replace("'", "\\'", $call_id);
    $Existing = $wsC->operation(
        "query",
        array("query" => "SELECT id FROM PBXManager where sourceuuid = '$safeCallId' limit 1;"),
        "GET"
    );
    if ($wsC->errorMsg) {
        pbx_log("SOURCEUUID QUERY ERROR: " . $wsC->errorMsg);
    } elseif (!empty($Existing) && isset($Existing[0]['id'])) {
        $existing_id = $Existing[0]['id'];
    }
}

if ($existing_id) {
    $element["id"] = $existing_id;
    $result = $wsC->operation(
        "update",
        array("element" => json_encode($element)),
        "POST"
    );
    $action = "UPDATE";
} else {
    $result = $wsC->operation(
        "create",
        array("elementType" => "PBXManager", "element" => json_encode($element)),
        "POST"
    );
    $action = "CREATE";
}

if ($wsC->errorMsg) {
    pbx_log("$action ERROR: " . $wsC->errorMsg . " | element=" . json_encode($element));
    http_response_code(500);
    echo json_encode(["status" => "error", "message" => $wsC->errorMsg]);
    exit;
}

pbx_log("$action OK: call_id=$call_id disposition=$disposition existing_id=" . ($existing_id ?? 'null') . " contact_id=" . ($contact_id ?? 'null') . " result=" . json_encode($result));
echo json_encode(["status" => "ok", "action" => $action, "call_id" => $call_id, "result" => $result]);
