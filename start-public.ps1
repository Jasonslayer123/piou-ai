# start-public.ps1 - starts PIOU AI and a temporary public Cloudflare tunnel.
# Run with:  powershell -ExecutionPolicy Bypass -File .\start-public.ps1
# Press Ctrl+C to stop both the tunnel and the server.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = "C:\Users\User\AppData\Local\Python\bin\python.exe"
$cloudflared = Join-Path $env:LOCALAPPDATA "cloudflared\cloudflared.exe"

if (-not (Test-Path $python)) { throw "Python not found at $python" }
if (-not (Test-Path $cloudflared)) { throw "cloudflared not found at $cloudflared (run the installer step again)" }

# Load .env into this process (keys, access code, limits).
if (Test-Path (Join-Path $root ".env")) {
  Get-Content (Join-Path $root ".env") | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $name = $matches[1]
      $value = $matches[2].Trim().Trim('"').Trim("'")
      Set-Item -Path "env:$name" -Value $value
    }
  }
}

if (-not $env:PIOU_ACCESS_CODE) {
  Write-Host "WARNING: PIOU_ACCESS_CODE is empty - the public link will be open to anyone." -ForegroundColor Yellow
}
$env:PIOU_PRODUCTION = "1"

$port = if ($env:PORT) { $env:PORT } else { "5000" }

$existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existing) { throw "Port $port is already in use. Stop that process first." }

Write-Host "Starting PIOU AI on port $port ..." -ForegroundColor Cyan
$server = Start-Process -FilePath $python -ArgumentList "server.py" -PassThru -WindowStyle Hidden -WorkingDirectory $root

try {
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/health" -TimeoutSec 2
      if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $ready) { throw "PIOU AI did not start. Run 'python server.py' to see the error." }

  Write-Host ""
  Write-Host "Access code: $($env:PIOU_ACCESS_CODE)" -ForegroundColor Green
  Write-Host "Waiting for the public link ..." -ForegroundColor Cyan
  Write-Host ""

  & $cloudflared tunnel --url "http://127.0.0.1:$port" --no-autoupdate
}
finally {
  if ($server -and -not $server.HasExited) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  }
}
