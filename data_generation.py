#!/usr/bin/env python3
import json, os, pathlib, sys, math, time, requests
from dotenv import load_dotenv

load_dotenv()

KEY = os.environ["TOPDECK-API-KEY"]
DAYS = int(os.getenv("DAYS", 180))
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", 32))
MIN_SWISS = int(os.getenv("MIN_SWISS", 4))
CHUNK_DAYS = int(os.getenv("CHUNK_DAYS", 14))   # light-pass window size
TID_BATCH = int(os.getenv("TID_BATCH", 10))     # heavy-pass batch size

URL = "https://topdeck.gg/api/v2/tournaments"
HEADERS = {"Authorization": KEY, "Content-Type": "application/json"}
OUT = pathlib.Path(f"data/standings_{DAYS}d_p{MIN_PLAYERS}_swiss{MIN_SWISS}.json")
OUT.parent.mkdir(exist_ok=True)

if OUT.exists() and "--force" not in sys.argv:
    print(f"{OUT} exists, skipping API call (--force to refetch)")
    sys.exit(0)

KEEP = ["TID", "tournamentName", "swissNum", "startDate", "topCut", "standings"]


def post(body, retries=4):
    """POST with backoff on 429 / 5xx. Raises RuntimeError if it never succeeds."""
    for attempt in range(retries):
        r = requests.post(URL, headers=HEADERS, json=body, timeout=180)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30))
            print(f"  429, sleeping {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = 2 ** (attempt + 1)
            print(f"  {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"gave up after {retries} attempts")


def player_points(p):
    return int(p.get("wins") or 0) * 4 + int(p.get("draws") or 0)


def max_possible_rounds(t):
    swiss = int(t.get("swissNum") or 0)
    top_cut = int(t.get("topCut") or 0)
    if top_cut <= 0:
        return swiss
    extra = 1 if top_cut <= 4 else int(math.log2(top_cut)) - 2
    return swiss + max(0, extra)


def qualifying(t):
    """Standings rows that clear the points threshold (decklist check done separately)."""
    if (t.get("swissNum") or 0) < MIN_SWISS:
        return []
    threshold = max_possible_rounds(t) * 4 / 2
    return [p for p in (t.get("standings") or []) if player_points(p) >= threshold]


# ---------- Pass 1: light (no decklists), windowed by date ----------
now = int(time.time())
t0 = now - DAYS * 86400
light = {}
while t0 < now:
    t1 = min(t0 + CHUNK_DAYS * 86400, now)
    print(f"pass 1: {time.strftime('%Y-%m-%d', time.gmtime(t0))} -> {time.strftime('%Y-%m-%d', time.gmtime(t1))}")
    chunk = post({
        "game": "Magic: The Gathering",
        "format": "EDH",
        "start": t0,
        "end": t1,
        "participantMin": MIN_PLAYERS,
        "columns": ["wins", "draws", "losses"],
    })
    for t in chunk:
        light[t["TID"]] = t          # dict dedupes overlapping window edges
    t0 = t1 + 1

wanted = [tid for tid, t in light.items() if qualifying(t)]
print(f"pass 1: {len(light)} tournaments, {len(wanted)} have qualifying players")


# ---------- Pass 2: heavy (decklists) only for the survivors ----------
def fetch_full(tids):
    try:
        return post({"TID": tids, "columns": ["decklist", "wins", "draws", "losses"]}, retries=2)
    except RuntimeError:
        if len(tids) == 1:
            print(f"  skipping {tids[0]} (keeps failing)")
            return []
        mid = len(tids) // 2
        print(f"  batch of {len(tids)} failed, splitting")
        return fetch_full(tids[:mid]) + fetch_full(tids[mid:])


slim = []
for i in range(0, len(wanted), TID_BATCH):
    batch = wanted[i:i + TID_BATCH]
    print(f"pass 2: {i + 1}-{i + len(batch)} of {len(wanted)}")
    for t in fetch_full(batch):
        standings = [p for p in qualifying(t) if p.get("decklist") is not None]
        if not standings:
            continue
        row = {k: t.get(k) for k in KEEP}
        row["standings"] = standings
        slim.append(row)

print(f"kept {len(slim)} tournaments")
if not slim:
    sys.exit("No tournaments kept, not saving")

OUT.write_text(json.dumps(slim))
sizes = [len(t["standings"]) for t in slim]
print("qualifying players per tournament (min/max):", min(sizes), max(sizes))