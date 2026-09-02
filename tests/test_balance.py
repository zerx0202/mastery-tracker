"""(48) Parser mnoznikow Mayhema z arammayhem.com. Fixture to doslowny
wycinek zywej strony z 2.09 (Astro, minifikowany HTML) - trzy warianty:
pojedynczy buff, pojedynczy nerf, multi-mnoznik z haste/tenacity."""
from app import balance, db
from tests.conftest import insert_row  # noqa: F401 (spojnosc importow testow)

CHUNK = (
    '<a href="/build/aatrox/" class="champion-row block p-4" data-name="aatrox">'
    '<div class="flex items-center gap-4"><img src="x" alt="Aatrox" class="w-12">'
    '<div class="flex-1"><div class="font-medium">Aatrox</div>'
    '<div class="flex flex-wrap gap-2 mt-2">'
    '<span class="text-xs px-2 py-1 rounded bg-positive/20 text-positive">Damage Dealt: +5%</span>'
    '</div></div></div></a>'
    '<a href="/build/ahri/" class="champion-row block p-4" data-name="ahri">'
    '<div class="flex items-center gap-4"><img src="x" alt="Ahri" class="w-12">'
    '<div class="flex-1"><div class="font-medium">Ahri</div>'
    '<div class="flex flex-wrap gap-2 mt-2">'
    '<span class="text-xs px-2 py-1 rounded bg-negative/20 text-negative">Damage Dealt: -5%</span>'
    '</div></div></div></a>'
    '<a href="/build/akali/" class="champion-row block p-4" data-name="akali">'
    '<div class="flex items-center gap-4"><img src="x" alt="Akali" class="w-12">'
    '<div class="flex-1"><div class="font-medium">Akali</div>'
    '<div class="flex flex-wrap gap-2 mt-2">'
    '<span class="text-xs px-2 py-1 rounded bg-positive/20 text-positive">Damage Received: -5%</span>'
    '<span class="text-xs px-2 py-1 rounded bg-positive/20 text-positive">Tenacity: +20%</span>'
    '<span class="text-xs px-2 py-1 rounded bg-positive/20 text-positive">Ability Haste: -10</span>'
    '</div></div></div></a>'
)


def test_parse_balance_variants():
    out = balance.parse_balance(CHUNK)
    assert out == {
        "Aatrox": {"Damage Dealt": "+5%"},
        "Ahri": {"Damage Dealt": "-5%"},
        "Akali": {"Damage Received": "-5%", "Tenacity": "+20%",
                  "Ability Haste": "-10"},
    }


def test_parse_unescapes_html_entities(fresh_db, monkeypatch):
    # zlapane na produkcji 2.09: Kog&#39;Maw / Nunu &amp; Willump / Rek&#39;Sai
    # zostawaly w "unmatched", bo encje psuly dopasowanie nazwy do id
    monkeypatch.setattr(balance, "MIN_CHAMPIONS", 1)
    chunk = (
        '<a href="/build/kogmaw/" class="champion-row" data-name="kogmaw">'
        '<div class="font-medium">Kog&#39;Maw</div>'
        '<span class="text-positive">Damage Dealt: +5%</span></a>'
        '<a href="/build/nunu/" class="champion-row" data-name="nunu">'
        '<div class="font-medium">Nunu &amp; Willump</div>'
        '<span class="text-negative">Healing: -10%</span></a>'
    )
    assert set(balance.parse_balance(chunk)) == {"Kog'Maw", "Nunu & Willump"}
    db.save_champions([(96, "Kog'Maw", "KogMaw"), (20, "Nunu & Willump", "Nunu")])
    out = balance.store_balance(chunk)
    assert out["matched"] == 2 and out["unmatched"] == []


def test_parse_garbage_gives_empty():
    assert balance.parse_balance("<html><body>przebudowa strony</body></html>") == {}


def test_store_refuses_suspiciously_small_parse(fresh_db):
    out = balance.store_balance(CHUNK)
    assert out["stored"] is False
    assert db.get_json_setting("mayhem_balance") is None
    kinds = [e["kind"] for e in db.recent_events(5)]
    assert "balance_parse_failed" in kinds


def test_store_maps_names_to_ids(fresh_db, monkeypatch):
    # prog w dol, zeby fixture z trzema championami przeszedl bramke
    monkeypatch.setattr(balance, "MIN_CHAMPIONS", 2)
    db.save_champions([(266, "Aatrox", "Aatrox"), (103, "Ahri", "Ahri"),
                       (84, "Akali", "Akali")])
    out = balance.store_balance(CHUNK, ts=1700000000)
    assert out["stored"] is True and out["matched"] == 3
    st = db.get_json_setting("mayhem_balance")
    assert st["champions"]["103"] == {"Damage Dealt": "-5%"}
    assert st["champions"]["84"]["Ability Haste"] == "-10"
    assert st["unmatched"] == []


def test_store_keeps_old_data_on_broken_fetch(fresh_db, monkeypatch):
    monkeypatch.setattr(balance, "MIN_CHAMPIONS", 2)
    db.save_champions([(266, "Aatrox", "Aatrox"), (103, "Ahri", "Ahri")])
    balance.store_balance(CHUNK, ts=1700000000)
    out = balance.store_balance("<html>pusto</html>", ts=1700000500)
    assert out["stored"] is False
    st = db.get_json_setting("mayhem_balance")
    assert st["fetched_at"] == 1700000000  # stare dane nietkniete
