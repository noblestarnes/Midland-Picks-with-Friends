#!/usr/bin/env python3
"""
Syncs live college-football scores into the Midland Picks Firestore project
(midland-picks-with-friends), used by the public GitHub Pages site.

Score source: CollegeFootballData.com (CFBD), not ESPN. ESPN's public
scoreboard endpoint started outright blocking requests from GitHub Actions'
IP ranges with a blanket 403 (confirmed even after sending full browser-style
headers), which is a known issue for CI systems calling ESPN's unofficial
API. CFBD is a real API meant for exactly this kind of programmatic use —
free, but requires a personal API key (see README note below).

Stdlib only (urllib) so it runs anywhere with no pip installs.

SETUP: get a free API key at https://collegefootballdata.com/key, then add
it as a GitHub repo secret named CFBD_API_KEY (Settings -> Secrets and
variables -> Actions -> New repository secret). The workflow file passes it
in as an environment variable — never hardcode the key in this file, since
this repo is public.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

PROJECT_ID = "midland-picks-with-friends"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"

CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "").strip()

# Optional shortcuts for cases where a school's dashboard name and CFBD's
# name for it don't share an obvious substring (e.g. an abbreviation you
# used in an old week). Most full team names need nothing here — CFBD uses
# plain school names ("Missouri", "Texas A&M") that already match directly.
TEAM_ALIASES = {
    "MIA": ["MIA", "MIAMI"], "STAN": ["STAN", "STANFORD"],
    "TXST": ["TXST", "TEXAS STATE"], "TEX": ["TEX", "TEXAS"],
    "BAY": ["BAY", "BAYLOR"], "AUB": ["AUB", "AUBURN"],
    "TULANE": ["TULN", "TULANE"], "DUKE": ["DUKE"],
    "UCLA": ["UCLA"], "CAL": ["CAL", "CALIFORNIA"],
    "CLEM": ["CLEM", "CLEMSON"], "LSU": ["LSU"],
    "WISC": ["WIS", "WISCONSIN"], "ND": ["ND", "NOTRE DAME"],
    "LOU": ["LOU", "LOUISIANA"], "MISS": ["MISS", "OLE MISS", "MISSISSIPPI"],
    "SMU": ["SMU"], "FSU": ["FSU", "FLORIDA STATE"],
    "MISSOURI": ["MIZ", "MISSOURI"], "KANSAS": ["KU", "KANSAS"],
}

# ---------------- Firestore REST helpers (typed value <-> plain JSON) ----------------

def _fs_val_to_plain(v):
    if "nullValue" in v: return None
    if "booleanValue" in v: return v["booleanValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "stringValue" in v: return v["stringValue"]
    if "arrayValue" in v: return [_fs_val_to_plain(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v: return _fs_fields_to_plain(v["mapValue"].get("fields", {}))
    return None

def _fs_fields_to_plain(fields):
    return {k: _fs_val_to_plain(v) for k, v in (fields or {}).items()}

def _plain_to_fs_val(o):
    if o is None: return {"nullValue": None}
    if isinstance(o, bool): return {"booleanValue": o}
    if isinstance(o, int): return {"integerValue": str(o)}
    if isinstance(o, float): return {"doubleValue": o}
    if isinstance(o, str): return {"stringValue": o}
    if isinstance(o, list): return {"arrayValue": {"values": [_plain_to_fs_val(x) for x in o]}}
    if isinstance(o, dict): return {"mapValue": {"fields": {k: _plain_to_fs_val(v) for k, v in o.items()}}}
    raise ValueError(f"unsupported type for Firestore: {type(o)}")

def _http(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}

def fs_get(path):
    try:
        data = _http(f"{BASE}/{path}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return _fs_fields_to_plain(data.get("fields", {}))

def fs_set(path, obj):
    body = {"fields": {k: _plain_to_fs_val(v) for k, v in obj.items()}}
    return _http(f"{BASE}/{path}", method="PATCH", body=body)

def fs_list(collection, page_size=100):
    out = {}
    page_token = None
    while True:
        url = f"{BASE}/{collection}?pageSize={page_size}"
        if page_token:
            url += f"&pageToken={page_token}"
        data = _http(url)
        for d in data.get("documents", []):
            doc_id = d["name"].split("/")[-1]
            out[doc_id] = _fs_fields_to_plain(d.get("fields", {}))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out

# ---------------- CollegeFootballData ----------------

def cfbd_games(year):
    """Fetches every FBS game (regular season + postseason/bowls) for the
    given season in one call. seasonType=both covers bowl games automatically,
    so no extra cron windows are needed in December like the old ESPN setup
    would have required."""
    if not CFBD_API_KEY:
        raise RuntimeError(
            "CFBD_API_KEY is not set. Get a free key at "
            "https://collegefootballdata.com/key and add it as a GitHub "
            "repo secret named CFBD_API_KEY."
        )
    qs = urllib.parse.urlencode({"year": year, "seasonType": "both"})
    url = f"{CFBD_BASE}/games?{qs}"
    headers = {"Authorization": f"Bearer {CFBD_API_KEY}", "Accept": "application/json"}
    data = _http(url, headers=headers)
    return data if isinstance(data, list) else []

def games_in_date_window(all_games, dates):
    """dates: list of 'YYYYMMDD' strings. Filters CFBD's season-long game
    list down to just the ones starting on one of those UTC calendar days."""
    wanted = set(dates)
    out = []
    for g in all_games:
        start = g.get("start_date") or g.get("startDate")
        if not start:
            continue
        try:
            day = start[:10].replace("-", "")  # 'YYYY-MM-DDT...' -> 'YYYYMMDD'
        except Exception:
            continue
        if day in wanted:
            out.append(g)
    return out

def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

def matches_alias(cfbd_team_name, dashboard_name):
    aliases = TEAM_ALIASES.get(dashboard_name, [dashboard_name])
    name_n = _norm(cfbd_team_name)
    for a in aliases:
        an = _norm(a)
        if an == name_n or an in name_n or name_n in an:
            return True
    return False

def find_cfbd_match(dashboard_game, cfbd_games_list):
    home_field = "home_team" if cfbd_games_list and "home_team" in cfbd_games_list[0] else "homeTeam"
    away_field = "away_team" if cfbd_games_list and "away_team" in cfbd_games_list[0] else "awayTeam"
    for cg in cfbd_games_list:
        cg_home = cg.get(home_field) or cg.get("home_team") or cg.get("homeTeam")
        cg_away = cg.get(away_field) or cg.get("away_team") or cg.get("awayTeam")
        if cg_home and cg_away and \
           matches_alias(cg_home, dashboard_game["home"]) and \
           matches_alias(cg_away, dashboard_game["away"]):
            return cg
    return None

def cfbd_score(cg, side):
    """side: 'home' or 'away'. Handles both snake_case and camelCase field
    names, since CFBD has multiple API versions in the wild."""
    for key in (f"{side}_points", f"{side}Points"):
        if key in cg:
            return cg[key]
    return None

def cfbd_completed(cg):
    for key in ("completed",):
        if key in cg:
            return bool(cg[key])
    return False

# ---------------- main sync ----------------

def candidate_dates():
    today = datetime.now(timezone.utc)
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(0, 4)]

def main():
    weeks = fs_list("weeks")
    if not weeks:
        print("No weeks found in Firestore yet — nothing to sync.")
        return

    dates = candidate_dates()
    year = datetime.now(timezone.utc).year
    try:
        all_games = cfbd_games(year)
    except Exception as e:
        print(f"CollegeFootballData fetch failed: {e}", file=sys.stderr)
        all_games = []
    cfbd_recent = games_in_date_window(all_games, dates)
    print(f"Fetched {len(all_games)} total CFBD games for {year}, {len(cfbd_recent)} in date window {dates}")

    any_changes = False
    for week_id, week in sorted(weeks.items(), key=lambda kv: kv[1].get("order", 0)):
        changed = False
        games = week.get("games") or []
        for g in games:
            if g.get("final"):
                continue
            match = find_cfbd_match(g, cfbd_recent)
            if not match:
                continue
            try:
                away_score = cfbd_score(match, "away")
                home_score = cfbd_score(match, "home")
                away_score = int(away_score) if away_score not in (None, "") else None
                home_score = int(home_score) if home_score not in (None, "") else None
            except (TypeError, ValueError):
                continue
            is_final = cfbd_completed(match)
            if away_score is not None and g.get("awayScore") != away_score:
                g["awayScore"] = away_score; changed = True
            if home_score is not None and g.get("homeScore") != home_score:
                g["homeScore"] = home_score; changed = True
            if is_final and not g.get("final"):
                g["final"] = True; changed = True
        if changed:
            week["games"] = games
            fs_set(f"weeks/{week_id}", week)
            print(f"Updated {week_id} ({week.get('label')})")
            any_changes = True

    fs_set("meta/sync", {
        "lastSyncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "note": "auto-synced from CollegeFootballData",
    })
    print("Sync complete." if any_changes else "Sync ran, no score changes this pass.")

if __name__ == "__main__":
    main()
