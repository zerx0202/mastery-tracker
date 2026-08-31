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

# Prefix pola rawChampionName w Live Client Data - reszta to wewnetrzna
# nazwa championa, niezalezna od jezyka klienta i zgodna z kluczem
# Data Dragona (np. game_character_displayname_MonkeyKing -> MonkeyKing).
RAW_CHAMP_PREFIX = "game_character_displayname_"

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
    """Klient backendu Mastery Tracker.

    Nieudane wysylki na sciezki krytyczne laduja w agent/queue/ i sa
    dosylane w tle. Ocena i ekran koncowy istnieja w LCU chwile - jesli
    backend akurat lezy (rebuild, uspiony Mac, czknieta siec), bez bufora
    przepadaja bezpowrotnie. Backend deduplikuje po match_id, wiec dosylka
    jest bezpieczna nawet podwojna. Bledy 4xx NIE trafiaja do kolejki -
    zle dane nie stana sie dobre od powtarzania.
    """

    DURABLE = ("/grade", "/eog", "/snapshot", "/history/lcu")
    QUEUE_DIR = HERE / "queue"

    def __init__(self, cfg, session):
        self.cfg = cfg
        self.session = session
        self._flush_task = None

    def _headers(self):
        tok = self.cfg.get("api_token")
        return {"X-API-Token": tok} if tok else {}

    async def post(self, path, payload=None, timeout=90, _from_queue=False):
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())
        url = path if path.startswith("http") else self.cfg["api_base"] + path
        try:
            async with self.session.post(url, json=payload, headers=self._headers(),
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                txt = await r.text()
                if r.status >= 500:
                    log(f"serwer {r.status} na {path}: {txt[:120]}", "warn")
                    if not _from_queue:
                        self._enqueue(path, payload, timeout)
                    return None
                if r.status >= 400:
                    log(f"serwer {r.status} na {path}: {txt[:120]}", "warn")
                    return None
                try:
                    return json.loads(txt)
                except ValueError:
                    return {"raw": txt}
        except Exception as e:
            log(f"blad polaczenia z serwerem ({path}): {e}", "warn")
            if not _from_queue:
                self._enqueue(path, payload, timeout)
            return None

    def _enqueue(self, path, payload, timeout):
        if not any(path.startswith(d) for d in self.DURABLE):
            return
        try:
            self.QUEUE_DIR.mkdir(exist_ok=True)
            name = f"{int(time.time() * 1000)}.json"
            (self.QUEUE_DIR / name).write_text(
                json.dumps({"path": path, "payload": payload, "timeout": timeout},
                           ensure_ascii=False), encoding="utf-8")
            log(f"zapisano do kolejki: {path} - dosle, gdy serwer wroci", "warn")
        except OSError as e:
            log(f"NIE MOGE zapisac kolejki: {e}", "err")

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._flush_once()
            except Exception as e:
                log(f"blad dosylki: {type(e).__name__}: {e}", "warn")

    async def _flush_once(self):
        if not self.QUEUE_DIR.exists():
            return
        for f in sorted(self.QUEUE_DIR.glob("*.json")):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                f.rename(f.with_suffix(".bad"))
                continue
            r = await self.post(item["path"], item.get("payload"),
                                item.get("timeout", 90), _from_queue=True)
            if r is None:
                return  # serwer dalej lezy - kolejka czeka w spokoju
            f.unlink(missing_ok=True)
            log(f"dosylka z kolejki: {item['path']} ok", "ok")

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
        self.ws_dead_port = None   # port, na ktorym WS juz odmowil - nie mecz
        self.ws_failures = 0
        self.champ_ids = {}
        self.champ_keys_ci = {}    # klucz DD malymi literami -> (klucz, id)
        self.live_state = {}
        self._eog_task = None      # biezacy epizod lapania ekranu koncowego
        self._grade_done = True    # False tylko w trakcie epizodu
        self._eog_done = True

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

    async def submit_grade(self, data, source):
        """Wysyla ocene na serwer, jesli w danych faktycznie jest grade.
        Zwraca True po wyslaniu. Wolane z petli dopytujacej i z WebSocketu;
        _grade_done gasi dalsze odpytywanie. Wpis bez grade to jeszcze nie
        ocena - klient potrafi najpierw opublikowac szkielet, a grade
        dolozyc chwile pozniej, wiec wtedy pytamy dalej. POST, ktory padnie
        na 5xx/braku polaczenia, laduje w kolejce dyskowej - dane juz mamy,
        wiec epizod i tak liczy sie jako zamkniety."""
        entries = [e for e in (data if isinstance(data, list) else [data])
                   if isinstance(e, dict)]
        if not any(e.get("grade") for e in entries):
            return False
        self._grade_done = True
        r = await self.server.post("/grade", {"updates": data})
        for e in entries:
            if e.get("grade"):
                log(f"ocena ({source}): {e['grade']} (champion {e.get('championId')}, "
                    f"+{e.get('pointsGained')} pkt)", "ok")
        if r and r.get("errors"):
            log(f"blad zapisu oceny: {r['errors'][0]}", "warn")
        return True

    async def capture_grade(self):
        """Jedna proba odczytu oceny z LCU. Zwraca True, gdy zlapana."""
        data = await self.lcu.get(MASTERY_UPDATES)
        if not data:
            return False
        return await self.submit_grade(data, "poll")

    async def capture_eog(self):
        """Jedna proba odczytu ekranu koncowego: 183 pola statystyk, augmenty
        i wyniki pozostalych graczy - material na percentyle. Zwraca True,
        gdy blok zlapany (POST z kolejka dyskowa jak przy ocenie)."""
        block = await self.lcu.get(EOG_STATS_BLOCK, timeout=20)
        if not isinstance(block, dict) or not block:
            return False
        self._eog_done = True
        r = await self.server.post("/eog", {"block": block}, timeout=60)
        if r and r.get("stored"):
            log("statystyki koncowe zapisane" + (" (nowe)" if r.get("new") else " (aktualizacja)"), "ok")
        elif r and r.get("errors"):
            log(f"blad zapisu statystyk: {r['errors'][0]}", "warn")
        return True

    def _puuids_from_text(self, text, my_puuid=""):
        import re
        return [q for q in dict.fromkeys(
            re.findall(r'"puuid"\s*:\s*"([0-9a-f\-]{36})"', text))
            if q and q != my_puuid]

    async def snowball_harvest(self, texts):
        """Wysyla na serwer puuid-y graczy z podanych zrzutow eog."""
        if not self.cfg.get("snowball"):
            return
        me = await self.lcu.get("/lol-summoner/v1/current-summoner") or {}
        my = me.get("puuid", "")
        found = []
        for t in texts:
            found += self._puuids_from_text(t, my)
        found = list(dict.fromkeys(found))
        if not found:
            return
        r = await self.server.post("/snowball/candidates", {"puuids": found})
        if r:
            log(f"snowball: {r['received']} kandydatow, w rejestrze {r['known_total']}", "dim")

    async def snowball_loop(self):
        """Co ~60 s bierze jednego gracza z rejestru serwera i dosyla jego
        gry KIWI. Chodzi tylko przy bezczynnym kliencie (nie w champ selekcie,
        nie w grze) - LCU ma byc responsywne dla Ciebie, nie dla snowballa."""
        while True:
            await asyncio.sleep(60)
            try:
                if (self.cfg.get("snowball") != "on" or not self.lcu.port
                        or self.in_game or self.last_pool_key):
                    continue
                nxt = await self.session.get(
                    self.cfg["api_base"] + "/snowball/next",
                    timeout=aiohttp.ClientTimeout(total=15))
                data = await nxt.json()
                puuids = data.get("puuids") or []
                if not puuids:
                    continue
                pu = puuids[0]
                h = await self.lcu.get(
                    f"/lol-match-history/v1/products/lol/{pu}/matches"
                    f"?begIndex=0&endIndex=19", timeout=20)
                games = (h or {}).get("games", {}).get("games") or []
                r = await self.server.post("/snowball/ingest",
                                           {"puuid": pu, "games": games})
                if r is not None:
                    kiwi, new = r.get("kiwi") or 0, r.get("new_rows") or 0
                    if new:
                        log(f"snowball: gracz {pu[:8]}… — {kiwi} gier KIWI, "
                            f"+{new} wierszy statystyk", "ok")
                    elif kiwi:
                        # 0 nowych to dedup po game_id (kandydaci pochodza
                        # z Twoich meczow, wiec historie mocno sie nakladaja),
                        # nie awaria ingestu
                        log(f"snowball: gracz {pu[:8]}… — {kiwi} gier KIWI, "
                            "wszystkie juz w bazie (dedup)", "dim")
            except Exception as e:
                log(f"snowball: {type(e).__name__}: {e}", "dim")

    async def snowball_probe(self):
        """Jednorazowa sonda pod snowball: czy /lol-match-history dziala dla
        OBCYCH puuid. Wlacza sie tylko przy "snowball": "probe" w configu.
        Bierze puuid innego gracza z najnowszego zrzutu eog i pyta o 1 gre.
        Dedup meczow po gameId juz istnieje po stronie serwera, wiec docelowy
        snowball nie zdubluje danych powtarzajacych sie graczy."""
        if self.cfg.get("snowball") != "probe":
            return
        import re
        dumps = sorted((HERE / "dumps").glob("*eog-stats-block*.json"))
        if not dumps:
            log("sonda snowball: brak zrzutow eog w agent/dumps", "warn")
            return
        text = dumps[-1].read_text(encoding="utf-8", errors="ignore")
        me = await self.lcu.get("/lol-summoner/v1/current-summoner") or {}
        my = me.get("puuid", "")
        others = [q for q in dict.fromkeys(
            re.findall(r'"puuid"\s*:\s*"([0-9a-f\-]{36})"', text)) if q and q != my]
        if not others:
            log("sonda snowball: nie znalazlem obcego puuid w zrzucie", "warn")
            return
        status, body = await self.lcu.get_raw(
            f"/lol-match-history/v1/products/lol/{others[0]}/matches"
            f"?begIndex=0&endIndex=1", timeout=15)
        n = "?"
        try:
            n = len((json.loads(body).get("games") or {}).get("games") or [])
        except Exception:
            pass
        log(f"sonda snowball: HTTP {status}, gier w odpowiedzi: {n}",
            "ok" if status == 200 else "warn")

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
        # rotacja: dumps to material diagnostyczny, nie archiwum - wszystko
        # trwale i tak plynie do bazy na serwerze
        keep = int(self.cfg.get("dumps_keep", 60))
        old_files = sorted(out_dir.glob("*.json"))[:-keep] if keep else []
        for f in old_files:
            f.unlink(missing_ok=True)
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
            # Osobne zadanie, nie await: petla dopytujaca trwa do 2 min,
            # a w tym czasie WebSocket i polling maja dalej obslugiwac
            # zdarzenia (w tym event z ocena, ktory te petle wygasza).
            if self._eog_task is None or self._eog_task.done():
                self._eog_task = asyncio.create_task(self.post_game_capture(phase))

    async def post_game_capture(self, first_phase):
        """Ekran koncowy nie jest gotowy w chwili konca gry: WaitingForStats
        znaczy doslownie "czekam na statystyki", a maestria potrafi przyjsc
        jeszcze pozniej (kiedy dokladnie - rozstrzygna ponizsze logi).
        Zamiast strzelac raz i sie poddawac, dopytujemy: co eog_retry_seconds
        az oba endpointy oddadza dane, klient sprzatnie ekran koncowy albo
        skonczy sie budzet eog_wait_seconds. Rownolegle ocena moze przyjsc
        z WebSocketu - flagi _grade_done/_eog_done gasza wtedy petle."""
        self._grade_done = False
        self._eog_done = False
        deadline = time.monotonic() + float(self.cfg.get("eog_wait_seconds", 120))
        retry = float(self.cfg.get("eog_retry_seconds", 2))
        attempt, gone, phase, logged = 0, 0, first_phase, ""
        while time.monotonic() < deadline:
            attempt += 1
            if attempt > 1:
                # pierwsza proba strzela od razu (endpointy sa ulotne),
                # kazda kolejna najpierw czyta fazę - log ma pokazywac
                # stan z chwili proby, nie sprzed cyklu
                phase = await self.lcu.get("/lol-gameflow/v1/gameflow-phase") or phase
            if not self._grade_done:
                await self.capture_grade()
            if not self._eog_done:
                await self.capture_eog()
            state = (f"faza {phase}, ocena: {'jest' if self._grade_done else 'brak'}, "
                     f"statystyki: {'sa' if self._eog_done else 'brak'}")
            # log przy kazdej zmianie (fazy albo zlapania) + puls co 10 prob
            if state != logged or attempt % 10 == 0:
                log(f"eog: proba {attempt}, {state}", "dim")
                logged = state
            if self._grade_done and self._eog_done:
                break
            # klient zszedl z ekranu koncowego - te dane juz nie wroca
            if phase in ("None", "Lobby", "TerminatedInError"):
                gone += 1
                if gone >= 3:
                    break
            else:
                gone = 0
            await asyncio.sleep(retry)
        if not self._grade_done:
            log(f"ocena nie pojawila sie (prob {attempt}, ostatnia faza {phase}) - "
                "tryb bez maestrii albo LCU jej nie oddal", "warn")
        if not self._eog_done:
            log(f"ekran koncowy nie oddal statystyk (ostatnia faza {phase})", "warn")
        self._grade_done = True
        self._eog_done = True
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
                await asyncio.sleep(self.cfg.get("live_poll_seconds", 2))
                continue

            try:
                await self.send_live(data)
                if not was_live:
                    was_live = True
                    log("dane na zywo aktywne", "ok")
            except Exception as e:
                log(f"blad odczytu na zywo: {type(e).__name__}: {e}", "warn")

            await asyncio.sleep(self.cfg.get("live_poll_seconds", 2))

    def resolve_champion(self, player):
        """(klucz_dd, champion_id) dla gracza z Live Client Data.
        championName jest w locale klienta gry, wiec dopasowanie po nim
        pekalo na przypadkach brzegowych (Wukong -> MonkeyKing,
        FiddleSticks, kropki i apostrofy). rawChampionName niesie nazwe
        wewnetrzna - po obcieciu prefixu dopasowujemy ja do kluczy DD
        bez zgadywania. Stara heurystyka zostaje jako zapas."""
        raw = (player.get("rawChampionName") or "").strip()
        internal = (raw[len(RAW_CHAMP_PREFIX):]
                    if raw.lower().startswith(RAW_CHAMP_PREFIX) else "")
        hit = self.champ_keys_ci.get(internal.lower()) if internal else None
        if hit:
            return hit
        name = ((player.get("championName") or "")
                .replace(" ", "").replace("'", "").replace(".", ""))
        return self.champ_keys_ci.get(name.lower()) or (None, None)

    async def send_live(self, data):
        ap = data.get("activePlayer") or {}
        gd = data.get("gameData") or {}
        name = ap.get("summonerName") or ap.get("riotId") or ""

        me = None
        for pl in data.get("allPlayers") or []:
            cand = [pl.get("summonerName"), pl.get("riotId"), pl.get("riotIdGameName")]
            if name and any(c and (c == name or str(c).startswith(name)) for c in cand):
                me = pl
                break
        if me is None:
            return

        sc = me.get("scores") or {}
        gold_now = ap.get("currentGold") or 0
        gt = gd.get("gameTime") or 0

        # Zlota zarobionego nie ma w API, a suma cen przedmiotow nie wystarcza:
        # kowadla (750 zl) znikaja po uzyciu, mikstury sie zuzywaja, sprzedaz
        # zwraca czesc. Liczymy wiec wydatki z ubytkow stanu zlota - kazde
        # kupno to spadek, niezaleznie od tego, czy cos zostalo w ekwipunku.
        if gt < self.live_state.get("game_time", 0) - 5 or \
           me.get("championName") != self.live_state.get("champion"):
            self.live_state = {"spent": 0.0, "earned": 0.0,
                               "champion": me.get("championName")}

        prev = self.live_state.get("gold")
        if prev is not None:
            delta = gold_now - prev
            if delta > 0:
                # przyrost stanu = zarobek (minony, zabojstwa, dochod pasywny)
                # albo zwrot ze sprzedazy - obu nie da sie rozroznic, ale
                # sumowanie przyrostow jest odporne na cykle kup/sprzedaj,
                # ktore zawyzaly licznik oparty wylacznie na wydatkach
                self.live_state["earned"] = self.live_state.get("earned", 0.0) + delta
            else:
                self.live_state["spent"] = self.live_state.get("spent", 0.0) - delta
        self.live_state["gold"] = gold_now
        self.live_state["game_time"] = gt

        spent = self.live_state.get("spent", 0.0)
        earned = self.live_state.get("earned", 0.0)

        # Zloto startowe rozni sie miedzy trybami; bierzemy pierwszy
        # zaobserwowany stan jako punkt odniesienia.
        if "start_gold" not in self.live_state:
            self.live_state["start_gold"] = gold_now
        gold_est = int(self.live_state["start_gold"] + earned)
        inventory = sum((it.get("price") or 0) * (it.get("count") or 1)
                        for it in (me.get("items") or []))

        champ_id = self.resolve_champion(me)[1]
        await self.server.post("/live", {
            "champion": me.get("championName"),
            "champion_id": champ_id,
            "game_mode": gd.get("gameMode"),
            "game_time": gt,
            "kills": sc.get("kills"), "deaths": sc.get("deaths"),
            "assists": sc.get("assists"), "cs": sc.get("creepScore"),
            "ward_score": sc.get("wardScore"),
            "gold_est": gold_est, "level": me.get("level"),
            "raw": {"spent": round(spent), "earned": round(earned),
                    "start_gold": round(self.live_state.get("start_gold", 0)),
                    "current_gold": gold_now,
                    "inventory_value": inventory,
                    "consumed": round(spent - inventory),
                    "items": [i.get("displayName") for i in (me.get("items") or [])]},
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
            self.champ_keys_ci = {k.lower(): (k, cid)
                                  for k, cid in self.champ_ids.items()}
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

        KLUCZOWE: osobna sesja aiohttp tylko dla WS. Wspolna sesja agenta
        trzyma w puli ciepłe polaczenia keep-alive do LCU (polling REST
        co 10 s), a ws_connect na takiej sesji dostawal polaczenie Z PULI -
        serwer RiotRemoting odrzuca upgrade na uzytym polaczeniu i stad
        byl wieczny 404. Sonda ws-probe (31.08): z czystej sesji przechodzi
        KAZDY wariant naglowkow, w tym z podprotokolem wamp - wiec warianty
        i Origin wylecialy, zostal najprostszy handshake jak w Willump
        i league-connect. Wiadomosci to nadal format WAMP 1.0, ale samego
        podprotokolu nie trzeba negocjowac."""
        async with aiohttp.ClientSession() as ws_session:
            ws_ctx = await ws_session.ws_connect(
                f"wss://127.0.0.1:{self.lcu.port}/",
                headers=self.lcu.headers, ssl=self.lcu.ssl, heartbeat=30)
            async with ws_ctx as ws:
                # opcode 5 = subscribe
                await ws.send_str(json.dumps([5, "OnJsonApiEvent_lol-gameflow_v1_gameflow-phase"]))
                await ws.send_str(json.dumps([5, "OnJsonApiEvent_lol-champ-select_v1_session"]))
                await ws.send_str(json.dumps(
                    [5, "OnJsonApiEvent_lol-end-of-game_v1_champion-mastery-updates"]))
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
                    elif uri.endswith("/champion-mastery-updates"):
                        # Ocena wypchnieta przez klienta w momencie powstania -
                        # zero zgadywania fazy. Dziala tez, gdy petla dopytujaca
                        # akurat nie biegnie (backend deduplikuje po meczu).
                        if data and await self.submit_grade(data, "ws"):
                            log("ocena zlapana z WebSocketu", "ok")

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.lcu.session = session
            self.server = Server(self.cfg, session)

            log("agent startuje")
            log(f"serwer: {self.cfg['api_base']}", "dim")

            await self.load_champ_ids()
            asyncio.create_task(self.poll_loop())
            asyncio.create_task(self.snowball_loop())
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
                    await self.snowball_probe()
                    if self.cfg.get("snowball"):
                        texts = [f.read_text(encoding="utf-8", errors="ignore")
                                 for f in sorted((HERE / "dumps").glob("*eog-stats-block*.json"))]
                        await self.snowball_harvest(texts)
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
