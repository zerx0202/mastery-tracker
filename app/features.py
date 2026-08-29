"""Jedno zrodlo prawdy dla cech liczonych na minute.

Przed tym modulem gold/min i dmg/min liczyly niezaleznie cztery miejsca
(model, historia ocen, reference_pace, panel live) z trzema roznymi
podlogami czasu. Kazda rozbieznosc miedzy nimi to falszywe porownanie.

Podlogi sa jawne, bo roznia sie CELOWO:
  - mecze zakonczone: 1.0 min (remake'i i smieciowe wpisy nie daja dzieleniem
    przez ulamek absurdalnych temp)
  - gra na zywo: 0.5 min (pierwsze sekundy meczu tez maja pokazywac liczby)
"""

FLOOR_MATCH = 1.0
FLOOR_LIVE = 0.5


def minutes(duration_s, floor=FLOOR_MATCH):
    return max((duration_s or 0) / 60, floor)


def per_min(value, mins):
    return (value or 0) / mins


def match_features(row, floor=FLOOR_MATCH):
    """Cechy tempa z wiersza meczu (match_player albo dict o tych polach)."""
    mins = minutes(row.get("duration") if hasattr(row, "get") else row["duration"], floor)
    g = row.get if hasattr(row, "get") else row.__getitem__
    def val(k):
        try:
            return g(k) or 0
        except (KeyError, IndexError):
            return 0
    return {
        "minutes": mins,
        "gpm": per_min(val("gold"), mins),
        "dpm": per_min(val("dmg_champ"), mins),
        "ka_per_min": (val("kills") + val("assists")) / mins,
        "deaths_per_min": per_min(val("deaths"), mins),
        "cs_per_min": per_min(val("cs"), mins),
    }
