# Agent LCU

Dziala na stacji roboczej z Windows i klientem League of Legends.
Backend (FastAPI) stoi osobno - agent tylko do niego wysyla.

## Pierwsze uruchomienie

1. Skopiuj `agent.config.example.json` jako `agent.config.json`
2. Ustaw `api_base` na adres swojego backendu
3. Dwuklik na `start-agent.cmd`

`agent.config.json` jest w `.gitignore` i nie trafia do repozytorium.

## Co robi

- czyta champ select z klienta i wysyla pule championow na `POST /api/lobby`
- po zakonczonej grze robi `POST /api/snapshot` (stan maestrii)
- zasysa historie meczow z LCU i wysyla na `POST /api/history/lcu`

Historia z LCU jest jedynym zrodlem gier ARAM Mayhem - publiczne match-v5
tych meczow nie zwraca. LCU pamieta tylko kilkanascie ostatnich gier, wiec
agent musi byc uruchomiony, zeby zadna gra nie przepadla.

## Narzedzia

`lcu-dump.ps1` - zrzut struktury historii meczow, do diagnostyki.
