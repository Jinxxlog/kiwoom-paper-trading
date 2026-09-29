$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "runtime"
$txtPath = Join-Path $runtimeDir "outside_status.txt"
$jsonPath = Join-Path $runtimeDir "outside_status.json"
$apiUrl = "http://127.0.0.1:8765/api/status"

New-Item -ItemType Directory -Force $runtimeDir | Out-Null

while ($true) {
    $now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        $status = Invoke-RestMethod -Uri $apiUrl -Method Get -TimeoutSec 10
        $runtime = $status.runtime
        $api = $status.api
        $collection = $status.collection
        $market = $status.market

        $summary = [ordered]@{
            checked_at = $now
            dashboard_api = "ok"
            running = [bool]$runtime.running
            pid = $runtime.pid
            unattended = [bool]$runtime.metadata.unattended
            account = $runtime.metadata.account_name
            started_at = $runtime.metadata.started_at
            market_open = [bool]$market.open
            market_date = $market.date
            api_connection = $api.connection_label
            recent_api_success = [bool]$api.recent_success
            latest_price_collection = $collection.latest_at
            collection_fresh = [bool]$collection.fresh
        }

        $summary | ConvertTo-Json -Depth 5 | Set-Content -Path $jsonPath -Encoding UTF8

        @(
            "checked_at: $($summary.checked_at)"
            "dashboard_api: $($summary.dashboard_api)"
            "running: $($summary.running)"
            "pid: $($summary.pid)"
            "unattended: $($summary.unattended)"
            "account: $($summary.account)"
            "started_at: $($summary.started_at)"
            "market_open: $($summary.market_open)"
            "api_connection: $($summary.api_connection)"
            "recent_api_success: $($summary.recent_api_success)"
            "latest_price_collection: $($summary.latest_price_collection)"
            "collection_fresh: $($summary.collection_fresh)"
        ) | Set-Content -Path $txtPath -Encoding UTF8
    }
    catch {
        $message = $_.Exception.Message
        $summary = [ordered]@{
            checked_at = $now
            dashboard_api = "error"
            error = $message
        }

        $summary | ConvertTo-Json -Depth 5 | Set-Content -Path $jsonPath -Encoding UTF8
        @(
            "checked_at: $now"
            "dashboard_api: error"
            "error: $message"
        ) | Set-Content -Path $txtPath -Encoding UTF8
    }

    Start-Sleep -Seconds 60
}
