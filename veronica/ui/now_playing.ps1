# What's playing, from Windows' own media controls (the same source as the volume flyout).
# Prints one JSON line: {"title","artist","app","playing"} or {} when nothing is playing.
# Used by ui/island.py's music tile; read-only, no network.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(1500) | Out-Null
    $task.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$s = $mgr.GetCurrentSession()
if (-not $s) { '{}'; exit }
$p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
$playing = $s.GetPlaybackInfo().PlaybackStatus -eq 'Playing'
[pscustomobject]@{ title = $p.Title; artist = $p.Artist; app = $s.SourceAppUserModelId; playing = $playing } | ConvertTo-Json -Compress
