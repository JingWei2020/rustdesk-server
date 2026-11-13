$ErrorActionPreference = 'SilentlyContinue'
for ($i = 0; $i -lt 20; $i++) { if (Get-Service RustDesk) { break }; Start-Sleep -Seconds 2 }
Start-Service RustDesk
Start-Sleep -Seconds 6
Stop-Service RustDesk -Force
Start-Sleep -Seconds 3
Get-Process | Where-Object { $_.ProcessName -match 'rustdesk' } | ForEach-Object { Stop-Process -Id $_.Id -Force }
Start-Sleep -Seconds 2
$vals = [ordered]@{
  'custom-rendezvous-server' = '23.95.243.27:21116'
  'relay-server'             = '23.95.243.27:21117'
  'api-server'               = 'https://rd.ljwc.eu.cc'
  'key'                      = 'AgY96DpUBIH6MvNSM56itLNqCHCZ7FWXjrWjqINCqvo='
}
$dirs = @(
  "$env:APPDATA\RustDesk\config",
  'C:\Users\Administrator\AppData\Roaming\RustDesk\config',
  'C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\RustDesk\config'
) | Select-Object -Unique
foreach ($d in $dirs) {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
  $f = Join-Path $d 'RustDesk2.toml'
  if (Test-Path $f) { $text = Get-Content $f -Raw } else { $text = "[options]`r`n" }
  if ($text -notmatch '\[options\]') { $text = "[options]`r`n" + $text }
  foreach ($k in $vals.Keys) {
    $v = $vals[$k]
    $pat = "(?m)^" + [regex]::Escape($k) + "\s*=.*$"
    if ($text -match $pat) {
      $text = $text -replace $pat, ($k + " = '" + $v + "'")
    } else {
      $text = $text -replace "(?m)^\[options\]\s*$", ("[options]`r`n" + $k + " = '" + $v + "'")
    }
  }
  Set-Content $f $text -Encoding UTF8
}
Start-Service RustDesk
