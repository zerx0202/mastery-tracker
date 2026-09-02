"""Harness logiki agenta — bez zywego LCU (raport 2.09, P2).

agent/agent.py nie jest pakietem, wiec ladujemy go po sciezce. Zalezy od
aiohttp (requirements agenta) - przy braku testy pomijaja sie same, ale
CI instaluje aiohttp jawnie, zeby ta warstwa nie byla martwa.

Pokrycie: ekstrakcja puuid, klucz dedup puli z rotacja, kolejka dyskowa
(zapis przy awarii, pomijanie 4xx, dosylka, zatruta glowa - bug z raportu),
submit_grade bez pola grade."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_under_test",
    Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
MY = "99999999-9999-9999-9999-999999999999"


class FakeResp:
    def __init__(self, status, text="{}"):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text


class FakeSession:
    """Kazde post() zdejmuje kolejny scenariusz z listy: int = status HTTP,
    Exception = awaria polaczenia."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(url)
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        return FakeResp(step)


class FakeServer:
    """Rejestrator POST-ow do testow logiki champ selecta i odzysku.
    Odpowiedz udaje szczesliwy backend (new/stored), bo czesc logiki agenta
    czyta wynik: odzysk P6 uznaje sukces dopiero po new > 0."""

    def __init__(self):
        self.posts = []

    async def post(self, path, payload=None, timeout=90):
        self.posts.append((path, payload))
        return {"new": 1, "stored": True}


def make_server(tmp_path, script):
    srv = ag.Server({"api_base": "http://backend"}, FakeSession(script))
    srv.QUEUE_DIR = tmp_path / "queue"
    return srv


# ---------- puuidy ----------

def test_puuids_from_text_dedup_and_self():
    a = ag.Agent({"api_base": "http://backend"})
    text = json.dumps({"players": [
        {"puuid": U1}, {"puuid": MY}, {"puuid": U2}, {"puuid": U1}]})
    assert a._puuids_from_text(text, my_puuid=MY) == [U1, U2]


# ---------- klucz dedup puli ----------

def _sess(bench, team, me_cell=0):
    return {
        "benchEnabled": True,
        "benchChampions": [{"championId": c} for c in bench],
        "myTeam": [{"cellId": i, "championId": c} for i, c in enumerate(team)],
        "localPlayerCellId": me_cell,
    }


def _pool_agent():
    a = ag.Agent({"api_base": "http://backend"})

    class _Lcu:
        port = "1"

        async def get(self, path, timeout=8):
            return {"gameData": {"queue": {"gameMode": "KIWI", "id": 2400}}}

    a.lcu = _Lcu()
    a.server = FakeServer()
    a.pre_snapshot_done = True   # snapshot to osobny tor, nie ten test
    return a


def test_pool_dedup_and_bench_rotation():
    async def run():
        a = _pool_agent()
        # lawka {10,11}, ja gram 20, sojusznicy 21 i 22
        await a.handle_champ_select(_sess([10, 11], [20, 21, 22]))
        await a.handle_champ_select(_sess([10, 11], [20, 21, 22]))  # bez zmian
        # rotacja sojusznika z lawka: 21 <-> 10, unia puli identyczna
        await a.handle_champ_select(_sess([21, 11], [20, 10, 22]))
        return a.server.posts

    posts = asyncio.run(run())
    lobby = [p for path, p in posts if path == "/lobby"]
    assert len(lobby) == 2, "dedup ma polknac powtorke, ale NIE rotacje"
    assert lobby[0]["champion_ids"] == lobby[1]["champion_ids"]
    assert lobby[0]["trade_ids"] == [21, 22]
    assert lobby[1]["trade_ids"] == [10, 22]


def test_pool_exit_clears_state():
    async def run():
        a = _pool_agent()
        await a.handle_champ_select(_sess([10], [20, 21]))
        await a.handle_champ_select(None)
        assert a.last_pool_key is None
        return a.server.posts

    posts = asyncio.run(run())
    assert posts[-1][0] == "/lobby" and posts[-1][1]["champion_ids"] == []


# ---------- kolejka dyskowa ----------

def test_network_error_enqueues_durable(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("brak sieci")])
        await srv.post("/grade", {"updates": [{"grade": "A-"}]})

    asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 1
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert item["path"] == "/grade"


def test_4xx_on_direct_durable_post_saved_as_bad(tmp_path):
    """Partia A (K3): trwale 4xx na bezposrednim POST (np. 401 po rotacji
    tokenu) odrzucalo ocene bez zadnej kopii - mechanizm .bad istnial tylko
    we flushu. Po fixie payload laduje na dysku do recznego odzysku."""
    async def run():
        srv = make_server(tmp_path, [401])
        await srv.post("/grade", {"updates": [{"grade": "S-"}]})

    asyncio.run(run())
    q = tmp_path / "queue"
    assert not list(q.glob("*.json")), "4xx nie idzie do dosylki"
    bad = list(q.glob("*.bad"))
    assert len(bad) == 1
    item = json.loads(bad[0].read_text(encoding="utf-8"))
    assert item["path"] == "/grade"
    assert item["payload"] == {"updates": [{"grade": "S-"}]}


def test_4xx_on_nondurable_post_leaves_no_trace(tmp_path):
    async def run():
        srv = make_server(tmp_path, [401])
        await srv.post("/live", {"ended": True})

    asyncio.run(run())
    q = tmp_path / "queue"
    assert not q.exists() or not list(q.glob("*"))


def test_eventdata_is_durable(tmp_path):
    """Partia A (K1): /eventdata nie bylo w DURABLE, wbrew komentarzowi
    w live_loop - log zdarzen ginal przy lezacym backendzie."""
    async def run():
        srv = make_server(tmp_path, [ConnectionError("backend lezy")])
        await srv.post("/eventdata", {"events": [{"EventName": "ChampionKill"}],
                                      "champion_id": 45})

    asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["path"] == "/eventdata"


def test_ensure_flush_starts_without_any_post(tmp_path):
    """Partia A (A3): dosylka startowala dopiero przy pierwszym post(),
    a kazdy post() wymagal zywego klienta LoL - wpisy z poprzedniego
    uruchomienia lezaly godzinami mimo zywego backendu."""
    async def run():
        srv = make_server(tmp_path, [])
        assert srv._flush_task is None
        srv.ensure_flush()
        t1 = srv._flush_task
        srv.ensure_flush()
        assert srv._flush_task is t1, "wielokrotne wywolanie = jeden task"
        t1.cancel()

    asyncio.run(run())


def test_flush_delivers_and_consumes(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), 200])
        await srv.post("/eog", {"block": {}})
        await srv._flush_once()

    asyncio.run(run())
    assert not list((tmp_path / "queue").glob("*.json"))


def test_flush_poisoned_head_does_not_block_queue(tmp_path):
    """Bug z raportu (P1): element odrzucony 4xx stawal w glowie kolejki
    i nic za nim nigdy nie wychodzilo. Po fixie: 4xx -> .bad, reszta idzie."""
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), ConnectionError("x"),
                                     422, 200])
        # dwa wpisy w tej samej milisekundzie - kiedys nadpisywaly sie
        # w kolejce (nazwa = sam timestamp ms); licznik w nazwie to lata
        await srv.post("/grade", {"updates": [{"zly": "ksztalt"}]})
        await srv.post("/eog", {"block": {"gameId": 7}})
        await srv._flush_once()

    asyncio.run(run())
    q = tmp_path / "queue"
    assert not list(q.glob("*.json")), "zdrowy element ma wyjsc mimo zatrutej glowy"
    bad = list(q.glob("*.bad"))
    assert len(bad) == 1, "odrzucony 4xx ma zostac odlozony jako .bad"


def test_flush_stops_on_server_down(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), ConnectionError("x"),
                                     ConnectionError("wciaz lezy")])
        await srv.post("/grade", {"updates": []})
        await srv.post("/eog", {"block": {}})
        await srv._flush_once()
        return srv.session.calls

    calls = asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 2, "przy lezacym serwerze kolejka czeka w spokoju"
    assert len(calls) == 3, "dosylka ma stanac na pierwszym bledzie, nie mielic reszty"


# ---------- konsola LCU (42) + odzysk gier (P6) ----------

class FakeGetSession:
    """GET-y agenta do backendu (probe/pending, history/missing).
    Agent robi `await session.get(...)` bez context managera (wzorzec ze
    snowball_loop), wiec get jest korutyna oddajaca obiekt z .json()."""

    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def get(self, url, timeout=None):
        body = self.payloads.pop(0) if self.payloads else {}

        class R:
            async def json(self):
                return body
        return R()


class FakeLcuRaw:
    port = "1"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get_raw(self, path, timeout=8):
        self.calls.append(path)
        return 200, self.responses.get(path, "{}")

    async def get(self, path, timeout=8):
        self.calls.append(path)
        return self.responses.get(path)


def test_probe_once_executes_and_reports():
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.session = FakeGetSession([{"probes": [{"id": 7, "path": "/lol-x"}]}])
        a.lcu = FakeLcuRaw({"/lol-x": '{"ok": 1}'})
        a.server = FakeServer()
        n = await a._probe_once()
        return n, a.server.posts

    n, posts = asyncio.run(run())
    assert n == 1
    assert posts == [("/probe/result",
                      {"id": 7, "http_status": 200, "response": '{"ok": 1}'})]


def test_own_slice_picks_me_by_puuid():
    g = {"gameId": 9, "gameMode": "KIWI",
         "participants": [{"participantId": 1, "championId": 45},
                          {"participantId": 2, "championId": 99}],
         "participantIdentities": [
             {"participantId": 1, "player": {"puuid": U1}},
             {"participantId": 2, "player": {"puuid": MY}}]}
    out = ag.Agent.own_slice(g, MY)
    assert [p["championId"] for p in out["participants"]] == [99]
    assert out["participantIdentities"][0]["player"]["puuid"] == MY
    assert g["participants"][0]["championId"] == 45  # oryginal nietkniety
    assert ag.Agent.own_slice(g, "nie-ma-mnie") is None


def test_recover_once_posts_full_game():
    """Partia D: own_slice wyrzucal 9/10 pobranego materialu (statystyki
    i puuidy pozostalych graczy - paliwo norm i karty 9). Agent wysyla
    teraz PELNA gre; przycina backend, ktory umie ja skonsumowac.
    own_slice zostaje jako walidacja 'czy to moja gra'."""
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.session = FakeGetSession([{"game_ids": [9]}])
        a.lcu = FakeLcuRaw({
            "/lol-summoner/v1/current-summoner": {"puuid": MY},
            "/lol-match-history/v1/games/9": {
                "gameId": 9,
                "participants": [{"participantId": 1, "championId": 45},
                                 {"participantId": 2, "championId": 99}],
                "participantIdentities": [
                    {"participantId": 1, "player": {"puuid": U1}},
                    {"participantId": 2, "player": {"puuid": MY}}]},
        })
        a.server = FakeServer()
        ok = await a._recover_once()
        return ok, a.server.posts

    ok, posts = asyncio.run(run())
    assert ok is True
    path, payload = posts[0]
    assert path == "/history/lcu"
    assert payload["games"][0]["gameId"] == 9
    assert len(payload["games"][0]["participants"]) == 2, \
        "pelny obiekt - bez przycinania po stronie agenta"


def test_recover_skips_poisoned_head(tmp_path):
    """Partia A (A4): odzysk bral zawsze glowe listy (limit=1, DESC) i po
    kazdym niepowodzeniu wracal do tej samej gry - jedna niepobieralna gra
    blokowala wszystkie starsze na zawsze (ta sama klasa co zatruta kolejka
    z 2.09). Po fixie: po RECOVER_MAX_FAILS probach gid jest pomijany
    do restartu agenta, a odzysk idzie dalej."""
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        missing = {"game_ids": [9, 8]}
        # kazde _recover_once robi jeden GET /history/missing
        a.session = FakeGetSession([missing] * (ag.RECOVER_MAX_FAILS + 1))
        a.lcu = FakeLcuRaw({
            "/lol-summoner/v1/current-summoner": {"puuid": MY},
            # gra 9: LCU stale nie oddaje danych (None); gra 8: zdrowa
            "/lol-match-history/v1/games/8": {
                "gameId": 8,
                "participants": [{"participantId": 2, "championId": 99}],
                "participantIdentities": [
                    {"participantId": 2, "player": {"puuid": MY}}]},
        })
        a.server = FakeServer()
        results = []
        for _ in range(ag.RECOVER_MAX_FAILS + 1):
            results.append(await a._recover_once())
        return results, a.server.posts, dict(a._recover_fails)

    results, posts, fails = asyncio.run(run())
    assert results[:ag.RECOVER_MAX_FAILS] == [False] * ag.RECOVER_MAX_FAILS
    assert results[-1] is True, "po odstawieniu gry 9 odzysk siega po gre 8"
    assert fails.get(9) == ag.RECOVER_MAX_FAILS
    lcu_posts = [p for path, p in posts if path == "/history/lcu"]
    assert len(lcu_posts) == 1
    assert lcu_posts[0]["games"][0]["gameId"] == 8


def test_recover_counts_server_rejection_as_failure():
    """Serwer potrafi polknac wyjatek zapisu w 200 z new=0 i errors -
    agent logowal to jako sukces i mielil te sama gre co 120 s."""
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.session = FakeGetSession([{"game_ids": [9]}])
        a.lcu = FakeLcuRaw({
            "/lol-summoner/v1/current-summoner": {"puuid": MY},
            "/lol-match-history/v1/games/9": {
                "gameId": 9,
                "participants": [{"participantId": 2, "championId": 99}],
                "participantIdentities": [
                    {"participantId": 2, "player": {"puuid": MY}}]},
        })

        class RejectingServer(FakeServer):
            async def post(self, path, payload=None, timeout=90):
                self.posts.append((path, payload))
                return {"received": 1, "new": 0,
                        "errors": ["ValueError: zly ksztalt"]}

        a.server = RejectingServer()
        ok = await a._recover_once()
        return ok, dict(a._recover_fails)

    ok, fails = asyncio.run(run())
    assert ok is False
    assert fails.get(9) == 1


# ---------- czarna skrzynka (E) ----------

def test_report_health_counts_queue_files(tmp_path):
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.server = FakeServer()
        a.server.QUEUE_DIR = tmp_path / "queue"
        a.server.QUEUE_DIR.mkdir()
        (a.server.QUEUE_DIR / "1.json").write_text("{}")
        (a.server.QUEUE_DIR / "2.json").write_text("{}")
        (a.server.QUEUE_DIR / "3.bad").write_text("{}")
        a.ws_failures = 2
        await a.report_health()
        return a.server.posts

    posts = asyncio.run(run())
    path, payload = posts[0]
    assert path == "/agent/health"
    assert payload == {"queue": 2, "bad": 1, "ws_ok": False}


def test_incident_is_durable(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("backend lezy")])
        await srv.post("/agent/incident", {"kind": "start"})

    asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["path"] == "/agent/incident"


# ---------- epizod pomeczowy (post_game_capture) ----------

class FakeLcuEpisode(FakeLcuRaw):
    """Odpowiedzi zalezne od numeru proby - ocena i eog pojawiaja sie
    dopiero za drugim odczytem, jak w realnym oknie PreEndOfGame."""

    def __init__(self):
        super().__init__({})
        self.tries = {}

    async def get(self, path, timeout=8):
        self.calls.append(path)
        n = self.tries[path] = self.tries.get(path, 0) + 1
        if path == "/lol-gameflow/v1/gameflow-phase":
            return "PreEndOfGame"
        if path == ag.MASTERY_UPDATES:
            return [{"grade": "S-", "championId": 45, "gameId": 9,
                     "pointsGained": 800}] if n >= 2 else None
        if path == ag.EOG_STATS_BLOCK:
            return {"gameId": 9, "teams": []} if n >= 2 else {}
        return None


def test_post_game_capture_sends_grade_eog_and_closes_episode():
    """Jedyny moment lapania ulotnej oceny nie mial harnessu (luka
    z przegladu 2.09) - sekwencja: dopytywanie do skutku, POST /grade
    i /eog, potem snapshot i sync. Fazy koncowe i retry w minutach sa
    sciete configiem do ulamkow sekund."""
    async def run():
        a = ag.Agent({"api_base": "http://backend",
                      "eog_wait_seconds": 2, "eog_retry_seconds": 0.01,
                      "post_game_delay_seconds": 0, "enable_dumps": False,
                      "history_pages_after_game": 1})
        a.lcu = FakeLcuEpisode()
        a.server = FakeServer()
        await a.post_game_capture("WaitingForStats")
        return a

    a = asyncio.run(run())
    paths = [p for p, _ in a.server.posts]
    assert "/grade" in paths and "/eog" in paths
    assert a._grade_done and a._eog_done
    gi = paths.index("/grade")
    assert "/snapshot" in paths[gi:], "snapshot po grze domyka parowanie oceny"
    grade_payload = dict(a.server.posts)["/grade"]
    assert grade_payload["updates"][0]["grade"] == "S-"


# ---------- akwizycja timelines ----------

def test_timeline_once_fetches_and_posts():
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.session = FakeGetSession([{"game_ids": [9]}])
        a.lcu = FakeLcuRaw({
            "/lol-match-history/v1/game-timelines/9": {
                "frames": [{"timestamp": 0, "participantFrames": {}}]},
        })
        a.server = FakeServer()
        ok = await a._timeline_once()
        return ok, a.server.posts

    ok, posts = asyncio.run(run())
    assert ok is True
    path, payload = posts[0]
    assert path == "/timeline"
    assert payload["game_id"] == 9
    assert payload["timeline"]["frames"]


def test_timeline_once_counts_empty_as_failure_and_skips():
    """Pusty/martwy timeline nie moze mielic w kolo tej samej gry -
    ta sama skip-lista co w odzysku statystyk (A4)."""
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.session = FakeGetSession(
            [{"game_ids": [9, 8]}] * (ag.RECOVER_MAX_FAILS + 1))
        a.lcu = FakeLcuRaw({
            # gra 9: LCU oddaje pusty obiekt; gra 8: zdrowy timeline
            "/lol-match-history/v1/game-timelines/9": {"frames": []},
            "/lol-match-history/v1/game-timelines/8": {
                "frames": [{"timestamp": 0}]},
        })
        a.server = FakeServer()
        results = [await a._timeline_once()
                   for _ in range(ag.RECOVER_MAX_FAILS + 1)]
        return results, a.server.posts, dict(a._timeline_fails)

    results, posts, fails = asyncio.run(run())
    assert results[-1] is True, "po odstawieniu gry 9 kolej na gre 8"
    assert fails.get(9) == ag.RECOVER_MAX_FAILS
    assert [p["game_id"] for _, p in posts if _ == "/timeline"] == [8]


# ---------- ocena ----------

def test_submit_grade_ignores_entries_without_grade():
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.server = FakeServer()
        sent = await a.submit_grade([{"championId": 45}], "poll")
        return sent, a.server.posts

    sent, posts = asyncio.run(run())
    assert sent is False and posts == []
