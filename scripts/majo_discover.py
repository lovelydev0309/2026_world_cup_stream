#!/usr/bin/env python3
"""
Find acclaimed, high-IMDb films in the provider's VOD catalog to backfill the MAJO disk.
Matches a curated target list (famous title + year) against get_vod_streams, prefers the
Spanish/Latino audio variant, excludes titles already on disk, and writes the winning
stream_ids to a file for vod_ingest2.py RESTORE mode. Read-only on the catalog.

Usage: majo_discover.py [out_ids_file]   (default: /opt/streaming-stack/config/majo_ingest_ids.txt)
"""
import json, os, re, sys, unicodedata, urllib.request

def cfg(k, d=""):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "accounts.env")
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            kk, vv = line.split("=", 1)
            if kk.strip() == k: return vv.strip()
    return d

HOST = cfg("VOD_HOST", "tvon247.com"); USER = cfg("VOD_USER"); PW = cfg("VOD_PW")
if not HOST.startswith("http"): HOST = "http://" + HOST
DISK = "/opt/streaming-stack/vod-disk"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/opt/streaming-stack/config/majo_ingest_ids.txt"
UA = "okhttp/4.9.3"

def norm(s):
    n = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", n.lower())

def clean_title(name):
    t = name or ""
    t = re.sub(r"^\s*[A-Za-z]{2,4}\s*-\s*", "", t)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\s*-\s*(19|20)\d{2}\s*$", "", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -")

def lang_of(name):
    m = re.match(r"\s*([A-Za-z]{2,4})\s*-\s*", name or "")
    return m.group(1).upper() if m else ""

def year_of(name):
    m = re.search(r"(19|20)\d{2}", name or "")
    return int(m.group(0)) if m else 0

# ---- curated target list: acclaimed / popular high-IMDb films (title, year) ----
TARGETS = [
    ("The Shawshank Redemption",1994),("The Godfather",1972),("The Godfather Part II",1974),
    ("Pulp Fiction",1994),("Fight Club",1999),("Forrest Gump",1994),("The Matrix",1999),
    ("Goodfellas",1990),("Se7en",1995),("Seven",1995),("The Silence of the Lambs",1991),
    ("Saving Private Ryan",1998),("The Green Mile",1999),("The Departed",2006),("The Prestige",2006),
    ("The Usual Suspects",1995),("Leon The Professional",1994),("Schindler's List",1993),
    ("Terminator 2 Judgment Day",1991),("Back to the Future",1985),("Alien",1979),("Aliens",1986),
    ("The Lion King",1994),("Toy Story",1995),("Toy Story 3",2010),("Up",2009),("WALL-E",2008),
    ("Finding Nemo",2003),("Spirited Away",2001),("Princess Mononoke",1997),("Braveheart",1995),
    ("American History X",1998),("Requiem for a Dream",2000),("Memento",2000),("Snatch",2000),
    ("Kill Bill Vol 1",2003),("Kill Bill Vol 2",2004),("Eternal Sunshine of the Spotless Mind",2004),
    ("No Country for Old Men",2007),("There Will Be Blood",2007),("Slumdog Millionaire",2008),
    ("Avatar",2009),("District 9",2009),("Inglourious Basterds",2009),("The Wrestler",2008),
    ("Inception",2010),("The Social Network",2010),("Black Swan",2010),("The King's Speech",2010),
    ("Shutter Island",2010),("Drive",2011),("The Artist",2011),("Warrior",2011),
    ("Django Unchained",2012),("The Avengers",2012),("The Dark Knight Rises",2012),("Life of Pi",2012),
    ("Argo",2012),("Skyfall",2012),("Gravity",2013),("The Wolf of Wall Street",2013),
    ("12 Years a Slave",2013),("Her",2013),("Prisoners",2013),("Rush",2013),("Gone Girl",2014),
    ("Birdman",2014),("The Grand Budapest Hotel",2014),("Guardians of the Galaxy",2014),
    ("Nightcrawler",2014),("Mad Max Fury Road",2015),("The Martian",2015),("Inside Out",2015),
    ("Room",2015),("Spotlight",2015),("Sicario",2015),("The Revenant",2015),("Creed",2015),
    ("La La Land",2016),("Arrival",2016),("Moonlight",2016),("Deadpool",2016),("Zootopia",2016),
    ("Hell or High Water",2016),("Manchester by the Sea",2016),("Coco",2017),("Dunkirk",2017),
    ("Blade Runner 2049",2017),("Get Out",2017),("Three Billboards Outside Ebbing Missouri",2017),
    ("Logan",2017),("The Shape of Water",2017),("Baby Driver",2017),("Avengers Infinity War",2018),
    ("Spider-Man Into the Spider-Verse",2018),("A Star is Born",2018),("Bohemian Rhapsody",2018),
    ("Green Book",2018),("Roma",2018),("Black Panther",2018),("A Quiet Place",2018),
    ("Joker",2019),("Parasite",2019),("Avengers Endgame",2019),("1917",2019),
    ("Once Upon a Time in Hollywood",2019),("Ford v Ferrari",2019),("Jojo Rabbit",2019),
    ("Knives Out",2019),("Marriage Story",2019),("The Irishman",2019),("Little Women",2019),
    ("Tenet",2020),("Nomadland",2020),("Sound of Metal",2020),("The Trial of the Chicago 7",2020),
    ("The Father",2020),("CODA",2021),("The Power of the Dog",2021),("Encanto",2021),
    ("No Time to Die",2021),("Shang-Chi",2021),("Everything Everywhere All at Once",2022),
    ("The Batman",2022),("Avatar The Way of Water",2022),("The Banshees of Inisherin",2022),
    ("The Whale",2022),("Nope",2022),("Barbie",2023),("Poor Things",2023),
    ("Killers of the Flower Moon",2023),("Spider-Man Across the Spider-Verse",2023),
    ("John Wick Chapter 4",2023),("The Holdovers",2023),("Anatomy of a Fall",2023),
    ("Past Lives",2023),("The Zone of Interest",2023),("Dune Part Two",2024),("Wicked",2024),
    ("The Wild Robot",2024),("Inside Out 2",2024),("Deadpool & Wolverine",2024),("Gladiator II",2024),
    ("A Complete Unknown",2024),("Conclave",2024),("The Substance",2024),("Anora",2024),
    ("Harry Potter and the Philosopher's Stone",2001),("Harry Potter and the Deathly Hallows Part 2",2011),
    ("The Lord of the Rings The Fellowship of the Ring",2001),("The Lord of the Rings The Return of the King",2003),
    ("Jurassic Park",1993),("Titanic",1997),("The Silence",2019),("Gladiator",2000),
    ("The Pianist",2002),("Iron Man",2008),("Casino Royale",2006),("Mission Impossible Fallout",2018),
    ("John Wick",2014),("Rocky",1976),("Whiplash",2014),("Interstellar",2014),
    ("The Dark Knight",2008),("Oppenheimer",2023),("Top Gun Maverick",2022),("Dune",2021),
    ("Soul",2020),("Your Name",2016),("Coco",2017),("Spider-Man No Way Home",2021),
]

# fetch catalog
u = "%s/player_api.php?username=%s&password=%s&action=get_vod_streams" % (HOST, USER, PW)
req = urllib.request.Request(u, headers={"User-Agent": UA})
data = json.load(urllib.request.urlopen(req, timeout=60))
print("provider catalog: %d titles" % len(data))

tset = {}
for t, y in TARGETS:
    tset.setdefault(norm(t), set()).add(y)

# titles already on disk (don't re-ingest)
have = set()
for m in json.load(open(DISK + "/movies.json")):
    have.add(norm(m.get("title", "")))

LANG_PREF = {"ES": 0, "LAT": 0, "MX": 0, "LA": 0, "ESP": 0, "LATINO": 0, "BR": 2, "PT": 2, "EN": 3, "US": 3}
best = {}   # norm_title -> (pref, candidate)
for c in data:
    raw = (c.get("name") or "").strip()
    if str(c.get("container_extension", "")).lower() not in ("mp4", "mkv"): continue
    nt = norm(clean_title(raw))
    if nt not in tset or nt in have: continue
    y = year_of(raw)
    yrs = tset[nt]
    if not any(ty and abs(y - ty) <= 1 for ty in yrs): continue   # require a year match (avoid dupes)
    pref = LANG_PREF.get(lang_of(raw), 4)
    if nt not in best or pref < best[nt][0]:
        best[nt] = (pref, c)

winners = [c for _, c in best.values()]
winners.sort(key=lambda c: norm(clean_title(c.get("name", ""))))
open(OUT, "w").write("\n".join(str(c["stream_id"]) for c in winners) + "\n")

print("MATCHED %d acclaimed films present on the provider (of %d targets):" % (len(winners), len(TARGETS)))
for c in winners:
    print("  [%-4s] %-46s id=%s" % (lang_of(c.get("name","")), clean_title(c.get("name",""))[:46], c["stream_id"]))
print("\nstream_ids -> %s" % OUT)
