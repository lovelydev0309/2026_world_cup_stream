#!/usr/bin/env python3
# Sequential VOD ingest -> HLS on the 200GB movie disk. H.264+AAC copy only.
import json, os, re, subprocess, sys, time, urllib.request, unicodedata, html

HOST="tvon247.com"; USER="2E3VBEM"; PW="QA91PXZ"
UA="okhttp/4.9.3"
DISK="/opt/streaming-stack/vod-disk"
CATALOG=DISK+"/_catalog.json"
JSONL=DISK+"/_ingest.jsonl"
LOG=DISK+"/ingest2.log"
CDN="https://stream.tv247on.com/player/vod"
TARGET=int(sys.argv[1]) if len(sys.argv)>1 else 30
MIN_FREE_GB=int(sys.argv[2]) if len(sys.argv)>2 else 20   # stop if disk free drops under this

def log(m):
    line=f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line,flush=True)
    try: open(LOG,"a").write(line+"\n")
    except: pass

def api(action=None, **kw):
    q=f"http://{HOST}/player_api.php?username={USER}&password={PW}"
    if action: q+=f"&action={action}"
    for k,v in kw.items(): q+=f"&{k}={v}"
    req=urllib.request.Request(q, headers={"User-Agent":UA})
    return json.load(urllib.request.urlopen(req, timeout=40))

def lang_of(name):
    m=re.match(r"\s*([A-Za-z]{2,4})\s*-\s*", name or "")
    return m.group(1).upper() if m else ""

def clean_title(name):
    t=name or ""
    t=re.sub(r"^\s*[A-Za-z]{2,4}\s*-\s*","",t)      # drop leading "ES - " language tag
    t=re.sub(r"\[[^\]]*\]","",t)                       # drop [MULTI-SUB] / [4K] tags
    t=re.sub(r"\s*-\s*(19|20)\d{2}\s*$","",t)         # drop trailing " - 2026"
    t=re.sub(r"\s{2,}"," ",t).strip(" -")
    return t or (name or "").strip()

def year_from_name(name):
    m=re.search(r"(19|20)\d{2}", name or "")
    return m.group(0) if m else ""

def slugify(name):
    n=unicodedata.normalize("NFKD",name).encode("ascii","ignore").decode()
    n=re.sub(r'[^a-zA-Z0-9]+','-',n).strip('-').lower()
    return n[:55] or "movie"

def norm_title(name):
    n=unicodedata.normalize("NFKD",name or "").encode("ascii","ignore").decode()
    return re.sub(r'[^a-z0-9]','',n.lower())

def free_gb():
    st=os.statvfs(DISK); return st.f_bavail*st.f_frsize/1e9

def fmt_hms(sec):
    sec=int(sec); return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"

def fmt_hm(sec):
    sec=int(sec); h=sec//3600; m=(sec%3600)//60
    return (f"{h}h {m}m" if h else f"{m}m")

CSS=r"""
:root{--bg:#0B0A0D;--bg2:#08070A;--band:#100E14;--raise:#151119;--surface:#17131C;--surface2:#1E1926;--border:#2A2531;--border2:#3A3342;--text:#F3EFE9;--muted:#9C93A3;--faint:#6B6270;--accent:#C9A45C;--accent2:#E6CE93;--good:#49c98a;--field:rgba(255,255,255,.03);--field2:rgba(255,255,255,.05);--topbar:linear-gradient(180deg,rgba(11,10,13,.97),rgba(11,10,13,.80));--shadow:0 20px 44px rgba(0,0,0,.55);--optbg:#17131C;--optfg:#F3EFE9;--serif:"Playfair Display",Didot,"Bodoni MT",Georgia,"Times New Roman",serif;--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color-scheme:dark}
:root[data-theme="light"]{--bg:#F6F1E8;--bg2:#EFE8DB;--band:#FBF8F2;--raise:#EDE6D8;--surface:#FFFFFF;--surface2:#F5F0E6;--border:#E5DCCB;--border2:#D8CDB8;--text:#211C15;--muted:#6E6353;--faint:#9A8E7B;--accent:#98742A;--accent2:#7C5D1E;--field:#FFFFFF;--field2:#FBF9F4;--topbar:linear-gradient(180deg,rgba(251,248,242,.97),rgba(251,248,242,.82));--shadow:0 18px 40px rgba(120,95,35,.16);--optbg:#FFFFFF;--optfg:#211C15;color-scheme:light}
*{box-sizing:border-box}
html,body{margin:0;color:var(--text);font-family:var(--sans);-webkit-font-smoothing:antialiased;line-height:1.5}
html{scrollbar-width:thin;scrollbar-color:var(--accent) var(--band)}
::-webkit-scrollbar{width:13px;height:13px}
::-webkit-scrollbar-track{background:var(--band)}
::-webkit-scrollbar-thumb{background:var(--accent);border-radius:10px;border:3px solid var(--band)}
::-webkit-scrollbar-thumb:hover{background:var(--accent2);border-width:2px}
::-webkit-scrollbar-corner{background:var(--band)}
body{min-height:100vh;background:radial-gradient(115% 85% at 100% 100%,rgba(201,164,92,.10),rgba(201,164,92,0) 52%),radial-gradient(90% 60% at 0% 0%,rgba(150,140,170,.05),transparent 48%),var(--bg);background-attachment:fixed}
img{display:block;max-width:100%}a{color:inherit;text-decoration:none}
.wrap{max-width:1200px;margin:0 auto;padding:0 26px;position:relative;z-index:1}
:focus-visible{outline:1px solid var(--accent);outline-offset:3px;border-radius:3px}
/* bottom-right luxury deco */
.lux-deco{position:fixed;right:0;bottom:0;width:min(48vw,540px);height:min(48vw,540px);z-index:0;pointer-events:none}
.lux-deco svg{width:100%;height:100%;display:block}
.topbar{position:sticky;top:0;z-index:10;background:var(--topbar);backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
.topbar:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,rgba(201,164,92,.55),transparent)}
.topbar .wrap{display:flex;align-items:center;gap:22px;height:74px}
.mark{font-family:var(--serif);font-style:italic;font-weight:700;letter-spacing:.005em;font-size:27px;white-space:nowrap;line-height:1}.mark b{color:var(--accent);font-style:normal}
.seg{font-size:10.5px;letter-spacing:.34em;text-transform:uppercase;color:var(--muted);border-left:1px solid var(--border2);padding-left:22px;font-weight:600}.seg b{color:var(--accent2);font-weight:600}
.search{margin-left:auto;position:relative}
.search input{background:var(--field);border:1px solid var(--border2);color:var(--text);border-radius:0;padding:10px 16px 10px 40px;font-size:13px;width:220px;transition:width .25s,border-color .25s,background .25s;font-family:var(--sans);letter-spacing:.02em}
.search input:focus{width:280px;border-color:var(--accent);outline:none;background:var(--field2)}
.search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);opacity:.5}
.search input::placeholder{color:var(--faint);letter-spacing:.05em}
.themebtn{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex:none;background:var(--field);border:1px solid var(--border2);color:var(--muted);border-radius:2px;cursor:pointer;transition:color .2s,border-color .2s,background .2s}
.themebtn:hover{color:var(--accent2);border-color:var(--accent)}
.themebtn svg{width:17px;height:17px}
.themebtn .ic-moon{display:none}
:root[data-theme="light"] .themebtn .ic-sun{display:none}
:root[data-theme="light"] .themebtn .ic-moon{display:block}
:root[data-theme="light"] .lux-deco{opacity:.7}
.controls{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin:44px 0 6px}
.controls .htext .kicker{display:block;font-size:10.5px;letter-spacing:.36em;text-transform:uppercase;color:var(--accent);margin:0 0 11px;font-weight:600}
.controls h2{font-family:var(--serif);font-size:clamp(28px,4vw,38px);margin:0;font-weight:600;letter-spacing:.004em;line-height:.98}
.controls .count{color:var(--faint);font-size:12px;font-variant-numeric:tabular-nums;letter-spacing:.05em;text-transform:uppercase;padding-bottom:6px}
.sortwrap{margin-left:auto;display:flex;align-items:center;gap:11px;padding-bottom:4px}
.sortwrap label{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.18em}
select{background-color:var(--field);border:1px solid var(--border2);color:var(--text);border-radius:0;padding:9px 34px 9px 14px;font-size:13px;font-weight:500;letter-spacing:.03em;cursor:pointer;appearance:none;font-family:var(--sans);background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23C9A45C' stroke-width='2.4'><path d='M6 9l6 6 6-6'/></svg>");background-repeat:no-repeat;background-position:right 12px center}
select:focus{border-color:var(--accent);outline:none}
select option{background-color:var(--optbg);color:var(--optfg)}
select option:checked{background-color:var(--accent);color:var(--optbg)}
.rule{height:1px;background:linear-gradient(90deg,rgba(201,164,92,.35),rgba(201,164,92,.06) 40%,transparent);margin:16px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(166px,1fr));gap:28px 20px;margin:30px 0 8px}
.card{display:block;background:var(--surface);border:1px solid var(--border);border-radius:2px;overflow:hidden;transition:transform .3s cubic-bezier(.2,.7,.2,1),border-color .3s,box-shadow .3s;position:relative}
.card:hover{transform:translateY(-6px);border-color:rgba(201,164,92,.5);box-shadow:var(--shadow)}
.poster{position:relative;aspect-ratio:2/3;background:var(--raise);overflow:hidden}
.poster:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 50%,rgba(8,7,10,.55));opacity:0;transition:opacity .3s}
.card:hover .poster:after{opacity:1}
.poster img{width:100%;height:100%;object-fit:cover;transition:transform .55s ease}
.card:hover .poster img{transform:scale(1.05)}
.poster .rt{position:absolute;top:9px;right:9px;background:rgba(8,7,10,.72);color:#E6CE93;font-weight:600;font-size:11.5px;padding:3px 9px;border:1px solid rgba(201,164,92,.32);border-radius:999px;backdrop-filter:blur(4px);font-variant-numeric:tabular-nums;letter-spacing:.02em}
.cbody{padding:13px 14px 16px}
.ctitle{font-family:var(--serif);font-size:15.5px;font-weight:600;letter-spacing:.005em;line-height:1.26;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:40px}
.cmeta{margin-top:8px;color:var(--faint);font-size:10.5px;display:flex;gap:8px;flex-wrap:wrap;font-variant-numeric:tabular-nums;letter-spacing:.06em;text-transform:uppercase}
.cmeta .dot{width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:.65;align-self:center}
/* grid flip cards (hover to reveal metadata on the back) */
.card.flip{background:transparent;border:0;border-radius:2px;overflow:visible;perspective:1300px;transition:box-shadow .3s}
.card.flip:hover{transform:none;box-shadow:var(--shadow)}
.flip-inner{position:relative;width:100%;transform-style:preserve-3d;transition:transform .65s cubic-bezier(.2,.75,.25,1)}
.card.flip:hover .flip-inner{transform:rotateY(180deg)}
.flip-front,.flip-back{backface-visibility:hidden;-webkit-backface-visibility:hidden;border:1px solid var(--border);border-radius:2px;overflow:hidden;background:var(--surface);transition:border-color .3s}
.card.flip:hover .flip-front,.card.flip:hover .flip-back{border-color:rgba(201,164,92,.5)}
.flip-back{position:absolute;inset:0;transform:rotateY(180deg);display:flex;flex-direction:column;padding:16px 15px;background:linear-gradient(165deg,var(--surface2),var(--surface))}
.card.flip .poster:after{display:none}
.card.flip:hover .poster img{transform:none}
.fb-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.fb-title{font-family:var(--serif);font-size:16px;font-weight:600;line-height:1.2;color:var(--text)}
.fb-rating{color:var(--accent);font-weight:700;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
.fb-meta{margin-top:10px;color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.07em;display:flex;flex-wrap:wrap;gap:7px}
.fb-meta .dot{width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:.65;align-self:center}
.fb-plot{margin-top:12px;color:var(--muted);font-size:12px;line-height:1.5;flex:1 1 auto;overflow:hidden;display:-webkit-box;-webkit-line-clamp:8;-webkit-box-orient:vertical}
.fb-cta{margin-top:10px;color:var(--accent2);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
.empty{text-align:center;color:var(--muted);padding:70px 0;font-size:14px;font-family:var(--serif);font-style:italic}
.pager{grid-column:2;display:flex;justify-content:center;align-items:center;gap:6px;flex-wrap:wrap}
.pager button{background:var(--field);border:1px solid var(--border2);color:var(--text);border-radius:2px;min-width:40px;height:40px;padding:0 13px;font-size:13px;font-weight:500;cursor:pointer;font-variant-numeric:tabular-nums;letter-spacing:.02em;transition:border-color .2s,color .2s,background .2s}
.pager button:hover:not(:disabled){border-color:var(--accent);color:var(--accent2)}
.pager button.active{background:var(--accent);color:#0B0A0D;border-color:var(--accent);font-weight:700}
.pager button:disabled{opacity:.35;cursor:default}
.pager .ell{color:var(--faint);padding:0 2px}
.viewtoggle{display:flex;gap:2px;background:var(--field);border:1px solid var(--border2);border-radius:2px;padding:3px}
.viewtoggle button{background:transparent;border:0;color:var(--muted);width:34px;height:32px;border-radius:1px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:color .2s,background .2s}
.viewtoggle button:hover{color:var(--text)}
.viewtoggle button.active{background:var(--accent);color:#0B0A0D}
.list{display:flex;flex-direction:column;gap:12px;margin:30px 0 8px}
.list .card{display:flex;flex-direction:row;align-items:stretch}
.list .poster{width:66px;min-width:66px;aspect-ratio:2/3}
.list .poster .rt{display:none}
.list .cbody{flex:1;min-width:0;padding:13px 17px;display:flex;flex-direction:column;justify-content:center}
.list .lhead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.list .ctitle{font-size:17px;-webkit-line-clamp:1;min-height:0}
.list .lrating{color:var(--accent);font-weight:600;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
.list .lplot{color:var(--faint);font-size:12.5px;line-height:1.55;margin-top:7px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pagerbar{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;margin:30px 0 60px}
.pagesize{grid-column:1;justify-self:end;display:flex;align-items:center;gap:9px}
.pagesize label{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.16em}
.pagesize select{height:40px;padding:0 32px 0 14px;font-size:13px}
@media(max-width:560px){.pagerbar{grid-template-columns:1fr;justify-items:center;gap:16px}.pagesize{grid-column:1;justify-self:center}.pager{grid-column:1}}
/* left hover-reveal category drawer */
.catwrap{position:fixed;left:0;top:74px;bottom:0;z-index:9}
.cattab{display:flex;flex-direction:column;align-items:center;gap:14px;width:44px;padding:18px 0;margin-top:16px;background:linear-gradient(180deg,var(--surface),var(--band));border:1px solid var(--border);border-left:0;border-radius:0 10px 10px 0;color:var(--muted);cursor:pointer;transition:color .2s,opacity .25s;box-shadow:3px 5px 22px rgba(0,0,0,.22)}
.cattab:hover{color:var(--accent2)}
.cattab .lbl{writing-mode:vertical-rl;text-orientation:mixed;transform:rotate(180deg);font-size:10.5px;letter-spacing:.26em;text-transform:uppercase;font-weight:600}
.cattab svg{width:18px;height:18px}
.catpanel{position:absolute;left:0;top:0;bottom:0;width:264px;transform:translateX(-101%);transition:transform .4s cubic-bezier(.2,.7,.2,1);background:linear-gradient(175deg,var(--band),var(--bg));border-right:1px solid var(--border2);box-shadow:10px 0 44px rgba(0,0,0,.4);overflow-y:auto;padding:24px 0 34px}
.catwrap:hover .catpanel,.catwrap.open .catpanel{transform:translateX(0)}
.catwrap:hover .cattab,.catwrap.open .cattab{opacity:0;pointer-events:none}
.catpanel h3{font-family:var(--serif);font-size:20px;margin:0;padding:0 24px;font-weight:600}
.catpanel .csub{font-size:10px;letter-spacing:.28em;text-transform:uppercase;color:var(--accent);padding:0 24px;margin:5px 0 16px;font-weight:600}
.catitem{display:flex;justify-content:space-between;align-items:center;gap:10px;width:100%;background:transparent;border:0;border-left:2px solid transparent;color:var(--muted);text-align:left;padding:9px 24px;font-size:13.5px;cursor:pointer;font-family:var(--sans);transition:color .15s,background .15s}
.catitem:hover{color:var(--text);background:rgba(201,164,92,.06)}
.catitem.active{color:var(--accent2);border-left-color:var(--accent);background:rgba(201,164,92,.09)}
.catitem .n{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.catitem.active .n{color:var(--accent)}
@media(max-width:700px){.catwrap{top:64px}}
"""

def build_list_page():
    # data-driven catalog page: single sort dropdown + pagination
    return """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){try{var t=localStorage.getItem('vodTheme');if(t!=='light'&&t!=='dark')t=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();</script>
<title>Catálogo VOD — tv247on</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,600;1,700&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head><body>
<div class="lux-deco" aria-hidden="true"><svg viewBox="0 0 540 540" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMaxYMax meet">
<defs>
<radialGradient id="lxg" cx="100%" cy="100%" r="100%"><stop offset="0%" stop-color="#C9A45C" stop-opacity=".45"/><stop offset="46%" stop-color="#C9A45C" stop-opacity=".18"/><stop offset="100%" stop-color="#C9A45C" stop-opacity="0"/></radialGradient>
<radialGradient id="lxglow" cx="100%" cy="100%" r="90%"><stop offset="0%" stop-color="#C9A45C" stop-opacity=".13"/><stop offset="100%" stop-color="#C9A45C" stop-opacity="0"/></radialGradient>
</defs>
<rect x="0" y="0" width="540" height="540" fill="url(#lxglow)"/>
<g stroke="url(#lxg)" stroke-width="1" fill="none">
<circle cx="540" cy="540" r="150"/><circle cx="540" cy="540" r="232"/><circle cx="540" cy="540" r="322"/><circle cx="540" cy="540" r="420"/><circle cx="540" cy="540" r="524"/>
</g>
<g stroke="url(#lxg)" stroke-width="1" fill="none" stroke-linecap="round">
<line x1="540" y1="540" x2="16" y2="540"/><line x1="540" y1="540" x2="38" y2="405"/><line x1="540" y1="540" x2="90" y2="280"/><line x1="540" y1="540" x2="172" y2="172"/><line x1="540" y1="540" x2="280" y2="90"/><line x1="540" y1="540" x2="405" y2="38"/><line x1="540" y1="540" x2="540" y2="16"/>
</g>
</svg></div>
<header class="topbar"><div class="wrap">
  <span class="mark">tv247<b>on</b></span>
  <span class="seg">Catálogo <b>VOD</b></span>
  <div class="search">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    <input id="q" type="search" placeholder="Buscar películas…" autocomplete="off">
  </div>
  <button id="themeBtn" class="themebtn" type="button" title="Cambiar tema" aria-label="Cambiar tema">
    <svg class="ic-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
    <svg class="ic-moon" viewBox="0 0 24 24" fill="currentColor"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
  </button>
</div></header>

<aside class="catwrap" id="catwrap">
  <div class="cattab" id="cattab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M7 12h10M10 18h4"/></svg>
    <span class="lbl">Categorías</span>
  </div>
  <nav class="catpanel" id="catpanel">
    <h3>Categorías</h3><div class="csub">Explorar por género</div>
    <div id="catlist"></div>
  </nav>
</aside>

<main class="wrap">
  <div class="controls">
    <div class="htext"><span class="kicker">Colección · tv247on</span><h2>Cine &amp; Estrenos</h2></div>
    <span class="count" id="count"></span>
    <div class="sortwrap">
      <div class="viewtoggle" id="viewtoggle">
        <button type="button" data-view="grid" title="Vista cuadrícula" aria-label="Vista cuadrícula"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></button>
        <button type="button" data-view="list" title="Vista lista" aria-label="Vista lista"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="4.5" width="18" height="3" rx="1.5"/><rect x="3" y="10.5" width="18" height="3" rx="1.5"/><rect x="3" y="16.5" width="18" height="3" rx="1.5"/></svg></button>
      </div>
      <label for="sort">Ordenar</label>
      <select id="sort">
        <option value="recent">Recientes</option>
        <option value="rating">Mejor valoradas</option>
        <option value="az">A - Z</option>
        <option value="year">Año</option>
      </select>
    </div>
  </div>
  <div class="rule"></div>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">No se encontraron películas.</div>
  <div class="pagerbar">
    <div class="pagesize">
      <label for="pageSize">Por página</label>
      <select id="pageSize">
        <option value="12">12</option>
        <option value="24" selected>24</option>
        <option value="48">48</option>
        <option value="96">96</option>
      </select>
    </div>
    <div class="pager" id="pager"></div>
  </div>
</main>

<script>
const PAGE_SIZES=['12','24','48','96'];
let ALL=[], view=[], page=1, viewMode='grid', PER_PAGE=24, selectedCat='';
const grid=document.getElementById('grid'), pager=document.getElementById('pager'),
      countEl=document.getElementById('count'), emptyEl=document.getElementById('empty'),
      qEl=document.getElementById('q'), sortEl=document.getElementById('sort'),
      pageSizeEl=document.getElementById('pageSize'), viewtoggle=document.getElementById('viewtoggle');

function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// restore saved preferences
try{
  const vm=localStorage.getItem('vodView'); if(vm==='list'||vm==='grid') viewMode=vm;
  const ps=localStorage.getItem('vodPageSize'); if(ps && PAGE_SIZES.includes(ps)){ pageSizeEl.value=ps; PER_PAGE=parseInt(ps); }
}catch(e){}

function posterImg(m){
  return `<img loading="lazy" src="${esc(m.logo_url)}" alt="${esc(m.title)}"
     onerror="this.style.display='none';this.parentNode.style.background='linear-gradient(135deg,#141a25,#1d2634)'">`;
}
function fmtDur(d){ const m=/^(\d+):(\d+):(\d+)/.exec(d||''); if(!m) return esc(d||''); const h=+m[1],mm=+m[2]; return h?`${h}h ${mm}m`:`${mm}m`; }
function metaBits(m){
  const b=[]; if(m.year) b.push(esc(m.year));
  if(m.genre) b.push(esc(String(m.genre).split(',')[0].trim()));
  if(m.duration && /^\d/.test(m.duration)) b.push(fmtDur(m.duration));
  return b;
}
function joinMeta(bits){ return bits.map((t,i)=>(i?'<span class="dot"></span>':'')+`<span>${t}</span>`).join(''); }
function cardGrid(m){
  const rt=(m.rating&&m.rating>0)?`<span class="rt">★ ${Number(m.rating).toFixed(1)}</span>`:'';
  const rb=(m.rating&&m.rating>0)?`<span class="fb-rating">★ ${Number(m.rating).toFixed(1)}</span>`:'';
  const plot=`<div class="fb-plot">${m.plot?esc(m.plot):''}</div>`;
  return `<a class="card flip" href="${esc(m.player_url)}"><div class="flip-inner">`
    +`<div class="flip-front"><div class="poster">${posterImg(m)}${rt}</div>`
    +`<div class="cbody"><div class="ctitle">${esc(m.title)}</div><div class="cmeta">${joinMeta(metaBits(m).slice(0,2))}</div></div></div>`
    +`<div class="flip-back"><div class="fb-top"><div class="fb-title">${esc(m.title)}</div>${rb}</div>`
    +`<div class="fb-meta">${joinMeta(metaBits(m))}</div>${plot}`
    +`<div class="fb-cta">Ver película →</div></div>`
    +`</div></a>`;
}
function cardList(m){
  const rt=(m.rating&&m.rating>0)?`<span class="lrating">★ ${Number(m.rating).toFixed(1)}</span>`:'';
  const plot=m.plot?`<div class="lplot">${esc(m.plot)}</div>`:'';
  return `<a class="card" href="${esc(m.player_url)}"><div class="poster">${posterImg(m)}</div>
    <div class="cbody"><div class="lhead"><div class="ctitle">${esc(m.title)}</div>${rt}</div>
    <div class="cmeta">${joinMeta(metaBits(m))}</div>${plot}</div></a>`;
}
function applySort(list){
  const s=sortEl.value, a=list.slice();
  if(s==='az') a.sort((x,y)=>x.title.localeCompare(y.title,'es'));
  else if(s==='rating') a.sort((x,y)=>(y.rating||0)-(x.rating||0));
  else if(s==='year') a.sort((x,y)=>(parseInt(y.year)||0)-(parseInt(x.year)||0));
  else a.sort((x,y)=>(y._added||0)-(x._added||0)); // recent
  return a;
}
function refresh(){
  const q=qEl.value.trim().toLowerCase();
  let list=ALL.filter(m=>{
    if(selectedCat && !String(m.genre||'').toLowerCase().split(',').map(s=>s.trim()).includes(selectedCat)) return false;
    return !q || m.title.toLowerCase().includes(q) || (m.genre||'').toLowerCase().includes(q);
  });
  view=applySort(list);
  page=Math.min(page, Math.max(1,Math.ceil(view.length/PER_PAGE)));
  render();
}
function render(){
  const total=view.length, pages=Math.max(1,Math.ceil(total/PER_PAGE));
  if(page>pages) page=pages;
  const start=(page-1)*PER_PAGE, slice=view.slice(start,start+PER_PAGE);
  grid.className = viewMode==='list' ? 'list' : 'grid';
  grid.innerHTML = slice.map(viewMode==='list'?cardList:cardGrid).join('');
  emptyEl.style.display=total?'none':'block';
  countEl.textContent=total?`${total} título${total>1?'s':''}`:'';
  // pager
  pager.innerHTML='';
  if(pages<=1) return;
  const btn=(label,pg,{active=false,dis=false,ell=false}={})=>{
    if(ell){const s=document.createElement('span');s.className='ell';s.textContent='…';pager.appendChild(s);return;}
    const b=document.createElement('button');b.textContent=label;if(active)b.className='active';
    b.disabled=dis;if(!dis&&!active)b.onclick=()=>{page=pg;window.scrollTo({top:0,behavior:'smooth'});render();};
    pager.appendChild(b);};
  btn('‹',page-1,{dis:page===1});
  const nums=new Set([1,pages,page,page-1,page+1].filter(n=>n>=1&&n<=pages));
  let prev=0;
  [...nums].sort((a,b)=>a-b).forEach(n=>{ if(n-prev>1)btn('',0,{ell:true}); btn(String(n),n,{active:n===page}); prev=n; });
  btn('›',page+1,{dis:page===pages});
}
// view mode toggle
[...viewtoggle.querySelectorAll('button')].forEach(b=>{
  if(b.dataset.view===viewMode) b.classList.add('active');
  b.onclick=()=>{ viewMode=b.dataset.view;
    [...viewtoggle.querySelectorAll('button')].forEach(x=>x.classList.toggle('active',x===b));
    try{localStorage.setItem('vodView',viewMode);}catch(e){}
    render(); };
});
// page-size dropdown
pageSizeEl.addEventListener('change',()=>{ const v=pageSizeEl.value;
  PER_PAGE = parseInt(v)||24; page=1;
  try{localStorage.setItem('vodPageSize',v);}catch(e){}
  refresh(); });
qEl.addEventListener('input',()=>{page=1;refresh();});
sortEl.addEventListener('change',()=>{page=1;refresh();});
document.getElementById('themeBtn').addEventListener('click',()=>{var n=document.documentElement.getAttribute('data-theme')==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',n);try{localStorage.setItem('vodTheme',n);}catch(e){}});
// category drawer (derived from genres)
const catwrap=document.getElementById('catwrap'), cattab=document.getElementById('cattab'), catlist=document.getElementById('catlist');
function buildCats(){
  const map={};
  ALL.forEach(m=>String(m.genre||'').split(',').map(s=>s.trim()).filter(Boolean).forEach(g=>{
    const k=g.toLowerCase(); (map[k]=map[k]||{name:g,count:0}).count++;
  }));
  const cats=Object.keys(map).map(k=>({key:k,name:map[k].name,count:map[k].count})).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name,'es'));
  const item=(key,name,n,act)=>`<button class="catitem${act?' active':''}" data-cat="${esc(key)}"><span>${esc(name)}</span><span class="n">${n}</span></button>`;
  catlist.innerHTML=item('','Todas',ALL.length,selectedCat==='')+cats.map(c=>item(c.key,c.name,c.count,selectedCat===c.key)).join('');
  [...catlist.querySelectorAll('.catitem')].forEach(b=>b.onclick=()=>{
    selectedCat=b.dataset.cat;
    [...catlist.querySelectorAll('.catitem')].forEach(x=>x.classList.toggle('active',x===b));
    catwrap.classList.remove('open'); page=1; refresh();
  });
}
if(cattab) cattab.addEventListener('click',()=>catwrap.classList.toggle('open'));

fetch('movies.json?_='+Date.now()).then(r=>r.json()).then(d=>{
  ALL=(Array.isArray(d)?d:(d.movies||[])).map((m,i)=>({...m,_added:(m.added?parseInt(m.added):(1e9-i))}));
  buildCats();
  refresh();
}).catch(e=>{emptyEl.textContent='No se pudo cargar el catálogo.';emptyEl.style.display='block';});
</script>
</body></html>""".replace("__CSS__",CSS)

MOVIE_TPL=r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%% — tv247on VOD</title>
<script>(function(){try{var t=localStorage.getItem('vodTheme');if(t!=='light'&&t!=='dark')t=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();</script>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<style>
:root{--bg:#080A0F;--band:#0b0f17;--raise:#10151f;--surface:#141a25;--border:#232c3a;--border2:#313d4f;--text:#EEF1F6;--muted:#8f99ab;--faint:#616b7d;--accent:#F5C451;--good:#49c98a;color-scheme:dark}
:root[data-theme="light"]{--bg:#F6F1E8;--band:#FBF8F2;--raise:#EDE6D8;--surface:#FFFFFF;--border:#E5DCCB;--border2:#D8CDB8;--text:#211C15;--muted:#6E6353;--faint:#9A8E7B;--accent:#98742A;color-scheme:light}
:root[data-theme="light"] .topbar{background:linear-gradient(180deg,#FBF8F2,#F6F1E8)}
:root[data-theme="light"] .plot{color:#3A352C}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}
html{scrollbar-width:thin;scrollbar-color:var(--accent) var(--band)}
::-webkit-scrollbar{width:13px;height:13px}
::-webkit-scrollbar-track{background:var(--band)}
::-webkit-scrollbar-thumb{background:var(--accent);border-radius:10px;border:3px solid var(--band)}
::-webkit-scrollbar-thumb:hover{border-width:2px}
::-webkit-scrollbar-corner{background:var(--band)}
img{display:block;max-width:100%}a{color:inherit;text-decoration:none}.wrap{max-width:1200px;margin:0 auto;padding:0 22px}
.topbar{position:sticky;top:0;z-index:10;background:linear-gradient(180deg,#0b1019,#080A0F);border-bottom:1px solid var(--border)}
.topbar .wrap{display:flex;align-items:center;gap:20px;height:60px}
.mark{font-weight:800;letter-spacing:-.03em;font-size:20px;white-space:nowrap}.mark b{color:var(--accent)}
.seg{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);font-weight:600}
.back{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:13px}.back:hover{color:var(--text)}
.player{background:#000;border-radius:0 0 14px 14px;overflow:hidden}.stage{position:relative;width:100%;aspect-ratio:16/9;background:#000}
video{width:100%;height:100%;display:block;background:#000}
.loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px;pointer-events:none;transition:opacity .3s}.loading.hide{opacity:0}
.meta{padding:22px 0 8px;max-width:900px;margin:0 auto}.titlerow{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
h1{font-size:clamp(21px,3.2vw,29px);margin:0;letter-spacing:-.02em;font-weight:800;text-wrap:balance}
.rating{color:var(--accent);font-weight:700;font-size:15px;white-space:nowrap;font-variant-numeric:tabular-nums;background:rgba(245,196,81,.10);border:1px solid rgba(245,196,81,.22);padding:4px 11px;border-radius:999px}
.metarow{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin:12px 0 0;color:var(--muted);font-size:13.5px;font-variant-numeric:tabular-nums}
.metarow .sep{width:3px;height:3px;border-radius:50%;background:var(--faint)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0 0}.chip{font-size:11.5px;color:var(--muted);border:1px solid var(--border2);padding:3px 10px;border-radius:999px}.chip.lang{color:var(--text);background:var(--band)}
.plot{font-size:15px;color:#c9d0dc;line-height:1.62;margin:18px 0 0;max-width:70ch}
.credits{margin-top:16px;display:grid;gap:5px}.credit{font-size:12.5px;color:var(--faint)}.credit .k{display:inline-block;min-width:74px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:10.5px}
.foot{border-top:1px solid var(--border);margin-top:30px;background:var(--band)}.foot .wrap{padding:18px 22px 40px;max-width:900px}.foot p{margin:0;font-size:12px;color:var(--faint);line-height:1.6}.foot .vg{color:var(--good)}
.pwrap{max-width:900px;margin:0 auto;padding:0}
.playbtn{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;width:84px;height:84px;border-radius:50%;border:1px solid rgba(245,196,81,.55);background:rgba(8,10,15,.5);backdrop-filter:blur(6px);color:var(--accent);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:opacity .25s,transform .2s,background .2s,box-shadow .2s}
.playbtn svg{width:34px;height:34px;fill:currentColor}.playbtn .i-play{margin-left:5px}
.playbtn:hover{background:rgba(245,196,81,.16);box-shadow:0 8px 30px rgba(0,0,0,.5);transform:translate(-50%,-50%) scale(1.06)}
.playbtn.hide{opacity:0;pointer-events:none;transform:translate(-50%,-50%) scale(.82)}
</style></head><body>
<header class="topbar"><div class="wrap">
  <span class="mark">tv247<b>on</b></span>
  <a class="back" href="../">← Volver al catálogo</a>
  <span class="seg" style="margin-left:auto">VOD</span>
</div></header>
<div class="player"><div class="pwrap"><div class="stage">
  <video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>
  <button class="playbtn" id="playbtn" type="button" aria-label="Reproducir"><svg class="i-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg></button>
  <div class="loading hide" id="load">Cargando…</div>
</div></div></div>
<main class="wrap"><section class="meta">
  <div class="titlerow"><h1>%%TITLE%%</h1>%%RATING%%</div>
  <div class="metarow">%%METAROW%%</div>
  <div class="chips">%%CHIPS%%</div>
  %%PLOT%%
  <div class="credits">%%CREDITS%%</div>
</section></main>
<footer class="foot"><div class="wrap">
  <p><span class="vg">Reproduciéndose desde stream.tv247on.com</span> — alojado en tu CDN como HLS (H.264/AAC). Nada pasa por el proveedor durante la reproducción.</p>
</div></footer>
<script>
(function(){var video=document.getElementById('v'),load=document.getElementById('load'),SRC='index.m3u8';
function hide(){load.classList.add('hide');}
video.addEventListener('playing',hide);video.addEventListener('loadeddata',hide);
if(window.Hls&&Hls.isSupported()){var hls=new Hls({maxBufferLength:30,backBufferLength:30});
hls.loadSource(SRC);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,hide);
hls.on(Hls.Events.ERROR,function(e,d){if(d.fatal){load.classList.remove('hide');load.textContent='Error de reproducción — reintentando…';
if(d.type===Hls.ErrorTypes.NETWORK_ERROR)hls.startLoad();else if(d.type===Hls.ErrorTypes.MEDIA_ERROR)hls.recoverMediaError();}});
}else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=SRC;video.addEventListener('loadedmetadata',hide);}
else{load.textContent='HLS no soportado en este navegador.';}
var playbtn=document.getElementById('playbtn');function syncBtn(){playbtn.classList.toggle('hide',!video.paused);}if(playbtn){playbtn.addEventListener('click',function(){video.paused?video.play():video.pause();});video.addEventListener('play',syncBtn);video.addEventListener('pause',syncBtn);video.addEventListener('ended',syncBtn);video.addEventListener('waiting',function(){if(!video.paused)load.classList.remove('hide');});syncBtn();}})();
</script></body></html>"""

def write_movie_html(outdir, rec, w, h, vcodec):
    e=html.escape
    rating_html = f'<span class="rating">★ {rec["rating"]:.1f}</span>' if rec.get("rating") else ''
    parts=[]
    if rec.get("year"): parts.append(f'<span>{e(str(rec["year"]))}</span>')
    if rec.get("duration"):
        try:
            hh,mm,ss=[int(x) for x in rec["duration"].split(":")]; parts.append(f'<span>{fmt_hm(hh*3600+mm*60+ss)}</span>')
        except: pass
    if rec.get("country"): parts.append(f'<span>{e(rec["country"])}</span>')
    parts.append(f'<span>{w}×{h} · H.264</span>')
    metarow="<span class=\"sep\"></span>".join(parts)
    chips=['<span class="chip lang">Español</span>']
    for g in [x.strip() for x in (rec.get("genre") or "").split(",") if x.strip()][:3]:
        chips.append(f'<span class="chip">{e(g)}</span>')
    chips_html="".join(chips)
    plot_html=f'<p class="plot">{e(rec["plot"])}</p>' if rec.get("plot") else ''
    creds=[]
    if rec.get("director"): creds.append(f'<div class="credit"><span class="k">Director</span>{e(rec["director"])}</div>')
    if rec.get("cast"): creds.append(f'<div class="credit"><span class="k">Reparto</span>{e(rec["cast"])}</div>')
    creds_html="".join(creds)
    out=(MOVIE_TPL.replace("%%TITLE%%",e(rec["title"])).replace("%%RATING%%",rating_html)
         .replace("%%METAROW%%",metarow).replace("%%CHIPS%%",chips_html)
         .replace("%%PLOT%%",plot_html).replace("%%CREDITS%%",creds_html))
    open(os.path.join(outdir,"index.html"),"w").write(out)

def probe(url):
    cmd=["ffprobe","-v","error","-user_agent",UA,
         "-show_entries","stream=codec_type,codec_name,width,height:format=duration",
         "-of","json","-analyzeduration","6M","-probesize","6M",url]
    try:
        r=subprocess.run(cmd,capture_output=True,timeout=45,text=True)
        j=json.loads(r.stdout or "{}")
    except Exception:
        return None
    v=a=None; w=h=0
    for s in j.get("streams",[]):
        if s.get("codec_type")=="video" and not v:
            v=s.get("codec_name"); w=s.get("width",0) or 0; h=s.get("height",0) or 0
        if s.get("codec_type")=="audio" and not a:
            a=s.get("codec_name")
    try: dur=float(j.get("format",{}).get("duration",0) or 0)
    except: dur=0
    return (v,a,w,h,dur)

def main():
    log(f"=== ingest start, target={TARGET}, free={free_gb():.0f}GB ===")
    # deploy redesigned catalog page (single sort + pagination) immediately, and sync movies.json
    open(DISK+"/index.html","w").write(build_list_page())
    regen_movies_json()
    log("redesigned catalog list page deployed (single sort dropdown + pagination)")
    if not os.path.exists(CATALOG):
        log("fetching vod catalog...")
        json.dump(api("get_vod_streams"), open(CATALOG,"w"))
    data=json.load(open(CATALOG))
    log(f"catalog: {len(data)} vods")
    existing=set(d for d in os.listdir(DISK) if os.path.isdir(os.path.join(DISK,d)) and d!="lost+found")
    done_ids=set(); seen_titles=set()
    op=DISK+"/_original.json"
    if os.path.exists(op):
        for m in json.load(open(op)): seen_titles.add(norm_title(m.get("title","")))
    if os.path.exists(JSONL):
        for line in open(JSONL):
            try:
                r=json.loads(line); done_ids.add(r["stream_id"]); seen_titles.add(norm_title(r.get("title","")))
            except: pass
    def added_key(x):
        try: return int(x.get("added",0))
        except: return 0
    # prefer Spanish/Latino for the Mexican audience, newest first within each tier
    PREF={"ES":0,"LAT":0,"MX":0,"LA":0,"BR":2}
    cands=[x for x in data if x.get("stream_icon") and str(x.get("container_extension","")).lower() in ("mp4","mkv")]
    cands.sort(key=lambda x:(PREF.get(lang_of(x.get("name","")),3), -added_key(x)))
    es=sum(1 for x in cands if lang_of(x.get("name","")) in ("ES","LAT","MX","LA"))
    log(f"candidates (mp4/mkv w/ poster): {len(cands)} (Spanish/Latino preferred: {es})")
    count=0; probed=0
    for c in cands:
        if count>=TARGET: break
        if free_gb()<MIN_FREE_GB: log(f"stop: disk free {free_gb():.0f}GB < {MIN_FREE_GB}"); break
        sid=c["stream_id"]
        if sid in done_ids: continue
        raw=(c.get("name") or "").strip()
        if not raw: continue
        name=clean_title(raw)
        if norm_title(name) in seen_titles: continue   # skip title already in catalog
        slug=slugify(name)
        if slug in existing: slug=f"{slug}-{sid}"
        ext=(c.get("container_extension") or "mp4").lower()
        url=f"http://{HOST}/movie/{USER}/{PW}/{sid}.{ext}"
        probed+=1
        pr=probe(url)
        if not pr: log(f"skip {sid} {name[:42]}: probe-fail"); continue
        v,a,w,h,dur=pr
        if v!="h264" or a!="aac": log(f"skip {sid} {name[:42]}: {v}/{a}"); continue
        if dur<600: log(f"skip {sid} {name[:42]}: short {int(dur)}s"); continue
        outdir=os.path.join(DISK,slug); os.makedirs(outdir,exist_ok=True)
        log(f"INGEST {sid} '{name[:45]}' [{w}x{h} {v}/{a} {fmt_hms(dur)}] -> {slug}")
        t0=time.time()
        # -bsf:v h264_mp4toannexb embeds SPS/PPS in-band; without it some mkv/mp4
        # sources remux to TS with no decoder headers -> audio plays but no video.
        cmd=["ffmpeg","-y","-nostdin","-loglevel","error","-user_agent",UA,
             "-i",url,"-map","0:v:0","-map","0:a:0","-c","copy","-bsf:v","h264_mp4toannexb",
             "-f","hls","-hls_time","10","-hls_playlist_type","vod",
             "-hls_flags","independent_segments",
             "-hls_segment_filename",os.path.join(outdir,"seg_%04d.ts"),
             os.path.join(outdir,"index.m3u8")]
        try:
            r=subprocess.run(cmd,capture_output=True,timeout=2400,text=True)
        except subprocess.TimeoutExpired:
            log("  FAIL ffmpeg timeout"); subprocess.run(["rm","-rf",outdir]); continue
        if r.returncode!=0 or not os.path.exists(os.path.join(outdir,"index.m3u8")):
            log(f"  FAIL ffmpeg rc={r.returncode}: {(r.stderr or '')[-160:]}"); subprocess.run(["rm","-rf",outdir]); continue
        # verify the remuxed video actually has decoder headers (width>0); transcode fallback if not
        try:
            pv=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width","-of","csv=p=0",os.path.join(outdir,"index.m3u8")],capture_output=True,text=True,timeout=30)
            vw=(pv.stdout or "").strip().splitlines(); vw=int(vw[0]) if vw and vw[0].strip().isdigit() else 0
        except Exception: vw=0
        if vw<=0:
            log(f"  no video headers after copy -> transcoding {name[:40]}")
            for sf in os.listdir(outdir):
                if sf.startswith("seg_") and sf.endswith(".ts"): os.remove(os.path.join(outdir,sf))
            tcmd=["ffmpeg","-y","-nostdin","-loglevel","error","-user_agent",UA,"-i",url,
                  "-map","0:v:0","-map","0:a:0","-c:v","libx264","-preset","veryfast","-crf","21","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k","-ac","2",
                  "-f","hls","-hls_time","10","-hls_playlist_type","vod","-hls_flags","independent_segments",
                  "-hls_segment_filename",os.path.join(outdir,"seg_%04d.ts"),os.path.join(outdir,"index.m3u8")]
            try: r=subprocess.run(tcmd,capture_output=True,timeout=9000,text=True)
            except subprocess.TimeoutExpired: log("  FAIL transcode timeout"); subprocess.run(["rm","-rf",outdir]); continue
            if r.returncode!=0 or not os.path.exists(os.path.join(outdir,"index.m3u8")):
                log(f"  FAIL transcode rc={r.returncode}"); subprocess.run(["rm","-rf",outdir]); continue
        # poster
        try:
            req=urllib.request.Request(c["stream_icon"],headers={"User-Agent":UA})
            open(os.path.join(outdir,"poster.jpg"),"wb").write(urllib.request.urlopen(req,timeout=30).read())
        except Exception as ex: log(f"  poster-fail: {ex}")
        info={}
        try: info=(api("get_vod_info",vod_id=sid).get("info",{}) or {})
        except Exception as ex: log(f"  vod_info-fail: {ex}")
        year=((info.get("releasedate") or info.get("release_date") or "")[:4]) or year_from_name(raw)
        try: rating=float(info.get("rating") or c.get("rating") or 0)
        except: rating=0.0
        rec={"title":name,"slug":slug,"year":year,"rating":round(rating,1),
             "language":"Spanish","genre":info.get("genre","") or "","duration":fmt_hms(dur),
             "country":info.get("country","") or "","director":info.get("director","") or "",
             "cast":info.get("cast","") or info.get("actors","") or "",
             "plot":info.get("plot") or info.get("description") or "",
             "m3u8_url":f"{CDN}/{slug}/index.m3u8","logo_url":f"{CDN}/{slug}/poster.jpg",
             "player_url":f"{CDN}/{slug}/","stream_id":sid,"added":c.get("added")}
        try: write_movie_html(outdir,rec,w,h,v)
        except Exception as ex: log(f"  html-fail: {ex}")
        open(JSONL,"a").write(json.dumps(rec,ensure_ascii=False)+"\n")
        done_ids.add(sid); existing.add(slug); seen_titles.add(norm_title(name)); count+=1
        log(f"  OK ({time.time()-t0:.0f}s, {os.path.getsize(os.path.join(outdir,'index.m3u8'))}B m3u8) [{count}/{TARGET}] free={free_gb():.0f}GB")
        if count % 3 == 0:
            try: regen_movies_json()   # refresh catalog page live during the long fill
            except Exception as ex: log(f"  regen-warn: {ex}")
        time.sleep(2)
    log(f"=== DONE: {count} new movies (probed {probed}) ===")
    regen_movies_json()

def regen_movies_json():
    # movies.json = original 10 (preserved verbatim) + all ingested records
    orig_path=DISK+"/_original.json"
    if not os.path.exists(orig_path):
        cur=json.load(open(DISK+"/movies.json"))
        orig=cur if isinstance(cur,list) else cur.get("movies",[])
        json.dump(orig, open(orig_path,"w"), ensure_ascii=False)
    orig=json.load(open(orig_path))
    orig_slugs={m.get("slug") for m in orig}
    ingested=[]
    if os.path.exists(JSONL):
        for line in open(JSONL):
            try:
                r=json.loads(line)
                if r.get("slug") not in orig_slugs: ingested.append(r)
            except: pass
    merged=orig+ingested
    json.dump(merged, open(DISK+"/movies.json","w"), ensure_ascii=False, indent=1)
    log(f"movies.json regenerated: {len(orig)} original + {len(ingested)} ingested = {len(merged)}")

if __name__=="__main__":
    main()
