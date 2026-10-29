import json, os, random, threading, pathlib, time
from collections import Counter

import requests
from flask import Flask, jsonify, render_template, request, session, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

DATA_DIR = pathlib.Path("data")
ETALI = "Etali, Primal Conqueror"

# Hostnames (first label) that get the standalone average-deck site.
STANDALONE_HOSTS = {
    h.strip().lower()
    for h in os.getenv("STANDALONE_HOSTS", "average-deck").split(",")
    if h.strip()
}


def is_standalone():
    host = request.host.split(":")[0].lower()
    return host.split(".")[0] in STANDALONE_HOSTS


# --------------------------------------------------------------------------
# Dataset (tournament standings pulled by data_generation.py)
# --------------------------------------------------------------------------

def dataset_settings():
    return {
        "days": int(os.getenv("DAYS", 180)),
        "min_players": int(os.getenv("MIN_PLAYERS", 32)),
        "min_swiss": int(os.getenv("MIN_SWISS", 4)),
    }


def dataset_path():
    s = dataset_settings()
    return DATA_DIR / f"standings_{s['days']}d_p{s['min_players']}_swiss{s['min_swiss']}.json"


def load_dataset():
    path = dataset_path()
    if not path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")
    with path.open() as f:
        return json.load(f)


DATA = load_dataset()


def commander_key(commanders):
    """Canonical name for a commander combination, e.g. 'Kraum... + Tymna...'."""
    return " + ".join(sorted(commanders, key=str.lower))


def deck_from_player(player):
    """Return (commanders, library_cards) from a standings entry, or None."""
    deck_obj = player.get("deckObj")
    if not isinstance(deck_obj, dict):
        return None

    commanders = sorted(
        commanders,
        key=lambda c: (-c["decks"], c["name"].lower())
    )

    cards = []
    for name, info in (deck_obj.get("Mainboard") or {}).items():
        cards.extend([name] * int((info or {}).get("count", 1)))

    return commanders, cards


def random_deck_records():
    decks = []
    for tournament in DATA:
        for player in tournament.get("standings") or []:
            parsed = deck_from_player(player)
            if not parsed:
                continue
            commanders, cards = parsed
            if not cards:
                continue
            decks.append({
                "tournament": tournament.get("tournamentName", "Unknown"),
                "commanders": commanders,
                "commander_key": commander_key(commanders),
                "cards": cards,
            })

    print(f"random_deck_records -> {len(decks)} valid deck entries loaded")
    return decks


DECKS = random_deck_records()
KEY_COUNTS = Counter(d["commander_key"] for d in DECKS if d["commander_key"])


# --------------------------------------------------------------------------
# Scryfall card info (image URL + front-face type), cached on disk
# --------------------------------------------------------------------------

INFO_FILE = DATA_DIR / "card_info.json"
CARD_INFO = json.loads(INFO_FILE.read_text()) if INFO_FILE.exists() else {}
MISSING = set()          # names Scryfall couldn't match (not persisted)
SCRY_LOCK = threading.Lock()
SCRY_HEADERS = {
    "User-Agent": "etali-flips/1.0 (personal playtesting tool)",
    "Accept": "application/json",
}
_last_call = 0.0


def image_key(name):
    return name.strip().lower()


def _scry_post(identifiers):
    """One rate-limited call to /cards/collection (max 75 identifiers)."""
    global _last_call
    for _ in range(4):
        wait = 0.12 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        r = requests.post(
            "https://api.scryfall.com/cards/collection",
            json={"identifiers": identifiers},
            headers=SCRY_HEADERS,
            timeout=20,
        )
        _last_call = time.time()
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Scryfall kept rate limiting us")


def _card_image(card):
    uris = card.get("image_uris") or (
        (card.get("card_faces") or [{}])[0].get("image_uris")
    ) or {}
    return uris.get("normal")


def ensure_images(names):
    """Resolve image URL + front-face type for uncached names, 75 at a time."""
    todo = sorted({n for n in names
                   if image_key(n) not in CARD_INFO and n not in MISSING})
    for i in range(0, len(todo), 75):
        with SCRY_LOCK:
            # Re-check inside the lock: another thread may have filled these in.
            batch = [n for n in todo[i:i + 75]
                     if image_key(n) not in CARD_INFO and n not in MISSING]
            if not batch:
                continue
            try:
                data = _scry_post([{"name": n} for n in batch])
            except Exception as e:
                print("scryfall batch failed:", e)
                continue   # not marked missing, so it gets retried later

            found = {}
            for card in data.get("data", []):
                url = _card_image(card)
                if url:
                    info = {
                        "image": url,
                        "type": card.get("type_line", "").split(" // ")[0],
                    }
                    full = card["name"].lower()
                    found[full] = info
                    found[full.split(" // ")[0]] = info   # front-face lookups

            for n in batch:
                k = image_key(n)
                info = found.get(k) or found.get(k.split(" // ")[0])
                if info:
                    CARD_INFO[k] = info
                else:
                    MISSING.add(n)

            INFO_FILE.write_text(json.dumps(CARD_INFO))


def card_info(name):
    info = CARD_INFO.get(image_key(name))
    if not info:
        ensure_images([name])
        info = CARD_INFO.get(image_key(name))
    return info or {}


def get_card_image_name(name):
    return card_info(name).get("image", "")


def is_land(name):
    return "Land" in card_info(name).get("type", "")


def backfill_images():
    names = {n for d in DECKS for n in d["cards"]}
    print(f"backfilling images for {len(names)} cards")
    ensure_images(names)
    print("image backfill done")


threading.Thread(target=backfill_images, daemon=True).start()


# --------------------------------------------------------------------------
# Deck selection, average deck, game state
# --------------------------------------------------------------------------

def normalize_query(q):
    """'B + A' and 'A + B' name the same combination; compare case-insensitively."""
    parts = [p.strip() for p in q.split("+") if p.strip()]
    return " + ".join(sorted(parts, key=str.lower)).lower()


def match_keys(query):
    """Commander combinations matching a query: an exact match wins, else substring."""
    q = normalize_query(query)
    if not q:
        return []
    exact = [k for k in KEY_COUNTS if k.lower() == q]
    if exact:
        return exact
    return sorted((k for k in KEY_COUNTS if q in k.lower()), key=str.lower)


def commander_counts():
    return sorted(
        ({"name": k, "decks": n} for k, n in KEY_COUNTS.items()),
        key=lambda x: x["name"].lower(),
    )


def pick_decks(choices):
    """choices: up to 3 commander names/combinations, "" meaning a fully random deck."""
    chosen, used = [], set()
    for i in range(3):
        wanted = (choices[i] if i < len(choices) else "").strip()
        keys = set(match_keys(wanted)) if wanted else None
        candidates = [
            k for k, d in enumerate(DECKS)
            if k not in used and (keys is None or d["commander_key"] in keys)
        ]
        if not candidates:
            raise ValueError(f"No deck available for {wanted or 'a random slot'}")
        k = random.choice(candidates)
        used.add(k)
        chosen.append(DECKS[k])
    return chosen


def average_deck(query):
    keys = match_keys(query)
    if len(keys) > 1:
        # Ambiguous: let the caller show the options instead of blending them.
        return {
            "commander": query, "deck_count": 0, "cards": [],
            "choices": [{"name": k, "decks": KEY_COUNTS[k]} for k in keys],
        }

    decks = [d for d in DECKS if keys and d["commander_key"] == keys[0]]
    n = len(decks)

    present, copies = Counter(), Counter()
    for d in decks:
        for name, k in Counter(d["cards"]).items():
            present[name] += 1
            copies[name] += k

    ensure_images(list(present))

    cards = [
        {
            "name": name,
            "inclusion": present[name] / n,
            "decks": present[name],
            "avg_copies": copies[name] / present[name],
            "image": get_card_image_name(name),
            "land": is_land(name),
        }
        for name in present
    ]
    # Inclusion first; among equal inclusion, nonlands before lands.
    cards.sort(key=lambda c: (-c["inclusion"], c["land"], c["name"]))
    return {"commander": keys[0] if keys else query, "deck_count": n, "cards": cards}


def new_game_state(choices=()):
    chosen = pick_decks(choices)
    game = []
    for deck in chosen:
        cards = deck["cards"][:]
        random.shuffle(cards)

        # No dedupe: with counts expanded, skipping "duplicates" would
        # silently delete basic lands from the library.
        stash = [cards.pop() for _ in range(min(5, len(cards)))]

        game.append({
            "tournament": deck["tournament"],
            "commanders": deck["commanders"],
            "remaining": cards,
            "stash": stash,
            "revealed": [],
        })

    ensure_images([c for deck in game for c in deck["stash"]])
    return game


def reveal_next_card(game, deck_index):
    deck = game[deck_index]
    if not deck["stash"]:
        return None

    card_name = deck["stash"].pop(0)
    deck["revealed"].append(card_name)

    # refill stash to 5 cards once a card is revealed
    while len(deck["stash"]) < 5 and deck["remaining"]:
        deck["stash"].append(deck["remaining"].pop(0))

    # make sure art exists for the newly added card
    if deck["stash"]:
        get_card_image_name(deck["stash"][-1])

    return {
        "card": card_name,
        "image": get_card_image_name(card_name),
        "deck_index": deck_index,
        "revealed": deck["revealed"],
        "stash": deck["stash"],
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    if is_standalone():
        return average_deck_page()
    return render_template("index.html")


@app.route("/average-deck")
def average_deck_page():
    standalone = is_standalone()
    return render_template(
        "average_deck.html",
        standalone=standalone,
        default_commander="" if standalone else ETALI,
    )


@app.route("/api/average-deck")
def api_average_deck():
    query = request.args.get("commander", "").strip()
    if not query:
        return jsonify({"error": "Pass ?commander=<name>"}), 400
    data = average_deck(query)
    if data.get("choices"):
        return jsonify({
            "error": f'"{query}" matches several commander combinations. Pick one:',
            "choices": data["choices"],
        }), 400
    if not data["deck_count"]:
        return jsonify({"error": f"No decks found for {query}"}), 404
    return jsonify(data)


@app.route("/api/dataset-info")
def api_dataset_info():
    return jsonify({**dataset_settings(), "tournaments": len(DATA), "decks": len(DECKS)})


@app.route("/api/commanders")
def api_commanders():
    return jsonify(commander_counts())


@app.route("/api/new-game", methods=["POST"])
def api_new_game():
    body = request.get_json(silent=True) or {}
    choices = [c if isinstance(c, str) else "" for c in body.get("commanders", [])]
    try:
        game = new_game_state(choices)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    session["game"] = game
    return jsonify({
        "decks": [
            {
                "tournament": deck["tournament"],
                "commanders": deck["commanders"],
                "stash": [
                    {"name": c, "image": get_card_image_name(c)}
                    for c in deck["stash"]
                ],
                "revealed": deck["revealed"],
            }
            for deck in game
        ]
    })


@app.route("/cardback.jpg")
def cardback():
    # Serve from the app folder, or from templates/ if the file lives there.
    for folder in (".", "templates"):
        if (pathlib.Path(folder) / "cardback.jpg").exists():
            return send_from_directory(folder, "cardback.jpg")
    return ("cardback.jpg not found", 404)


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    game = session.get("game")
    if not game:
        return jsonify({"error": "No game in session"}), 400

    deck_index = int((request.get_json(silent=True) or {}).get("deck_index", -1))
    if not 0 <= deck_index < len(game):
        return jsonify({"error": "Bad deck index"}), 400

    result = reveal_next_card(game, deck_index)
    session["game"] = game
    return jsonify(result)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )