#!/usr/bin/env python3
"""CollX batch builder. Handles both the API schema (lowercase cols, ISO dates)
and the manual-export schema. Diffs newest vs previous, excludes served,
scores, writes ~/collx_targets.txt"""
# CLOUD FETCH: pull the current export straight from Metabase
import urllib.request as _ur, urllib.parse as _up, datetime as _dt, os as _os
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
_ts = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H_%M_%S")
open(f"{_wd}/export_{_ts}.csv", "wb").write(_raw)
print(f"fetched {_rows} rows")
# keep only the 2 newest exports in the repo
import glob as _glob
_eps = sorted(_glob.glob(f"{_wd}/export_*.csv"))
for _old in _eps[:-2]:
    _os.remove(_old)

import pandas as pd, numpy as np, re, glob, os, sys

HOME = os.environ.get("WORKDIR", ".")
DATA = HOME
EXCL = f"{DATA}/served_ids.txt"
TARGETS = f"{HOME}/targets.txt"
POS = f"{HOME}/.collx_pos_unused"
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
icloud = "/nonexistent"

# CLOUD PUBLISH: write pages as files; the workflow git-commits everything
open(f"{HOME}/index.html", "w").write(page)

# RANDOM TAB
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
    + '<div class="fbar">''<span class="fc" data-f="hv">Hide viewed</span>''<span class="fc" data-f="c10">10+</span>''<span class="fc" data-f="c25">25+</span>''<span class="fc" data-f="c50">50+</span>''<span class="fc" data-f="on">Online now</span>''<span class="fc" data-f="sort">Newest first</span>''</div>'
    + '</div>'
    + ''.join(rrows) + '</div></body>')
open(f"{HOME}/random.html", "w").write(rpage)
print("pages written")
