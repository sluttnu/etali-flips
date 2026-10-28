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

def normalize_decklist(decklist):
    if not isinstance(decklist, str):
        return ""
    # handle both real newlines and escaped ones from JSON
    return (
        decklist
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )

def parse_decklist(decklist: str):
    decklist = normalize_decklist(decklist)
    if not decklist:
        return []

    cards = []
    for line in decklist.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("~~") or line.startswith("--"):
            continue

        m = re.match(r"^\d+\s+(.+)$", line)
        if not m:
            continue

        name = m.group(1).strip()
        if name:
            cards.append(name)

    return cards

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
    for tournament_index, tournament in enumerate(DATA):
        standings = tournament.get("standings") or []
        for player_index, player in enumerate(standings):
            decklist = player.get("decklist")
            if decklist is None:
                continue

            parsed = parse_decklist(decklist)
            if not parsed:
                continue

            decks.append({
                "tournament": tournament.get("tournamentName", "Unknown"),
                "cards": parsed,
            })

    print(f"random_deck_records -> {len(decks)} valid deck entries loaded")
    return decks

def new_game_state():
    pool = random_deck_records()
    if len(pool) < 3:
        raise ValueError("Not enough decks in dataset")

    chosen = random.sample(pool, 3)

    game = []
    for deck in chosen:
        cards = deck["cards"][:]
        random.shuffle(cards)

        # keep a random 5-card stash, no duplicates
        stash = []
        seen = set()
        while len(stash) < 5 and cards:
            c = cards.pop()
            key = card_cache_key(c)
            if key in seen:
                continue
            seen.add(key)
            stash.append(c)

        game.append({
            "tournament": deck["tournament"],
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

@app.route("/api/new-game", methods=["POST"])
def api_new_game():
    game = new_game_state()
    session["game"] = game
    return jsonify({
        "decks": [
            {
                "tournament": deck["tournament"],
                "stash": [
                    {
                        "name": c,
                        "image": get_card_image_name(c),
                    }
                    for c in deck["stash"]
                ],
                "revealed": deck["revealed"],
            }
            for deck in game
        ]
    })

@app.route("/api/reveal", methods=["POST"])

@app.route("/cardback.jpg")
def cardback():
    return send_from_directory("cardback.jpg")

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
