# Mastery Tracker

Osobiste, niekomercyjne narzędzie do śledzenia postępów w sezonowej maestrii
championów w League of Legends. Odpowiada na jedno pytanie: **którym championem
zagrać teraz, żeby najszybciej dowieźć milestone 4.**

Działa dla jednego konta, w sieci prywatnej, bez reklam i bez monetyzacji.

## Problem

Klient gry pokazuje bieżący stan maestrii, ale nie pokazuje, jak się zmienia
między sesjami, i nie pozwala porównać championów pod kątem tego, ile pracy
zostało do kolejnego milestone'a. Przy misjach z przepustki, które wymagają
osiągnięcia konkretnego milestone'a, sprawdzanie tego ręcznie przez cały
champion pool jest bez sensu.

Do tego w trybie ARAM pula jest losowa — decyzję podejmujesz w kilka sekund
spośród kilkunastu postaci, które akurat wypadły.

## Jak to działa

Windows (klient LoL) Mac (serwer) Przeglądarka
┌──────────────────┐ ┌──────────────────┐ ┌──────────┐
│ agent LCU │─────────▶│ FastAPI + SQLite │◀───────│ frontend │
│ (PowerShell) │ HTTPS │ │ │ │
└──────────────────┘ └──────────────────┘ └──────────┘
│ │
lockfile + Riot API
/lol-champ-select Data Dragon
/lol-match-history


Agent na stacji roboczej czyta lokalne API klienta gry (LCU) i wysyła na serwer
pulę championów z champ selecta oraz historię meczów. Serwer dokłada dane
z publicznego Riot API, trzyma wszystko w SQLite i liczy ranking.

### Drabinka milestone'ów

Sezonowa maestria dzieli się na cztery milestone'y plus powtarzalny bonusowy.
Progi nie są udokumentowane w API — aplikacja **uczy się ich sama**, obserwując
pole `nextSeasonMilestone` dla championów stojących na różnych szczeblach:

| Krok | Wymóg | Nagroda |
|---|---|---|
| 0 → 1 | A- ×1 | 1 Mark |
| 1 → 2 | A- ×1 | 1 Mark |
| 2 → 3 | S- ×1 | 2 Marks |
| 3 → 4 | S- ×1 | 2 Marks + Crest Highlighting |

### Dlaczego historia idzie z LCU, a nie z match-v5

Publiczne `MATCH-V5` **nie zwraca gier z rodziny ARAM** dla tego konta —
sprawdzone: filtr po kolejce ARAM daje zero wyników w całej historii, mimo
rozegranych meczów matchmade. LCU je ma, więc to on jest źródłem podstawowym.

Ograniczenie: LCU pamięta około 20 ostatnich gier, a parametr `begIndex` jest
ignorowany (potwierdzone testem — `lcu-depth-test.ps1`). Starszych meczów nie da
się nadrobić z żadnego źródła, więc **agent musi być uruchomiony przy każdej
sesji**, inaczej gry przepadają bezpowrotnie.

## Stack

- Backend: Python 3.12, FastAPI, SQLite (WAL)
- Frontend: jeden plik HTML, bez frameworka i bez builda
- Agent: PowerShell (docelowo Python z WebSocketem LCU)
- Dostęp: Tailscale z certyfikatami HTTPS, bez wystawiania czegokolwiek publicznie
- Uruchomienie: Docker Compose

## Źródła danych

| Źródło | Do czego |
|---|---|
| `ACCOUNT-V1` | Riot ID → PUUID, raz, potem cache |
| `CHAMPION-MASTERY-V4` | snapshoty maestrii, milestone'y, zebrane oceny |
| `SUMMONER-V4` | podstawowe dane profilu |
| `MATCH-V5` | historia meczów spoza rodziny ARAM |
| LCU `/lol-champ-select` | pula championów w champ selecie |
| LCU `/lol-match-history` | historia meczów, w tym ARAM Mayhem |
| Data Dragon | nazwy i ikony championów, bez klucza i bez limitów |

## Uruchomienie

### Serwer

```bash
cp .env.example .env      # uzupełnij klucz i Riot ID
docker network create proxy
docker compose up -d --build
curl -s localhost:8000/api/health
```

### Agent

Zobacz [`agent/README.md`](agent/README.md).

## Ograniczenia i limity

Personal API Key ma te same limity co deweloperski: 20 zapytań na sekundę
i 100 na 2 minuty. Aplikacja cache'uje agresywnie — mecze są niezmienne, więc
raz pobrane nigdy nie są odpytywane ponownie — i pilnuje limitów po swojej
stronie przed wysłaniem zapytania.

## Status

W rozwoju. Działa: snapshoty maestrii, nauka drabinki milestone'ów, ranking
championów, wykrywanie champ selecta, historia meczów z LCU, backup.

W planach: zapis ocen pomeczowych, model prawdopodobieństwa oceny oparty na
własnych danych, przebudowany interfejs, widok live w trakcie gry.

## Disclaimer

Mastery Tracker isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
Riot Games properties. Riot Games and all associated properties are trademarks
or registered trademarks of Riot Games, Inc.

Projekt niekomercyjny, do użytku własnego.
