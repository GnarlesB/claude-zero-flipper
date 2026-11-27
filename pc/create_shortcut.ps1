# Creates a "Claude Zero" shortcut on the Desktop that launches the tray feed,
# using the icon and launcher in this same folder. Location-independent.
$here = $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')

$target = Join-Path $here 'run_claude_usage.bat'
$icon   = Join-Path $here 'claude_zero.ico'

foreach ($old in @('Claude Usage.lnk','Claude Zero.lnk')) {
    $p = Join-Path $desktop $old
    if (Test-Path $p) { Remove-Item $p -Force }
}

if (-not (Test-Path $target)) { Write-Host "ERROR: $target not found"; exit 1 }

$lnk = Join-Path $desktop 'Claude Zero.lnk'
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $here
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.WindowStyle      = 7   # minimized (the bat flashes briefly, then hides to tray)
$sc.Description      = 'Stream Claude usage to the Flipper Zero'
$sc.Save()

Start-Process ie4uinit.exe -ArgumentList '-show' -WindowStyle Hidden
Write-Host "DONE - created: $lnk"
