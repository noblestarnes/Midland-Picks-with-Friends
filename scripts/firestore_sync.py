#!/usr/bin/env python3
"""
Syncs live ESPN college-football scores into the Midland Picks Firestore
project (midland-picks-with-friends), used by the public GitHub Pages site.

Stdlib only (urllib) so it runs anywhere with no pip installs.
"""
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

PROJECT_ID = "midland-picks-with-friends"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"

# Short codes used in the dashboard -> things ESPN might call the team
# (abbreviation and/or a distinctive substring of the full name).
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

def _http(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
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

# ---------------- ESPN ----------------

def espn_scoreboard(yyyymmdd):
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates={yyyymmdd}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def espn_games_for_dates(dates):
    """dates: list of 'YYYYMMDD' strings. Returns list of parsed games."""
    games = []
    for d in dates:
        try:
            data = espn_scoreboard(d)
        except Exception as e:
            print(f"  (ESPN fetch failed for {d}: {e})", file=sys.stderr)
            continue
        for ev in data.get("events", []):
            try:
                comp = ev["competitions"][0]
                status_name = comp.get("status", ev.get("status", {})).get("type", {}).get("name", "")
                teams = {}
                for c in comp["competitors"]:
                    teams[c["homeAway"]] = {
                        "name": c["team"].get("displayName", ""),
                        "abbr": c["team"].get("abbreviation", ""),
                        "score": c.get("score"),
                    }
                games.append({
                    "date": d, "status": status_name,
                    "home": teams.get("home"), "away": teams.get("away"),
                })
            except Exception:
                continue
    return games

def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

def matches_alias(espn_team, short_code):
    aliases = TEAM_ALIASES.get(short_code, [short_code])
    name_n = _norm(espn_team["name"])
    abbr_n = _norm(espn_team["abbr"])
    for a in aliases:
        an = _norm(a)
        if an == abbr_n or an in name_n or name_n in an:
            return True
    return False

def find_espn_match(dashboard_game, espn_games):
    for eg in espn_games:
        if eg["home"] and eg["away"] and \
           matches_alias(eg["home"], dashboard_game["home"]) and \
           matches_alias(eg["away"], dashboard_game["away"]):
            return eg
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
    espn_games = espn_games_for_dates(dates)
    print(f"Fetched {len(espn_games)} ESPN games across dates {dates}")

    any_changes = False
    for week_id, week in sorted(weeks.items(), key=lambda kv: kv[1].get("order", 0)):
        changed = False
        games = week.get("games") or []
        for g in games:
            if g.get("final"):
                continue
            match = find_espn_match(g, espn_games)
            if not match:
                continue
            try:
                away_score = int(match["away"]["score"]) if match["away"]["score"] not in (None, "") else None
                home_score = int(match["home"]["score"]) if match["home"]["score"] not in (None, "") else None
            except (TypeError, ValueError):
                continue
            is_final = match["status"] == "STATUS_FINAL"
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
        "note": "auto-synced from ESPN",
    })
    print("Sync complete." if any_changes else "Sync ran, no score changes this pass.")

if __name__ == "__main__":
    main()
