# Reach this laptop from your phone (or any device) over Tailscale, and keep it awake while plugged in.
#
#   Run it yourself, in an ADMINISTRATOR PowerShell (right-click PowerShell > Run as administrator):
#       powershell -ExecutionPolicy Bypass -File "C:\Users\HomePC\Desktop\Veronica\scripts\remote-access-setup.ps1"
#   Undo everything it did:
#       powershell -ExecutionPolicy Bypass -File "C:\Users\HomePC\Desktop\Veronica\scripts\remote-access-setup.ps1" -Undo
#
# What it changes (it lists these and waits for you to type YES first):
#   1. Turns on Remote Desktop (Windows 11 Pro can host it), with Network Level Authentication.
#   2. Opens Remote Desktop's port (3389) ONLY to Tailscale addresses (100.64.0.0/10). Nothing is opened to
#      your Wi-Fi, your router or the internet; the built-in "Remote Desktop" firewall rules stay off.
#   3. While plugged in: never go to sleep or hibernate, and closing the lid does nothing. The screen still
#      turns off after 10 minutes. On battery nothing changes.
# What it does NOT do: store your password, create accounts, forward any router port, or touch VERONICA.
param([switch]$Undo)

$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an Administrator PowerShell (right-click PowerShell > Run as administrator)." }

$ts      = "C:\Program Files\Tailscale\tailscale.exe"
$rule    = "Remote Desktop via Tailscale only"
$tsRange = "100.64.0.0/10"
$tsKey   = "HKLM:\System\CurrentControlSet\Control\Terminal Server"

if ($Undo) {
    Write-Host "Undoing: Remote Desktop off, Tailscale-only rule removed, sleep and lid back to normal."
    Set-ItemProperty -Path $tsKey -Name fDenyTSConnections -Value 1
    Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
    powercfg /change standby-timeout-ac 5
    powercfg /change hibernate-timeout-ac 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1
    powercfg /setactive SCHEME_CURRENT
    Write-Host "Done."
    return
}

Write-Host ""
Write-Host "This will:"
Write-Host "  1. Turn on Remote Desktop (with Network Level Authentication)"
Write-Host "  2. Allow it ONLY from Tailscale devices ($tsRange), not from Wi-Fi/LAN/internet"
Write-Host "  3. While plugged in: no sleep, no hibernate, lid close = do nothing"
Write-Host ""
$answer = Read-Host "Type YES to continue"
if ($answer -ne "YES") { Write-Host "Cancelled. Nothing was changed."; return }

# 1. Remote Desktop on, with NLA (the sign-in happens before a session is created)
Set-ItemProperty -Path $tsKey -Name fDenyTSConnections -Value 0
Set-ItemProperty -Path "$tsKey\WinStations\RDP-Tcp" -Name UserAuthentication -Value 1
Set-Service TermService -StartupType Manual
Start-Service TermService -ErrorAction SilentlyContinue

# 2. Firewall: only Tailscale may reach it
Disable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort 3389 `
    -RemoteAddress $tsRange -Action Allow -Profile Any | Out-Null

# 3. Stay awake while plugged in (battery behaviour is untouched)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

Write-Host ""
Write-Host "Done. Checking:"
Write-Host ("  Remote Desktop listening : " + [bool](Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue))
Write-Host ("  Firewall rule            : " + (Get-NetFirewallRule -DisplayName $rule).Enabled + " (Tailscale only)")
if (Test-Path $ts) {
    $ip = (& $ts ip -4 2>$null | Select-Object -First 1)
    Write-Host "  This laptop's Tailscale address: $ip"
    Write-Host ""
    Write-Host "On your phone: turn Tailscale on, open the 'Windows App' (Microsoft Remote Desktop),"
    Write-Host "add a PC with address $ip, and sign in with your Microsoft account EMAIL and PASSWORD."
    Write-Host "(A Windows Hello PIN does not work for Remote Desktop; it needs the real account password.)"
}
Write-Host ""
Write-Host "Keep the laptop plugged in. Lock it with Win+L instead of signing out: a locked laptop keeps VERONICA running."
