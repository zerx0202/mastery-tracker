"""Historia Riot ID gracza (player_alias) i regula "najnowsza nazwa".

Kluczem tozsamosci jest puuid (staly, nadaje Riot); Riot ID to etykieta,
ktora gracz moze zmienic. Gry laczy puuid, wiec zmiana nazwy niczego nie
rozdziela - trzeba tylko (1) pokazywac najnowsza nazwe, (2) pamietac stare
jako "dawniej", (3) nie cofac etykiety, gdy odzysk z historii LCU dowiezie
STARSZA gre po dzisiejszym ekranie koncowym. Backfill z blobow eog_raw
odzyskuje historie nazw sprzed wprowadzenia tabeli, jednorazowo."""
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_partia_m import _block, _game

MY, A, B = "m" * 36, "a" * 36, "b" * 36
NOW = 1_700_000_000


def _lcu_game(gid, creation_s, players):
    """Pelna gra z historii LCU (format v4): players [(puuid, 'Nazwa', 'TAG')]."""
    return {"gameId": gid, "platformId": "EUW1", "gameMode": "KIWI", "queueId": 2400,
            "gameCreation": creation_s * 1000, "gameDuration": 1200,
            "participantIdentities": [
                {"participantId": i + 1, "player": {"puuid": p, "gameName": n, "tagLine": t}}
                for i, (p, n, t) in enumerate(players)],
            "participants": [
                {"participantId": i + 1, "teamId": 100, "championId": 45 + i,
                 "stats": {"kills": i}} for i in range(len(players))]}


def test_every_name_seen_stays_in_history_with_its_window(fresh_db):
    db.save_player_names([(A, "Stara#EUW")], NOW)
    db.save_player_names([(A, "Nowa#EUW")], NOW + 100)
    db.save_player_names([(A, "Stara#EUW")], NOW + 50)
    assert db.player_aliases([A, B]) == {A: ["Nowa#EUW", "Stara#EUW"]}
    with db.connect() as con:
        r = con.execute("SELECT first_seen, last_seen FROM player_alias "
                        "WHERE puuid=? AND name=?", (A, "Stara#EUW")).fetchone()
    assert (r["first_seen"], r["last_seen"]) == (NOW, NOW + 50)
    assert db.player_aliases([]) == {} and db.save_player_aliases([]) == 0


def test_newest_observation_wins_the_label_not_the_last_write(fresh_db):
    db.save_player_names([(A, "Nowa#EUW")], NOW + 100)
    db.save_player_names([(A, "Stara#EUW")], NOW + 50)          # starsza gra dowieziona pozniej
    with db.connect() as con:
        r = con.execute("SELECT name, seen_at FROM player_name WHERE puuid=?", (A,)).fetchone()
    assert (r["name"], r["seen_at"]) == ("Nowa#EUW", NOW + 100)
    db.save_player_names([(A, "Trzecia#EUW")], NOW + 100)        # ten sam czas: nowszy zapis
    with db.connect() as con:
        assert con.execute("SELECT name FROM player_name WHERE puuid=?",
                           (A,)).fetchone()["name"] == "Trzecia#EUW"


def test_lcu_recovery_of_an_old_game_does_not_roll_the_name_back(fresh_db):
    # dzisiejszy ekran koncowy: A gra juz jako "Nowa"; potem odzysk z historii
    # LCU dowozi gre sprzed roku, w ktorej A byl "Stara"
    _game("EUW1_2", 2, 1, NOW + 86400, [(MY, "Ja#1", 100, 45), (A, "Nowa#EUW", 100, 238)])
    assert db.save_lcu_participants(
        _lcu_game(1, NOW, [(MY, "Ja", "1"), (A, "Stara", "EUW")]), "EUW1_1", MY) == 2
    s = db.players_summary([A], MY)
    assert s[A]["name"] == "Nowa#EUW" and s[A]["aliases"] == ["Stara#EUW"]
    with db.connect() as con:
        r = con.execute("SELECT first_seen, last_seen FROM player_alias "
                        "WHERE puuid=? AND name='Stara#EUW'", (A,)).fetchone()
    assert (r["first_seen"], r["last_seen"]) == (NOW + 1200, NOW + 1200)   # koniec TAMTEJ gry


def test_summary_recurring_and_api_expose_old_ids(fresh_db):
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Stara#EUW", 100, 238)])
    _game("EUW1_2", 2, 0, NOW + 3600, [(MY, "Ja#1", 200, 45), (A, "Nowa#EUW", 100, 238)])
    # oba ekrany w tej samej sekundzie - nowsza nazwa dostaje pozniejsze widzenie
    db.save_player_names([(A, "Nowa#EUW")], int(time.time()) + 100)
    s = db.players_summary([A], MY)
    assert s[A]["name"] == "Nowa#EUW" and s[A]["aliases"] == ["Stara#EUW"]
    assert s[A]["games"] == 2                              # gry pod obiema nazwami razem
    assert db.recurring_players(MY)[0]["aliases"] == ["Stara#EUW"]
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/players?puuids=" + A).json()[A]["aliases"] == ["Stara#EUW"]
    # gracz bez zmiany nazwy: pusta lista, nie brak klucza
    _game("EUW1_3", 3, 1, NOW + 7200, [(MY, "Ja#1", 100, 45), (B, "Bob#EUW", 200, 12)])
    assert db.players_summary([B], MY)[B]["aliases"] == []


def test_backfill_reads_old_ids_from_eog_blobs_once(fresh_db):
    db.save_eog(_block(1, [(MY, "Ja#1", 100, 45), (A, "Stara#EUW", 100, 238),
                           (B, "Annie bot#BOT", 200, 12)]), "EUW1", NOW)
    db.save_eog(_block(2, [(MY, "Ja#1", 100, 45), (A, "Nowa#EUW", 200, 238)]),
                "EUW1", NOW + 3600)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) c FROM player_alias").fetchone()["c"] == 0
    db.upgrade_player_alias_backfill()
    assert db.player_aliases([A, B]) == {A: ["Nowa#EUW", "Stara#EUW"]}   # bot pominiety
    with db.connect() as con:
        r = con.execute("SELECT first_seen, last_seen FROM player_alias "
                        "WHERE puuid=? AND name='Stara#EUW'", (A,)).fetchone()
    assert (r["first_seen"], r["last_seen"]) == (NOW, NOW)             # czas ekranu, nie "teraz"
    # drugi start (migrate() przy kazdym uruchomieniu) niczego nie dubluje ani nie cofa
    db.save_player_aliases([(A, "Trzecia#EUW", NOW + 7200)])
    db.upgrade_player_alias_backfill()
    assert db.player_aliases([A])[A] == ["Trzecia#EUW", "Nowa#EUW", "Stara#EUW"]


def test_participant_backfill_replays_names_in_screen_time(fresh_db):
    # replay blobow w dowolnej kolejnosci nie moze cofnac nazwy do starszej
    db.save_eog(_block(2, [(MY, "Ja#1", 100, 45), (A, "Nowa#EUW", 100, 238)]),
                "EUW1", NOW + 3600)
    db.save_eog(_block(1, [(MY, "Ja#1", 100, 45), (A, "Stara#EUW", 100, 238)]),
                "EUW1", NOW)
    db.backfill_participants_from_eog()
    with db.connect() as con:
        r = con.execute("SELECT name, seen_at FROM player_name WHERE puuid=?", (A,)).fetchone()
    assert (r["name"], r["seen_at"]) == ("Nowa#EUW", NOW + 3600)
    assert db.player_aliases([A]) == {A: ["Nowa#EUW", "Stara#EUW"]}


def test_migrate_runs_alias_after_note_then_backfill(fresh_db):
    names = db.migrate()
    assert names.index("init_player_alias") > names.index("init_player_note")
    assert names.index("upgrade_player_alias_backfill") > names.index("init_player_alias")
