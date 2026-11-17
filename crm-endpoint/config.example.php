<?php
// Copy this file to config.php and fill in your own values.
// config.php is listed in .gitignore - never commit it.

return [
    // Shared secret - must be identical to PBX_API_SECRET used by vtiger_pbx_monitor.py.
    // Generate one with:  openssl rand -hex 32
    'api_secret' => '',

    // vtiger webservice URL. If this endpoint runs on the same server as vtiger,
    // a local URL avoids going through the public internet / proxies.
    'vtiger_ws_url' => 'http://127.0.0.1/vtigercrm/webservice.php',

    // vtiger user and its Access Key (My Preferences -> User Advanced Options -> Access Key).
    'vtiger_user'       => 'admin',
    'vtiger_access_key' => '',

    // vtiger user id (webservice format 19xNN) used when the extension cannot be
    // matched to a user via the "CRM Phone Extension" field. 19x1 is usually admin.
    'fallback_user_id' => '19x1',

    // Text stored as the customer number when the caller ID is withheld.
    'hidden_caller_label' => 'Anonymous',

    // Diagnostics log. Keep it OUTSIDE the web root, or make sure the web server
    // refuses to serve it (see .htaccess) - it contains phone numbers.
    // Set to '' to disable logging.
    'log_file' => __DIR__ . '/pbx_endpoint.log',
];
