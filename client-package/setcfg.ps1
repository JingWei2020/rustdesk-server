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
# ---- JingWei branding: swap in the pre-branded exe (star icon + product name baked in) ----
$installDir = 'C:\Program Files\RustDesk'
$exePath = Join-Path $installDir 'RustDesk.exe'
$brandedExe = Join-Path $PSScriptRoot 'RustDesk-branded.exe'
$brandIco = Join-Path $PSScriptRoot 'brand.ico'
if ((Test-Path $exePath) -and (Test-Path $brandedExe)) {
  $bak = Join-Path $installDir 'RustDesk.exe.bak-stock'
  if (-not (Test-Path $bak)) { Copy-Item $exePath $bak -Force }
  Copy-Item $brandedExe $exePath -Force
  if (Test-Path $brandIco) { Copy-Item $brandIco (Join-Path $installDir 'brand.ico') -Force }
}
# ---- rename shortcuts to JingWei远程控制 and point them at the brand icon ----
$iconTarget = Join-Path $installDir 'brand.ico'
$ws = New-Object -ComObject WScript.Shell
$lnks = @()
foreach ($root in @('C:\Users\Public\Desktop', 'C:\Users\Administrator\Desktop', "$env:USERPROFILE\Desktop", 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs', "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  if (Test-Path $root) { $lnks += Get-ChildItem $root -Recurse -Filter 'RustDesk.lnk' -ErrorAction SilentlyContinue }
}
foreach ($lnk in $lnks) {
  $newPath = Join-Path $lnk.DirectoryName 'JingWei远程控制.lnk'
  if ($lnk.FullName -ne $newPath) { Move-Item $lnk.FullName $newPath -Force }
  if (Test-Path $newPath) {
    $sc = $ws.CreateShortcut($newPath)
    if (Test-Path $iconTarget) { $sc.IconLocation = "$iconTarget,0" }
    $sc.Description = 'JingWei远程控制'
    $sc.Save()
  }
}
Start-Service RustDesk
