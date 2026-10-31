"""Parser checks against saved pages. Run: python test_scrape.py"""
import json
from datetime import date, datetime
from pathlib import Path

import scrape

F = Path(__file__).parent / "fixtures"
read = lambda name: (F / name).read_text(encoding="utf-8")


def find(shows, title_part, start):
    hits = [s for s in shows if title_part in s[0].upper() and s[1] == datetime.fromisoformat(start)]
    assert hits, f"{title_part} @ {start} not found"
    return hits[0]


def check_basic(shows):
    assert shows, "no shows parsed"
    for title, start, vo, url in shows:
        assert title.strip() and isinstance(start, datetime) and isinstance(vo, bool)
        assert url.startswith("https://"), url
    assert any(s[2] for s in shows) and not all(s[2] for s in shows), "expected both V.O. and dubbed shows"


def test_uci():
    shows = scrape.parse_uci(json.loads(read("uci_2026-10-31.json")))
    check_basic(shows)
    assert len(shows) == 76
    assert find(shows, "CLAYFACE", "2026-10-31T19:10")[3].startswith("https://www.ucicinemas.it/movies/clayface/")
    assert any(s[2] for s in shows if s[0] == "It Ends")  # language EN + subtitles ITA


def test_t18_home():
    anteo = scrape.parse_t18_home(read("t18_anteo.html"))
    check_basic(anteo)
    t, _, vo, url = find(anteo, "MINOTAUR", "2026-10-31T14:45")
    assert vo and t == "MINOTAUR - NEL LABIRINTO DEL POTERE", t
    assert url.startswith("https://anteo.spaziocinema.18tickets.it/film/123998/")
    # Arlecchino marks V.O. only in the "Lingua:" field
    arl = scrape.parse_t18_home(read("t18_arlecchino.html"))
    check_basic(arl)
    assert find(arl, "LA CASA DELLE DONNE", "2026-10-31T14:00")[2]
    # Cinemino's homepage has no showtimes -> caller falls back to film pages
    assert scrape.parse_t18_home(read("t18_cinemino.html")) == []
    assert scrape.film_ids_t18(read("t18_cinemino.html"))


def test_t18_film():
    url = "https://ilcinemino.ilcinemino.18tickets.it/film/151997"
    shows = scrape.parse_t18_film(read("t18_cinemino_film.html"), url)
    assert shows == [("TRAINSPOTTING (RIED.)", datetime(2026, 11, 2, 21, 30), True, url)], shows


def test_cvu():
    base = "https://www.notoriouscinemas.it/gloria/index.php"
    gloria = scrape.parse_cvu(read("cvu_gloria.html"), base, date(2026, 10, 31))
    assert gloria and all(s[3] == base for s in gloria)
    t, _, vo, _ = find(gloria, "DIO RIDE", "2026-10-31T14:30")
    assert t == "DIO RIDE" and not vo  # "NEW!" badge stripped
    assert len([s for s in gloria if s[0] == "DIO RIDE"]) == 19
    col = scrape.parse_cvu(read("cvu_colosseo.html"), base, date(2026, 10, 31))
    check_basic(col)
    assert any(s[0] == "IT ENDS" and s[2] for s in col) and any(s[0] == "IT ENDS" and not s[2] for s in col)
    # dd/mm in January seen in December belongs to next year
    dec = scrape.parse_cvu(read("cvu_gloria.html").replace("31/10", "02/01"), base, date(2026, 12, 30))
    assert any(s[1].year == 2027 for s in dec)


def test_titles():
    k = scrape.film_key
    assert k("DIO RIDE | FILM IN INGLESE") == k("Dio ride") == "dio ride"
    assert k("V. O. SOTT. ITA MINOTAUR - NEL LABIRINTO DEL POTERE") == k("Minotaur: nel labirinto del potere")
    assert k("TRAINSPOTTING (RIED.) - V. O.") == k("Trainspotting") == "trainspotting"
    assert k("LA CASA DELLE DONNE (LA MAISON DES FEMMES)") == k("La casa delle donne")
    assert k("JE S&Ograve; PAZZO") == k("Je so' pazzo") == "je so pazzo"
    assert k("DIGGER [2026]") == "digger"
    assert k("Avengers: Doomsday") == k("AVENGERS - DOOMSDAY")
    assert k("MAMMINA - V.M.14") == k("Mammina - v.m.18") == "mammina"
    assert k("Dracula di Bram Stoker - riedizione") == k("Dracula di Bram Stoker")
    assert k("Stray Kids World Tour RUN IT C.A.") == "stray kids world tour run it"
    assert k("L'assurda storia della Gialappa's Band") == k("L'assurda storia della gialappàs band")
    # different chains, different subtitle: merged only when no cinema lists both
    canon = scrape.merge_keys({
        "minotaur": {"uci"}, "minotaur nel labirinto del potere": {"anteo", "gloria"},
        "avatar": {"uci"}, "avatar fuoco e cenere": {"uci", "anteo"},
        "it": {"x"}, "it ends": {"y"},  # too short to be a safe prefix
    })
    assert canon["minotaur nel labirinto del potere"] == "minotaur"
    assert canon["avatar fuoco e cenere"] == "avatar fuoco e cenere"
    assert canon["it ends"] == "it ends"
    # Italian vs original title: grouped through the manual alias table
    scrape.ALIASES["the bride"] = "la sposa"
    assert k("The Bride!") == k("LA SPOSA")
    del scrape.ALIASES["the bride"]
    assert scrape.display_title({"DIO RIDE": 3, "Dio ride": 1}) == "Dio ride"
    assert scrape.display_title({"DIO RIDE": 3}) == "Dio ride"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
