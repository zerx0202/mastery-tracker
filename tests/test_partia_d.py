"""Partia D z przegladu 2.09: higiena, ktorej audyt nadal wage.

Drabinka milestone z kluczem (from_milestone, split_id) - historia progow
przestaje ginac miedzy splitami, a backfill cenzur nie dostaje progow
z przyszlosci. Sargowalny GLOB zamiast LIKE po najwiekszej tabeli na
sciezce rankingu. save_grade i backfill_grades - pierwsze testy sciezek,
ktorych awaria kosztuje dane. train() na realnych wierszach SQL. Naprawa
martwego last_queue_mode (ochrona offsetu JADE w /eog) i bezwarunkowego
filtra w /grades/history. Snowball: druga obserwacja tej samej gry od
INNEGO gracza przestaje przepadac (snowball_pair). Odzysk P6: pelny obiekt
gry karmi player_stat i match_participant zamiast wyrzucac 9/10 materialu."""

from fastapi.testclient import TestClient

from app import db, model
from app.main import app
from tests.conftest import insert_row

MY = "99999999-9999-9999-9999-999999999999"
OTHER = "11111111-1111-1111-1111-111111111111"


# ---------- drabinka per split ----------

def _entry(ms, games, grade="A-"):
    return {"championSeasonMilestone": ms,
            "nextSeasonMilestone": {"requireGradeCounts": {grade: 1},
                                    "totalGamesRequires": games,
                                    "rewardMarks": 1, "bonus": False}}


def test_ladder_keeps_history_between_splits(fresh_db):
    with db.connect() as con:
        insert_row(con, "split", id=1, started_at=1, detected_at=1)
    db.learn_ladder([_entry(0, 1), _entry(2, 1, "S-")], ts=10)
    assert db.get_ladder()[0]["games"] == 1

    with db.connect() as con:
        insert_row(con, "split", id=2, started_at=100, detected_at=100)
    db.learn_ladder([_entry(0, 7), _entry(2, 7, "S-")], ts=110)

    ladder = db.get_ladder()
    assert ladder[0]["games"] == 7, "biezacy split ma swoje progi"
    with db.connect() as con:
        rows = con.execute(
            "SELECT split_id, games FROM milestone_ladder "
            "WHERE from_milestone=0 ORDER BY split_id").fetchall()
    assert [(r["split_id"], r["games"]) for r in rows] == [(1, 1), (2, 7)], \
        "REPLACE po samym from_milestone kasowal historie poprzedniego splitu"


def test_ladder_falls_back_to_last_known_split(fresh_db):
    with db.connect() as con:
        insert_row(con, "split", id=1, started_at=1, detected_at=1)
    db.learn_ladder([_entry(0, 3)], ts=10)
    with db.connect() as con:
        insert_row(con, "split", id=2, started_at=100, detected_at=100)
    # swiezy split, learn_ladder jeszcze nie pobiegl - lepsza stara
    # drabinka niz pusty ranking
    assert db.get_ladder()[0]["games"] == 3


# ---------- popularnosc snowballa: GLOB zamiast pelnego skanu ----------

def test_sb_popularity_counts_only_snowball_rows(fresh_db):
    with db.connect() as con:
        for mid, pn, cid in (("SB_1", 1, 45), ("SB_1", 2, 99),
                             ("SB_2", 1, 45), ("EUW1_7", 1, 45)):
            insert_row(con, "player_stat", match_id=mid, participant_no=pn,
                       champion_id=cid, stat_key="goldEarned", stat_value=1)
    assert db.champion_sb_popularity() == {45: 2, 99: 1}


# ---------- save_grade: semantyki, na ktorych wisza liczniki ----------

def test_save_grade_dedup_and_censored_replacement(fresh_db):
    entry = {"gameId": 9, "grade": "S-", "championId": 45, "score": 700}
    assert db.save_grade(entry, "euw1", 100) is True
    assert db.save_grade(entry, "euw1", 200) is False, \
        "powtorka z dosylki nie moze dublowac eventow i licznikow"

    # wpis cenzurowany ze snapshot_diff ustepuje dokladnej ocenie w calosci
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_10", game_id=10,
                   champion_id=45, grade=">=A-", censored=1,
                   threshold="A-", source="snapshot_diff", observed_at=50)
    db.save_grade({"gameId": 10, "grade": "A", "championId": 45}, "euw1", 60)
    with db.connect() as con:
        r = con.execute("SELECT grade, COALESCE(censored,0) c, source "
                        "FROM grade_observation WHERE match_id='EUW1_10'"
                        ).fetchone()
    assert r["grade"] == "A" and r["c"] == 0 and r["source"] is None
    gates = {g["key"]: g for g in db.data_gates()}
    assert gates["fatigue"]["have"] == 2, "dokladna ocena wchodzi do licznika"


# ---------- backfill ocen ze snapshotow: jedyny producent cenzur ----------

def test_backfill_grades_exact_and_censored(fresh_db):
    with db.connect() as con:
        insert_row(con, "split", id=1, started_at=1, detected_at=1)
        insert_row(con, "milestone_ladder", from_milestone=1,
                   require_grades='{"A-": 1}', games=1, observed_at=1,
                   split_id=1)
        # snapshoty: przyrost grades_earned, potem awans (tablica sie zeruje)
        for sid, ts in ((1, 1000), (2, 2000), (3, 3000)):
            insert_row(con, "snapshot", id=sid, taken_at=ts, split_id=1)
        for sid, grades, ms in ((1, "[]", 1), (2, '["B+"]', 1), (3, "[]", 2)):
            insert_row(con, "mastery", snapshot_id=sid, champion_id=45,
                       milestone=ms, grades_earned=grades, points=0, level=1)
        # mecze tym championem ZAKONCZONE tuz przed snapshotami 2 i 3
        # (find_match liczy koniec = game_creation/1000 + duration)
        insert_row(con, "match_player", match_id="EUW1_20", champion_id=45,
                   duration=1200, game_creation=700 * 1000)   # koniec 1900
        insert_row(con, "match_player", match_id="EUW1_30", champion_id=45,
                   duration=1200, game_creation=1700 * 1000)  # koniec 2900
    out = db.backfill_grades_from_snapshots(window=7200)
    assert out["added"] == 2 and out["unmatched"] == 0
    with db.connect() as con:
        rows = {r["match_id"]: r for r in con.execute(
            "SELECT match_id, grade, censored, threshold FROM grade_observation")}
    assert rows["EUW1_20"]["grade"] == "B+" and rows["EUW1_20"]["censored"] == 0
    assert rows["EUW1_30"]["grade"] == ">=A-" and rows["EUW1_30"]["censored"] == 1
    # idempotencja: drugi przebieg niczego nie dubluje
    assert db.backfill_grades_from_snapshots(window=7200)["added"] == 0


# ---------- train(): sciezka szczesliwa na realnych wierszach ----------

def test_train_happy_path_on_sql_rows(fresh_db, monkeypatch):
    monkeypatch.setattr(model, "EPOCHS_FINAL", 60)
    monkeypatch.setattr(model, "EPOCHS_TUNE", 25)
    monkeypatch.setattr(model, "EPOCHS_VAL", 25)
    monkeypatch.setattr(model, "L2_GRID", [1.0])
    grades = ["C", "C+", "B", "B", "B+", "A", "A", ">=A-", ">=A-", "S-", "B", "C"]
    with db.connect() as con:
        for i, g in enumerate(grades, start=1):
            insert_row(con, "match_player", match_id=f"EUW1_{i}",
                       champion_id=40 + i, duration=1200, game_mode="KIWI",
                       gold=8000 + 400 * i, kills=i, deaths=3, assists=6,
                       dmg_champ=15000 + 900 * i)
            insert_row(con, "grade_observation", match_id=f"EUW1_{i}",
                       game_id=i, champion_id=40 + i, grade=g, observed_at=i)
    out = model.train(mode="KIWI", save=True)
    m = out["models"]["A-"]
    assert "weights" in m and set(m["weights"]) == set(model.FEATURES), \
        "rozjazd training_rows z extract_features konczyl sie treningiem na zerach"
    assert out["ordinal"]["trained_on"] == len(grades)


# ---------- /grades/history: filtr trybu jak wszedzie ----------

def test_history_without_default_mode_shows_all(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200)          # game_mode NULL (stare wpisy)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="B+", observed_at=1)
    client = TestClient(app, raise_server_exceptions=False)
    d = client.get("/api/grades/history").json()
    assert d["count"] == 1, \
        "bezwarunkowe `game_mode = ?` z NULL zwracalo zawsze pusto"


# ---------- last_queue_mode: ochrona offsetu JADE w /eog ----------

def test_eog_link_normalizes_pick_using_lobby_mode(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/lobby", json={"champion_ids": [45, 60045],
                                        "queue": "JADE", "queue_id": 4320,
                                        "pool_kind": "limited"})
    assert r.status_code == 200
    block = {"gameId": 70, "teams": [{"teamId": 100, "players": [
        {"isLocalPlayer": True, "championId": 60045, "puuid": "a" * 36,
         "stats": {"WIN": 1}}]}]}
    assert client.post("/api/eog", json={"block": block}).status_code == 200
    with db.connect() as con:
        row = con.execute("SELECT picked_id FROM champ_select_pool "
                          "WHERE match_id='EUW1_70'").fetchone()
    assert row["picked_id"] == 45, \
        "state[last_queue_mode] nie byl nigdzie zapisywany - offset zostawal"


# ---------- snowball: para (gra, gracz) zamiast dedupu po samej grze ----------

def _sb_game(gid, cid, puuid_unused=None):
    return {"gameId": gid, "gameMode": "KIWI", "queueId": 2400,
            "gameDuration": 1200, "gameCreation": 1700000000000,
            "participants": [{"championId": cid, "teamId": 100,
                              "stats": {"kills": 2, "goldEarned": 9000}}]}


def test_snowball_second_observer_of_same_game_is_kept(fresh_db):
    k1, r1 = db.snowball_ingest(OTHER, [_sb_game(77, 45)])
    assert k1 == 1 and r1 > 0
    # ten sam mecz widziany od DRUGIEGO gracza = INNY uczestnik, nowa
    # obserwacja norm - dotad przepadala na dedupie po samym game_id
    k2, r2 = db.snowball_ingest("2" * 36, [_sb_game(77, 99)])
    assert k2 == 1 and r2 > 0
    # a powtorka od tego samego gracza to nadal zwykly dedup
    k3, r3 = db.snowball_ingest(OTHER, [_sb_game(77, 45)])
    assert (k3, r3) == (1, 0)
    with db.connect() as con:
        pns = [r["participant_no"] for r in con.execute(
            "SELECT DISTINCT participant_no FROM player_stat "
            "WHERE match_id='SB_77' ORDER BY participant_no")]
        view = con.execute("SELECT COUNT(*) c FROM norm_source "
                           "WHERE match_id='SB_77'").fetchone()["c"]
    assert pns == [1, 2]
    assert view == 1, "norm_source ma zostac 1 wiersz na gre (bez duplikacji)"


# ---------- odzysk P6: pelny obiekt gry karmi normy i karte 9 ----------

def _full_game(gid=9):
    parts, idents = [], []
    for i in range(1, 11):
        parts.append({"participantId": i, "championId": 100 + i,
                      "teamId": 100 if i <= 5 else 200,
                      "stats": {"win": i <= 5, "kills": i,
                                "goldEarned": 9000 + i,
                                "totalDamageDealtToChampions": 15000 + i},
                      "timeline": {}})
        idents.append({"participantId": i,
                       "player": {"puuid": (MY if i == 2 else f"{i:036d}")}})
    return {"gameId": gid, "platformId": "EUW1", "queueId": 2400,
            "gameMode": "KIWI", "gameCreation": 1700000000000,
            "gameDuration": 1200, "participants": parts,
            "participantIdentities": idents}


def test_full_game_recovery_feeds_stats_and_participants(fresh_db):
    # (N) wlasny wiersz idzie po puuid w formacie KLIENTA (my_lcu_puuid),
    # nie po zaszyfrowanym puuid z ACCOUNT-V1 w puuid_cache
    db.set_setting("my_lcu_puuid", MY)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/history/lcu", json={"games": [_full_game(9)]})
    assert r.status_code == 200 and r.json()["new"] == 1
    with db.connect() as con:
        me = con.execute("SELECT champion_id FROM match_player "
                         "WHERE match_id='EUW1_9'").fetchone()
        n_part = con.execute("SELECT COUNT(*) c FROM match_participant "
                             "WHERE match_id='EUW1_9'").fetchone()["c"]
        locals_ = [r2["participant_no"] for r2 in con.execute(
            "SELECT DISTINCT participant_no FROM player_stat "
            "WHERE match_id='EUW1_9' AND is_local=1")]
        n_stats = con.execute("SELECT COUNT(DISTINCT participant_no) c "
                              "FROM player_stat WHERE match_id='EUW1_9'"
                              ).fetchone()["c"]
    assert me["champion_id"] == 102, "wlasny wiersz wybrany po puuid, nie [0]"
    assert n_part == 10 and n_stats == 10
    assert locals_ == [2]
    # jednoosobowy format z wlasnego listingu dziala jak dotad
    g1 = _full_game(10)
    g1["participants"] = [g1["participants"][1]]
    g1["participantIdentities"] = [g1["participantIdentities"][1]]
    assert client.post("/api/history/lcu",
                       json={"games": [g1]}).json()["new"] == 1
