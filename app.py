import json, os, random, re, threading, pathlib, time
from urllib.parse import quote, quote_plus
from flask import Flask, jsonify, render_template, request, session, send_from_directory

import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

DATA_DIR = pathlib.Path("data")
CARD_CACHE = {}

def dataset_path():
    days = int(os.getenv("DAYS", 180))
    min_players = int(os.getenv("MIN_PLAYERS", 32))
    min_swiss = int(os.getenv("MIN_SWISS", 4))
    return DATA_DIR / f"standings_{days}d_p{min_players}_swiss{min_swiss}.json"

def load_dataset():
    path = dataset_path()
    if not path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")

    with path.open() as f:
        return json.load(f)

DATA = load_dataset()

def deck_from_player(player):
    """Return (commanders, library_cards) from a standings entry, or None."""
    deck_obj = player.get("deckObj")
    if not isinstance(deck_obj, dict):
        return None

    commanders = list((deck_obj.get("Commanders") or {}).keys())

    cards = []
    for name, info in (deck_obj.get("Mainboard") or {}).items():
        cards.extend([name] * int((info or {}).get("count", 1)))

    return commanders, cards

def normalize_card_name(name):
    return quote_plus(name.strip())

def card_cache_key(name):
    return normalize_card_name(name).lower()

def get_card_image_name(name):
    key = card_cache_key(name)
    if key in CARD_CACHE:
        return CARD_CACHE[key]

    exact = quote(name)
    image_url = f"https://api.scryfall.com/cards/named?exact={exact}&format=image"

    if not image_url:
        raise ValueError(f"Could not find art for {name}")

    CARD_CACHE[key] = image_url
    return image_url

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
                "cards": cards,
            })

    print(f"random_deck_records -> {len(decks)} valid deck entries loaded")
    return decks

DECKS = random_deck_records()

def commander_counts():
    counts = {}
    for d in DECKS:
        for name in d["commanders"]:
            counts[name] = counts.get(name, 0) + 1
    return sorted(
        ({"name": n, "decks": c} for n, c in counts.items()),
        key=lambda x: x["name"].lower(),
    )

def pick_decks(choices):
    """choices: up to 3 commander names, "" meaning a fully random deck."""
    chosen, used = [], set()
    for i in range(3):
        wanted = choices[i] if i < len(choices) else ""
        candidates = [
            k for k, d in enumerate(DECKS)
            if k not in used and (not wanted or wanted in d["commanders"])
        ]
        if not candidates:
            raise ValueError(f"No deck available for {wanted or 'a random slot'}")
        k = random.choice(candidates)
        used.add(k)
        chosen.append(DECKS[k])
    return chosen

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

    # prefetch art for initial stash, background style
    for deck in game:
        for card_name in deck["stash"]:
            try:
                get_card_image_name(card_name)
            except Exception:
                pass

    return game

def reveal_next_card(game, deck_index):
    deck = game[deck_index]
    if not deck["stash"]:
        return None

    card_name = deck["stash"].pop(0)
    deck["revealed"].append(card_name)

    # refill stash to 5 cards once a card is revealed
    while len(deck["stash"]) < 5 and deck["remaining"]:
        next_name = deck["remaining"].pop(0)
        deck["stash"].append(next_name)

    # also prefetch art for the newly-added replacement card
    if deck["stash"]:
        newest = deck["stash"][-1]
        try:
            get_card_image_name(newest)
        except Exception:
            pass

    return {
        "card": card_name,
        "image": get_card_image_name(card_name),
        "deck_index": deck_index,
        "revealed": deck["revealed"],
        "stash": deck["stash"],
    }

@app.route("/")
def index():
    return render_template("index.html")

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
    return send_from_directory(".","cardback.jpg")

@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    deck_index = int(request.json["deck_index"])
    game = session.get("game")
    if not game:
        return jsonify({"error": "No game in session"}), 400

    result = reveal_next_card(game, deck_index)
    session["game"] = game
    return jsonify(result)

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False
    )
