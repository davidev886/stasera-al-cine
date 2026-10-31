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
    data = json.loads(read("uci_2026-10-31.json"))
    for film in data["data"]:
        for group in film["screens"]:
            for entries in group.values():
                for e in entries:
                    e["language"]["slug"] = "DSA"
    assert not any(s[2] for s in scrape.parse_uci(data)), "DSA screenings are in Italian"


def test_t18_home():
    anteo = scrape.parse_t18_home(read("t18_anteo.html"))
    check_basic(anteo)
    assert len(anteo) == read("t18_anteo.html").count("data-time="), "a showtime was silently dropped"
    assert any(s[2] for s in anteo if s[0] == "BURNING"), "V.O. is only in the 2nd label"
    t, _, vo, url = find(anteo, "MINOTAUR", "2026-10-31T14:45")
    assert vo and t == "MINOTAUR - NEL LABIRINTO DEL POTERE", t
    assert url.startswith("https://anteo.spaziocinema.18tickets.it/film/123998/")
    # Arlecchino marks V.O. only in the "Lingua:" field
    arl = scrape.parse_t18_home(read("t18_arlecchino.html"))
    check_basic(arl)
    assert find(arl, "LA CASA DELLE DONNE", "2026-10-31T14:00")[2]
    # Cinemino's homepage has no showtimes -> caller falls back to film pages
    assert scrape.parse_t18_home(read("t18_cinemino.html")) == []


def test_t18_film():
    url = "https://ilcinemino.ilcinemino.18tickets.it/film/157443"
    shows = scrape.parse_t18_film(read("t18_cinemino_film.html"), read("t18_cinemino_occ.js"), url)
    assert [s[1].strftime("%d/%m %H:%M") for s in shows] == [
        "31/10 15:00", "01/11 17:00", "02/11 19:30", "03/11 15:00", "04/11 21:30"], shows
    assert all(s[0] == "BALCANICA" and not s[2] and s[3] == url for s in shows)  # Lingua: Italiano


def test_cvu():
    base = "https://www.notoriouscinemas.it/gloria/index.php"
    gloria = scrape.parse_cvu(read("cvu_gloria.html"), base, date(2026, 10, 31))
    assert gloria and all(s[3] == base for s in gloria)
    t, _, vo, _ = find(gloria, "DIO RIDE", "2026-10-31T14:30")
    assert t == "DIO RIDE" and not vo  # "NEW!" badge stripped
    assert len([s for s in gloria if s[0] == "DIO RIDE"]) == 19
    assert len(gloria) == read("cvu_gloria.html").count('class="orario_"'), "a showtime was silently dropped"
    real_get = scrape.get
    def fake_get(url, ajax=False):
        if "week=next" in url:
            raise OSError("next week down")
        return read("cvu_gloria.html")
    scrape.get = fake_get
    try:
        assert scrape.fetch_cvu("https://www.notoriouscinemas.it/gloria/"), "next-week failure hid the current week"
    finally:
        scrape.get = real_get
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
    assert not scrape.is_vo("SOTTO LE FOGLIE") and scrape.is_vo("Burning", "francese sott ita")
    # Italian vs original title: grouped through the manual alias table
    scrape.ALIASES["the bride"] = "la sposa"
    assert k("The Bride!") == k("LA SPOSA")
    del scrape.ALIASES["the bride"]
    assert scrape.display_title({"DIO RIDE": 3, "Dio ride": 1}) == "Dio ride"
    assert scrape.display_title({"DIO RIDE": 3}) == "Dio ride"


def row(title, cid, start, vo=False):
    return (scrape.film_key(title), title, cid, datetime.fromisoformat(start), vo, "https://x/")


def test_build_data():
    rows = [
        row("Minotaur", "uci", "2026-11-01T21:00"),
        row("MINOTAUR - NEL LABIRINTO DEL POTERE", "anteo", "2026-11-01T18:00", True),
        row("Avatar", "uci", "2026-11-01T20:00"),
        row("Avatar: fuoco e cenere", "uci", "2026-11-01T17:00"),  # same cinema lists both: two films
        row("Scream", "gloria", "2026-11-01T22:00"),
        row("Scream 7", "uci", "2026-11-01T22:30"),  # sequel, no subtitle separator
        row("Frankenstein Junior", "mic", "2026-11-01T16:00"),
        row("Frankenstein", "uci", "2026-11-02T00:20"),  # after midnight: still Sunday night
    ]
    data = scrape.build_data(rows, ["colosseo"])
    keys = {s["key"] for s in data["shows"]}
    assert keys == {"minotaur", "avatar", "avatar fuoco e cenere", "scream", "scream 7", "frankenstein", "frankenstein junior"}, keys
    assert "nel labirinto" in data["alt"]["minotaur"], "merged film must stay findable by its own title"
    late = next(s for s in data["shows"] if s["start"] == "2026-11-02T00:20")
    assert late["day"] == "2026-11-01", late
    assert data["failed"] == ["Colosseo"] and "colosseo" not in data["cinemas"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
