$html = Join-Path $PSScriptRoot 'static\index.html'
$out  = Join-Path $PSScriptRoot '_check.js'
$h = [IO.File]::ReadAllText($html, [Text.Encoding]::UTF8)
$m = [regex]::Match($h, '(?s)<script>(.*?)</script>')
[IO.File]::WriteAllText($out, $m.Groups[1].Value, (New-Object Text.UTF8Encoding($false)))
node --check $out
if ($LASTEXITCODE -eq 0) { Write-Host 'JS syntax OK' }
