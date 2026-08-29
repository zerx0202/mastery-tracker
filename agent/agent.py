#!/usr/bin/env python3
"""
Mastery Tracker - agent LCU.

Dziala na stacji z klientem League of Legends. Sluchа zdarzen z lokalnego
API klienta (LCU) przez WebSocket i wysyla dane na serwer.

Co robi:
  - champ select        -> POST /api/lobby     (pula championow)
  - wejscie do gry      -> POST /api/snapshot  (stan maestrii PRZED gra)
  - koniec gry          -> POST /api/snapshot  (stan PO grze)
                        -> POST /api/history/lcu
                        -> POST backup_url
                        -> zrzut diagnostyczny endpointow z ocena

Snapshot przed i po grze jest konieczny, zeby jednoznacznie przypisac
zdobyta ocene do konkretnego meczu.
"""

import asyncio
import base64
import json
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "agent.config.json"

# Fazy gameflow wg dokumentacji LCU, w kolejnosci cyklu zycia.
PHASE_IN_GAME = "InProgress"
PHASES_AFTER_GAME = {"WaitingForStats", "PreEndOfGame", "EndOfGame",
                     "TerminatedInError", "None", "Lobby"}

# Kandydaci na ocene pomeczowa - zrzucamy surowe odpowiedzi do analizy.
MASTERY_UPDATES = "/lol-end-of-game/v1/champion-mastery-updates"
EOG_STATS_BLOCK = "/lol-end-of-game/v1/eog-stats-block"

# Live Client Data - osobny serwer na stalym porcie 2999, dostepny tylko
# w trakcie meczu. Brak polaczenia = nie gramy, i to jest caly mechanizm
# wykrywania; nic nie trzeba uruchamiac recznie.
LIVE_BASE = "https://127.0.0.1:2999/liveclientdata"

DIAG_ENDPOINTS = [
    "/lol-end-of-game/v1/eog-stats-block",
    MASTERY_UPDATES,
    "/lol-champion-mastery/v1/local-player/champion-mastery",
    "/lol-collections/v1/inventories/champion-mastery",
    "/lol-career-stats/v1/champion-averages",
]


def log(msg, level="info"):
    colors = {"info": "", "ok": "\033[92m", "warn": "\033[93m",
              "err": "\033[91m", "dim": "\033[90m"}
    reset = "\033[0m" if colors.get(level) else ""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{colors.get(level, '')}{stamp}  {msg}{reset}", flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        log(f"BRAK PLIKU KONFIGURACJI: {CONFIG_PATH}", "err")
        log("Skopiuj agent.config.example.json jako agent.config.json", "warn")
        input("Enter konczy")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg.pop("_comment", None)
    cfg["api_base"] = cfg["api_base"].rstrip("/")
    return cfg


class Lcu:
    """Polaczenie z lokalnym API klienta gry."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.port = None
        self.password = None
        self.session = None
        self.ssl = ssl.create_default_context()
        self.ssl.check_hostname = False
        self.ssl.verify_mode = ssl.CERT_NONE

    def read_lockfile(self):
        """Zwraca (port, haslo) albo (None, None). Klient trzyma plik otwarty,
        wiec czytamy w trybie dzielonym."""
        for raw in self.cfg["lockfile_paths"]:
            p = Path(raw)
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parts = text.strip().split(":")
            if len(parts) >= 5:
                return parts[2], parts[3]
        return None, None

    @property
    def base(self):
        return f"https://127.0.0.1:{self.port}"

    @property
    def headers(self):
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def get(self, path, timeout=8):
        try:
            async with self.session.get(self.base + path, headers=self.headers,
                                        ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception:
            return None

    async def get_raw(self, path, timeout=8):
        """Jak get, ale zwraca (status, tekst) - do diagnostyki."""
        try:
            async with self.session.get(self.base + path, headers=self.headers,
                                        ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status, await r.text()
        except Exception as e:
            return None, str(e)


class Server:
    """Klient backendu Mastery Tracker."""

    def __init__(self, cfg, session):
        self.cfg = cfg
        self.session = session

    async def post(self, path, payload=None, timeout=90):
        url = path if path.startswith("http") else self.cfg["api_base"] + path
        try:
            async with self.session.post(url, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                txt = await r.text()
                if r.status >= 400:
                    log(f"serwer {r.status} na {path}: {txt[:120]}", "warn")
                    return None
                try:
                    return json.loads(txt)
                except ValueError:
                    return {"raw": txt}
        except Exception as e:
            log(f"blad polaczenia z serwerem ({path}): {e}", "warn")
            return None


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lcu = Lcu(cfg)
        self.server = None
        self.session = None
        self.last_pool_key = None
        self.in_game = False
        self.pre_snapshot_done = False
        self.history_bootstrapped = False
        self.ws_failures = 0
        self.champ_ids = {}

    # ---------- akcje ----------

    async def snapshot(self, label):
        r = await self.server.post("/snapshot")
        if r and "snapshot_id" in r:
            log(f"snapshot {label}: #{r['snapshot_id']}, {r['champions']} championow", "ok")
        return r

    async def sync_history(self, pages):
        total = new = 0
        seen = set()
        for i in range(pages):
            beg, end = i * 20, i * 20 + 19
            h = await self.lcu.get(
                f"/lol-match-history/v1/products/lol/current-summoner/matches"
                f"?begIndex={beg}&endIndex={end}")
            games = (h or {}).get("games", {}).get("games") or []
            if not games:
                break
            # LCU ignoruje begIndex i poza zakresem oddaje te sama strone
            fresh = [g for g in games if g.get("gameId") not in seen]
            for g in fresh:
                seen.add(g.get("gameId"))
            if not fresh:
                break
            r = await self.server.post("/history/lcu", {"games": fresh})
            if not r:
                break
            total += r.get("received", 0)
            new += r.get("new", 0)
            if r.get("errors"):
                log(f"blad zapisu meczu: {r['errors'][0]}", "warn")
            if len(games) < 20:
                break
        log(f"historia LCU: {total} przeslanych, {new} nowych", "ok")

    async def send_pool(self, ids, mode, pool_kind, queue_id):
        await self.server.post("/lobby", {
            "champion_ids": sorted(set(ids)),
            "queue": mode,
            "pool_kind": pool_kind,
            "queue_id": queue_id,
        })

    async def trigger_backup(self):
        url = self.cfg.get("backup_url")
        if not url:
            return
        r = await self.server.post(url, timeout=20)
        if r is not None:
            log("backup wyzwolony", "ok")

    async def capture_grade(self):
        """Ocena pomeczowa. Endpoint zyje tylko na ekranie koncowym,
        wiec czytamy go zanim klient wyczysci stan."""
        data = await self.lcu.get(MASTERY_UPDATES)
        if not data:
            log("brak danych o ocenie (za pozno albo tryb bez maestrii)", "dim")
            return
        r = await self.server.post("/grade", {"updates": data})
        if not r:
            return
        entries = data if isinstance(data, list) else [data]
        for e in entries:
            if e.get("grade"):
                log(f"ocena: {e['grade']} (champion {e.get('championId')}, "
                    f"score {e.get('score')}, +{e.get('pointsGained')} pkt)", "ok")
        if r.get("errors"):
            log(f"blad zapisu oceny: {r['errors'][0]}", "warn")

    async def capture_eog(self):
        """Caly ekran koncowy: 183 pola statystyk, augmenty i wyniki
        pozostalych graczy - material na percentyle."""
        block = await self.lcu.get(EOG_STATS_BLOCK, timeout=20)
        if not isinstance(block, dict):
            log("brak danych z ekranu koncowego", "dim")
            return
        r = await self.server.post("/eog", {"block": block}, timeout=60)
        if r and r.get("stored"):
            log("statystyki koncowe zapisane" + (" (nowe)" if r.get("new") else " (aktualizacja)"), "ok")
        elif r and r.get("errors"):
            log(f"blad zapisu statystyk: {r['errors'][0]}", "warn")

    async def dump_diagnostics(self):
        """Zrzuca surowe odpowiedzi endpointow, ktore moga zawierac ocene."""
        if not self.cfg.get("enable_dumps", True):
            return
        out_dir = HERE / "dumps"
        out_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        found = []
        for path in DIAG_ENDPOINTS:
            status, body = await self.lcu.get_raw(path)
            if status == 200 and body and body not in ("null", "{}", "[]"):
                name = path.strip("/").replace("/", "_")
                (out_dir / f"{stamp}-{name}.json").write_text(body, encoding="utf-8")
                found.append(f"{path} ({len(body)} B)")
        if found:
            log(f"zrzut diagnostyczny -> agent/dumps/: {len(found)} plikow", "ok")
            for f in found:
                log(f"  {f}", "dim")
        else:
            log("zaden z kandydatow na ocene nie zwrocil danych", "dim")

    # ---------- obsluga stanow ----------

    async def handle_champ_select(self, sess):
        if not sess:
            if self.last_pool_key is not None:
                log("wyjscie z champ selecta")
                await self.send_pool([], None, None, 0)
                self.last_pool_key = None
            return

        flow = await self.lcu.get("/lol-gameflow/v1/session") or {}
        queue = (flow.get("gameData") or {}).get("queue") or {}
        mode = queue.get("gameMode") or (flow.get("map") or {}).get("gameMode") or "UNKNOWN"
        queue_id = queue.get("id") or 0

        # benchEnabled = pula losowana i ograniczona (ARAM, Mayhem, kolejne tryby)
        benched = bool(sess.get("benchEnabled"))
        ids = []
        if benched:
            pool_kind = "limited"
            ids += [c.get("championId") for c in (sess.get("benchChampions") or [])]
            ids += [p.get("championId") for p in (sess.get("myTeam") or [])]
        else:
            pool_kind = "full"
            pick = await self.lcu.get("/lol-champ-select/v1/pickable-champion-ids")
            if pick:
                ids += pick
            if not ids:
                owned = await self.lcu.get("/lol-champions/v1/owned-champions-minimal")
                if owned:
                    ids += [c.get("id") for c in owned]
            if not ids:
                ids += [p.get("championId") for p in (sess.get("myTeam") or [])]

        ids = sorted({i for i in ids if i and i > 0})
        if not ids:
            return

        key = f"{mode}|{pool_kind}|" + ",".join(map(str, ids))
        if key == self.last_pool_key:
            return
        self.last_pool_key = key
        await self.send_pool(ids, mode, pool_kind, queue_id)
        log(f"[{mode} q={queue_id}/{pool_kind}] wyslano {len(ids)} championow", "ok")

        # snapshot PRZED gra - raz na champ select
        if not self.pre_snapshot_done:
            self.pre_snapshot_done = True
            await self.snapshot("przed gra")

    async def handle_phase(self, phase):
        if phase == PHASE_IN_GAME:
            if not self.in_game:
                log("gra w toku", "dim")
            self.in_game = True
            return

        if self.in_game and phase in PHASES_AFTER_GAME:
            self.in_game = False
            self.pre_snapshot_done = False
            log(f"koniec gry (faza {phase})", "ok")
            await self.capture_grade()             # zanim klient wyczysci stan
            await self.capture_eog()
            await self.dump_diagnostics()
            await asyncio.sleep(self.cfg["post_game_delay_seconds"])
            await self.snapshot("po grze")
            await self.sync_history(self.cfg["history_pages_after_game"])
            await self.trigger_backup()

    # ---------- petle ----------

    async def live_loop(self):
        """Odczyt stanu gry na zywo. Port 2999 istnieje tylko podczas meczu,
        wiec ConnectionError jest normalnym stanem, nie bledem."""
        was_live = False
        while True:
            try:
                async with self.session.get(
                    LIVE_BASE + "/allgamedata", ssl=self.lcu.ssl,
                    timeout=aiohttp.ClientTimeout(total=4)
                ) as r:
                    data = await r.json(content_type=None) if r.status == 200 else None
            except Exception:
                data = None

            if not data:
                if was_live:
                    was_live = False
                    log("koniec danych na zywo", "dim")
                    await self.server.post("/live", {"ended": True}, timeout=15)
                await asyncio.sleep(self.cfg.get("live_poll_seconds", 5))
                continue

            try:
                await self.send_live(data)
                if not was_live:
                    was_live = True
                    log("dane na zywo aktywne", "ok")
            except Exception as e:
                log(f"blad odczytu na zywo: {type(e).__name__}: {e}", "warn")

            await asyncio.sleep(self.cfg.get("live_poll_seconds", 5))

    async def send_live(self, data):
        ap = data.get("activePlayer") or {}
        gd = data.get("gameData") or {}
        name = ap.get("summonerName") or ap.get("riotId") or ""

        me = None
        for pl in data.get("allPlayers") or []:
            cand = [pl.get("summonerName"), pl.get("riotId"),
                    pl.get("riotIdGameName")]
            if name and any(c and (c == name or str(c).startswith(name)) for c in cand):
                me = pl
                break
        if me is None:
            return

        sc = me.get("scores") or {}

        # Zlota zarobionego nie ma w API. Suma cen kupionych przedmiotow
        # plus zloto w kieszeni to pomiar, a nie zgadywanie z zabojstw i CS.
        spent = 0
        for it in me.get("items") or []:
            price = it.get("price") or 0
            spent += price * (it.get("count") or 1)
        gold_est = int((ap.get("currentGold") or 0) + spent)

        await self.server.post("/live", {
            "champion": me.get("championName"),
            "champion_id": self.champ_ids.get(
                (me.get("championName") or "").replace(" ", "").replace("'", "")),
            "game_mode": gd.get("gameMode"),
            "game_time": gd.get("gameTime"),
            "kills": sc.get("kills"), "deaths": sc.get("deaths"),
            "assists": sc.get("assists"), "cs": sc.get("creepScore"),
            "ward_score": sc.get("wardScore"),
            "gold_est": gold_est, "level": me.get("level"),
            "raw": {"items": me.get("items"), "spent": spent,
                    "current_gold": ap.get("currentGold")},
        }, timeout=15)

    async def load_champ_ids(self):
        """Nazwa championa -> id, z Data Dragon. Live API podaje tylko nazwe."""
        try:
            async with self.session.get(
                "https://ddragon.leagueoflegends.com/api/versions.json",
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                patch = (await r.json())[0]
            async with self.session.get(
                f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json",
                timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json()
            self.champ_ids = {v["id"]: int(v["key"]) for v in data["data"].values()}
            log(f"mapa championow zaladowana ({len(self.champ_ids)})", "dim")
        except Exception as e:
            log(f"nie udalo sie zaladowac mapy championow: {e}", "warn")

    async def poll_loop(self):
        """Siatka bezpieczenstwa - gdyby WebSocket przeoczyl zmiane stanu."""
        while True:
            try:
                if self.lcu.port:
                    phase = await self.lcu.get("/lol-gameflow/v1/gameflow-phase")
                    if phase:
                        await self.handle_phase(phase)
                    sess = await self.lcu.get("/lol-champ-select/v1/session")
                    await self.handle_champ_select(sess)
            except Exception as e:
                log(f"blad w pollingu: {type(e).__name__}: {e}", "warn")
            await asyncio.sleep(self.cfg.get("fallback_poll_seconds", 10))

    async def ws_loop(self):
        """Glowny kanal - zdarzenia z LCU po WebSocket.
        LCU mowi protokolem WAMP 1.0, wiec podprotokol musi byc zadeklarowany,
        inaczej serwer odrzuca handshake."""
        url = f"wss://127.0.0.1:{self.lcu.port}/"
        async with self.session.ws_connect(
            url,
            headers=self.lcu.headers,
            protocols=("wamp",),
            ssl=self.lcu.ssl,
            heartbeat=30,
        ) as ws:
            # opcode 5 = subscribe
            await ws.send_str(json.dumps([5, "OnJsonApiEvent_lol-gameflow_v1_gameflow-phase"]))
            await ws.send_str(json.dumps([5, "OnJsonApiEvent_lol-champ-select_v1_session"]))
            log("WebSocket podlaczony", "ok")
            self.ws_failures = 0

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                    continue
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                # WAMP: [opcode, nazwa_eventu, {uri, eventType, data}]
                # Czesc wiadomosci ma inny ksztalt - ignorujemy je.
                if not isinstance(payload, list) or len(payload) < 3:
                    continue
                event = payload[2]
                if not isinstance(event, dict):
                    continue
                uri = event.get("uri", "")
                data = event.get("data")
                if uri.endswith("/gameflow-phase"):
                    await self.handle_phase(data)
                elif uri.endswith("/champ-select/v1/session"):
                    await self.handle_champ_select(data if isinstance(data, dict) else None)

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.lcu.session = session
            self.server = Server(self.cfg, session)

            log("agent startuje")
            log(f"serwer: {self.cfg['api_base']}", "dim")

            await self.load_champ_ids()
            asyncio.create_task(self.poll_loop())
            asyncio.create_task(self.live_loop())

            while True:
                port, pw = self.lcu.read_lockfile()
                if not port:
                    if self.lcu.port:
                        log("klient zamkniety")
                        self.lcu.port = None
                        self.last_pool_key = None
                    await asyncio.sleep(5)
                    continue

                if port != self.lcu.port:
                    self.lcu.port, self.lcu.password = port, pw
                    log(f"klient wykryty (port {port})", "ok")
                    if not self.history_bootstrapped:
                        self.history_bootstrapped = True
                        await self.sync_history(self.cfg["history_pages_on_start"])

                try:
                    await self.ws_loop()
                    self.ws_failures = 0
                except aiohttp.WSServerHandshakeError as e:
                    self.ws_failures += 1
                    if self.ws_failures == 1:
                        log(f"WebSocket odrzucil polaczenie: HTTP {e.status} {e.message}", "warn")
                        log("dzialam na pollingu co "
                            f"{self.cfg.get('fallback_poll_seconds', 10)}s - nic nie ginie", "dim")
                except Exception as e:
                    self.ws_failures += 1
                    if self.ws_failures == 1:
                        log(f"WebSocket rozlaczony: {type(e).__name__}: {e}", "dim")

                # rosnaca przerwa, zeby nie zasypywac logu
                delay = min(5 * (2 ** min(self.ws_failures, 5)), 120)
                await asyncio.sleep(delay if self.ws_failures else 3)


def main():
    cfg = load_config()
    try:
        asyncio.run(Agent(cfg).run())
    except KeyboardInterrupt:
        log("zatrzymany", "dim")


if __name__ == "__main__":
    main()
