#!/usr/bin/env python3
"""
Consolidate the mixed EN/ES/PT genre labels in a VOD movies.json into clean, canonical
Spanish tags, so the web player's classification chips group correctly (e.g. Comedy +
Comedia + Comédia -> "Comedia"). Reversible (backs up first); only rewrites the `genre`
field. Unmapped labels are kept as-is (title-cased) so nothing is lost.

Usage: vod_normalize_genres.py <movies.json>
"""
import json, os, sys, time

SRC = sys.argv[1] if len(sys.argv) > 1 else "/opt/streaming-stack/vod-disk/movies.json"

# lowercase label -> canonical Spanish tag
MAP = {}
def alias(canon, *variants):
    for v in variants: MAP[v] = canon
    MAP[canon.lower()] = canon

alias("Drama", "drama")
alias("Comedia", "comedy", "comedia", "comédia", "comedie")
alias("Romance", "romance", "romância", "romantico", "romántico")
alias("Acción", "action", "acción", "accion", "ação", "acao")
alias("Animación", "animation", "animación", "animacion", "animação", "animacao")
alias("Familia", "family", "familia", "família", "kids", "niños")
alias("Ciencia ficción", "science fiction", "sci-fi", "scifi", "ciencia ficción",
      "ciencia ficcion", "ficção científica", "ficcao cientifica")
alias("Suspenso", "thriller", "suspense", "suspenso")
alias("Crimen", "crime", "crimen")
alias("Documental", "documentary", "documental", "documentário", "documentario")
alias("Aventura", "adventure", "aventura")
alias("Misterio", "mystery", "misterio", "mistério")
alias("Música", "music", "música", "musica", "musical")
alias("Fantasía", "fantasy", "fantasía", "fantasia")
alias("Bélica", "war", "bélica", "belica", "guerra")
alias("Historia", "history", "historia", "história")
alias("Terror", "horror", "terror")
alias("Western", "western", "oeste")
alias("Deporte", "sport", "sports", "deporte", "deportes", "esporte")
alias("Reality", "reality", "reality-tv", "reality tv")

def norm_one(g):
    g = (g or "").strip()
    if not g: return None
    return MAP.get(g.lower(), g[:1].upper() + g[1:])   # keep unknowns, just title-case

def norm_field(s):
    seen, out = set(), []
    for part in (s or "").split(","):
        c = norm_one(part)
        if c and c not in seen:
            seen.add(c); out.append(c)
    return ", ".join(out)

d = json.load(open(SRC))
open(SRC + ".bak.genres-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()), "w").write(
    json.dumps(d, ensure_ascii=False))

changed = 0
from collections import Counter
before, after = Counter(), Counter()
for m in d:
    old = m.get("genre") or ""
    for g in old.split(","):
        g = g.strip()
        if g: before[g] += 1
    new = norm_field(old)
    if new != old:
        m["genre"] = new; changed += 1
    for g in (new or "").split(","):
        g = g.strip()
        if g: after[g] += 1

tmp = SRC + ".tmp"; open(tmp, "w").write(json.dumps(d, ensure_ascii=False)); os.replace(tmp, SRC)
print("NORMALIZE: %d films rewritten. distinct genres %d -> %d" % (changed, len(before), len(after)))
print("canonical tags now:", dict(after.most_common(20)))
