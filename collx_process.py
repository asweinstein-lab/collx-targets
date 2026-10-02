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
df = df[~is_excl | df['resurface']]

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
pool = df[df['cards'] >= 4].sort_values('score', ascending=False)
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
open(POS, 'w').write('1\n')

# PHONE VERSION: tappable HTML into iCloud Drive
icloud = f"{HOME}/Library/Mobile Documents/com~apple~CloudDocs"
if True:
    import datetime, html as _html
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
        badge = '<span style="color:#0a0">FRESH</span>' if fresh else '<span style="color:#888">carry</span>'
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
        'var as=document.querySelectorAll(\'a[href^="collx://profiles/"]\');'
        'as.forEach(function(a){'
        ' var pid=a.href.split("/").pop();'
        ' var holder=a.closest("div")||a;'
        ' if(localStorage.getItem(k(pid))){'
        '  holder.style.opacity="0.5";'
        '  var b=document.createElement("span");'
        '  b.textContent="VIEWED";'
        '  b.style.cssText="color:#000;background:#ccc;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:700;margin-left:8px;vertical-align:middle";'
        '  a.parentNode.insertBefore(b,a.nextSibling);'
        ' }'
        ' a.addEventListener("click",function(){localStorage.setItem(k(pid),Date.now());});'
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
        + pwa_tags
        + refresh_js
        + f'<body style="font-family:-apple-system;margin:0"><div style="padding:12px;background:#111;color:#fff;'
        f'position:sticky;top:0">CollX targets · updated {ts} · {len(lines)} fresh / {len(carry)} carry'
        f' <a href="javascript:location.replace(location.pathname+\'?t=\'+Date.now())"'
        f' style="float:right;color:#4af;text-decoration:none">refresh</a></div>'
        + ''.join(rows) + '</body>'
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
    except Exception as e:
        print(f"pages push failed: {e}")
