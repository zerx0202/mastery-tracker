# Szuka endpointu historii, ktory faktycznie paginuje.
# Uruchom przy wlaczonym kliencie LoL, bez uprawnien administratora.

Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllEp : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllEp
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
if (-not $p) { Write-Host "brak lockfile - klient wlaczony?" -ForegroundColor Red; exit }

$fs = [System.IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
$sr = New-Object System.IO.StreamReader($fs)
$parts = $sr.ReadToEnd().Trim().Split(':')
$sr.Close(); $fs.Close()
$auth = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$($parts[3])")) }
$base = "https://127.0.0.1:$($parts[2])"

function Try-Get($path) {
  try { return Invoke-RestMethod -Uri "$base$path" -Headers $auth -TimeoutSec 25 }
  catch {
    $code = "?"
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    Write-Host ("  {0,-70} HTTP {1}" -f $path, $code) -ForegroundColor DarkGray
    return $null
  }
}

Write-Host "=== kim jestem ===" -ForegroundColor Cyan
$me = Try-Get "/lol-summoner/v1/current-summoner"
if (-not $me) { Write-Host "nie moge pobrac current-summoner" -ForegroundColor Red; exit }
$puuid = $me.puuid
Write-Host "puuid: $puuid"
Write-Host "summonerId: $($me.summonerId)"

function Report($label, $obj) {
  if (-not $obj) { return }
  $g = $null
  if ($obj.games.games)   { $g = $obj.games.games }
  elseif ($obj.games)     { $g = $obj.games }
  elseif ($obj -is [array]) { $g = $obj }
  if ($g -and $g.Count -gt 0) {
    $f = $g[0]; $l = $g[$g.Count - 1]
    $fd = if ($f.gameCreationDate) { $f.gameCreationDate.Substring(0,10) } else { "?" }
    $ld = if ($l.gameCreationDate) { $l.gameCreationDate.Substring(0,10) } else { "?" }
    Write-Host ("  {0,-46} gier={1,-4} od {2} do {3}  pierwsze ID={4}" -f $label, $g.Count, $fd, $ld, $f.gameId) -ForegroundColor Green
  } else {
    Write-Host ("  {0,-46} zwrocono 0 / inna struktura" -f $label) -ForegroundColor Yellow
  }
}

Write-Host "`n=== wariant po PUUID ===" -ForegroundColor Cyan
Report "puuid beg=0  end=19"  (Try-Get "/lol-match-history/v1/products/lol/$puuid/matches?begIndex=0&endIndex=19")
Report "puuid beg=20 end=39"  (Try-Get "/lol-match-history/v1/products/lol/$puuid/matches?begIndex=20&endIndex=39")
Report "puuid beg=40 end=59"  (Try-Get "/lol-match-history/v1/products/lol/$puuid/matches?begIndex=40&endIndex=59")

Write-Host "`n=== inne kandydaty ===" -ForegroundColor Cyan
Report "delayed-matchlist"    (Try-Get "/lol-match-history/v1/delayed-matchlist/$puuid?begIndex=0&endIndex=19")
Report "career summoner-games" (Try-Get "/lol-career-stats/v1/summoner-games/$puuid")
Report "bez parametrow"        (Try-Get "/lol-match-history/v1/products/lol/current-summoner/matches")

Write-Host "`n=== pojedyncza gra po ID ===" -ForegroundColor Cyan
$one = Try-Get "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=0"
if ($one -and $one.games.games -and $one.games.games.Count -gt 0) {
  $gid = $one.games.games[0].gameId
  $single = Try-Get "/lol-match-history/v1/games/$gid"
  if ($single) { Write-Host "  /lol-match-history/v1/games/{id} DZIALA - da sie ciagnac gry po ID" -ForegroundColor Green }
}
