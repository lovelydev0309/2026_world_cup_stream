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
:root{--bg:#080A0F;--band:#0b0f17;--raise:#10151f;--surface:#141a25;--border:#232c3a;--border2:#313d4f;--text:#EEF1F6;--muted:#8f99ab;--faint:#616b7d;--accent:#F5C451;--good:#49c98a;}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}
img{display:block;max-width:100%}a{color:inherit;text-decoration:none}
.wrap{max-width:1200px;margin:0 auto;padding:0 22px}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:6px}
.topbar{position:sticky;top:0;z-index:10;background:linear-gradient(180deg,#0b1019,#080A0F);border-bottom:1px solid var(--border)}
.topbar .wrap{display:flex;align-items:center;gap:20px;height:60px}
.mark{font-weight:800;letter-spacing:-.03em;font-size:20px;white-space:nowrap}.mark b{color:var(--accent)}
.seg{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);border-left:1px solid var(--border2);padding-left:20px;font-weight:600}.seg b{color:var(--text)}
.search{margin-left:auto;position:relative}
.search input{background:var(--surface);border:1px solid var(--border2);color:var(--text);border-radius:999px;padding:9px 16px 9px 38px;font-size:13px;width:230px;transition:width .2s,border-color .2s}
.search input:focus{width:280px;border-color:var(--accent);outline:none}
.search svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);opacity:.5}
.search input::placeholder{color:var(--faint)}
.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:26px 0 4px}
.controls h2{font-size:19px;margin:0;font-weight:800;letter-spacing:-.01em}
.controls .count{color:var(--faint);font-size:13px;font-variant-numeric:tabular-nums}
.sortwrap{margin-left:auto;display:flex;align-items:center;gap:9px}
.sortwrap label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
select{background:var(--surface);border:1px solid var(--border2);color:var(--text);border-radius:9px;padding:8px 34px 8px 13px;font-size:13.5px;font-weight:600;cursor:pointer;appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238f99ab' stroke-width='3'><path d='M6 9l6 6 6-6'/></svg>");background-repeat:no-repeat;background-position:right 12px center}
select:focus{border-color:var(--accent);outline:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:20px 18px;margin:22px 0 8px}
.card{display:block;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:transform .16s,border-color .16s,box-shadow .16s}
.card:hover{transform:translateY(-4px);border-color:var(--border2);box-shadow:0 12px 30px rgba(0,0,0,.45)}
.poster{position:relative;aspect-ratio:2/3;background:var(--raise)}
.poster img{width:100%;height:100%;object-fit:cover}
.poster .rt{position:absolute;top:8px;right:8px;background:rgba(8,10,15,.82);color:var(--accent);font-weight:700;font-size:12px;padding:3px 8px;border-radius:999px;backdrop-filter:blur(4px);font-variant-numeric:tabular-nums}
.cbody{padding:11px 12px 13px}
.ctitle{font-size:13.5px;font-weight:700;letter-spacing:-.01em;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:35px}
.cmeta{margin-top:6px;color:var(--faint);font-size:11.5px;display:flex;gap:7px;flex-wrap:wrap;font-variant-numeric:tabular-nums}
.cmeta .dot{width:3px;height:3px;border-radius:50%;background:var(--faint);align-self:center}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:14px}
.pager{grid-column:2;display:flex;justify-content:center;align-items:center;gap:6px;flex-wrap:wrap}
.pager button{background:var(--surface);border:1px solid var(--border2);color:var(--text);border-radius:8px;min-width:38px;height:38px;padding:0 12px;font-size:13.5px;font-weight:600;cursor:pointer;font-variant-numeric:tabular-nums}
.pager button:hover:not(:disabled){border-color:var(--accent)}
.pager button.active{background:var(--accent);color:#101010;border-color:var(--accent)}
.pager button:disabled{opacity:.35;cursor:default}
.pager .ell{color:var(--faint);padding:0 2px}
.viewtoggle{display:flex;gap:2px;background:var(--surface);border:1px solid var(--border2);border-radius:9px;padding:3px}
.viewtoggle button{background:transparent;border:0;color:var(--muted);width:33px;height:30px;border-radius:6px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.viewtoggle button:hover{color:var(--text)}
.viewtoggle button.active{background:var(--accent);color:#101010}
.list{display:flex;flex-direction:column;gap:10px;margin:22px 0 8px}
.list .card{display:flex;flex-direction:row;align-items:stretch}
.list .poster{width:62px;min-width:62px;aspect-ratio:2/3}
.list .poster .rt{display:none}
.list .cbody{flex:1;min-width:0;padding:11px 15px;display:flex;flex-direction:column;justify-content:center}
.list .lhead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.list .ctitle{font-size:15px;-webkit-line-clamp:1;min-height:0}
.list .lrating{color:var(--accent);font-weight:700;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
.list .lplot{color:var(--faint);font-size:12.5px;line-height:1.5;margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pagerbar{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;margin:26px 0 50px}
.pagesize{grid-column:1;justify-self:end;display:flex;align-items:center;gap:8px}
.pagesize label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.pagesize select{height:38px;padding:0 32px 0 13px;font-size:13.5px}
@media(max-width:560px){.pagerbar{grid-template-columns:1fr;justify-items:center;gap:16px}.pagesize{grid-column:1;justify-self:center}.pager{grid-column:1}}
"""

def build_list_page():
    # data-driven catalog page: single sort dropdown + pagination
    return """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Catálogo VOD — tv247on</title>
<style>__CSS__</style>
</head><body>
<header class="topbar"><div class="wrap">
  <span class="mark">tv247<b>on</b></span>
  <span class="seg">Catálogo <b>VOD</b></span>
  <div class="search">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    <input id="q" type="search" placeholder="Buscar películas…" autocomplete="off">
  </div>
</div></header>

<main class="wrap">
  <div class="controls">
    <h2>Películas</h2><span class="count" id="count"></span>
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
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">No se encontraron películas.</div>
  <div class="pagerbar">
    <div class="pagesize">
      <label for="pageSize">Por página</label>
      <select id="pageSize">
        <option value="10">10</option>
        <option value="20" selected>20</option>
        <option value="50">50</option>
        <option value="100">100</option>
      </select>
    </div>
    <div class="pager" id="pager"></div>
  </div>
</main>

<script>
const PAGE_SIZES=['10','20','50','100'];
let ALL=[], view=[], page=1, viewMode='grid', PER_PAGE=20;
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
  return `<a class="card" href="${esc(m.player_url)}"><div class="poster">${posterImg(m)}${rt}</div>
    <div class="cbody"><div class="ctitle">${esc(m.title)}</div><div class="cmeta">${joinMeta(metaBits(m).slice(0,2))}</div></div></a>`;
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
  let list=ALL.filter(m=>!q || m.title.toLowerCase().includes(q) || (m.genre||'').toLowerCase().includes(q));
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
  PER_PAGE = parseInt(v)||20; page=1;
  try{localStorage.setItem('vodPageSize',v);}catch(e){}
  refresh(); });
qEl.addEventListener('input',()=>{page=1;refresh();});
sortEl.addEventListener('change',()=>{page=1;refresh();});

fetch('movies.json?_='+Date.now()).then(r=>r.json()).then(d=>{
  ALL=(Array.isArray(d)?d:(d.movies||[])).map((m,i)=>({...m,_added:(m.added?parseInt(m.added):(1e9-i))}));
  refresh();
}).catch(e=>{emptyEl.textContent='No se pudo cargar el catálogo.';emptyEl.style.display='block';});
</script>
</body></html>""".replace("__CSS__",CSS)

MOVIE_TPL=r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%% — tv247on VOD</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<style>
:root{--bg:#080A0F;--band:#0b0f17;--raise:#10151f;--surface:#141a25;--border:#232c3a;--border2:#313d4f;--text:#EEF1F6;--muted:#8f99ab;--faint:#616b7d;--accent:#F5C451;--good:#49c98a;}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}
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
</style></head><body>
<header class="topbar"><div class="wrap">
  <span class="mark">tv247<b>on</b></span>
  <a class="back" href="../">← Volver al catálogo</a>
  <span class="seg" style="margin-left:auto">VOD</span>
</div></header>
<div class="player"><div class="pwrap"><div class="stage">
  <video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>
  <div class="loading" id="load">Cargando…</div>
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
else{load.textContent='HLS no soportado en este navegador.';}})();
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
        cmd=["ffmpeg","-y","-nostdin","-loglevel","error","-user_agent",UA,
             "-i",url,"-map","0:v:0","-map","0:a:0","-c","copy",
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
