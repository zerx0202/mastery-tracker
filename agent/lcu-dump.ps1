# Zrzut struktury historii meczow z LCU - narzedzie diagnostyczne.
# Uruchom przy wlaczonym kliencie LoL.

Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllDump : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllDump
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir "agent.config.json"
$Paths = @(
  "C:\Riot Games\League of Legends\lockfile",
  "D:\Riot Games\League of Legends\lockfile",
  "C:\Program Files\Riot Games\League of Legends\lockfile"
)
if (Test-Path $ConfigPath) {
  $Paths = (Get-Content $ConfigPath -Raw | ConvertFrom-Json).lockfile_paths
}

$p = $Paths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $p) { Write-Host "nie znalazlem lockfile - klient LoL wlaczony?" -ForegroundColor Red; exit }

$fs = [System.IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
$sr = New-Object System.IO.StreamReader($fs)
$parts = $sr.ReadToEnd().Trim().Split(':')
$sr.Close(); $fs.Close()

$auth = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$($parts[3])")) }
$base = "https://127.0.0.1:$($parts[2])"

Write-Host "lockfile: $p   port: $($parts[2])" -ForegroundColor DarkGray

try {
  $h = Invoke-RestMethod -Uri "$base/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=10" -Headers $auth
} catch {
  Write-Host "blad zapytania: $($_.Exception.Message)" -ForegroundColor Red
  exit
}

Write-Host "`n=== ile gier ===" -ForegroundColor Cyan
Write-Host "zwrocono: $($h.games.games.Count)   gameCount w indeksie: $($h.games.gameCount)"

Write-Host "`n=== lista ===" -ForegroundColor Cyan
$h.games.games | Select-Object gameId, gameCreationDate, gameMode, queueId, gameType | Format-Table -AutoSize

$g = $h.games.games[0]
if (-not $g) { Write-Host "brak gier w historii" -ForegroundColor Yellow; exit }

Write-Host "=== pola gry ===" -ForegroundColor Cyan
($g.PSObject.Properties.Name -join ", ")

Write-Host "`n=== uczestnicy ===" -ForegroundColor Cyan
Write-Host "liczba: $($g.participants.Count)"

Write-Host "`n=== pola stats ===" -ForegroundColor Cyan
($g.participants[0].stats.PSObject.Properties.Name -join ", ")

$out = Join-Path $ScriptDir "lcu-game.json"
$g | ConvertTo-Json -Depth 8 | Out-File -Encoding UTF8 $out
Write-Host "`npelny JSON pierwszej gry: $out" -ForegroundColor Green
