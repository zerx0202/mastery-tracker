"""Partia F (3.09, po sondach C1/C2 i zrzutach z produkcji):

(1) krotnosc szczebla w E(c) - drabinka IV->5 (bonus milestone, misja
    przepustki po domknieciu IV) wymaga S- x2, a koszt szczebla byl 1/p
    niezaleznie od krotnosci; przy celu 4 bez skutkow, przy celu 5 zanizal
    ostatni szczebel dwukrotnie. Ta sama krotnosc w projekcji misji
    i symulatorze strategii.
(5) scoring puli champ selecta liczony RAZ na (pula, snapshot, model)
    zamiast na kazde odpytanie /lobby co 4 s - w oknie champ selecta lista
    i plakietki nie nadazaly.
(2+3) System: liczniki potoku bez falszywych alarmow - custom (KIWI_CUSTOM)
    z definicji nie ma oceny, pula bez zadnej gry to dodge/trening; realny
    przeciek to pula, po ktorej byla gra NIEPRZYPISANA do zadnej puli.
(6) augmenty sciagi per tier: JSON-LD strony buildu sortuje pryzmatyczne
    na wierzch (pozycje 1-6), wiec "top 5" to zawsze same pryzmatyczne;
    karty na stronie niosa tier w klasie text-rarity-* + title."""
import importlib.util
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import balance, db, scoring
from app.main import app
from tests.conftest import insert_row

LADDER = {
    0: {"require_grades": {"A-": 1}, "games": 1, "reward_marks": 1, "bonus": False},
    1: {"require_grades": {"A-": 1}, "games": 1, "reward_marks": 1, "bonus": False},
    2: {"require_grades": {"S-": 1}, "games": 1, "reward_marks": 2, "bonus": False},
    3: {"require_grades": {"S-": 1}, "games": 1, "reward_marks": 2, "bonus": False},
    4: {"require_grades": {"S-": 2}, "games": 2, "reward_marks": 1, "bonus": True},
}
PRIOR = {"A-": 0.5, "S-": 0.1}


# ---------- (1) krotnosc szczebla ----------

def test_expected_games_counts_required_grades():
    # IV->5 to S- x2: oczekiwane 2/p gier, nie 1/p
    total, steps, known = scoring.expected_games(1, 4, 5, LADDER, {}, PRIOR)
    assert known and total == 20
    assert (steps[0]["need"], steps[0]["have"], steps[0]["remaining"]) == (2, 0, 2)


def test_expected_games_credits_grades_already_earned():
    # jedna S juz uzbierana na biezacym szczeblu -> zostaje jedna ocena
    total, steps, _ = scoring.expected_games(
        1, 4, 5, LADDER, {}, PRIOR, grades_earned=["S", "B+"])
    assert total == 10 and steps[0]["have"] == 1 and steps[0]["remaining"] == 1
    # nadwyzka bez awansu (opoznienie snapshotu) nie daje darmowego szczebla
    _, steps2, _ = scoring.expected_games(
        1, 4, 5, LADDER, {}, PRIOR, grades_earned=["S", "S+", "S-"])
    assert steps2[0]["remaining"] == 1


def test_expected_games_future_rungs_use_full_count():
    # z III: III->IV (S- x1) + IV->5 (S- x2); uzbierane oceny dotycza
    # wylacznie biezacego szczebla
    total, steps, _ = scoring.expected_games(
        1, 3, 5, LADDER, {}, PRIOR, grades_earned=["B+"])
    assert [s["remaining"] for s in steps] == [1, 2] and total == 30
    assert scoring.expected_games_prior_only(4, 5, LADDER, PRIOR) == 20
    assert scoring.expected_games_prior_only(4, 5, LADDER, PRIOR, ["S"]) == 10


def test_score_rows_exposes_rung_count_for_ui():
    rows = [{"champion_id": 1, "milestone": 4, "points": 0, "grades_earned": ["S"]},
            {"champion_id": 2, "milestone": 3, "points": 0, "grades_earned": []}]
    scoring.score_rows(rows, LADDER, {}, PRIOR, 5)
    lead = rows[0]
    assert lead["champion_id"] == 1                  # 10 gier < 30 gier
    assert (lead["next_grade"], lead["next_need"], lead["next_have"]) == ("S-", 2, 1)
    assert lead["expected_games"] == 10 and lead["steps_remaining"] == 1


def test_mission_projection_counts_required_grades(fresh_db, monkeypatch):
    """Dwoch championow na IV, p=1 (kazda gra to sukces): bonus milestone
    wymaga DWOCH ocen, wiec mediana to 2 gry, nie 1."""
    from app import model
    ts = 1700000000
    with db.connect() as con:
        insert_row(con, "snapshot", id=1, taken_at=ts, split_id=1)
        for cid in (1, 2):
            insert_row(con, "mastery", snapshot_id=1, champion_id=cid,
                       milestone=4, points=0, level=1)
        insert_row(con, "milestone_ladder", from_milestone=4,
                   require_grades='{"S-": 2}', games=2, bonus=1, observed_at=ts)
    monkeypatch.setattr(model, "champion_rates", lambda mode=None, **k: {
        "champions": {}, "prior": {"S-": 1.0, "A-": 1.0}})
    out = model.mission_projection(5, runs=20, pool_size=2, seed=1)
    assert out["median"] == 2 and out["p75"] == 2


def test_simulate_needs_two_grades_for_double_rung(fresh_db):
    spec = importlib.util.spec_from_file_location(
        "simulate_under_test_f",
        Path(__file__).resolve().parents[1] / "tools" / "simulate.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    ladder = {3: {"require_grades": {"S-": 2}, "games": 2, "reward_marks": 1,
                  "bonus": False}}
    prior = {"S-": 1.0, "A-": 1.0}
    # GOAL symulatora = 4 (env); szczebel III->IV x2 przy p=1 = dokladnie 2 gry
    r = sim.simulate(sim.pick_expected, {81: 3, 516: 3}, {}, prior,
                     pool_size=2, ladder=ladder, runs=5)
    assert r == [2, 2, 2, 2, 2]


def test_split_progress_counts_champions_at_or_above_goal(fresh_db, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "GOAL", 5)
    ts = 1700000000
    with db.connect() as con:
        insert_row(con, "snapshot", id=1, taken_at=ts, split_id=1)
        for cid, ms in ((1, 5), (2, 6), (3, 4)):
            insert_row(con, "mastery", snapshot_id=1, champion_id=cid,
                       milestone=ms, points=0, level=1)
    client = TestClient(app, raise_server_exceptions=False)
    d = client.get("/api/split/progress").json()
    assert d["goal"] == 5 and d["at_goal"] == 2


# ---------- (5) scoring puli raz na pule ----------

def _entry(cid, milestone, grades=None):
    return {"championId": cid, "championLevel": 5, "championPoints": 1000,
            "lastPlayTime": 0, "championSeasonMilestone": milestone,
            "tokensEarned": 0, "markRequiredForNextLevel": 2,
            "nextSeasonMilestone": {"requireGradeCounts": {"A-": 1},
                                    "totalGamesRequires": 1, "rewardMarks": 1,
                                    "bonus": False},
            "milestoneGrades": grades or []}


def _world(now):
    db.save_champions([(45, "Veigar", "Veigar"), (12, "Alistar", "Alistar"),
                       (99, "Lux", "Lux")])
    entries = [_entry(45, 1), _entry(12, 0), _entry(99, 2)]
    db.learn_ladder(entries, now)
    db.save_snapshot(now, entries)


def _counting_targets(monkeypatch):
    from app import main
    main._LOBBY_CACHE.clear()
    calls = []
    real = main.targets

    async def counting(*a, **kw):
        calls.append(kw.get("ids"))
        return await real(*a, **kw)
    monkeypatch.setattr(main, "targets", counting)
    return calls


def test_lobby_scored_once_per_pool_snapshot_and_model(fresh_db, monkeypatch):
    now = int(time.time())
    _world(now)
    db.set_lobby([45, 12, 99], "KIWI", "limited", now)
    calls = _counting_targets(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    a = client.get("/api/lobby").json()
    b = client.get("/api/lobby").json()
    assert a["active"] and a["targets"] and a["targets"] == b["targets"]
    assert len(calls) == 1                       # drugie odpytanie z cache
    db.set_lobby([45, 12], "KIWI", "limited", now + 5)
    assert client.get("/api/lobby").json()["champion_ids"] == [45, 12]
    assert len(calls) == 2                       # inna pula = nowy scoring
    db.save_snapshot(now + 10, [_entry(45, 2), _entry(12, 0)])
    client.get("/api/lobby")
    assert len(calls) == 3                       # nowy snapshot = nowy swiat
    db.set_json_setting("grade_model", {"trained_at": now + 20})
    client.get("/api/lobby")
    client.get("/api/lobby")
    assert len(calls) == 4                       # nowy model = raz, nie dwa


def test_push_lobby_warms_cache_for_first_get(fresh_db, monkeypatch):
    from app.main import state
    now = int(time.time())
    _world(now)
    state.pop("last_pool_id", None)
    calls = _counting_targets(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    body = {"champion_ids": [45, 12, 99], "trade_ids": [], "queue": "KIWI",
            "pool_kind": "limited", "queue_id": 2400}
    assert client.post("/api/lobby", json=body).status_code == 200
    assert len(calls) == 1                       # predykcje sprzed gry
    out = client.get("/api/lobby").json()
    assert out["active"] and len(calls) == 1     # UI dostaje ten sam wynik
    # rotacja z lawka (ta sama unia, inne trade_ids) nie przelicza,
    # a plakietki wymiany i tak sa swieze - ida prosto z tabeli lobby
    client.post("/api/lobby", json={**body, "trade_ids": [12]})
    out = client.get("/api/lobby").json()
    assert out["trade_ids"] == [12] and len(calls) == 1


# ---------- (2+3) zdrowie potoku bez falszywych alarmow ----------

def test_pipeline_ignores_customs_and_names_eog_without_grade(fresh_db):
    now = int(time.time())
    with db.connect() as con:
        # trening (custom) ma ekran koncowy, z definicji nie ma oceny
        insert_row(con, "match_player", match_id="EUW1_7", champion_id=14,
                   duration=1200, game_mode="KIWI_CUSTOM")
        insert_row(con, "eog_raw", match_id="EUW1_7", game_id=7,
                   payload=b"x", captured_at=now)
        # gra misji z ekranem i bez oceny - realny przeciek, wskazany po id
        insert_row(con, "match_player", match_id="EUW1_8", champion_id=14,
                   duration=1200, game_mode="KIWI")
        insert_row(con, "eog_raw", match_id="EUW1_8", game_id=8,
                   payload=b"x", captured_at=now)
        # ekran bez wiersza meczu: trybu nie znamy, liczymy ostroznie
        insert_row(con, "eog_raw", match_id="EUW1_9", game_id=9,
                   payload=b"x", captured_at=now)
        # tryb wykluczony (JADE, produkcja 4.09) i remake w trybie misji -
        # bez oceny z definicji, nie przeciek
        insert_row(con, "match_player", match_id="EUW1_10", champion_id=12,
                   duration=350, game_mode="JADE")
        insert_row(con, "eog_raw", match_id="EUW1_10", game_id=10,
                   payload=b"x", captured_at=now)
        insert_row(con, "match_player", match_id="EUW1_11", champion_id=14,
                   duration=200, game_mode="KIWI")
        insert_row(con, "eog_raw", match_id="EUW1_11", game_id=11,
                   payload=b"x", captured_at=now)
    assert db.pipeline_sanity()["eog_bez_oceny"] == 2
    assert db.eog_without_grade_ids() == ["EUW1_8", "EUW1_9"]
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["pipeline_detail"]["eog_bez_oceny"] == ["EUW1_8", "EUW1_9"]


def test_pipeline_separates_dodge_from_unlinked_game(fresh_db):
    now = int(time.time())
    old = now - 90000
    with db.connect() as con:
        # dodge: stara pula bez zadnej gry w poblizu - informacyjnie
        insert_row(con, "champ_select_pool", ts=old, champion_ids="[1]",
                   pool_size=1)
        # przeciek: pula, po ktorej w 2 h byla gra nieprzypisana do zadnej puli
        insert_row(con, "champ_select_pool", ts=old - 50000, champion_ids="[2]",
                   pool_size=1)
        insert_row(con, "match_player", match_id="EUW1_3", champion_id=2,
                   duration=1200, game_creation=(old - 50000 + 600) * 1000)
        # gra przypisana do puli nie jest przeciekiem
        insert_row(con, "champ_select_pool", ts=old - 80000, champion_ids="[3]",
                   pool_size=1, match_id="EUW1_4")
        insert_row(con, "match_player", match_id="EUW1_4", champion_id=3,
                   duration=1200, game_creation=(old - 80000 + 600) * 1000)
    p = db.pipeline_sanity()
    assert p["stale_pools"] == 2 and p["pools_unlinked_game"] == 1


# ---------- (6) augmenty per tier ----------

CARDS_HTML = (
    '<div class="border-b text-sm font-semibold text-rarity-prismatic" data-x>'
    'Prismatic</div><div class="space-y-2">'
    '<a href="/augments/eureka/"><div class="line-clamp-2 text-rarity-prismatic" '
    'title="Eureka">Eureka</div><span>Appearance rate: <span>16.32%</span></span>'
    '<span>Win rate: <span class="font-data text-positive">58.23%</span></span></a>'
    '<a href="/augments/x/"><div class="line-clamp-2 text-rarity-prismatic" '
    'title="Quest: Wooglet&#39;s Witchcap">Quest</div>'
    '<span>Win rate: <span>51.5%</span></span></a></div>'
    '<div class="text-rarity-gold">Gold</div>'
    '<div class="line-clamp-2 text-rarity-gold" title="Recursion">Recursion</div>'
    '<span>Win rate: <span>52.1%</span></span>'
    '<div class="line-clamp-2 text-rarity-silver" title="Homeguard">Homeguard</div>'
)


def test_parse_build_groups_augments_by_tier():
    out = balance.parse_build(CARDS_HTML)
    bt = out["augments_by_tier"]
    assert [a["name"] for a in bt["Prismatic"]] == ["Eureka", "Quest: Wooglet's Witchcap"]
    assert bt["Prismatic"][0]["win_rate"] == 58.23
    assert bt["Gold"] == [{"name": "Recursion", "win_rate": 52.1}]
    # win rate nastepnej karty nie przecieka do karty bez wlasnego
    assert bt["Silver"] == [{"name": "Homeguard", "win_rate": None}]
    # naglowki sekcji (bez title=) nie sa kartami
    assert sum(len(v) for v in bt.values()) == 4


def test_parse_build_without_cards_keeps_flat_list_only():
    html = ('<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"ItemList","name":"Best Augments for Veigar",'
            '"itemListElement":[{"@type":"ListItem","position":1,"name":"Eureka"}]}'
            '</script>')
    out = balance.parse_build(html)
    assert out["augments"] == ["Eureka"] and "augments_by_tier" not in out
