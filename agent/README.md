# Agent LCU

Dziala na stacji z Windows i klientem League of Legends. Backend (FastAPI)
stoi osobno — agent tylko do niego wysyla.

## Pierwsze uruchomienie

1. Skopiuj `agent.config.example.json` jako `agent.config.json`
2. Ustaw `api_base` na adres backendu
3. Dwuklik na `start-agent.cmd`

`start-agent.cmd` przy kazdym starcie robi `git pull` i `pip install -r
requirements.txt`, wiec agent aktualizuje sie sam. `agent.config.json` jest
w `.gitignore` i nie trafia do repozytorium.

## Co robi

- slucha zdarzen klienta po WebSockecie: fazy gry, champ select, ocena
  pomeczowa. WS chodzi na dedykowanej sesji aiohttp — LCU odrzuca upgrade
  na polaczeniu wzietym z puli keep-alive. Polling co 10 s jako fallback
- champ select: wysyla pule (lawka + druzyna) na `POST /api/lobby`
  i aktualizuje ja na zywo przy kazdym picku
- po grze: petla dopytujaca (co 2 s, do 120 s) lapie ocene
  (`champion-mastery-updates`) i ekran koncowy (`eog-stats-block`,
  183 pola x 10 graczy). Ocena przychodzi tez zdarzeniem WS — co pierwsze
- w trakcie gry: Live Client Data (port 2999) co 2 s zasila panel live;
  zloto szacowane z ubytkow stanu, bo API nie oddaje zarobionego
- snowball: z kazdego bloku eog zbiera PUUID-y pozostalych graczy,
  a przy bezczynnym kliencie dociaga ich historie (1 gracz na minute) —
  z tego rosna normy per champion
- snapshoty maestrii przed i po grze, historia meczow (okno 20 gier)
- nieudane POST-y (5xx, brak polaczenia) laduja w kolejce dyskowej
  `agent/queue/` i sa dosylane co 30 s

Ocena istnieje tylko na ekranie koncowym — gra bez dzialajacego agenta traci
ja bezpowrotnie. Statystyki gry da sie pozniej odzyskac po ID, oceny nie.

## Narzedzia

`lcu-dump.ps1` — zrzut struktury historii meczow, do diagnostyki.
