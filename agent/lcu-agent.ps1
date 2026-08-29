# ============================================================
#  Mastery Tracker - agent LCU
#  Konfiguracja: agent.config.json obok tego pliku.
#
#  - czyta champ select i wysyla pule na serwer
#  - po zakonczonej grze robi snapshot maestrii
#  - zasysa historie meczow z LCU (zero zapytan do Riot API)
# ============================================================

$ErrorActionPreference = "Stop"

# ---------- konfiguracja ----------

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir "agent.config.json"

if (-not (Test-Path $ConfigPath)) {
  Write-Host "BRAK PLIKU KONFIGURACJI: $ConfigPath" -ForegroundColor Red
  Write-Host "Skopiuj agent.config.example.json jako agent.config.json i uzupelnij api_base." -ForegroundColor Yellow
  Read-Host "Enter konczy"
  exit 1
}

$Cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json

$ApiBase    = $Cfg.api_base.TrimEnd('/')
$Server     = "$ApiBase/lobby"
$Poll       = $Cfg.poll_seconds
$PostDelay  = $Cfg.post_game_delay_seconds
$PagesBoot  = $Cfg.history_pages_on_start
$PagesGame  = $Cfg.history_pages_after_game
$Paths      = $Cfg.lockfile_paths

# LCU uzywa certyfikatu self-signed - trzeba go zignorowac
Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAll : ICertificatePolicy {
  public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAll
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

# ---------- funkcje ----------

function Read-Lockfile {
  foreach ($p in $Paths) {
    if (Test-Path $p) {
      # klient trzyma plik otwarty - konieczne FileShare.ReadWrite
      $fs = [System.IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
      $sr = New-Object System.IO.StreamReader($fs)
      $txt = $sr.ReadToEnd()
      $sr.Close(); $fs.Close()
      $parts = $txt.Trim().Split(':')
      if ($parts.Count -ge 5) { return @{ Port = $parts[2]; Pass = $parts[3] } }
    }
  }
  return $null
}

function Get-Lcu($port, $pass, $path) {
  $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$pass"))
  try {
    return Invoke-RestMethod -Uri "https://127.0.0.1:$port$path" `
      -Headers @{ Authorization = "Basic $auth" } -TimeoutSec 5
  } catch { return $null }
}

function Send-Pool($ids, $mode, $poolKind, $queueId) {
  $body = @{
    champion_ids = @($ids)
    queue        = $mode
    pool_kind    = $poolKind
    queue_id     = $queueId
  } | ConvertTo-Json -Compress
  Invoke-RestMethod -Uri $Server -Method Post -ContentType "application/json" `
    -Body $body -TimeoutSec 10 | Out-Null
}

function Sync-LcuHistory($port, $pass, $pages) {
  $total = 0; $new = 0
  for ($i = 0; $i -lt $pages; $i++) {
    $beg = $i * 20
    $end = $beg + 19
    $h = Get-Lcu $port $pass "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=$beg&endIndex=$end"
    if (-not $h -or -not $h.games.games -or $h.games.games.Count -eq 0) { break }
    $body = @{ games = $h.games.games } | ConvertTo-Json -Depth 12 -Compress
    try {
      $r = Invoke-RestMethod -Uri "$ApiBase/history/lcu" -Method Post `
        -ContentType "application/json" -Body $body -TimeoutSec 90
      $total += $r.received
      $new   += $r.new
    } catch {
      Write-Host "blad wysylki historii: $($_.Exception.Message)" -ForegroundColor Yellow
      break
    }
    if ($h.games.games.Count -lt 20) { break }
  }
  Write-Host "historia LCU: $total gier, $new nowych" -ForegroundColor Green
}

function Refresh-Server($port, $pass) {
  Write-Host "gra skonczona - odswiezam dane" -ForegroundColor Cyan
  Start-Sleep $PostDelay   # Riot potrzebuje chwili, zeby mecz sie zaksiegowal
  try {
    Invoke-RestMethod -Uri "$ApiBase/snapshot" -Method Post -TimeoutSec 60 | Out-Null
    Write-Host "snapshot maestrii zrobiony" -ForegroundColor Green
  } catch {
    Write-Host "blad snapshotu: $($_.Exception.Message)" -ForegroundColor Yellow
  }
  Sync-LcuHistory $port $pass $PagesGame
}

# ---------- petla glowna ----------

Write-Host "agent startuje" -ForegroundColor Cyan
Write-Host "serwer: $ApiBase" -ForegroundColor DarkGray
Write-Host "czekam na klienta LoL..." -ForegroundColor Cyan

$last     = ""
$inGame   = $false
$firstRun = $true

while ($true) {
  try {
    $lock = Read-Lockfile
    if (-not $lock) {
      if ($last -ne "") { Write-Host "klient zamkniety"; $last = "" }
      $inGame = $false
      Start-Sleep 5
      continue
    }

    if ($firstRun) {
      $firstRun = $false
      Write-Host "pierwsze uruchomienie - zasysam historie LCU" -ForegroundColor Cyan
      Sync-LcuHistory $lock.Port $lock.Pass $PagesBoot
    }

    # --- wykrywanie konca gry ---
    $phase = Get-Lcu $lock.Port $lock.Pass "/lol-gameflow/v1/gameflow-phase"
    if ($phase -eq "InProgress") {
      $inGame = $true
    }
    elseif ($inGame -and $phase -in @("None", "Lobby", "EndOfGame", "WaitingForStats", "PreEndOfGame", "TerminatedInError")) {
      $inGame = $false
      Refresh-Server $lock.Port $lock.Pass
    }

    # --- champ select ---
    $sess = Get-Lcu $lock.Port $lock.Pass "/lol-champ-select/v1/session"
    if (-not $sess) {
      if ($last -ne "") {
        Write-Host "wyjscie z champ selecta"
        Send-Pool @() $null $null 0
        $last = ""
      }
      Start-Sleep $Poll
      continue
    }

    $flow = Get-Lcu $lock.Port $lock.Pass "/lol-gameflow/v1/session"
    $mode = "UNKNOWN"
    if     ($flow.gameData.queue.gameMode) { $mode = $flow.gameData.queue.gameMode }
    elseif ($flow.map.gameMode)            { $mode = $flow.map.gameMode }
    $queueId = 0
    if ($flow.gameData.queue.id) { $queueId = $flow.gameData.queue.id }

    # benchEnabled = pula losowana i ograniczona (ARAM, Mayhem, przyszle tryby)
    $benched = [bool]$sess.benchEnabled

    $ids = @()
    if ($benched) {
      $poolKind = "limited"
      if ($sess.benchChampions) { $ids += $sess.benchChampions.championId }
      if ($sess.myTeam)         { $ids += $sess.myTeam.championId }
    } else {
      $poolKind = "full"
      $pick = Get-Lcu $lock.Port $lock.Pass "/lol-champ-select/v1/pickable-champion-ids"
      if ($pick) { $ids += $pick }
      if ($ids.Count -eq 0) {
        $owned = Get-Lcu $lock.Port $lock.Pass "/lol-champions/v1/owned-champions-minimal"
        if ($owned) { $ids += $owned.id }
      }
      if ($ids.Count -eq 0 -and $sess.myTeam) { $ids += $sess.myTeam.championId }
    }
    $ids = $ids | Where-Object { $_ -gt 0 } | Sort-Object -Unique

    if ($ids.Count -gt 0) {
      $key = "$mode|$poolKind|" + ($ids -join ",")
      if ($key -ne $last) {
        Send-Pool $ids "$mode" $poolKind $queueId
        Write-Host "[$mode q=$queueId/$poolKind] wyslano $($ids.Count) championow" -ForegroundColor Green
        $last = $key
      }
    }
  }
  catch {
    Write-Host "blad: $($_.Exception.Message)" -ForegroundColor Yellow
  }
  Start-Sleep $Poll
}
