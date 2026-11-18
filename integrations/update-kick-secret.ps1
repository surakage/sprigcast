# Called by "Update Kick secret.bat". Reads the new Kick client secret once, hidden, and pipes it into
# "wrangler secret put KICK_CLIENT_SECRET" for each Worker that uses it. The secret is never shown, never
# written to disk, and cleared from memory at the end (wrangler trims the line ending a pipe adds).

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$workers = @(
    @{ Name = "Sign-in helper"; Folder = Join-Path $here "signin-helper" },
    @{ Name = "Kick chat relay"; Folder = Join-Path $here "kick-chat" }
)

Write-Host ""
Write-Host " SprigCast - update the Kick client secret"
Write-Host " ========================================="
Write-Host ""
Write-Host " Paste the new Client Secret from kick.com/settings/developer, then press Enter."
Write-Host " (Nothing shows while you paste.)"
$secure = Read-Host -AsSecureString " Secret"
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
if ([string]::IsNullOrWhiteSpace($plain)) {
    Write-Host ""
    Write-Host " [!] Nothing was pasted. Nothing changed."
    exit 1
}

# Signed in to Cloudflare? (wrangler login opens the browser when not.)
$who = (& npx.cmd --yes wrangler whoami 2>$null) -join "`n"
if ($who -notmatch "You are logged in") {
    Write-Host ""
    Write-Host " Signing in to Cloudflare first: use the account that has slothbloom.com."
    & npx.cmd --yes wrangler login
    if ($LASTEXITCODE -ne 0) {
        Write-Host " [!] Cloudflare sign-in didn't finish. Nothing changed."
        exit 1
    }
}

$results = @()
foreach ($worker in $workers) {
    Write-Host ""
    Write-Host " Updating the $($worker.Name)..."
    Push-Location $worker.Folder
    try {
        $plain | & npx.cmd --yes wrangler secret put KICK_CLIENT_SECRET
        $ok = ($LASTEXITCODE -eq 0)
    } catch {
        $ok = $false
    } finally {
        Pop-Location
    }
    $results += @{ Name = $worker.Name; Ok = $ok }
}
$plain = $null
[GC]::Collect()

Write-Host ""
Write-Host " Result"
Write-Host " ------"
$failed = 0
foreach ($r in $results) {
    if ($r.Ok) {
        Write-Host "  OK      $($r.Name)"
    } else {
        Write-Host "  FAILED  $($r.Name)  (the message above says why; double-click again to retry)"
        $failed++
    }
}
Write-Host ""
if ($failed -eq 0) {
    Write-Host " Both have the new secret. Kick sign-in and Kick chat use it from now on."
    exit 0
}
exit 1
