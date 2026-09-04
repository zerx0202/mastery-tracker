"""Partia L (4.09): narzedzia analiz na kopii - kopia SQLite z JSON-a
eksportu, protokol zmeczenia, test cech klasowych. Werdykty naleza do
danych; tu pilnujemy mechaniki: sesje, statystyki, reguly werdyktu
i decyzji, bramki, pomijanie tabel bez blobow."""
import importlib.util
import json
import sqlite3
from pathlib import Path


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", Path(__file__).resolve().parents[1] / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_db_from_export_builds_copy_and_skips_blob_tables(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DB_PATH", db.DB_PATH)      # build podmienia; przywroc po tescie
    tool = _load("db_from_export")
    export = {"champion": [{"id": 45, "name": "Veigar", "key": "Veigar"}],
              "match_player": [{"match_id": "EUW1_1", "champion_id": 45,
                                "duration": 1200, "game_mode": "KIWI"}],
              "eog_raw": [{"match_id": "EUW1_1", "game_id": 1, "captured_at": 1}],
              "nieznana_tabela": [{"x": 1}]}
    src = tmp_path / "export.json"
    src.write_text(json.dumps(export), encoding="utf-8")
    counts, skipped = tool.build(str(src), str(tmp_path / "kopia.db"))
    assert counts == {"champion": 1, "match_player": 1}
    assert skipped == {"eog_raw": ["payload"]}          # eksport nie ma blobow
    con = sqlite3.connect(tmp_path / "kopia.db")
    assert con.execute("SELECT game_mode FROM match_player").fetchone()[0] == "KIWI"


def test_fatigue_sessions_statistics_and_verdict():
    fa = _load("fatigue_analysis")
    games = [{"start": 0, "duration": 1000, "rank": 5, "prior": 0},
             {"start": 2000, "duration": 1000, "rank": 8, "prior": 1},
             {"start": 100000, "duration": 1000, "rank": 3, "prior": 0}]
    s = fa.sessions_of(games)
    assert [(g["session"], g["pos"]) for g in s] == [(1, 1), (1, 2), (2, 1)]
    assert abs(fa.spearman([1, 2, 3, 4], [1, 2, 3, 4]) - 1.0) < 1e-9
    assert abs(fa.spearman([1, 2, 3, 4], [4, 3, 2, 1]) + 1.0) < 1e-9
    # SYGNAL wymaga zgodnosci wersji surowej i rezydualnej
    ok = {"warm_diff": -2.0, "warm_p": 0.01, "rho": -0.4, "rho_p": 0.01}
    no = {"warm_diff": -0.5, "warm_p": 0.4, "rho": 0.1, "rho_p": 0.8}
    assert fa.verdict({"rank": ok, "resid": ok}) == {"warm": "SYGNAL", "rho": "SYGNAL"}
    assert fa.verdict({"rank": ok, "resid": no}) == {"warm": "nierozstrzygniety",
                                                    "rho": "nierozstrzygniety"}
    assert fa.verdict({"rank": no, "resid": no}) == {"warm": "brak sygnalu",
                                                    "rho": "brak sygnalu"}


def test_fatigue_gate_refuses_below_threshold(fresh_db, capsys):
    fa = _load("fatigue_analysis")
    assert fa.main(str(fresh_db)) == 2
    assert "bramka" in capsys.readouterr().out


def test_class_features_decision_rule():
    cf = _load("class_features_test")
    base = {"A-": {"log_loss": 0.60, "auc": 0.80}, "S-": {"log_loss": 0.30, "auc": 0.70}}
    better = {"A-": {"log_loss": 0.55, "auc": 0.79}, "S-": {"log_loss": 0.31, "auc": 0.70}}
    assert cf.decide(base, better).startswith("WCHODZI")
    tiny = {"A-": {"log_loss": 0.59, "auc": 0.80}, "S-": {"log_loss": 0.30, "auc": 0.70}}
    assert cf.decide(base, tiny).startswith("odrzucony")           # -1.7 % < 5 %
    worse_s = {"A-": {"log_loss": 0.50, "auc": 0.80}, "S-": {"log_loss": 0.34, "auc": 0.70}}
    assert cf.decide(base, worse_s).startswith("odrzucony")        # S- +13 %
    auc_drop = {"A-": {"log_loss": 0.50, "auc": 0.75}, "S-": {"log_loss": 0.30, "auc": 0.70}}
    assert cf.decide(base, auc_drop).startswith("odrzucony")       # AUC -0.05
    assert cf.decide(base, {"A-": None, "S-": None}).startswith("nierozstrzygniety")


def test_class_features_gate_refuses_below_threshold(fresh_db, capsys):
    cf = _load("class_features_test")
    assert cf.main(str(fresh_db)) == 2
    assert "bramka" in capsys.readouterr().out
