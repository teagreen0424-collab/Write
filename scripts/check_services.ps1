param(
    [string] $BackendUrl = 'http://127.0.0.1:8000',
    [string] $FrontendUrl = 'http://127.0.0.1:5173',
    [ValidateRange(1, 10)]
    [int] $TimeoutSeconds = 1,
    [switch] $Quiet
)

$ErrorActionPreference = 'Stop'

function Get-ReadyResponse([string] $Url) {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSeconds
    if ($response.StatusCode -ne 200) {
        throw "Expected HTTP 200 from $Url"
    }
    return $response
}

function Assert-LingmoHealth([string] $Url) {
    $response = Get-ReadyResponse $Url
    try {
        $health = $response.Content | ConvertFrom-Json
    } catch {
        throw "The service at $Url did not return Lingmo health JSON."
    }
    if ($health.status -ne 'ok' -or $health.app -ne 'lingmo-writer') {
        throw "The service at $Url is not a ready Lingmo backend."
    }
}

try {
    Assert-LingmoHealth ($BackendUrl.TrimEnd('/') + '/api/v1/health')
    $frontend = Get-ReadyResponse ($FrontendUrl.TrimEnd('/') + '/')
    if ($frontend.Content -notmatch '<title>[^<]*Lingmo[^<]*</title>' -or
        $frontend.Content -notmatch '<div\s+id=["'']app["'']') {
        throw "The page at $FrontendUrl is not the Lingmo frontend."
    }
    Assert-LingmoHealth ($FrontendUrl.TrimEnd('/') + '/api/v1/health')
    if (-not $Quiet) { Write-Host '[OK] Lingmo backend, frontend and API proxy are ready.' }
    exit 0
} catch {
    if (-not $Quiet) { Write-Host ('[ERROR] ' + $_.Exception.Message) }
    exit 1
}
