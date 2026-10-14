#!/usr/bin/env python3
"""CollX batch builder. Handles both the API schema (lowercase cols, ISO dates)
and the manual-export schema. Diffs newest vs previous, excludes served,
scores, writes ~/collx_targets.txt"""
# CLOUD FETCH: pull the current export straight from Metabase
import urllib.request as _ur, urllib.parse as _up, datetime as _dt, os as _os, glob as _glob
_CSV_URL = ("https://metabase.collx.app/api/public/dashboard/"
            "d047d975-cd8b-4fdd-9bc2-9cb1ad3d6c1c/dashcard/235/card/229/csv")
_body = _up.urlencode({"parameters": "[]"}).encode()
_req = _ur.Request(_CSV_URL, data=_body, headers={"User-Agent": "collx-pipeline"})
_raw = _ur.urlopen(_req, timeout=60).read()
if _raw[:3] == b"\xef\xbb\xbf":
    _raw = _raw[3:]
_rows = _raw.count(b"\n")
if _rows < 100:
    raise SystemExit(f"fetch FAILED: only {_rows} rows")
_wd = _os.environ.get("WORKDIR", ".")
_ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H_%M_%S")
open(f"{_wd}/export_{_ts}.csv", "wb").write(_raw)
print(f"fetched {_rows} rows")
for _old_f in sorted(_glob.glob(f"{_wd}/export_*.csv"))[:-2]:
    _os.remove(_old_f)

import pandas as pd, numpy as np, re, glob, os, sys

HOME = os.environ.get("WORKDIR", ".")
DATA = HOME
EXCL = f"{DATA}/served_ids.txt"
TARGETS = f"{HOME}/targets.txt"
POS = f"{HOME}/pos.txt"
TCG = re.compile(r'pokemon|poké|poke\b|tcg|magic|mtg|yugioh|yu-gi|onepiece|one piece|lorcana|gundam|dragonball|dbz', re.I)

def load(p):
    df = pd.read_csv(p, encoding='utf-8-sig')
    cols = {c.lower().strip(): c for c in df.columns}
    # normalize both schemas
    name_c = cols.get('name')
    cards_c = cols.get('num_cards_for_sale') or cols.get('# cards for sale')
    addr_c = cols.get('address_set') or cols.get('set address')
    graded_c = cols.get('gradedforsale')
    deep_c = cols.get('deeplink')
    active_c = cols.get('last_active_at')
    out = pd.DataFrame()
    out['Name'] = df[name_c].astype(str)
    out['cards'] = df[cards_c].astype(str).str.replace(',', '').astype(float).astype(int)
    out['addr'] = df[addr_c].astype(str).str.lower().eq('true')
    out['graded'] = df[graded_c].astype(str).str.upper().eq('TRUE')
    out['pid'] = df[deep_c].astype(str).str.extract(r'(\d+)$')
    la = df[active_c].astype(str)
    # ISO (API) or "September 30, 2026, 10:04 PM" (manual export)
    parsed = pd.to_datetime(la, format='ISO8601', errors='coerce', utc=True)
    if parsed.isna().mean() > 0.5:
        parsed = pd.to_datetime(la, format='%B %d, %Y, %I:%M %p', errors='coerce')
        parsed = parsed.dt.tz_localize('America/New_York', ambiguous='NaT', nonexistent='NaT')
    out['last_active'] = parsed
    return out.set_index('pid')

files = sorted(glob.glob(f"{DATA}/export_*.csv"))
if not files:
    sys.exit("no exports")
new = load(files[-1])
old = load(files[-2]) if len(files) > 1 else None

excl = set()
if os.path.exists(EXCL):
    excl = {x.strip() for x in open(EXCL) if x.strip()}

df = new[~new['Name'].str.contains(TCG)].copy()

# delta vs previous export, computed BEFORE exclusion so re-surfaces can see it
df['delta'] = np.nan
df['new_arrival'] = True
if old is not None:
    common = df.index.intersection(old.index)
    df.loc[common, 'delta'] = df.loc[common, 'cards'] - old.loc[common, 'cards']
    df['new_arrival'] = ~df.index.isin(old.index)
full_df = df.copy()  # all eligible rows incl. delta, pre-exclusion

# RESURFACE: previously served sellers who just added 15+ cards in one hour
# come back tagged BACK. They're still zero-sales, and a big listing session
# is the strongest buy signal there is.
RESURFACE_MIN = 15
is_excl = df.index.isin(excl)
df['resurface'] = is_excl & (df['delta'] >= RESURFACE_MIN)

# RESURFACE COOLDOWN: a seller only resurfaces once per 7 days, no matter
# how many hours in a row they keep adding cards
import time as _time
RES_FILE = f"{HOME}/resurfaced.txt"
_now = _time.time()
_recent = {}
try:
    for _ln in open(RES_FILE):
        _p, _t = _ln.split()
        if _now - float(_t) < 7*86400:
            _recent[int(_p)] = float(_t)
except FileNotFoundError:
    pass
df['resurface'] = df['resurface'] & ~df.index.isin(_recent)
with open(RES_FILE, 'w') as _rf:
    for _p, _t in _recent.items():
        _rf.write(f"{_p} {_t}\n")
    for _p in df.index[df['resurface']]:
        _rf.write(f"{_p} {_now}\n")

df = df[~is_excl | df['resurface']]
# FRESH FILTER: only sellers that are new to the report, actively adding,
# or resurfacing. Everything else was either served already or is stale pool.
df = df[df['new_arrival'] | (df['delta'] > 0) | df['resurface']]

now = new['last_active'].max()
df['hrs'] = (now - df['last_active']).dt.total_seconds() / 3600

df['score'] = (
    np.log1p(df['cards'].clip(upper=250)) * 8
    + df['cards'].between(15, 120) * 15
    + (df['hrs'] < 12) * 30 + df['hrs'].between(12, 24) * 20 + df['hrs'].between(24, 72) * 8
    + (df['delta'] > 0) * 30 + df['delta'].clip(0, 50).fillna(0) * 0.5
    + df['graded'] * 10
    + df['new_arrival'] * 10
    + df['addr'] * 6
    + df['resurface'] * 25
)
pool = df[df['cards'] >= 1].sort_values('score', ascending=False)
if pool.empty:
    print("no fresh profiles this pull - rebuilding pages from carry")

lines = []
for pid, r in pool.iterrows():
    tags = []
    if r['resurface']: tags.append('BACK')
    if r['graded']: tags.append('G')
    if pd.notna(r['delta']) and r['delta'] > 0: tags.append(f"+{int(r['delta'])}")
    elif r['new_arrival']: tags.append('NEW')
    if not r['addr']: tags.append('NOADDR')
    h = r['hrs']
    tags.append("now" if h < 3 else (f"{h:.0f}h" if h < 48 else f"{h/24:.0f}d"))
    lines.append(f"{int(r['cards'])} cards [{' '.join(tags)}] {r['Name'].strip()} |collx://profiles/{pid}")

# CARRY-OVER: keep unworked targets from the previous list.
# Position file tells us how far Alex got; everything at or past that
# position (that isn't newly re-scored) gets appended after the fresh batch.
carry = []
try:
    prev_lines = [l.rstrip('\n') for l in open(TARGETS) if l.strip()]
    pos = int(open(POS).read().strip())
    fresh_pids = set(pool.index)
    still_zero = set(new.index)  # still in the zero-sales report
    for l in prev_lines[pos - 1:]:
        m = re.search(r'collx://profiles/(\d+)', l)
        if not m:
            continue
        pid = m.group(1)
        # keep if: not already in the fresh batch, still zero sales
        if pid not in fresh_pids and pid in still_zero:
            carry.append(l)
except (FileNotFoundError, ValueError):
    pass

DIVIDER = "0 cards [----] ======= SEEN BEFORE — hit q here ======= |collx://profiles/0"
all_lines = (lines + [DIVIDER] + carry) if carry else lines
open(TARGETS, 'w').write('\n'.join(all_lines) + '\n')

# SERVED LOG: record every freshly served pid so it never repeats
# (resurfaces are already in the log; appending again is harmless pre-dedup)
with open(EXCL, 'a') as _f:
    for _pid in pool.index:
        _f.write(f"{_pid}\n")
open(POS, 'w').write('1\n')

# PHONE VERSION: tappable HTML into iCloud Drive
icloud = f"{HOME}/Library/Mobile Documents/com~apple~CloudDocs"
if True:
    import datetime, html as _html

STYLE = (
 '<style>'
 ':root{--bg:#121A24;--line:#223042;--tx:#E8EEF4;--dim:#8A98A8;--act:#4FA3FF;'
 '--new:#3DD68C;--back:#F5B83D;--grad:#B48CFF}'
 '*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}'
 'body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:var(--bg);color:var(--tx)}'
 '.wrap{max-width:560px;margin:0 auto}'
 '.hdr{position:sticky;top:0;z-index:5;background:rgba(18,26,36,.86);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);'
 'border-bottom:1px solid var(--line);padding:calc(10px + env(safe-area-inset-top,0)) 14px 0}'
 '.hrow{display:flex;align-items:baseline;justify-content:space-between;font-size:13px;color:var(--dim)}'
 '.hrow b{color:var(--tx);font-size:15px;font-weight:600}'
 '.hrow a{color:var(--act);text-decoration:none;font-size:13px}'
 '.tabs{display:flex;gap:2px;margin:10px 0 0;background:#1A2430;border-radius:9px;padding:2px}'
 '.tabs a{flex:1;text-align:center;padding:7px 0;border-radius:7px;font-size:14px;font-weight:600;'
 'color:var(--dim);text-decoration:none}'
 '.tabs a.on{background:#2A3B4E;color:var(--tx)}'
 '.row{display:flex;gap:12px;align-items:center;padding:11px 14px;border-bottom:1px solid var(--line);'
 'text-decoration:none;color:inherit}'
 '.row{transition:transform .06s}'
 'a.row:active{background:#1A2430;transform:scale(.985)}'
 '@media(hover:hover){.row:hover{background:#17202B;cursor:pointer}}'
 '.ct{flex:0 0 52px;text-align:center}'
 '.ct b{display:block;font-size:19px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}'
 '.ct i{display:block;font-style:normal;font-size:10.5px;color:var(--dim)}'
 '.main{flex:1;min-width:0}'
 '.nm{font-size:16.5px;font-weight:600;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
 '.meta{margin-top:3px;font-size:12.5px;color:var(--dim);display:flex;gap:6px;align-items:center;flex-wrap:wrap}'
 '.ch{padding:1px 7px;border-radius:5px;font-weight:700;font-size:11px}'
 '.ch.new{background:rgba(61,214,140,.22);color:var(--new)}'
 '.ch.back{background:rgba(245,184,61,.22);color:var(--back)}'
 '.ch.g{background:rgba(180,140,255,.16);color:var(--grad)}'
 '.bdg{margin-left:auto;font-size:11px;font-weight:700;color:var(--new)}'
 '.bdg.c{color:var(--dim)}'
 '.row.viewed{opacity:.42}'
 '.row.viewed .bdg{color:var(--tx);opacity:.9}'
 '.fbar{display:flex;gap:6px;padding:10px 0;overflow-x:auto}'
 '.fc{flex:0 0 auto;padding:6px 11px;border-radius:15px;background:#1A2430;color:var(--dim);'
 'font-size:12.5px;font-weight:600;border:1px solid var(--line)}'
 '.fc.on{background:#2A3B4E;color:var(--tx);border-color:#3A5068}'
 '.dvd{padding:9px 14px;background:#1A2430;color:var(--dim);font-size:12.5px;font-weight:600;'
 'border-bottom:1px solid var(--line)}'
 '</style>')

def _hrs_from_tags(tags):
    import re as _re
    for w in tags.split():
        if w == 'now': return 0.0
        m = _re.match(r'^(\d+)h$', w)
        if m: return float(m.group(1))
        m = _re.match(r'^(\d+)d$', w)
        if m: return float(m.group(1))*24
    return 9999.0

_ROWI = [0]
def row_html(pid, nm, cards, tags, state):
    chips = []
    t = tags
    if 'BACK' in t: chips.append('<span class="ch back">BACK</span>'); t = t.replace('BACK','')
    if 'NEW' in t: chips.append('<span class="ch new">NEW</span>'); t = t.replace('NEW','')
    if 'NOADDR' in t: t = t.replace('NOADDR',''); chips.append('<span class="ch" style="background:#2A3B4E;color:var(--dim)">no addr</span>')
    tt = t.replace('G','').strip() if 'G' in t.split() or t.startswith('G ') or ' G ' in f' {t} ' else t.strip()
    if ('G' in t.split()) or t.startswith('G ') or (' G ' in f' {t} '):
        chips.append('<span class="ch g">graded</span>')
    delta = ' '.join(w for w in tt.split() if w.startswith('+'))
    rest = ' '.join(w for w in tt.split() if not w.startswith('+'))
    if delta: chips.append(f'<span class="ch new">{delta}</span>')
    if rest: chips.append(f'<span>{rest}</span>')
    _h = _hrs_from_tags(tags)
    _ROWI[0] += 1
    return (f'<a class="row" data-pid="{pid}" data-cards="{cards}" data-hrs="{_h}"'
            f' data-now="{1 if _h == 0 else 0}" data-i="{_ROWI[0]}" href="collx://profiles/{pid}">'
            f'<span class="ct"><b>{cards}</b><i>cards</i></span>'
            f'<span class="main"><span class="nm">{nm}</span>'
            f'<span class="meta">{"".join(chips)}<span class="bdg{" c" if state=="carry" else ""}">{state}</span></span></span></a>')

if True:  # page build block
    rows = []
    for i, l in enumerate(all_lines, 1):
        m = re.search(r'^(\d+) cards \[(.*?)\] (.*?) \|collx://profiles/(\d+)$', l)
        if not m:
            continue
        cards, tags, nm, pid = m.groups()
        if pid == '0':
            rows.append('<div class="dvd">Seen before — carry-overs below</div>')
            continue
        fresh = i <= len(lines)
        rows.append(row_html(pid, _html.escape(nm), cards, _html.escape(tags),
                             'FRESH' if fresh else 'carry'))
    ts = datetime.datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%-I:%M %p ET')
    import time as _time
    build_ms = int(_time.time() * 1000)
    refresh_js = (
        '<script>(function(){'
        f'var BUILD={build_ms};'
        'var AGE=Date.now()-BUILD;'
        'function bust(){'
        '  var last=+(sessionStorage.getItem("lastbust")||0);'
        '  if(Date.now()-last<60000)return;'  # max one auto-reload per minute, no loops
        '  sessionStorage.setItem("lastbust",Date.now());'
        '  location.replace(location.pathname+"?t="+Date.now());'
        '}'
        'if(AGE>10*60*1000)bust();'  # stale on open -> refetch
        'document.addEventListener("visibilitychange",function(){'
        '  if(!document.hidden && Date.now()-BUILD>10*60*1000)bust();'
        '});'
        '})();</script>'
    )
    app_js = (
        '<script>document.addEventListener("DOMContentLoaded",function(){'
        'function k(p){return "viewed_"+p}'
        'var as=document.querySelectorAll("a.row");'
        'as.forEach(function(a){'
        ' var pid=a.dataset.pid;'
        ' function mark(){'
        '  a.classList.add("viewed");'
        '  var b=a.querySelector(".bdg");'
        '  if(b)b.textContent="VIEWED";'
        ' }'
        ' if(localStorage.getItem(k(pid))){mark();a.classList.add("gone");}'
        ' a.addEventListener("click",function(){localStorage.setItem(k(pid),Date.now());mark();});'
        '});'
        'var FS=JSON.parse(localStorage.getItem("filters_v1")||"{}");'
        'FS.on=0;'
        'var chips=document.querySelectorAll(".fc");'
        'function savef(){localStorage.setItem("filters_v1",JSON.stringify(FS));}'
        'function apply(){'
        ' var rows=[].slice.call(document.querySelectorAll("a.row"));'
        ' var min=FS.c50?50:FS.c25?25:FS.c10?10:0;'
        ' rows.forEach(function(r){'
        '  var vis=true;'
        '  if(r.classList.contains("gone"))vis=false;'
        '  if(FS.hv&&r.classList.contains("viewed"))vis=false;'
        '  if(+r.dataset.cards<min)vis=false;'
        '  if(FS.on&&r.dataset.now!=="1")vis=false;'
        '  r.style.display=vis?"":"none";});'
        ' var shown=rows.filter(function(r){return r.style.display!=="none"}).length;'
        ' var eb=document.getElementById("emptyb");'
        ' if(!eb){eb=document.createElement("div");eb.id="emptyb";'
        '  eb.style.cssText="padding:26px 16px;text-align:center;color:var(--dim);font-size:14px;display:none";'
        '  eb.textContent="";var w=document.querySelector(".wrap");if(w)w.appendChild(eb);'
        '  eb.addEventListener("click",function(){FS={};savef();'
        '   document.querySelectorAll(".fc").forEach(function(c){c.classList.remove("on")});apply();});}'
        ' if(rows.length&&shown===0){eb.style.display="";'
        '  eb.textContent="All "+rows.length+" hidden by filters - tap here to clear";}'
        ' else eb.style.display="none";'
        ' var dvd=document.querySelector(".dvd");'
        ' if(FS.sort){'
        '  if(dvd)dvd.style.display="none";'
        '  var par=rows[0]&&rows[0].parentNode;'
        '  if(par){rows.sort(function(a,b){return (+a.dataset.hrs)-(+b.dataset.hrs)||(+a.dataset.i)-(+b.dataset.i);});'
        '   rows.forEach(function(r){par.appendChild(r);});}'
        ' }else{'
        '  if(dvd)dvd.style.display="";'
        '  var par2=rows[0]&&rows[0].parentNode;'
        '  if(par2){rows.sort(function(a,b){return (+a.dataset.i)-(+b.dataset.i);});'
        '   rows.forEach(function(r){par2.appendChild(r);});'
        '   if(dvd&&rows.length){var after=rows.filter(function(r){return r.querySelector(".bdg").textContent==="carry";})[0];'
        '    if(after)par2.insertBefore(dvd,after);}}'
        ' }'
        '}'
        'chips.forEach(function(ch){'
        ' var f=ch.dataset.f;'
        ' if(FS[f])ch.classList.add("on");'
        ' ch.addEventListener("click",function(){'
        '  if(f==="c10"||f==="c25"||f==="c50"){var was=FS[f];FS.c10=FS.c25=FS.c50=0;FS[f]=was?0:1;'
        '   chips.forEach(function(c2){if(/^c\\d/.test(c2.dataset.f))c2.classList.toggle("on",!!FS[c2.dataset.f]);});}'
        '  else{FS[f]=FS[f]?0:1;ch.classList.toggle("on",!!FS[f]);}'
        '  savef();apply();});'
        '});'
        'apply();'
        'var vt=document.getElementById("vt");'
        'if(vt){var d0=new Date();d0.setHours(0,0,0,0);var n=0;'
        'for(var j=0;j<localStorage.length;j++){var ky=localStorage.key(j);'
        'if(ky&&ky.indexOf("viewed_")===0&&+localStorage.getItem(ky)>=d0.getTime())n++;}'
        'vt.textContent=n+" viewed today";}'
        'var now=Date.now();'
        'for(var i=localStorage.length-1;i>=0;i--){var key=localStorage.key(i);'
        ' if(key&&key.indexOf("viewed_")===0&&now-(+localStorage.getItem(key))>14*86400000)localStorage.removeItem(key);}'
        '});</script>'
    )
    pwa_tags = (
        '<link rel="manifest" href="manifest.json">'
        '<link rel="apple-touch-icon" href="icon-180.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black">'
        '<meta name="theme-color" content="#111111">'
    )
    page = (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        + pwa_tags + STYLE + refresh_js
        + '<body><div class="wrap">'
        + f'<div class="hdr"><div class="hrow"><span><b>CollX targets</b> · {ts} · {len(lines)} fresh · <span id="vt"></span></span>'
        + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
        + '<div class="tabs"><a class="on" href="index.html">Fresh</a><a href="movers.html">Movers</a><a href="random.html">Random</a><a href="stats.html">Stats</a></div>'
        + '<div class="fbar">''<span class="fc" data-f="hv">Hide viewed</span>''<span class="fc" data-f="c10">10+</span>''<span class="fc" data-f="c25">25+</span>''<span class="fc" data-f="c50">50+</span>''<span class="fc" data-f="on">Online now</span>''<span class="fc" data-f="sort">Newest first</span>''</div>'
        + '</div>'
        + ''.join(rows) + '</div>' + app_js + '</body>'
    )
    if os.path.isdir(icloud):
        open(f"{icloud}/collx_targets.html", 'w').write(page)

# CLOUD PUBLISH: write pages; the workflow commits everything
open(f"{HOME}/index.html", "w").write(page)

rnd_pool = full_df[(full_df['cards'] >= 1) & ~full_df.index.isin(excl)]
rnd = rnd_pool.sample(min(100, len(rnd_pool))) if len(rnd_pool) else rnd_pool
rrows = []
for _pid, _r in rnd.iterrows():
    _nm = _html.escape(' '.join(str(_r['Name']).split()))
    if not _nm:
        continue
    rrows.append(row_html(_pid, _nm, int(_r["cards"]), '', 'RANDOM'))
rpage = (
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    + pwa_tags + STYLE + refresh_js
    + '<body><div class="wrap">'
    + f'<div class="hdr"><div class="hrow"><span><b>CollX random</b> · {ts} · {len(rrows)} never contacted</span>'
    + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
    + '<div class="tabs"><a href="index.html">Fresh</a><a href="movers.html">Movers</a><a class="on" href="random.html">Random</a><a href="stats.html">Stats</a></div>'
    + '<div class="fbar">'
    + '<span class="fc" data-f="hv">Hide viewed</span><span class="fc" data-f="c10">10+</span>'
    + '<span class="fc" data-f="c25">25+</span><span class="fc" data-f="c50">50+</span>'
    + '<span class="fc" data-f="on">Online now</span><span class="fc" data-f="sort">Newest first</span></div>'
    + '</div>'
    + ''.join(rrows) + '</div>' + app_js + '</body>')
# MOVERS TAB: already-served sellers who added cards since the last pull
mv = full_df[full_df.index.isin(excl) & (full_df['delta'] > 0)].copy()
mv = mv.sort_values('delta', ascending=False).head(100)
mrows = []
for _pid, _r in mv.iterrows():
    _nm = _html.escape(' '.join(str(_r['Name']).split()))
    if not _nm:
        continue
    _h = (now - _r['last_active']).total_seconds()/3600 if 'last_active' in _r.index else 9999
    _t = 'now' if _h < 3 else (f"{_h:.0f}h" if _h < 48 else f"{_h/24:.0f}d")
    mrows.append(row_html(_pid, _nm, int(_r['cards']), f"+{int(_r['delta'])} {_t}", 'MOVER'))
mpage = (
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    + pwa_tags + STYLE + refresh_js
    + '<body><div class="wrap">'
    + f'<div class="hdr"><div class="hrow"><span><b>CollX movers</b> · {ts} · {len(mrows)} added cards since last pull</span>'
    + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
    + '<div class="tabs"><a href="index.html">Fresh</a><a class="on" href="movers.html">Movers</a><a href="random.html">Random</a><a href="stats.html">Stats</a></div>'
    + '<div class="fbar">'
    + '<span class="fc" data-f="hv">Hide viewed</span><span class="fc" data-f="c10">10+</span>'
    + '<span class="fc" data-f="c25">25+</span><span class="fc" data-f="c50">50+</span>'
    + '<span class="fc" data-f="on">Online now</span><span class="fc" data-f="sort">Newest first</span></div>'
    + '</div>'
    + ''.join(mrows) + '</div>' + app_js + '</body>')
open(f"{HOME}/movers.html", "w").write(mpage)

# STATS LOG: one line per run, kept to ~3 weeks
import json as _json
_items = []
for _l in lines[:40]:
    _m = re.match(r'^(\d+) cards \[(.*?)\] (.*?) \|collx://profiles/(\d+)$', _l)
    if _m:
        _items.append([_m.group(4), _m.group(3), int(_m.group(1)), _m.group(2)])
_stat = {
    "t": datetime.datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M'),
    "fresh": len(lines), "carry": len(carry), "movers": len(mrows),
    "pool": int(len(rnd_pool)), "served": len(excl),
    "new_signups": int(df['new_arrival'].sum()) if 'new_arrival' in df else 0,
    "items": _items,
}
_hist = []
try:
    _hist = [_json.loads(x) for x in open(f"{HOME}/stats.jsonl") if x.strip()]
except FileNotFoundError:
    pass
_hist.append(_stat)
_hist = _hist[-500:]
open(f"{HOME}/stats.jsonl", "w").write("\n".join(_json.dumps(x) for x in _hist) + "\n")

# STATS PAGE (interactive)
_last24 = _hist[-24:]
_today = _stat["t"][:10]
_fresh_today = sum(x["fresh"] for x in _hist if x["t"][:10] == _today)
_signups_today = sum(x.get("new_signups", 0) for x in _hist if x["t"][:10] == _today)
_days = {}
for x in _hist:
    _days.setdefault(x["t"][:10], 0)
    _days[x["t"][:10]] += x["fresh"]
_day_list = sorted(_days.items())[-7:]

# drill datasets
_seen_today, _today_items = set(), []
for x in _hist:
    if x["t"][:10] == _today:
        for it in x.get("items", []):
            if it[0] not in _seen_today:
                _seen_today.add(it[0]); _today_items.append(it)
_today_items = _today_items[::-1][:80]

_mv_items = []
for _pid, _r in mv.head(60).iterrows():
    _nm = ' '.join(str(_r['Name']).split())
    if _nm: _mv_items.append([str(_pid), _nm, int(_r['cards']), f"+{int(_r['delta'])}"])

_pool_top = []
for _pid, _r in rnd_pool.sort_values('cards', ascending=False).head(50).iterrows():
    _nm = ' '.join(str(_r['Name']).split())
    if _nm: _pool_top.append([str(_pid), _nm, int(_r['cards']), ''])

_carry_items = []
for _l in carry[:60]:
    _m = re.match(r'^(\d+) cards \[(.*?)\] (.*?) \|collx://profiles/(\d+)$', _l)
    if _m and _m.group(4) != '0':
        _carry_items.append([_m.group(4), _m.group(3), int(_m.group(1)), _m.group(2)])

_served_series = [x["served"] for x in _hist[-48:]]
_drill = {
  "freshtoday": {"title": f"Fresh served today — {_fresh_today}", "rows": _today_items},
  "signups": {"title": f"New signups today — {_signups_today}", "rows": _today_items},
  "movers": {"title": f"Movers right now — {len(_mv_items)}", "rows": _mv_items},
  "pool": {"title": f"Never contacted — biggest collections", "rows": _pool_top},
  "served": {"title": "Served all-time — last 48 pulls", "series": _served_series},
  "carry": {"title": f"Carry-overs — {len(carry)}", "rows": _carry_items},
  "runs": [[x["t"], x["fresh"], x.get("new_signups", 0)] for x in _last24],
}

def _bars(vals, w=330, h=84):
    if not vals: return '<div style="color:var(--dim);font-size:13px;padding:6px 0">no runs yet</div>'
    mx = max(max(vals), 1)
    bw = w / len(vals)
    parts = []
    for i, v in enumerate(vals):
        bh = max(3, v / mx * (h - 20))
        parts.append(
            f'<g class="bar" data-i="{i}"><rect x="{i*bw+1.5:.1f}" y="0" width="{bw-3:.1f}" height="{h}" fill="transparent"/>'
            f'<rect x="{i*bw+1.5:.1f}" y="{h-bh-14:.1f}" width="{bw-3:.1f}" height="{bh:.1f}" rx="3" fill="var(--act)" opacity="{0.4 + 0.6*v/mx:.2f}"/></g>')
    return (f'<svg id="runchart" viewBox="0 0 {w} {h}" style="width:100%;height:auto">'
            + ''.join(parts) + f'<text id="barlab" x="4" y="{h-2}" fill="var(--dim)" font-size="10"></text></svg>')

def _card(key, label, val, sub=''):
    return (f'<div class="scard" data-d="{key}"><div class="sv">{val}</div><div class="sl">{label}</div>'
            + (f'<div class="ss">{sub}</div>' if sub else '') + '<div class="chv">›</div></div>')

import json as _j2
_spage = (
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    + pwa_tags + STYLE
    + '<style>'
    + '.sgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px}'
    + '.scard{position:relative;background:#1A2430;border:1px solid var(--line);border-radius:14px;padding:14px;transition:transform .06s}'
    + '.scard:active{transform:scale(.97);background:#20304-0}'
    + '.sv{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}'
    + '.sl{font-size:12px;color:var(--dim);margin-top:2px}'
    + '.ss{font-size:11px;color:var(--dim);margin-top:4px}'
    + '.chv{position:absolute;top:12px;right:12px;color:var(--dim);font-size:16px}'
    + '.sect{padding:4px 14px 2px;font-size:13px;font-weight:600;color:var(--dim)}'
    + '.chartbox{padding:6px 14px 14px}'
    + '.srow{display:flex;justify-content:space-between;padding:8px 14px;border-bottom:1px solid var(--line);font-size:14px}'
    + '.srow b{font-variant-numeric:tabular-nums}'
    + '.ovl{position:fixed;inset:0;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;transition:opacity .18s;z-index:20}'
    + '.sheet{position:fixed;left:0;right:0;bottom:0;max-height:75vh;background:#17202B;border-radius:18px 18px 0 0;'
    + 'transform:translateY(102%);transition:transform .22s;z-index:21;display:flex;flex-direction:column;'
    + 'padding-bottom:env(safe-area-inset-bottom,0)}'
    + '.sheet.open{transform:translateY(0)}'
    + '.ovl.open{opacity:1;pointer-events:auto}'
    + '.sh-h{padding:14px 16px 10px;font-weight:700;font-size:15px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}'
    + '.sh-x{color:var(--dim);font-size:20px;line-height:1}'
    + '.sh-b{overflow-y:auto}'
    + '.grab{width:36px;height:4px;background:#2A3B4E;border-radius:2px;margin:8px auto 0}'
    + '</style>'
    + '<body><div class="wrap">'
    + f'<div class="hdr"><div class="hrow"><span><b>CollX stats</b> · {ts}</span>'
    + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
    + '<div class="tabs"><a href="index.html">Fresh</a><a href="movers.html">Movers</a><a href="random.html">Random</a><a class="on" href="stats.html">Stats</a></div>'
    + '</div>'
    + '<div class="sgrid">'
    + _card('freshtoday', 'fresh served today', _fresh_today)
    + _card('signups', 'new signups today', _signups_today)
    + _card('movers', 'movers right now', len(_mv_items))
    + _card('pool', 'never contacted', f'{len(rnd_pool):,}')
    + _card('served', 'served all-time', f'{len(excl):,}')
    + _card('carry', 'carry-overs', len(carry))
    + '</div>'
    + '<div class="sect">Fresh per run — last 24 pulls · tap bars</div>'
    + f'<div class="chartbox">{_bars([x["fresh"] for x in _last24])}</div>'
    + '<div class="sect">Fresh per day</div>'
    + ''.join(f'<div class="srow"><span>{d[5:]}</span><b>{n}</b></div>' for d, n in _day_list)
    + '</div>'
    + '<div class="ovl" id="ovl"></div>'
    + '<div class="sheet" id="sheet"><div class="grab"></div>'
    + '<div class="sh-h"><span id="sh-t"></span><span class="sh-x" id="sh-x">✕</span></div>'
    + '<div class="sh-b" id="sh-b"></div></div>'
    + '<script type="application/json" id="D">' + _j2.dumps(_drill) + '</script>'
    + """<script>
(function(){
var D=JSON.parse(document.getElementById('D').textContent);
var sheet=document.getElementById('sheet'),ovl=document.getElementById('ovl');
function rowHtml(r){
 return '<a class="row" href="collx://profiles/'+r[0]+'">'
  +'<span class="ct"><b>'+r[2]+'</b><i>cards</i></span>'
  +'<span class="main"><span class="nm">'+r[1]+'</span>'
  +'<span class="meta"><span>'+(r[3]||'')+'</span></span></span></a>';
}
function sparkline(series){
 if(!series||!series.length)return '';
 var w=330,h=80,mn=Math.min.apply(null,series),mx=Math.max.apply(null,series);
 var pts=series.map(function(v,i){
  var x=i/(series.length-1)*w, y=h-6-((v-mn)/Math.max(mx-mn,1))*(h-16);
  return x.toFixed(1)+','+y.toFixed(1)}).join(' ');
 return '<div style="padding:14px"><svg viewBox="0 0 '+w+' '+h+'" style="width:100%">'
  +'<polyline points="'+pts+'" fill="none" stroke="var(--act)" stroke-width="2.5" stroke-linejoin="round"/></svg>'
  +'<div style="color:var(--dim);font-size:12px">'+mn.toLocaleString()+' → '+mx.toLocaleString()+'</div></div>';
}
function open(key){
 var d=D[key]; if(!d)return;
 document.getElementById('sh-t').textContent=d.title;
 var b=document.getElementById('sh-b');
 if(d.rows)b.innerHTML=d.rows.length?d.rows.map(rowHtml).join(''):'<div style="padding:18px;color:var(--dim)">nothing here right now</div>';
 else if(d.series)b.innerHTML=sparkline(d.series);
 sheet.classList.add('open');ovl.classList.add('open');
}
function close(){sheet.classList.remove('open');ovl.classList.remove('open');}
document.querySelectorAll('.scard').forEach(function(c){c.addEventListener('click',function(){open(c.dataset.d)})});
ovl.addEventListener('click',close);
document.getElementById('sh-x').addEventListener('click',close);
var runs=D.runs||[];
document.querySelectorAll('#runchart .bar').forEach(function(g){
 g.addEventListener('click',function(){
  var r=runs[+g.dataset.i];if(!r)return;
  document.getElementById('barlab').textContent=r[0].slice(5)+' — '+r[1]+' fresh, '+r[2]+' signups';
 });
});
})();
</script></body>""")
open(f"{HOME}/stats.html", "w").write(_spage)

open(f"{HOME}/random.html", "w").write(rpage)
print("pages written")
