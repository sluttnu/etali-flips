#!/usr/bin/env python3
import json, os, pathlib, sys, math, requests
from dotenv import load_dotenv

load_dotenv()

KEY = os.environ["TOPDECK-API-KEY"]
DAYS = int(os.getenv("DAYS", 180))
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", 32))
MIN_SWISS = int(os.getenv("MIN_SWISS", 4))

URL = "https://topdeck.gg/api/v2/tournaments"
OUT = pathlib.Path(f"data/standings_{DAYS}d_p{MIN_PLAYERS}_swiss{MIN_SWISS}.json")
OUT.parent.mkdir(exist_ok=True)

if OUT.exists() and "--force" not in sys.argv:
    print(f"{OUT} exists, skipping API call (--force to refetch)")
    sys.exit(0)

keep = ["TID", "tournamentName", "swissNum", "startDate", "topCut", "standings"]
body = {
    "game": "Magic: The Gathering",
    "format": "EDH",
    "last": DAYS,
    "participantMin": MIN_PLAYERS,
}
print("request body:", body)

r = requests.post(URL, headers={"Authorization": KEY, "Content-Type": "application/json"},
                  json=body, timeout=180)
print("status:", r.status_code, "| bytes:", len(r.content))
pathlib.Path(str(OUT) + ".raw").write_bytes(r.content)
r.raise_for_status()

def player_points(p):
    wins = int(p.get("wins") or 0)
    draws = int(p.get("draws") or 0)
    return (wins * 4) + draws

def max_possible_rounds(t):
    swiss = int(t.get("swissNum") or 0)
    top_cut = int(t.get("topCut") or 0)

    if top_cut <= 0:
        return swiss

    # Matches your tournament convention:
    # topCut 4 = 1 extra round
    # topCut 16 = 2 extra rounds
    # topCut 32 = 3 extra rounds
    if top_cut <= 4:
        extra = 1
    else:
        extra = int(math.log2(top_cut)) - 2

    return swiss + max(0, extra)

data = r.json()
slim = []

for t in data:
    if (t.get("swissNum") or 0) < MIN_SWISS:
        continue

    max_rounds = max_possible_rounds(t)
    max_points = max_rounds * 4

    standings = []
    for p in t.get("standings") or []:
        if p.get("decklist") is None:
            continue

        score = player_points(p)
        if score >= (max_points / 2):
            standings.append(p)

    if not standings:
        continue

    row = {k: t.get(k) for k in keep}
    row["standings"] = standings
    slim.append(row)

print(f"fetched {len(data)} tournaments, kept {len(slim)} with swissNum >= {MIN_SWISS}")

if not slim:
    sys.exit("No tournaments returned, not saving (check the request body above)")

OUT.write_text(json.dumps(slim))
sizes = [len(t["standings"] or []) for t in slim]
print("players per tournament (min/max):", min(sizes), max(sizes))
