param(
  [Parameter(Mandatory = $true)]
  [string]$RepoUrl
)

# Usage:
#   powershell -ExecutionPolicy Bypass -File .\push-to-github.ps1 https://github.com/YOURNAME/piou-ai.git

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($RepoUrl -notmatch '^https://github\.com/[^/]+/[^/]+(\.git)?$') {
  throw "Expected a URL like https://github.com/YOURNAME/piou-ai.git"
}
if ($RepoUrl -notmatch '\.git$') { $RepoUrl = "$RepoUrl.git" }

$existing = git remote 2>$null
if ($existing -contains "origin") { git remote remove origin }

git remote add origin $RepoUrl

Write-Host "Pushing to $RepoUrl ..." -ForegroundColor Cyan
Write-Host "A browser window may open: sign in to GitHub once, then the push finishes." -ForegroundColor Yellow
git push -u origin main

if ($LASTEXITCODE -eq 0) {
  Write-Host ""
  Write-Host "Done. Repo is live at $($RepoUrl -replace '\.git$','')" -ForegroundColor Green
} else {
  Write-Host "Push failed. Check that the repo exists and you are signed in." -ForegroundColor Red
}
