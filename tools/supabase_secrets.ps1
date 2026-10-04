# Push the send-alert Edge Function's secrets to Supabase.
#
# Why this exists: the Supabase MCP toolset exposes no secrets-set capability,
# and FCM_SERVICE_ACCOUNT_JSON is a 2330-character single-line value with
# embedded newlines and braces. Pasting it by hand is where mistakes happen, so
# this script reads it from disk, compacts it, and hands it to the Supabase CLI.
#
# The Firebase private key never leaves this machine except inside the secret.
#
#   powershell -File tools\supabase_secrets.ps1
#   powershell -File tools\supabase_secrets.ps1 -ProjectRef dnwwwsenujyaffcqiajt
#   powershell -File tools\supabase_secrets.ps1 -DryRun
#
# Requires the Supabase CLI. Not installed on this machine:
#   npm install -g supabase      (or: npx supabase ...)
# Then link the project once:
#   npx supabase login
#   npx supabase link --project-ref <ref>
#
# Reading SUPABASE_URL out of .env is how the project ref is discovered; pass
# -ProjectRef to override.

[CmdletBinding()]
param(
    [string] $ProjectRef = '',
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $RepoRoot '.env'
$ServiceAccountPath = Join-Path $RepoRoot 'secrets\fcm-service-account.json'

function Get-EnvValue {
    param([string] $Path, [string] $Name)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing $Path. Copy .env.example to .env and fill it in first."
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            if ($Matches[1] -eq $Name) {
                # Strip a trailing inline comment, but only when it is preceded by
                # whitespace so a '#' inside a base64 or JSON value survives.
                return ($Matches[2] -replace '\s+#.*$', '').Trim()
            }
        }
    }
    throw "$Name is not set in $Path."
}

function Test-Placeholder {
    param([string] $Value, [string] $Name)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    if ($Value -match '<[^>]*>') { return $true }
    if ($Value -match '\.\.\.') { return $true }
    return $false
}

# --- 1. Locate the Supabase CLI ------------------------------------------------

$cli = Get-Command supabase -ErrorAction SilentlyContinue
if ($cli) {
    $exe = $cli.Source
    $prefix = @()
} else {
    $npx = Get-Command npx -ErrorAction SilentlyContinue
    if (-not $npx) {
        throw 'Neither supabase nor npx is on PATH. Install Node.js or the Supabase CLI.'
    }
    $exe = $npx.Source
    $prefix = @('supabase')
    Write-Host 'supabase CLI not found; falling back to: npx supabase' -ForegroundColor Yellow
}

# --- 2. Resolve the project ref ------------------------------------------------

if (-not $ProjectRef) {
    $url = Get-EnvValue -Path $EnvPath -Name 'SUPABASE_URL'
    if ($url -notmatch '^https://([a-z0-9-]+)\.supabase\.co/?$') {
        throw "Could not read a project ref out of SUPABASE_URL='$url'. Pass -ProjectRef."
    }
    $ProjectRef = $Matches[1]
}
Write-Host "Project ref: $ProjectRef" -ForegroundColor Cyan

# --- 3. Build the three secret values -----------------------------------------

$projectId = Get-EnvValue -Path $EnvPath -Name 'FCM_PROJECT_ID'
if (Test-Placeholder $projectId 'FCM_PROJECT_ID') {
    throw 'FCM_PROJECT_ID is empty or still a placeholder in .env.'
}

if (-not (Test-Path -LiteralPath $ServiceAccountPath)) {
    throw "Missing $ServiceAccountPath. This is the file with the Firebase admin key."
}
$account = Get-Content -LiteralPath $ServiceAccountPath -Raw | ConvertFrom-Json
if ($account.project_id -ne $projectId) {
    throw "Project id mismatch: .env says '$projectId' but the service account says '$($account.project_id)'."
}
# -Compress keeps it on one line; the PEM newlines survive as the two-character
# escape \n, which is what Deno's JSON.parse then turns back into real newlines.
$accountJson = ($account | ConvertTo-Json -Compress -Depth 5)

# Reuse WEBHOOK_SECRET if it is already a real 64-char hex value, so re-running
# this script does not silently break a webhook that is already deployed.
$webhook = Get-EnvValue -Path $EnvPath -Name 'WEBHOOK_SECRET'
if ((Test-Placeholder $webhook 'WEBHOOK_SECRET') -or $webhook -notmatch '^[0-9a-f]{64}$') {
    Write-Host 'WEBHOOK_SECRET is a placeholder; generating 32 random bytes.' -ForegroundColor Yellow
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $webhook = -join ($bytes | ForEach-Object { '{0:x2}' -f $_ })
}

Write-Host ("FCM_SERVICE_ACCOUNT_JSON : {0} chars, project_id={1}" -f $accountJson.Length, $account.project_id)
Write-Host ("WEBHOOK_SECRET           : {0} chars" -f $webhook.Length)

# --- 4. Push -------------------------------------------------------------------
#
# --env-file, not KEY=VALUE on the command line. FCM_SERVICE_ACCOUNT_JSON is 2330
# characters of JSON containing double quotes, and PowerShell 5.1 re-quotes
# arguments passed to native executables: a value like
#   FCM_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
# does not survive the trip intact. The failure is quiet -- the CLI either
# rejects the argument or stores a mangled value, and either way the function
# later fails at getAccessToken() with an opaque 500. Writing the three pairs to
# a file and letting the CLI read that removes the shell from the path entirely.
#
# The file holds only these three secrets. It is NOT .env: that also contains
# SUPABASE_JWT_SECRET, SHIELD_LINK_KEY and SHIELD_WIFI_PSK, and pushing the whole
# file would copy the RPi<->ESP32 link key and the Wi-Fi PSK into a function that
# has no business holding them (D-044).

if ($DryRun) {
    Write-Host ''
    Write-Host 'Dry run. Nothing was sent. Would write a 3-line temp file and run:' -ForegroundColor Yellow
    Write-Host '  supabase secrets set --project-ref <ref> --env-file <temp>'
    Write-Host ''
    Write-Host 'Temp file would contain exactly:'
    Write-Host ("  FCM_PROJECT_ID            = {0} chars" -f $projectId.Length)
    Write-Host ("  FCM_SERVICE_ACCOUNT_JSON  = {0} chars" -f $accountJson.Length)
    Write-Host ("  WEBHOOK_SECRET            = {0} chars" -f $webhook.Length)
    Write-Host ''
    Write-Host 'NOT the rest of .env -- SUPABASE_JWT_SECRET, SHIELD_LINK_KEY and'
    Write-Host 'SHIELD_WIFI_PSK stay on this machine.'
    return
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("shield-secrets-" + [Guid]::NewGuid().ToString('N') + '.env')
try {
    # No BOM, LF endings: the CLI parses this file, and a BOM would corrupt the
    # first key name.
    $content = "FCM_PROJECT_ID=$projectId`nFCM_SERVICE_ACCOUNT_JSON=$accountJson`nWEBHOOK_SECRET=$webhook`n"
    [IO.File]::WriteAllText($tmp, $content, (New-Object Text.UTF8Encoding $false))

    Write-Host ''
    Write-Host ("Temp file: {0} bytes, 3 keys" -f (Get-Item -LiteralPath $tmp).Length) -ForegroundColor Cyan
    Write-Host "Setting 3 secrets on $ProjectRef ..." -ForegroundColor Cyan
    & $exe @prefix secrets set --project-ref $ProjectRef --env-file $tmp
    if ($LASTEXITCODE -ne 0) {
        throw "supabase secrets set failed with exit code $LASTEXITCODE. Not logged in? Run: npx supabase login"
    }
}
finally {
    # Overwrite before unlinking: the Firebase admin private key is in that file.
    if (Test-Path -LiteralPath $tmp) {
        try {
            $len = (Get-Item -LiteralPath $tmp).Length
            [IO.File]::WriteAllBytes($tmp, (New-Object byte[] $len))
            Remove-Item -LiteralPath $tmp -Force
        } catch {
            Write-Warning "Could not securely remove $tmp -- delete it by hand."
        }
    }
}

Write-Host ''
Write-Host 'Confirm all three are present (values are masked by the CLI):' -ForegroundColor Green
Write-Host "  npx supabase secrets list --project-ref $ProjectRef"
Write-Host ''
Write-Host 'Then probe the endpoint. A status 0 row short-circuits before any FCM send'
Write-Host 'and before any database write, so this proves auth without pushing:' -ForegroundColor Green
Write-Host ''
Write-Host '  curl -X POST https://<ref>.supabase.co/functions/v1/send-alert \'
Write-Host '    -H "Content-Type: application/json" \'
Write-Host '    -H "x-webhook-secret: <the WEBHOOK_SECRET value from .env>" \'
Write-Host '    -d ''{"type":"INSERT","table":"events","record":{"id":"00000000-0000-4000-8000-000000000000","ts":1,"status":0,"confidence":0,"image_path":"probe.jpg"}}'''
Write-Host ''
Write-Host 'Expect: {"sent":0,"failed":0,"skipped":true,"reason":"status 0"}'
Write-Host 'A 401 means WEBHOOK_SECRET still missing; a 500 naming FCM_PROJECT_ID or'
Write-Host 'FCM_SERVICE_ACCOUNT_JSON means only those two landed.'
Write-Host ''
Write-Host 'DO NOT run shield_private.set_webhook_secret(...)' -ForegroundColor Yellow
Write-Host ''
Write-Host 'It has already been run, on 2026-10-04, with this exact value -- verified by'
Write-Host 'comparing the SHA256 of the stored secret against the SHA256 of WEBHOOK_SECRET'
Write-Host 'in .env (366b0ad6...1eb05f, both sides hashed, neither printed). The pg_net'
Write-Host 'trigger and the function therefore already agree.'
Write-Host ''
Write-Host 'The SQL below is kept only because this script used to print it as a required'
Write-Host 'step. Running it is not free of risk: set_webhook_secret rejects values under'
Write-Host '32 chars, and the literal placeholder in that snippet is 36 -- so pasting it'
Write-Host 'verbatim SUCCEEDS and overwrites a working secret with garbage, after which'
Write-Host 'every delivery 401s at the function. If you ever do need to set it, paste the'
Write-Host 'real 64-hex value from .env, never the placeholder. To read it without'
Write-Host 'printing it here:'
Write-Host ''
Write-Host '  (Get-Content .env | Select-String ''^WEBHOOK_SECRET='').Line -split ''='',2''
Write-Host ''
Write-Host 'See supabase/migrations/20261003090200_send_alert_webhook.sql.'