#!/usr/bin/env python3
import json, os, pathlib, sys, math, shutil, time, requests
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

KEY = os.environ["TOPDECK-API-KEY"]
DAYS = int(os.getenv("DAYS", 180))
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", 32))
MIN_SWISS = int(os.getenv("MIN_SWISS", 4))
CHUNK_DAYS = int(os.getenv("CHUNK_DAYS", 14))        # light-pass window size
TID_BATCH = int(os.getenv("TID_BATCH", 10))          # heavy-pass batch size
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", 3))   # re-check this far behind newest stored date

URL = "https://topdeck.gg/api/v2/tournaments"
HEADERS = {"Authorization": KEY, "Content-Type": "application/json"}

# Same filename convention as before, so the app keeps working unchanged.
OUT = pathlib.Path(f"data/standings_{DAYS}d_p{MIN_PLAYERS}_swiss{MIN_SWISS}.json")
OUT.parent.mkdir(exist_ok=True)

# Full names ("A // B") of double-faced cards that have appeared as a commander.
# Used to fix decks that only registered the front half ("A").
DFC_FILE = pathlib.Path("data/dfc_commanders.json")

KEEP = ["TID", "tournamentName", "swissNum", "startDate", "topCut", "standings"]

# --force ignores the existing file and rebuilds the whole window from the API.
FORCE = "--force" in sys.argv


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
    # topCut 4 = 1 extra round, 16 = 2, 32 = 3
    extra = 1 if top_cut <= 4 else int(math.log2(top_cut)) - 2
    return swiss + max(0, extra)


def qualifying(t):
    """Standings rows that clear the points threshold (decklist checked separately)."""
    if (t.get("swissNum") or 0) < MIN_SWISS:
        return []
    threshold = max_possible_rounds(t) * 4 / 2
    return [p for p in (t.get("standings") or []) if player_points(p) >= threshold]


def fetch_full(tids):
    """Heavy fetch (with decklists) for specific TIDs; splits the batch on repeated failure."""
    try:
        return post({"TID": tids, "columns": ["decklist", "wins", "draws", "losses"]}, retries=2)
    except RuntimeError:
        if len(tids) == 1:
            print(f"  skipping {tids[0]} (keeps failing, will retry next run)")
            return []
        mid = len(tids) // 2
        print(f"  batch of {len(tids)} failed, splitting")
        return fetch_full(tids[:mid]) + fetch_full(tids[mid:])


def load_store():
    if FORCE or not OUT.exists():
        return {}
    try:
        rows = json.loads(OUT.read_text())
        return {t["TID"]: t for t in rows}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # Don't clobber a file we can't read. Fix it or rerun with --force.
        sys.exit(f"{OUT} exists but couldn't be parsed ({e}). Not touching it. Use --force to rebuild.")


def load_dfc():
    """The persisted set of full double-faced commander names."""
    try:
        return set(json.loads(DFC_FILE.read_text()))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, TypeError) as e:
        # Derived data: safe to rebuild from the stored decks.
        print(f"{DFC_FILE} unreadable ({e}); rebuilding it from the stored decks")
        return set()


def save_dfc(dfc):
    tmp = DFC_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(dfc), indent=2, ensure_ascii=False))
    os.replace(tmp, DFC_FILE)


def normalize_commanders(rows, dfc):
    """Learn full "A // B" commander names, then rewrite decks that registered
    only the front half ("A") to the full name. Mutates rows and dfc in place.
    Returns how many decks were changed."""

    def commanders_of(p):
        return (p.get("deckObj") or {}).get("Commanders")

    # 1) Learn every full name seen as a commander.
    for t in rows:
        for p in t.get("standings") or []:
            for name in commanders_of(p) or {}:
                if " // " in name:
                    dfc.add(name)

    # 2) Front half -> full name. A front half claimed by more than one full
    #    name is ambiguous, so those decks are left alone.
    claims = defaultdict(set)
    for full in dfc:
        claims[full.split(" // ")[0].strip()].add(full)
    full_for = {front: next(iter(fulls)) for front, fulls in claims.items() if len(fulls) == 1}

    # 3) Rewrite every stored deck, not just new ones, so older rows get fixed too.
    changed = 0
    for t in rows:
        for p in t.get("standings") or []:
            cmds = commanders_of(p)
            if not cmds or not any(n in full_for for n in cmds):
                continue
            p["deckObj"]["Commanders"] = {full_for.get(n, n): info for n, info in cmds.items()}
            changed += 1
    return changed


def main():
    now = int(time.time())
    cutoff = now - DAYS * 86400

    store = load_store()
    loaded = len(store)

    # One-time safety copy of the big file you don't want to refetch.
    bak = OUT.with_suffix(".json.bak")
    if OUT.exists() and not bak.exists() and not FORCE:
        shutil.copy2(OUT, bak)
        print(f"backup written: {bak}")

    # Prune days that fell out of the window.
    store = {k: t for k, t in store.items() if (t.get("startDate") or 0) >= cutoff}
    pruned = loaded - len(store)

    # Cursor: newest stored date, minus lookback so late-finishing events get picked up.
    newest = max((t["startDate"] for t in store.values() if t.get("startDate")), default=None)
    since = max(cutoff, newest - LOOKBACK_DAYS * 86400) if newest else cutoff
    print(f"window since {time.strftime('%Y-%m-%d', time.gmtime(since))} "
          f"({'incremental' if newest else 'full backfill'}); "
          f"{loaded} loaded, {pruned} pruned, {len(store)} kept")

    # Pass 1: light (no decklists), windowed by date.
    light = {}
    t0 = since
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
            light[t["TID"]] = t  # dict dedupes overlapping window edges
        t0 = t1 + 1

    wanted = [tid for tid, t in light.items() if qualifying(t) and tid not in store]
    print(f"pass 1: {len(light)} in window, {len(wanted)} new with qualifying players")

    # Pass 2: heavy, only for tournaments we don't already have.
    added = 0
    for i in range(0, len(wanted), TID_BATCH):
        batch = wanted[i:i + TID_BATCH]
        print(f"pass 2: {i + 1}-{i + len(batch)} of {len(wanted)}")
        for t in fetch_full(batch):
            standings = [p for p in qualifying(t) if p.get("decklist") is not None]
            if not standings:
                continue
            row = {k: t.get(k) for k in KEEP}
            row["standings"] = standings
            store[t["TID"]] = row
            added += 1

    if not store:
        sys.exit("Store is empty after sync, not saving (check parameters / API).")

    # Lump front-half-only commander names in with their full "A // B" name.
    dfc = load_dfc()
    dfc_before = set(dfc)
    fixed = normalize_commanders(list(store.values()), dfc)
    if fixed:
        print(f"normalized {fixed} deck(s) to full double-faced commander names")
    if dfc != dfc_before or not DFC_FILE.exists():
        save_dfc(dfc)
        print(f"{DFC_FILE}: {len(dfc)} double-faced commander name(s)")

    if added == 0 and pruned == 0 and fixed == 0 and not FORCE:
        print("nothing changed, leaving file as is")
        return

    # Atomic write: temp file in the same dir, then replace.
    rows = sorted(store.values(), key=lambda t: t.get("startDate") or 0)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, OUT)

    sizes = [len(t["standings"]) for t in rows]
    print(f"saved {OUT}: {len(rows)} tournaments (+{added} added, -{pruned} pruned, {fixed} commander names normalized)")
    print("qualifying players per tournament (min/max):", min(sizes), max(sizes))


if __name__ == "__main__":
    main()