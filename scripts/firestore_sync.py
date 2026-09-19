#!/usr/bin/env python3
"""
Syncs live college-football scores into the Midland Picks Firestore project
(midland-picks-with-friends), used by the public GitHub Pages site.

Score source: CollegeFootballData.com (CFBD), not ESPN. ESPN's public
scoreboard endpoint started outright blocking requests from GitHub Actions'
IP ranges with a blanket 403 (confirmed even after sending full browser-style
headers), which is a known issue for CI systems calling ESPN's unofficial
API. CFBD is a real API meant for exactly this kind of programmatic use.

IMPORTANT: real-time, in-progress scores are a CFBD Patreon-tier feature
(their free tier only has final/historical results — confirmed by testing:
a Thursday night game produced zero score updates all game, only the final
box score would have come through afterward). This script calls CFBD's
"live scoreboard" endpoint for in-game scores, which requires at least
Tier 1 ($1/mo, https://collegefootballdata.com/api-tiers). It also still
calls the plain /games endpoint as a backstop, so final scores keep coming
through even on the free tier if you ever drop the subscription.

Stdlib only (urllib) so it runs anywhere with no pip installs.

SETUP: get an API key at https://collegefootballdata.com/key, subscribe to
at least CFBD's Patreon Tier 1 for live scores, then add the key as a
GitHub repo secret named CFBD_API_KEY (Settings -> Secrets and variables ->
Actions -> New repository secret). The workflow file passes it in as an
environment variable — never hardcode the key in this file, since this
repo is public.
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

def _cfbd_get(path, params):
    if not CFBD_API_KEY:
        raise RuntimeError(
            "CFBD_API_KEY is not set. Get a key at "
            "https://collegefootballdata.com/key, subscribe to at least "
            "Tier 1 on their Patreon for live scores, and add the key as a "
            "GitHub repo secret named CFBD_API_KEY."
        )
    qs = urllib.parse.urlencode(params)
    url = f"{CFBD_BASE}{path}?{qs}"
    headers = {"Authorization": f"Bearer {CFBD_API_KEY}", "Accept": "application/json"}
    return _http(url, headers=headers)

def cfbd_scoreboard():
    """Live, real-time games (in-progress, and recently finished) — this is
    the Patreon-gated endpoint (Tier 1+). Returns [] without raising if the
    key isn't entitled to it, so the /games backstop below still runs."""
    try:
        data = _cfbd_get("/scoreboard", {"classification": "fbs"})
        return data if isinstance(data, list) else []
    except urllib.error.HTTPError as e:
        print(f"  (live /scoreboard fetch failed: HTTP {e.code} — "
              f"is the CFBD key subscribed to at least Tier 1 for live data?)", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  (live /scoreboard fetch failed: {e})", file=sys.stderr)
        return []

def cfbd_games(year):
    """Backstop, works on the free tier: every FBS game (regular season +
    postseason/bowls) for the season. seasonType=both covers bowl games
    automatically, so no extra cron windows are needed in December like the
    old ESPN setup would have required. Only carries FINAL scores reliably —
    see cfbd_scoreboard() above for in-progress scores."""
    data = _cfbd_get("/games", {"year": year, "seasonType": "both"})
    return data if isinstance(data, list) else []

def games_in_date_window(all_games, dates):
    """dates: list of 'YYYYMMDD' strings. Filters a season-long game list
    down to just the ones starting on one of those UTC calendar days."""
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

# CFBD's endpoints don't all share one schema: /games is flat snake_case
# (home_team, home_points, completed); /scoreboard nests each side under
# homeTeam/awayTeam objects (name/points/status can vary by field name too).
# These helpers dig through every shape seen in the wild instead of assuming
# one, since the live endpoint's exact response can't be tested until a
# subscribed key runs this for real.
def _team_name(cg, side):
    nested = cg.get(f"{side}Team") or cg.get(f"{side}_team")
    if isinstance(nested, dict):
        return nested.get("name") or nested.get("school") or nested.get("displayName")
    if isinstance(nested, str):
        return nested
    return None

def _team_points(cg, side):
    nested = cg.get(f"{side}Team") or cg.get(f"{side}_team")
    if isinstance(nested, dict):
        for key in ("points", "score"):
            if key in nested:
                return nested[key]
    for key in (f"{side}_points", f"{side}Points"):
        if key in cg:
            return cg[key]
    return None

def cfbd_completed(cg):
    if "completed" in cg:
        return bool(cg["completed"])
    status = str(cg.get("status") or "").lower()
    return status in ("completed", "final", "finished")

def find_cfbd_match(dashboard_game, cfbd_games_list):
    for cg in cfbd_games_list:
        cg_home, cg_away = _team_name(cg, "home"), _team_name(cg, "away")
        if cg_home and cg_away and \
           matches_alias(cg_home, dashboard_game["home"]) and \
           matches_alias(cg_away, dashboard_game["away"]):
            return cg
    return None

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

    live_games = cfbd_scoreboard()
    print(f"Fetched {len(live_games)} games from the live /scoreboard endpoint")
    if live_games:
        sample = live_games[0]
        print(f"  (sample live game keys: {sorted(sample.keys())})")
        print(f"  (sample homeTeam contents: {sample.get('homeTeam')})")
        print(f"  (sample awayTeam contents: {sample.get('awayTeam')})")
        print("  (all live game matchups: " +
              ", ".join(f"{_team_name(cg,'away')} @ {_team_name(cg,'home')}" for cg in live_games) + ")")

    try:
        all_games = cfbd_games(year)
    except Exception as e:
        print(f"CollegeFootballData /games fetch failed: {e}", file=sys.stderr)
        all_games = []
    cfbd_recent = games_in_date_window(all_games, dates)
    print(f"Fetched {len(all_games)} total CFBD games for {year}, {len(cfbd_recent)} in date window {dates}")

    # Live scores take priority (checked first below); /games is the
    # free-tier-safe backstop that still catches final scores.
    combined = live_games + cfbd_recent

    any_changes = False
    for week_id, week in sorted(weeks.items(), key=lambda kv: kv[1].get("order", 0)):
        changed = False
        games = week.get("games") or []
        for g in games:
            if g.get("final"):
                continue
            match = find_cfbd_match(g, combined)
            if not match:
                print(f"  [{week_id}] no CFBD match for '{g.get('away')} @ {g.get('home')}'")
                continue
            try:
                away_score = _team_points(match, "away")
                home_score = _team_points(match, "home")
                away_score = int(away_score) if away_score not in (None, "") else None
                home_score = int(home_score) if home_score not in (None, "") else None
            except (TypeError, ValueError) as e:
                print(f"  [{week_id}] matched '{g.get('away')} @ {g.get('home')}' but couldn't parse "
                      f"scores from it ({e}); raw match: {match}")
                continue
            is_final = cfbd_completed(match)
            print(f"  [{week_id}] matched '{g.get('away')} @ {g.get('home')}' -> "
                  f"away={away_score} home={home_score} final={is_final} "
                  f"(dashboard currently: away={g.get('awayScore')} home={g.get('homeScore')} final={g.get('final')})")
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
