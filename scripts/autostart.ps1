# Start VERONICA when you sign in to Windows (a shortcut in your own Startup folder).
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1            # turn on
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove    # turn off
# Only touches your user Startup folder; no admin rights, no registry, no scheduled task.
# Turning it off is just deleting the shortcut (this script, or Task Manager > Startup apps).
param([switch]$Remove)

$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "VERONICA.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; "VERONICA will no longer start at sign-in." }
    else { "VERONICA wasn't set to start at sign-in." }
    return
}

$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $root "Start VERONICA.bat"
if (-not (Test-Path $bat)) { throw "Can't find '$bat'." }

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($link)
$s.TargetPath = $bat
$s.WorkingDirectory = $root
$s.WindowStyle = 7            # minimized: she talks, the window stays out of the way
$s.Description = "VERONICA voice assistant"
$s.Save()
"VERONICA will start (minimized) each time you sign in. Shortcut: $link"
