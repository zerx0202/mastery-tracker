# Rozstrzyga, jak dziala paginacja historii w LCU i jak gleboko siega.
# Uruchom przy wlaczonym kliencie LoL, BEZ uprawnien administratora.

Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllDepth : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllDepth
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

function Probe($beg, $end) {
  $url = "$base/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=$beg&endIndex=$end"
  try { $h = Invoke-RestMethod -Uri $url -Headers $auth -TimeoutSec 20 }
  catch { Write-Host ("beg={0,-4} end={1,-4}  BLAD: {2}" -f $beg, $end, $_.Exception.Message) -ForegroundColor Red; return $null }

  $g = $h.games.games
  if (-not $g -or $g.Count -eq 0) {
    Write-Host ("beg={0,-4} end={1,-4}  zwrocono 0" -f $beg, $end) -ForegroundColor Yellow
    return @()
  }
  $first = $g[0]
  $lastG = $g[$g.Count - 1]
  Write-Host ("beg={0,-4} end={1,-4}  zwrocono {2,-4} gameCount={3,-4}  pierwsza={4} ({5})  ostatnia={6} ({7})" -f `
    $beg, $end, $g.Count, $h.games.gameCount, $first.gameId, $first.gameCreationDate.Substring(0,10), `
    $lastG.gameId, $lastG.gameCreationDate.Substring(0,10))
  return $g
}

Write-Host "`n=== A. czy begIndex w ogole dziala ===" -ForegroundColor Cyan
$a = Probe 0 19
$b = Probe 20 39
if ($a -and $b -and $a.Count -gt 0 -and $b.Count -gt 0) {
  $same = ($a[0].gameId -eq $b[0].gameId)
  if ($same) {
    Write-Host "WNIOSEK: begIndex jest ignorowany albo przycinany - obie strony maja to samo pierwsze ID" -ForegroundColor Yellow
  } else {
    Write-Host "WNIOSEK: begIndex dziala, strony sa rozne" -ForegroundColor Green
  }
}

Write-Host "`n=== B. jak szerokie okno da sie wziac jednym zapytaniem ===" -ForegroundColor Cyan
foreach ($n in 39, 49, 99, 199, 499) {
  $r = Probe 0 $n
  if ($r -and $r.Count -lt ($n + 1)) {
    Write-Host "WNIOSEK: okno przyciete do $($r.Count) - to jest realna glebokosc albo limit okna" -ForegroundColor Yellow
    break
  }
}

Write-Host "`n=== C. alternatywne endpointy ===" -ForegroundColor Cyan
foreach ($path in @(
  "/lol-match-history/v1/recently-played-games",
  "/lol-match-history/v3/matchlist/account/me?begIndex=0&endIndex=39"
)) {
  try {
    $r = Invoke-RestMethod -Uri "$base$path" -Headers $auth -TimeoutSec 20
    $cnt = if ($r.games.games) { $r.games.games.Count } elseif ($r.games) { $r.games.Count } else { "?" }
    Write-Host ("{0}  OK, gier: {1}" -f $path, $cnt) -ForegroundColor Green
  } catch {
    Write-Host ("{0}  {1}" -f $path, $_.Exception.Message) -ForegroundColor DarkGray
  }
}
