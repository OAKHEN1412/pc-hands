# Autostart pc-hands (:8792) at logon via HKCU\...\Run (pythonw, no window).
# NOT a Scheduled Task: a task-launched pythonw got no working SendInput/input hooks (clicks did nothing,
# user_idle_ms stuck at 1e9) even in session 1 at Limited level. Plain logon Run works.
# Usage: powershell -ExecutionPolicy Bypass -File install-autostart.ps1
# pythonw = the pythonw.exe next to `python` on PATH; falls back to the original Python 3.12 install.
$pyw = $null
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($py) {
    $cand = Join-Path (Split-Path $py -Parent) "pythonw.exe"
    if (Test-Path $cand) { $pyw = $cand }
}
if (-not $pyw) { throw "pythonw.exe not found next to python on PATH" }
$srv = Join-Path $PSScriptRoot "server.py"
Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name PCHands -Value "`"$pyw`" -X utf8 `"$srv`""
Unregister-ScheduledTask "PC Hands" -Confirm:$false -ErrorAction SilentlyContinue
