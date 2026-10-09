#!/usr/bin/env python3
"""CollX batch builder. Handles both the API schema (lowercase cols, ISO dates)
and the manual-export schema. Diffs newest vs previous, excludes served,
scores, writes ~/collx_targets.txt"""
import pandas as pd, numpy as np, re, glob, os, sys

HOME = os.path.expanduser("~")
DATA = f"{HOME}/collx_data"
EXCL = f"{DATA}/served_ids.txt"
TARGETS = f"{HOME}/collx_targets.txt"
POS = f"{HOME}/.collx_pos"
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
full_df = df.copy()  # for the random tab: all eligible, pre-exclusion

# delta vs previous export, computed BEFORE exclusion so re-surfaces can see it
df['delta'] = np.nan
df['new_arrival'] = True
if old is not None:
    common = df.index.intersection(old.index)
    df.loc[common, 'delta'] = df.loc[common, 'cards'] - old.loc[common, 'cards']
    df['new_arrival'] = ~df.index.isin(old.index)

# RESURFACE: previously served sellers who just added 15+ cards in one hour
# come back tagged BACK. They're still zero-sales, and a big listing session
# is the strongest buy signal there is.
RESURFACE_MIN = 15
is_excl = df.index.isin(excl)
df['resurface'] = is_excl & (df['delta'] >= RESURFACE_MIN)

# RESURFACE COOLDOWN: a seller only resurfaces once per 7 days, no matter
# how many hours in a row they keep adding cards
import time as _time
RES_FILE = f"{HOME}/collx_data/resurfaced.txt"
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
    print("no fresh profiles this pull")
    sys.exit(0)

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
 '.hdr{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px 0}'
 '.hrow{display:flex;align-items:baseline;justify-content:space-between;font-size:13px;color:var(--dim)}'
 '.hrow b{color:var(--tx);font-size:15px;font-weight:600}'
 '.hrow a{color:var(--act);text-decoration:none;font-size:13px}'
 '.tabs{display:flex;gap:2px;margin:10px 0 0;background:#1A2430;border-radius:9px;padding:2px}'
 '.tabs a{flex:1;text-align:center;padding:7px 0;border-radius:7px;font-size:14px;font-weight:600;'
 'color:var(--dim);text-decoration:none}'
 '.tabs a.on{background:#2A3B4E;color:var(--tx)}'
 '.row{display:flex;gap:12px;align-items:center;padding:11px 14px;border-bottom:1px solid var(--line);'
 'text-decoration:none;color:inherit}'
 '.row:active{background:#1A2430}'
 '@media(hover:hover){.row:hover{background:#17202B;cursor:pointer}}'
 '.ct{flex:0 0 52px;text-align:center}'
 '.ct b{display:block;font-size:19px;font-weight:700;line-height:1.1}'
 '.ct i{display:block;font-style:normal;font-size:10.5px;color:var(--dim)}'
 '.main{flex:1;min-width:0}'
 '.nm{font-size:16.5px;font-weight:600;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
 '.meta{margin-top:3px;font-size:12.5px;color:var(--dim);display:flex;gap:6px;align-items:center;flex-wrap:wrap}'
 '.ch{padding:1px 7px;border-radius:5px;font-weight:700;font-size:11px}'
 '.ch.new{background:rgba(61,214,140,.16);color:var(--new)}'
 '.ch.back{background:rgba(245,184,61,.16);color:var(--back)}'
 '.ch.g{background:rgba(180,140,255,.16);color:var(--grad)}'
 '.bdg{margin-left:auto;font-size:11px;font-weight:700;color:var(--new)}'
 '.bdg.c{color:var(--dim)}'
 '.row.viewed{opacity:.42}'
 '.row.viewed .bdg{color:var(--tx);opacity:.9}'
 '.dvd{padding:9px 14px;background:#1A2430;color:var(--dim);font-size:12.5px;font-weight:600;'
 'border-bottom:1px solid var(--line)}'
 '</style>')

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
    return (f'<a class="row" data-pid="{pid}" href="collx://profiles/{pid}">'
            f'<span class="ct"><b>{cards}</b><i>cards</i></span>'
            f'<span class="main"><span class="nm">{nm}</span>'
            f'<span class="meta">{"".join(chips)}<span class="bdg{" c" if state=="carry" else ""}">{state}</span></span></span></a>')

    rows = []
    for i, l in enumerate(all_lines, 1):
        m = re.search(r'^(\d+) cards \[(.*?)\] (.*?) \|collx://profiles/(\d+)$', l)
        if not m:
            continue
        cards, tags, nm, pid = m.groups()
        if pid == '0':
            rows.append('<div style="padding:10px;background:#222;color:#fff;'
                        'text-align:center;font-weight:bold">SEEN BEFORE — carry-overs below</div>')
            continue
        fresh = i <= len(lines)
        badge = '<span class="bdg" style="color:#0a0">FRESH</span>' if fresh else '<span class="bdg" style="color:#888">carry</span>'
        rows.append(
            f'<div style="padding:14px 10px;border-bottom:1px solid #ddd">'
            f'<a href="collx://profiles/{pid}" style="font-size:19px;text-decoration:none">{_html.escape(nm)}</a><br>'
            f'<span style="color:#555">{cards} cards · {_html.escape(tags)} · {badge}</span></div>'
        )
    ts = datetime.datetime.now().strftime('%-I:%M %p')
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
        '<script>(function(){'
        'function k(p){return "viewed_"+p}'
        'var as=document.querySelectorAll("a.row");'
        'as.forEach(function(a){'
        ' var pid=a.dataset.pid;'
        ' function mark(){'
        '  a.classList.add("viewed");'
        '  var b=a.querySelector(".bdg");'
        '  if(b)b.textContent="VIEWED";'
        ' }'
        ' if(localStorage.getItem(k(pid)))mark();'
        ' a.addEventListener("click",function(){localStorage.setItem(k(pid),Date.now());mark();});'
        '});'
        'var now=Date.now();'
        'for(var i=localStorage.length-1;i>=0;i--){var key=localStorage.key(i);'
        ' if(key&&key.indexOf("viewed_")===0&&now-(+localStorage.getItem(key))>14*86400000)localStorage.removeItem(key);}'
        '})();</script>'
    )
    pwa_tags = (
        '<link rel="manifest" href="manifest.json">'
        '<link rel="apple-touch-icon" href="icon-180.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black">'
        '<meta name="theme-color" content="#111111">'
    )
    page = (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        + pwa_tags + STYLE + refresh_js
        + '<body><div class="wrap">'
        + f'<div class="hdr"><div class="hrow"><span><b>CollX targets</b> · {ts} · {len(lines)} fresh</span>'
        + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
        + '<div class="tabs"><a class="on" href="index.html">Fresh</a><a href="random.html">Random</a></div>'
        + '</div>'
        + ''.join(rows) + '</div></body>'
    )
    if os.path.isdir(icloud):
        open(f"{icloud}/collx_targets.html", 'w').write(page)

# PAGES PUSH: update the phone page on GitHub Pages (opens in Chrome/Safari,
# collx:// links tappable). Config: ~/collx_pipeline/pages.conf = one line, repo-scope token.
REPO = "asweinstein-lab/collx-targets"
pconf = f"{HOME}/collx_pipeline/pages.conf"
if os.path.exists(pconf):
    try:
        import json, base64, urllib.request
        token = open(pconf).read().strip()
        hdrs = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        url = f"https://api.github.com/repos/{REPO}/contents/index.html"
        # need current sha to update
        sha = None
        try:
            req = urllib.request.Request(url, headers=hdrs)
            sha = json.loads(urllib.request.urlopen(req, timeout=30).read())["sha"]
        except Exception:
            pass
        payload = {"message": "update targets", "content": base64.b64encode(page.encode()).decode()}
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PUT", headers=hdrs)
        urllib.request.urlopen(req, timeout=30)
        print("pages updated")

        # RANDOM TAB: 100 random never-contacted accounts, regenerated hourly
        try:
            rnd_pool = full_df[(full_df['cards'] >= 1) & ~full_df.index.isin(excl)]
            rnd = rnd_pool.sample(min(100, len(rnd_pool))) if len(rnd_pool) else rnd_pool
            rrows = []
            for _pid, _r in rnd.iterrows():
                _nm = _html.escape(' '.join(str(_r['Name']).split()))
                if not _nm:
                    continue
                rrows.append(row_html(_pid, _nm, int(_r["cards"]), '', 'RANDOM'))
            rpage = (
                '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                + pwa_tags + STYLE + refresh_js
                + '<body><div class="wrap">'
                + f'<div class="hdr"><div class="hrow"><span><b>CollX random</b> · {ts} · {len(rrows)} never contacted</span>'
                + '<a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())">refresh</a></div>'
                + '<div class="tabs"><a href="index.html">Fresh</a><a class="on" href="random.html">Random</a></div>'
                + '</div>'
                + ''.join(rrows) + '</div></body>')
            rurl = f"https://api.github.com/repos/{REPO}/contents/random.html"
            rsha = None
            try:
                req = urllib.request.Request(rurl, headers=hdrs)
                rsha = json.loads(urllib.request.urlopen(req, timeout=30).read())["sha"]
            except Exception:
                pass
            rpayload = {"message": "update random tab", "content": base64.b64encode(rpage.encode()).decode()}
            if rsha:
                rpayload["sha"] = rsha
            req = urllib.request.Request(rurl, data=json.dumps(rpayload).encode(), method="PUT", headers=hdrs)
            urllib.request.urlopen(req, timeout=30)
            print("random tab updated")
        except Exception as e:
            print(f"random tab failed: {e}")

        # SERVED LOG PUSH: publish served_ids.txt so chat-side batches can
        # exclude against the pipeline's ground truth
        surl = f"https://api.github.com/repos/{REPO}/contents/served_ids.txt"
        ssha = None
        try:
            req = urllib.request.Request(surl, headers=hdrs)
            ssha = json.loads(urllib.request.urlopen(req, timeout=30).read())["sha"]
        except Exception:
            pass
        sbody = open(EXCL, 'rb').read()
        spayload = {"message": "update served log", "content": base64.b64encode(sbody).decode()}
        if ssha:
            spayload["sha"] = ssha
        req = urllib.request.Request(surl, data=json.dumps(spayload).encode(), method="PUT", headers=hdrs)
        urllib.request.urlopen(req, timeout=30)
        print("served log pushed")
    except Exception as e:
        print(f"pages push failed: {e}")
