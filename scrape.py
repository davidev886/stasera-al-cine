#!/usr/bin/env python3
"""Fetch showtimes for Milan cinemas and write site/data.json.

Three sources cover 15 venues: UCI (open JSON), 18tickets (Anteo, Cineteca,
Cinemino) and the "cvu" PHP sites (Notorious, Colosseo, Eliseo).
Pages are machine-generated templates (18tickets even leaves tags unclosed),
so they are read with regexes; test_scrape.py pins them to saved fixtures.
"""
import html
import json
import re
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
UA = "cinema-milano/1.0 (aggregatore personale di orari)"
DAYS_AHEAD = 7
OUT = Path(__file__).parent / "site" / "data.json"

# id: (name, address, reader, reader parameter, (lat, lon) from OpenStreetMap, for "Vicino a me")
CINEMAS = {
    "anteo": ("Anteo Palazzo del Cinema", "Piazza XXV Aprile 8", "t18", "anteo.spaziocinema", (45.48006, 9.18779)),
    "citylife": ("CityLife Anteo", "Piazza Tre Torri 1", "t18", "citylife.spaziocinema", (45.47831, 9.15706)),
    "ariosto": ("Ariosto Anteo spazioCinema", "Via Ariosto 16", "t18", "ariosto.spaziocinema", (45.46915, 9.16588)),
    "arlecchino": ("Cineteca Arlecchino", "Via San Pietro all'Orto 9", "t18", "arlecchino.cinetecadimilano", (45.46646, 9.19532)),
    "mic": ("Cineteca MIC", "Viale Fulvio Testi 121", "t18", "mic.cinetecadimilano", (45.51338, 9.20391)),
    "metropolis": ("Cineteca Metropolis", "Via Oslavia 8, Paderno Dugnano", "t18", "metropolis.cinetecadimilano", (45.56864, 9.15998)),
    "cinemino": ("Il Cinemino", "Via Seneca 6", "t18films", "ilcinemino.ilcinemino", (45.45412, 9.20833)),
    "colosseo": ("Colosseo", "Viale Monte Nero 84", "cvu", "https://www.ilregnodelcinema.com/colosseo/", (45.46171, 9.20785)),
    "eliseo": ("Eliseo", "Via Torino 64", "cvu", "https://www.ilregnodelcinema.com/eliseo/", (45.46079, 9.18272)),
    "gloria": ("Notorious Gloria", "Corso Vercelli 18", "cvu", "https://www.notoriouscinemas.it/gloria/", (45.46652, 9.16184)),
    "merlata": ("Notorious Merlata Bloom", "Via Daimler, Milano", "cvu", "https://www.notoriouscinemas.it/merlata/", (45.51352, 9.09795)),
    "sesto": ("Notorious Sesto San Giovanni", "Viale Sarca, Centro Sarca, Sesto S.G.", "cvu", "https://www.notoriouscinemas.it/sestosangiovanni/", (45.5299, 9.218)),
    "palestrina": ("Palestrina", "Via Palestrina 7", "palestrina", "", (45.48501, 9.21389)),
    "beltrade": ("Beltrade", "Via Nino Oxilia 10", "beltrade", "", (45.49247, 9.21489)),
    "plinius": ("Plinius Multisala", "Viale Abruzzi 28", "plinius", "", (45.47762, 9.21737)),
    "cinelandia": ("Cinelandia Certosa", "Via Giovanni Gentile 3", "cinelandia", "", (45.50964, 9.12815)),
    "ducale": ("Ducale", "Piazza Napoli 27", "cinenauta", "5065", (45.45245, 9.15467)),
    "arcobaleno": ("Arcobaleno Film Center", "Viale Tunisia 11", "cinenauta", "5066", (45.47725, 9.20531)),
    "uci-bicocca": ("UCI Bicocca", "Viale Sarca 336", "uci", "uci-cinemas-bicocca-milano", (45.52245, 9.21967)),
    "uci-milanofiori": ("UCI Milanofiori", "Viale Milanofiori, Assago", "uci", "uci-cinemas-milanofiori-milano", (45.40301, 9.14802)),
    "uci-pioltello": ("UCI Pioltello", "Via San Francesco 33, Pioltello", "uci", "uci-cinemas-pioltello-milano", (45.50319, 9.32181)),
}

# Same film under different titles (Italian vs original): film_key(a) -> film_key(b).
# ponytail: hand-kept table, fine for a few cases a week; switch to an external id (TMDB) if it grows.
ALIASES = {}

# --- titles -------------------------------------------------------------------

# V.O. markers that can appear in a title or a language label
VO_RE = re.compile(r"\bv\.\s?o\b|\bo\.\s?v\b|\bvos\b|film in inglese|lingua originale|versione originale", re.I)
# Noise stripped from titles: "| ANTEPRIMA" suffixes, "[2026]", "(O.V.)", "V.O. SOTT. ITA -" prefixes,
# "- V. O.", age ratings "- v.m.14", "- riedizione", UCI's "C.A." (contenuto alternativo)
NOISE_RE = re.compile(
    r"\|.*$|\[\d{4}\]|\(\s*(?:o\.\s?v\.?|v\.\s?o\.?|lingua originale)\s*\)"
    r"|(?:\s-)?\s*\bv\.\s?o\.?(?:\s?sott\.?\s?ita\.?)?(?:\s*-)?"
    r"|(?:\s-)?\s*\bv\.\s?m\.?\s?\d{2}\b|(?:\s-)?\s*\briedizione\b|\bc\.a\.\s*$"
    r"|^\s*(?:dolby\s+)?(?:atmos|imax|4dx|3d)\s*[-–]\s*",  # "Atmos – Clayface": the format, not the title
    re.I,
)
SUBTITLE_RE = re.compile(r"\s[-–]\s|:")  # "Minotaur - nel labirinto", "Avatar: fuoco e cenere"


def clean_title(raw):
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return " ".join(NOISE_RE.sub(" ", t).split()).strip(" -")


def is_vo(title, language=""):
    """language = the cinema's language/label fields; "sottotitoli" only counts there ("Sotto le foglie").
    "v.o. italiano" is an Italian film, not a foreign one in original language."""
    text = re.sub(r"(?:\bv\.\s?o\.?|versione originale)\s+italian\w*", " ", f"{title} {language}", flags=re.I)
    return bool(VO_RE.search(text) or re.search(r"\bsott", language, re.I))


def film_key(title):
    t = re.sub(r"\(.*?\)", " ", clean_title(title))  # "(LA MAISON DES FEMMES)", "(RIED.)"
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"(\w)['’](\w)", r"\1\2", t)  # "gialappa's" == "gialappàs" once accents are gone
    t = " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())
    return ALIASES.get(t, t)


def head_keys(title):
    """Keys of the two halves of "X - Y": "Minotaur - nel labirinto del potere" -> "minotaur";
    Palestrina puts the original title first: "La maison des femmes - La casa delle donne"."""
    return {film_key(part) for part in SUBTITLE_RE.split(clean_title(title), maxsplit=1)}


def merge_keys(sales, heads):
    """sales {key: set(cinema)}, heads {key: set(head_keys)} -> {key: canonical key}.
    "minotaur nel labirinto del potere" joins "minotaur" when its title is literally "Minotaur - ..."
    and no cinema lists both: chains disagree on subtitles, one cinema showing both means two films.
    Sequels without a separator ("Scream 7") never merge.
    ponytail: a re-release titled "X: something" can still join X where X isn't shown."""
    canon, roots = {}, {}
    for k in sorted(sales, key=len):
        a = next((h for h in sorted(heads.get(k, ()), key=len)
                  if h != k and len(h) >= 5 and h in roots and not roots[h] & sales[k]), None)
        if a:
            roots[a] |= sales[k]
        else:
            roots[k] = set(sales[k])
        canon[k] = a or k
    return canon


def display_title(counts):
    """Most common spelling, preferring one that isn't ALL CAPS."""
    best = max(counts, key=lambda t: (t != t.upper(), counts[t]))
    return best if best != best.upper() else best.capitalize()


def cinema_day(start):
    """A 00:20 show belongs to the previous evening's programme."""
    return (start - timedelta(hours=5)).date()


# --- readers: each returns [(title, start, vo, url)], start naive Europe/Rome ---

def get(url, ajax=False, pause=0.5):
    headers = {"User-Agent": UA}
    if ajax:  # 18tickets answers its own page's jQuery calls only
        headers |= {"X-Requested-With": "XMLHttpRequest", "Accept": "text/javascript, */*; q=0.01"}
    req = urllib.request.Request(url, headers=headers)
    for attempt in (1, 2):  # one retry: a transient error would hide the cinema until the next run
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            break
        except OSError as e:  # URLError, HTTPError and timeouts are all OSError
            if attempt == 2:
                raise
            print(f"retry {url}: {e!r}", file=sys.stderr)
            time.sleep(30 if getattr(e, "code", None) == 429 else 5)  # 18tickets rate-limits without Retry-After
    time.sleep(pause)  # be gentle
    return body


def parse_uci(data, base="https://www.ucicinemas.it"):
    shows = []
    for film in data["data"]:
        for group in film["screens"]:
            for entries in group.values():
                for e in entries:
                    # DSA = autism-friendly screening, still in Italian
                    vo = (e.get("language") or {}).get("slug", "ITA") not in ("ITA", "DSA")
                    for p in e["performances"]:
                        start = datetime.strptime(p["starts_at"], "%Y-%m-%d %H:%M:%S")
                        shows.append((film["title"], start, vo, base + p["cart_link"]))
    return shows


def fetch_uci(slug):
    api = f"https://backend.ucicinemas.it/api/theatres/{slug}"
    today = datetime.now(TZ).date()
    last = today + timedelta(days=DAYS_AHEAD)
    days = [d for d in json.loads(get(api + "/programmingDays")) if str(today) <= d <= str(last)]
    return [s for d in days for s in parse_uci(json.loads(get(f"{api}/programming/{d}")))]


def parse_t18_home(page):
    """Anteo/Cineteca homepages: one block per film, each showtime an <a data-time=epoch-ms>."""
    shows = []
    for block in page.split("class='page-heading")[1:]:
        title = re.search(r"class='movie__title'[^>]*>\s*([^<\s][^<]*)", block)
        times = re.findall(r"data-time='(\d+)' href='([^']+)'", block)
        if not (title and times):
            continue
        lingua = re.search(r"Lingua:\s*</strong>\s*([^<]*)", block)
        labels = re.findall(r"film-label[^>]*>\s*([^<]*)", block)  # e.g. ["LEE CHANG-DONG", "V.O. SOTT. ITA"]
        vo = is_vo(title.group(1), " ".join([lingua.group(1) if lingua else "", *labels]))
        for ms, url in times:
            start = datetime.fromtimestamp(int(ms) / 1000, TZ).replace(tzinfo=None)
            shows.append((clean_title(title.group(1)), start, vo, html.unescape(url)))
    return shows


def fetch_t18(host):
    return parse_t18_home(get(f"https://{host}.18tickets.it/"))


def parse_t18_film(page, occupations, url):
    """Cinemino: the film page has the language, the occupations endpoint (JS wrapping HTML) every date."""
    lingua = re.search(r"Lingua:\s*</strong>\s*([^<]*)", page)
    occupations = occupations.replace("\\'", "'").replace('\\"', '"').replace("\\/", "/")
    shows = []
    for tag in re.findall(r"<a class='film-projection[^>]*>", occupations):
        when = re.search(r"data-date-extended='[^']*?(\d{2})/(\d{2})/(\d{4}) ore (\d{2}):(\d{2})'", tag)
        title = re.search(r"data-title='([^']*)'", tag)
        if when and title:
            d, m, y, hh, mm = map(int, when.groups())
            vo = is_vo(title.group(1), lingua.group(1) if lingua else "")
            shows.append((clean_title(title.group(1)), datetime(y, m, d, hh, mm), vo, url))
    return shows


def fetch_t18_films(host):
    """Cinemino's homepage lists films without times: two requests per film."""
    base = f"https://{host}.18tickets.it"
    shows = []
    for fid in sorted(set(re.findall(r"/film/(\d+)'", get(base + "/")))):
        url = f"{base}/film/{fid}"
        page = get(url, pause=2)
        shows += parse_t18_film(page, get(url + "/fetch_film_occupations", ajax=True, pause=2), url)  # no date = all dates
    return shows


def infer_year(month, today):
    """cvu dates are dd/mm: January seen in December is next year."""
    if month < today.month - 6:
        return today.year + 1
    if month > today.month + 6:
        return today.year - 1
    return today.year


def parse_cvu(page, url, today):
    shows = []
    for block in page.split('class="filmContainer')[1:]:
        title = re.search(r'class="titolo">(.*?)</div>', block, re.S)
        if not title:
            continue
        raw = re.sub(r'<span class="newMovie">.*?</span>', "", title.group(1))
        vo = is_vo(html.unescape(raw))
        for d, m, hours in re.findall(r'class="dayName">[^<]*?(\d{2})/(\d{2})</div>(.*?)</li>', block, re.S):
            y = infer_year(int(m), today)
            for hh, mm in re.findall(r">(\d{2}):(\d{2})<", hours):
                shows.append((clean_title(raw), datetime(y, int(m), int(d), int(hh), int(mm)), vo, url))
    return shows


def fetch_cvu(base):
    today = datetime.now(TZ).date()
    url = base + "index.php"
    shows = parse_cvu(get(url), url, today)
    try:  # next week is only presales: losing it must not hide this week
        shows += parse_cvu(get(url + "?week=next"), url, today)
    except OSError as e:
        print(f"next week skipped {url}: {e!r}", file=sys.stderr)
    return shows


def parse_cinenauta(page, local_id):
    """cinenauta.it (Ducale, Arcobaleno): one page embeds `const DATA={...}` for both cinemas."""
    start = page.index("const DATA=") + len("const DATA=")
    data, _ = json.JSONDecoder().raw_decode(page, start)
    shows = []
    for cinema in data["cinemas"]:
        if cinema["local_id"] != local_id:
            continue
        for e in cinema["events"]:
            # Webtic titles are cut ("Ken Il Guerriero-Il Film 40° Ann"); original_title is the full one,
            # unless it's in another language ("Ai no korîda" for "L'impero dei sensi")
            title, orig = clean_title(e["title"]), clean_title(e.get("original_title") or "")
            if orig and film_key(orig).split()[:1] == film_key(title).split()[:1]:
                title = orig
            vo = "O.V." in e["properties"] or is_vo(e["title"])
            for day in e["days"]:
                for p in day["performances"]:
                    shows.append((title, datetime.fromisoformat(p["start"]), vo, e["webtic_url"]))
    return shows


def fetch_cinenauta(local_id):
    return parse_cinenauta(get("https://www.cinenauta.it/"), local_id)  # robots.txt forbids /api/


MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def week_start(text):
    """'Programmazione da giovedì 29 ottobre a mercoledì 04 novembre 2026' -> date(2026, 10, 29)."""
    m = re.search(r"da \w+ (\d{1,2}) (\w+) a \w+ \d{1,2} (\w+) (\d{4})", " ".join(text.split()))  # &nbsp;, newlines
    d, month, end_month, y = int(m[1]), MONTHS.index(m[2].lower()) + 1, MONTHS.index(m[3].lower()) + 1, int(m[4])
    return date(y - (month > end_month), month, d)  # "31 dicembre a 06 gennaio 2027" starts in 2026


def parse_palestrina(page, base="https://www.progettolumiere.it/palestrina/"):
    """One Thu-Wed week in tabs <div id="thu"> ... <div id="wed">; "VO 18:15" marks original language."""
    first = week_start(re.sub(r"<[^>]+>", " ", page))
    offset = {"thu": 0, "fri": 1, "sat": 2, "sun": 3, "mon": 4, "tue": 5, "wed": 6}
    shows = []
    tabs = re.split(r'<div id="(thu|fri|sat|sun|mon|tue|wed)">', page)
    for tab, body in zip(tabs[1::2], tabs[2::2]):
        day = first + timedelta(days=offset[tab])
        for block in body.split('class="row movie-tabs"')[1:]:
            title = re.search(r'<h3 class="no-underline">(.*?)</h3>', block, re.S)
            link = re.search(r'href="([\w-]+\.php)"', block)
            if not (title and link):
                continue
            for times in re.findall(r'class="time"[^>]*>([^<]*)<', block):
                for vo_tag, hh, mm in re.findall(r"(VO\s*)?(\d{2})[:.](\d{2})", times):
                    vo = bool(vo_tag) or is_vo(title.group(1))
                    start = datetime(day.year, day.month, day.day, int(hh), int(mm))
                    shows.append((clean_title(title.group(1)), start, vo, base + link.group(1)))
    return shows


def fetch_palestrina(_):
    return parse_palestrina(get("https://www.progettolumiere.it/palestrina/home.php"))


def parse_beltrade_list(page):
    """bandhi.it listing: <div data-wpt-start=epoch> + title link to the film page. -> [(title, start, url)]"""
    shows = []
    for block in page.split('<div class="wp_theatre_event ')[1:]:
        start = re.search(r'data-wpt-start="(\d+)"', block)
        title = re.search(r'wp_theatre_event_title"><a href="([^"]+)">([^<]+)<', block)
        if start and title:  # "prossimamente" entries have no time
            when = datetime.fromtimestamp(int(start.group(1)), TZ).replace(tzinfo=None)
            shows.append((clean_title(title.group(2)), when, title.group(1)))
    return shows


def beltrade_vo(film_page):
    """V.O. is only written in the film page credits: "v.o. russo con sottotitoli in italiano"."""
    credits = re.search(r'bah_prod_sottotitolo"[^>]*>\s*<div class="wpb_wrapper">(.*?)</div>', film_page, re.S)
    return bool(credits) and is_vo("", html.unescape(re.sub(r"<[^>]+>", " ", credits.group(1))))


def fetch_beltrade(_):
    now = datetime.now(TZ).replace(tzinfo=None)
    listing = [s for s in parse_beltrade_list(get("https://bandhi.it/bah/beltrade/"))
               if now <= s[1] < now + timedelta(days=DAYS_AHEAD + 1)]
    vo = {url: beltrade_vo(get(url, pause=1)) for url in sorted({s[2] for s in listing})}  # only films on in the window
    return [(title, start, vo[url], url) for title, start, url in listing]


def parse_plinius(page):
    """multisalaplinius.com: per film, days labelled "Oggi", "Domani", "Lun 02/11" with time pills.
    "Oggi" is relative to when the page was generated: the page cache stamps it as <!-- ...cached@epoch -->."""
    cached = re.search(r"cached@(\d+)", page)
    base = (datetime.fromtimestamp(int(cached[1]), TZ) if cached else datetime.now(TZ)).date()
    shows = []
    for block in page.split("film-list-title")[1:]:
        title = re.search(r">(.*?)</h2>", block, re.S)
        if not title:
            continue
        vo = is_vo(html.unescape(title[1]))
        for label, pills in re.findall(r'showtimes-day__label[^>]*>\s*([^<]*?)\s*</div>\s*'
                                       r'<div class="showtimes-day__pills">(.*?)</div>', block, re.S):
            dm = re.search(r"(\d{2})/(\d{2})", label)
            day = (date(infer_year(int(dm[2]), base), int(dm[2]), int(dm[1])) if dm
                   else base + timedelta(days={"Oggi": 0, "Domani": 1}[label]))
            for url, hh, mm in re.findall(r'href="([^"]+)">\s*(\d{2}):(\d{2})', pills):
                start = datetime(day.year, day.month, day.day, int(hh), int(mm))
                shows.append((clean_title(title[1]), start, vo, html.unescape(url)))
    return shows


def fetch_plinius(_):
    return parse_plinius(get("https://multisalaplinius.com/"))


WEEKDAYS = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]


def day_from_name(weekday, dom, today):
    """"SABATO 31" has no month: the date near today with that weekday and day of the month."""
    wd = WEEKDAYS.index(film_key(weekday).split()[0])
    return next(d for d in (today + timedelta(days=i) for i in range(-10, 25)) if d.day == dom and d.weekday() == wd)


def parse_cinelandia(page, today, base_url=""):
    """cinelandia.it: <h3>title</h3> then <div class="giorno">SABATO 31</div><div class="orario">15.10 – 17.40</div>."""
    shows = []
    for block in page.split('class="boxProg"')[1:]:
        title = re.search(r'<a href="([^"]+)"[^>]*><h3>(.*?)</h3>', block, re.S)
        if not title:
            continue
        vo = is_vo(html.unescape(title[2]))
        for name, dom, times in re.findall(r'class="giorno">\s*([A-Za-z]+)\S*\s+(\d{1,2})\s*</div>\s*'
                                           r'<div class="orario">([^<]*)<', block):
            day = day_from_name(name, int(dom), today)
            for hh, mm in re.findall(r"(\d{1,2})[.:](\d{2})", times):
                start = datetime(day.year, day.month, day.day, int(hh), int(mm))
                shows.append((clean_title(title[2]), start, vo, title[1]))
    return shows


def fetch_cinelandia(_):  # robots.txt: Crawl-delay 2
    page = get("https://www.cinelandia.it/cinema-cinelandia-milano-certosa/", pause=2)
    return parse_cinelandia(page, datetime.now(TZ).date())


READERS = {"uci": fetch_uci, "t18": fetch_t18, "t18films": fetch_t18_films, "cvu": fetch_cvu,
           "cinenauta": fetch_cinenauta, "palestrina": fetch_palestrina, "beltrade": fetch_beltrade,
           "plinius": fetch_plinius, "cinelandia": fetch_cinelandia}


# --- main ---------------------------------------------------------------------

def build_data(rows, failed):
    """rows [(key, title, cinema, start, vo, url)], failed [cinema id] -> the JSON the page reads."""
    sales, heads = {}, {}
    for key, title, cid, *_ in rows:
        sales.setdefault(key, set()).add(cid)
        heads.setdefault(key, set()).update(head_keys(title))
    canon = merge_keys(sales, heads)
    names, alt = {}, {}
    for key, title, *_ in rows:
        names.setdefault(canon[key], Counter())[title] += 1
        if canon[key] != key:
            alt.setdefault(canon[key], set()).add(key)
    return {
        "updated": datetime.now(TZ).isoformat(timespec="minutes"),
        "failed": [CINEMAS[cid][0] for cid in failed],
        "cinemas": {cid: {"name": c[0], "address": c[1], "lat": c[4][0], "lon": c[4][1]}
                    for cid, c in CINEMAS.items() if cid not in failed},
        "alt": {k: " ".join(sorted(v)) for k, v in alt.items()},  # merged titles stay searchable
        "shows": [
            {"film": display_title(names[canon[key]]), "key": canon[key], "cinema": cid,
             "start": start.strftime("%Y-%m-%dT%H:%M"), "day": str(cinema_day(start)), "vo": vo, "url": url}
            for key, title, cid, start, vo, url in sorted(rows, key=lambda r: (r[3], r[0]))
        ],
    }


def main():
    now = datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)
    end = (now + timedelta(days=DAYS_AHEAD + 1)).replace(hour=5, minute=0)
    seen, rows, failed = set(), [], []
    for cid, (name, _, reader, param, _) in CINEMAS.items():
        try:
            got = [s for s in READERS[reader](param) if now <= s[1] < end]
            if not got:
                raise ValueError("0 spettacoli")
        except Exception as e:  # one broken site must not take the others down
            print(f"FAIL {cid}: {e!r}", file=sys.stderr)
            failed.append(cid)
            continue
        print(f"ok   {cid}: {len(got)}")
        for title, start, vo, url in got:
            key = film_key(title)
            if (cid, key, start, vo) not in seen:
                seen.add((cid, key, start, vo))
                rows.append((key, clean_title(title), cid, start, vo, url))

    if len(failed) > len(CINEMAS) / 2:  # network blip on the runner: keep the last good data online
        print(f"{len(failed)} cinema falliti: non pubblico", file=sys.stderr)
        return 1
    data = build_data(rows, failed)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(data['shows'])} spettacoli, {len(set(s['key'] for s in data['shows']))} film, falliti: {failed or 'nessuno'}")
    return 1 if failed else 0  # non-zero -> GitHub emails; the deploy still runs


if __name__ == "__main__":
    sys.exit(main())
