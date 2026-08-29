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

## Model oceny

Aplikacja zbiera oceny pomeczowe i uczy się na nich przewidywać, czy na danym
championie przebijesz próg wymagany do kolejnego milestone'a.

Dwa niezależne klasyfikatory progowe: `P(ocena ≥ A-)` i `P(ocena ≥ S-)`.
Podział wynika z danych — część obserwacji jest **cenzurowana**: awans milestone'a
mówi „było A- lub lepiej", ale nie mówi, czy było S-. Taka obserwacja liczy się
do pierwszego modelu, a z drugiego musi wypaść.

### Dlaczego obrażenia są normalizowane przez championa

Z zebranych danych: oceny `B+` mają **niższe** obrażenia na minutę niż `C`,
bo `B+` padały na postaciach utility (Lulu, Veigar, Aurelion Sol), a `C` na
Viktorze z 48 tysiącami obrażeń. Ocena jest liczona względem innych grających
tą samą postacią, więc wartość bezwzględna wprowadza w błąd.

`gold/min` zachowuje się odwrotnie — rośnie monotonicznie wraz z oceną
(837 → 869 → 892 → 908 → 964 → 1040) i nie wymaga normalizacji.

Normalizator to własna mediana na championie, a docelowo średnia z serwisu
zewnętrznego (`external_dpm` w tabeli `settings`), która ma pierwszeństwo.

### Skąd normalizator, skoro nie ma go w sieci

Serwisy ze statystykami ARAM-a (np. aramstats.lol) nie mają Mayhema — Riot nie
wystawia tej kolejki w publicznym API. Zwykły ARAM nie jest przybliżeniem, bo
augmenty nie skalują championów równo, więc ranking obrażeń jest przetasowany.

Zamiast tego rozkład budowany jest z **własnych meczów**: ekran końcowy zawiera
statystyki wszystkich dziesięciu graczy, więc każda gra to dziesięć obserwacji
o Mayhemie — w tym o championach, którymi się nie grało. Wartości per champion
są ściągane do średniej globalnej proporcjonalnie do liczby obserwacji, więc
przy jednej grze wynik jest prawie równy globalnemu i nie udaje precyzji.

Po wpięciu tego normalizatora waga obrażeń w modelu wzrosła z 0.15 do 0.60.

### Marker gotowości

`GET /api/model/status` zwraca liczbę obserwacji i informację, czy zebrało się
ich dość, by stroić model. Poniżej progu model działa, ale jest poglądowy.

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
| LCU `/lol-end-of-game/champion-mastery-updates` | **ocena pomeczowa**, punkty, wkład indywidualny |
| LCU `/lol-end-of-game/eog-stats-block` | 110 pól statystyk wszystkich 10 graczy, augmenty |

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

Działa też: zapis ocen pomeczowych i pełnych statystyk końcowych, historia pul
z champ selecta, warstwa splitów odporna na reset, log zdarzeń, backup przez
Tailscale z testem odtworzenia, model prawdopodobieństwa oceny.

W planach: przebudowany interfejs z podstronami, widok live w trakcie gry przez
Live Client Data API, integracja danych zewnętrznych jako normalizatora progu.

## Disclaimer

Mastery Tracker isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
Riot Games properties. Riot Games and all associated properties are trademarks
or registered trademarks of Riot Games, Inc.

Projekt niekomercyjny, do użytku własnego.

## Uruchomienie

Backend (Mac/Linux z Dockerem):

cp .env.example .env # uzupełnij RIOT_API_KEY i RIOT_ID
docker compose up -d --build
curl localhost:8000/api/health


Agent (Windows, przy kliencie gry):

git clone git@github.com:zerx0202/mastery-tracker.git C:\repos\mastery-tracker
cd C:\repos\mastery-tracker\agent
copy agent.config.example.json agent.config.json # uzupełnij api_base
start-agent.cmd


Agent wymaga Pythona 3.11+ (`winget install Python.Python.3.12`). Środowisko
wirtualne i zależności stawia sam przy pierwszym starcie. Dostęp do backendu
spoza localhosta najprościej przez Tailscale (`tailscale serve`).

## Zastrzeżenie

Mastery Tracker isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
Riot Games properties. Riot Games, and all associated properties are trademarks
or registered trademarks of Riot Games, Inc.

Projekt prywatny i niekomercyjny. Korzysta z Riot API, lokalnego API klienta
(LCU) oraz Live Client Data API zgodnie z polityką Riot Games dla aplikacji
third-party.
