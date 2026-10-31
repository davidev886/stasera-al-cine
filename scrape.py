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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
UA = "cinema-milano/1.0 (aggregatore personale di orari)"
DAYS_AHEAD = 7
OUT = Path(__file__).parent / "site" / "data.json"

# id: (name, address, reader, reader parameter)
CINEMAS = {
    "anteo": ("Anteo Palazzo del Cinema", "Piazza XXV Aprile 8", "t18", "anteo.spaziocinema"),
    "citylife": ("CityLife Anteo", "Piazza Tre Torri 1", "t18", "citylife.spaziocinema"),
    "ariosto": ("Ariosto Anteo spazioCinema", "Via Ariosto 16", "t18", "ariosto.spaziocinema"),
    "arlecchino": ("Cineteca Arlecchino", "Via San Pietro all'Orto 9", "t18", "arlecchino.cinetecadimilano"),
    "mic": ("Cineteca MIC", "Viale Fulvio Testi 121", "t18", "mic.cinetecadimilano"),
    "metropolis": ("Cineteca Metropolis", "Via Oslavia 8, Paderno Dugnano", "t18", "metropolis.cinetecadimilano"),
    "cinemino": ("Il Cinemino", "Via Seneca 6", "t18films", "ilcinemino.ilcinemino"),
    "colosseo": ("Colosseo", "Viale Monte Nero 84", "cvu", "https://www.ilregnodelcinema.com/colosseo/"),
    "eliseo": ("Eliseo", "Via Torino 64", "cvu", "https://www.ilregnodelcinema.com/eliseo/"),
    "gloria": ("Notorious Gloria", "Corso Vercelli 18", "cvu", "https://www.notoriouscinemas.it/gloria/"),
    "merlata": ("Notorious Merlata Bloom", "Via Daimler, Milano", "cvu", "https://www.notoriouscinemas.it/merlata/"),
    "sesto": ("Notorious Sesto San Giovanni", "Viale Sarca, Centro Sarca, Sesto S.G.", "cvu", "https://www.notoriouscinemas.it/sestosangiovanni/"),
    "uci-bicocca": ("UCI Bicocca", "Viale Sarca 336", "uci", "uci-cinemas-bicocca-milano"),
    "uci-milanofiori": ("UCI Milanofiori", "Viale Milanofiori, Assago", "uci", "uci-cinemas-milanofiori-milano"),
    "uci-pioltello": ("UCI Pioltello", "Via San Francesco 33, Pioltello", "uci", "uci-cinemas-pioltello-milano"),
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
    r"|(?:\s-)?\s*\bv\.\s?m\.?\s?\d{2}\b|(?:\s-)?\s*\briedizione\b|\bc\.a\.\s*$",
    re.I,
)
SUBTITLE_RE = re.compile(r"\s[-–]\s|:")  # "Minotaur - nel labirinto", "Avatar: fuoco e cenere"


def clean_title(raw):
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return " ".join(NOISE_RE.sub(" ", t).split()).strip(" -")


def is_vo(title, language=""):
    """language = the cinema's language/label fields; "sottotitoli" only counts there ("Sotto le foglie")."""
    return bool(VO_RE.search(f"{title} {language}") or re.search(r"\bsott", language, re.I))


def film_key(title):
    t = re.sub(r"\(.*?\)", " ", clean_title(title))  # "(LA MAISON DES FEMMES)", "(RIED.)"
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"(\w)['’](\w)", r"\1\2", t)  # "gialappa's" == "gialappàs" once accents are gone
    t = " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())
    return ALIASES.get(t, t)


def head_key(title):
    """Key of the title before its subtitle: "Minotaur - nel labirinto del potere" -> "minotaur"."""
    return film_key(SUBTITLE_RE.split(clean_title(title), maxsplit=1)[0])


def merge_keys(sales, heads):
    """sales {key: set(cinema)}, heads {key: set(head_key)} -> {key: canonical key}.
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


READERS = {"uci": fetch_uci, "t18": fetch_t18, "t18films": fetch_t18_films, "cvu": fetch_cvu}


# --- main ---------------------------------------------------------------------

def build_data(rows, failed):
    """rows [(key, title, cinema, start, vo, url)], failed [cinema id] -> the JSON the page reads."""
    sales, heads = {}, {}
    for key, title, cid, *_ in rows:
        sales.setdefault(key, set()).add(cid)
        heads.setdefault(key, set()).add(head_key(title))
    canon = merge_keys(sales, heads)
    names, alt = {}, {}
    for key, title, *_ in rows:
        names.setdefault(canon[key], Counter())[title] += 1
        if canon[key] != key:
            alt.setdefault(canon[key], set()).add(key)
    return {
        "updated": datetime.now(TZ).isoformat(timespec="minutes"),
        "failed": [CINEMAS[cid][0] for cid in failed],
        "cinemas": {cid: {"name": c[0], "address": c[1]} for cid, c in CINEMAS.items() if cid not in failed},
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
    for cid, (name, _, reader, param) in CINEMAS.items():
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
