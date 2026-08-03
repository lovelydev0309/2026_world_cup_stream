#!/usr/bin/env python3
"""
Re-rate a VOD movies.json against OMDb.

The IPTV provider's `rating` field is unreliable (many titles default to a fake
10.0). This matches each movie by title(+year) on OMDb and replaces the rating
with the REAL IMDb rating, also capturing the Rotten Tomatoes score. Movies with
no verifiable IMDb rating get rating=null so the UI hides the badge rather than
showing a fake number. The original provider value is preserved in
`_provider_rating`, and the whole file is backed up first.

Key is read from config/accounts.env (OMDB_API_KEY=...) or argv[2] — never hardcoded.
Usage: omdb_rerate.py [movies.json] [OMDB_KEY]
"""
import json, os, re, sys, time, urllib.request, urllib.parse

def cfg(k, d=None):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "accounts.env")
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                kk, vv = line.split("=", 1)
                if kk.strip() == k:
                    return vv.strip()
    except FileNotFoundError:
        pass
    return os.environ.get(k, d)

SRC = sys.argv[1] if len(sys.argv) > 1 else "/opt/streaming-stack/vod-disk-us/movies.json"
KEY = (sys.argv[2] if len(sys.argv) > 2 else None) or cfg("OMDB_API_KEY")
assert KEY, "OMDB_API_KEY missing (config/accounts.env or argv)"
API = "https://www.omdbapi.com/"
MAX_CALLS = 980           # OMDb free tier = 1000/day; stay under it

calls = 0
quota_hit = [False]
def omdb(params):
    global calls
    calls += 1
    q = urllib.parse.urlencode(dict(params, apikey=KEY))
    for _ in range(3):
        try:
            with urllib.request.urlopen(API + "?" + q, timeout=20) as r:
                d = json.load(r)
            if isinstance(d, dict) and "limit" in str(d.get("Error", "")).lower():
                quota_hit[0] = True
            return d
        except Exception:
            time.sleep(0.6)
    return {"Response": "False", "Error": "net"}

def clean(t):
    t = t or ""
    t = re.sub(r"\(\d{4}\)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def parse(d):
    v = d.get("imdbRating")
    try: v = float(v)
    except Exception: v = None
    rt = None
    for r in (d.get("Ratings") or []):
        if r.get("Source") == "Rotten Tomatoes":
            rt = r.get("Value")
    return {"imdb": v, "rt": rt, "imdb_id": d.get("imdbID"),
            "mtitle": d.get("Title"), "myear": d.get("Year")}

def lookup(title, year):
    title = clean(title); year = str(year or "")
    for p in ({"t": title, "y": year, "type": "movie"}, {"t": title, "type": "movie"}):
        if calls >= MAX_CALLS or quota_hit[0]: return None
        d = omdb(p)
        if d.get("Response") == "True":
            return parse(d)
    # search fallback -> pick the result whose year is closest -> id lookup
    if calls >= MAX_CALLS or quota_hit[0]: return None
    s = omdb({"s": title, "type": "movie"})
    if s.get("Response") == "True" and s.get("Search"):
        def yd(c):
            m = re.search(r"\d{4}", c.get("Year", "") or "")
            try: return abs(int(m.group()) - int(year)) if (m and year.isdigit()) else 999
            except Exception: return 999
        best = sorted(s["Search"], key=yd)[0]
        if calls >= MAX_CALLS or quota_hit[0]: return None
        d = omdb({"i": best.get("imdbID")})
        if d.get("Response") == "True":
            return parse(d)
    return None

movies = json.load(open(SRC))
bak = SRC + ".bak.omdb-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
open(bak, "w").write(json.dumps(movies, ensure_ascii=False))

rated = 0; hidden = []; processed = 0
for m in movies:
    if calls >= MAX_CALLS or quota_hit[0]:
        break                                   # stop cleanly; remaining left untouched
    processed += 1
    if "_provider_rating" not in m:
        m["_provider_rating"] = m.get("rating")
    res = lookup(m.get("title"), m.get("year"))
    if res and res["imdb"] is not None:
        m["rating"] = res["imdb"]; m["rt"] = res["rt"]
        m["imdb_id"] = res["imdb_id"]; m["rating_source"] = "imdb"
        rated += 1
    else:
        m["rating"] = None; m["rt"] = None; m["rating_source"] = "unrated"
        hidden.append("%s (%s)" % (m.get("title"), m.get("year")))
    time.sleep(0.03)

tmp = SRC + ".tmp"; open(tmp, "w").write(json.dumps(movies, ensure_ascii=False)); os.replace(tmp, SRC)
print("DONE: %d/%d processed | real IMDb rating=%d | hidden(no verifiable rating)=%d | api_calls=%d%s"
      % (processed, len(movies), rated, len(hidden), calls, "  [QUOTA/CAP HIT]" if (quota_hit[0] or calls >= MAX_CALLS) else ""))
print("backup:", bak)
print("sample hidden:", "; ".join(hidden[:15]))
