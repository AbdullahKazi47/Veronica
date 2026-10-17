# Shrink Docker's virtual disk so Windows gets back the space freed inside it.
# Run in an ADMINISTRATOR PowerShell:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\HomePC\Desktop\Veronica\scripts\compact-docker-disk.ps1"
#
# What it does: stops Docker/WSL for a minute, compacts the disk file, done. Nothing
# inside is deleted — World Monitor's images and data stay. Start Docker Desktop again
# afterwards (VERONICA starts World Monitor by herself when it's needed).

$ErrorActionPreference = "Stop"
$vhd = "C:\Users\HomePC\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
if (-not (Test-Path $vhd)) { Write-Host "Docker disk not found at $vhd"; exit 1 }

$before = (Get-Item $vhd).Length / 1GB
Write-Host ("Docker disk is {0:N1} GB. Stopping Docker and WSL..." -f $before)
Get-Process "Docker Desktop" -ErrorAction SilentlyContinue | Stop-Process -Force
wsl --shutdown
Start-Sleep -Seconds 5

$script = New-TemporaryFile
@"
select vdisk file="$vhd"
attach vdisk readonly
compact vdisk
detach vdisk
"@ | Set-Content -Path $script -Encoding ascii
Write-Host "Compacting (a few minutes)..."
diskpart /s $script
Remove-Item $script

$after = (Get-Item $vhd).Length / 1GB
$free = (Get-PSDrive C).Free / 1GB
Write-Host ("Done: {0:N1} GB -> {1:N1} GB. C: now has {2:N1} GB free." -f $before, $after, $free)
Write-Host "Start Docker Desktop again when you want World Monitor."
