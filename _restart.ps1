# Dev helper: restart local server and wipe data/ (sample data re-seeds on next page open)
$dir = $PSScriptRoot
$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($c) { Stop-Process -Id $c.OwningProcess -Force; Start-Sleep -Seconds 1 }
Remove-Item -Recurse -Force (Join-Path $dir 'data') -ErrorAction SilentlyContinue
$py = Join-Path $dir '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
Start-Process $py -ArgumentList '-m','uvicorn','app:app','--host','127.0.0.1','--port','8000' -WorkingDirectory $dir -WindowStyle Hidden
Start-Sleep -Seconds 4
curl.exe -s http://127.0.0.1:8000/api/health
