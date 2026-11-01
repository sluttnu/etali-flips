const grid = document.getElementById("grid");
const DEFAULT_COMMANDER = {{ default_commander|tojson }};
const commander = (new URLSearchParams(location.search).get("commander")
                    || DEFAULT_COMMANDER).trim();

const state = {
data: null,
selectedCards: [],
excludedCards: [],
maxAgeDays: null,
hideLands: false,
};

function pctLabel(x) {
const p = x * 100;
return p > 0 && p < 1 ? "<1%" : Math.round(p) + "%";
}

const SCALE = [
[0,   [30, 58, 95]],     // dim blue
[0.5, [160, 96, 122]],   // purple midpoint
[1,   [255, 166, 61]],   // bright orange
];
const NO_DATA = "#3f4247";

function scaleColor(t) {
t = Math.max(0, Math.min(1, t));
for (let i = 1; i < SCALE.length; i++) {
    if (t <= SCALE[i][0]) {
    const [t0, c0] = SCALE[i - 1];
    const [t1, c1] = SCALE[i];
    const f = (t - t0) / (t1 - t0);
    return `rgb(${c0.map((v, k) => Math.round(v + (c1[k] - v) * f)).join(",")})`;
    }
}
}

function stripGradient(bins, totals) {
const n = totals.length;
const stops = [];
for (let i = 0; i < n; i++) {
    const color = totals[i] ? scaleColor((bins ? bins[i] : 0) / totals[i]) : NO_DATA;
    stops.push(`${color} ${(i / n * 100).toFixed(3)}% ${((i + 1) / n * 100).toFixed(3)}%`);
}
return `linear-gradient(to right, ${stops.join(",")})`;
}

function binLabel(axis, i) {
const w = (axis.end - axis.start) / axis.n;
const f = ts => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
return `${f(axis.start + i * w)} – ${f(axis.start + (i + 1) * w)}`;
}

function buildAxis() {
const end = Date.now() / 1000;
const days = state.maxAgeDays == null ? (state.data.days || 365) : state.maxAgeDays;
const start = end - days * DAY;
const n = Math.min(52, Math.max(12, Math.round(days / 7)));
return { start, end, n };
}

const DAY = 86400;

function cutoffTs() {
return state.maxAgeDays == null ? null : Date.now() / 1000 - state.maxAgeDays * DAY;
}

function updateAgeLabel() {
document.getElementById("age-label").textContent =
    state.maxAgeDays == null ? "all time" : `last ${state.maxAgeDays} days`;
}

const ageInput = document.getElementById("age");
ageInput.addEventListener("input", () => {
const v = Number(ageInput.value);
state.maxAgeDays = v >= Number(ageInput.max) ? null : v;
updateAgeLabel();
renderCurrentDeckView();
});

function updateLandToggleButton() {
const btn = document.getElementById("toggle-lands");
if (!btn) return;
btn.textContent = state.hideLands ? "Show lands" : "Hide lands";
btn.setAttribute("aria-pressed", String(state.hideLands));
}

document.getElementById("toggle-lands").addEventListener("click", () => {
state.hideLands = !state.hideLands;
updateLandToggleButton();
renderCurrentDeckView();
});

function buildFilteredCards() {
const deckIndexes = [...getCurrentDeckSet()];
const totalMatches = deckIndexes.length;

const axis = buildAxis();
axis.totals = new Array(axis.n).fill(0);   // decks per bin (all cards)

const deckCounts = new Map();   // card -> decks containing it
const copyCounts = new Map();   // card -> total copies across those decks
const binCounts = new Map();    // card -> Uint16Array of decks per bin

for (const deckIndex of deckIndexes) {
    const deck = state.data.decks[deckIndex];

    let bin = -1;
    if (deck.date != null) {
    bin = Math.floor((deck.date - axis.start) / (axis.end - axis.start) * axis.n);
    bin = Math.max(0, Math.min(axis.n - 1, bin));
    axis.totals[bin]++;
    }

    const perDeck = new Map();
    for (const cardName of deck.cards) {
    perDeck.set(cardName, (perDeck.get(cardName) || 0) + 1);
    }
    for (const [name, n] of perDeck) {
    deckCounts.set(name, (deckCounts.get(name) || 0) + 1);
    copyCounts.set(name, (copyCounts.get(name) || 0) + n);
    if (bin >= 0) {
        let arr = binCounts.get(name);
        if (!arr) { arr = new Uint16Array(axis.n); binCounts.set(name, arr); }
        arr[bin]++;
    }
    }
}

const metaByName = new Map((state.data.cards || []).map(c => [c.name, c]));

const names = new Set([
    ...deckCounts.keys(),
    ...state.selectedCards,
    ...state.excludedCards,
].filter(name => !state.hideLands || !metaByName.get(name)?.land));

const cards = [...names].map(name => {
    const meta = metaByName.get(name) || {};
    const decks = deckCounts.get(name) || 0;
    return {
    name,
    decks,
    inclusion: totalMatches ? decks / totalMatches : 0,
    avg_copies: decks ? copyCounts.get(name) / decks : 0,
    image: meta.image || "",
    type_rank: meta.type_rank ?? 5,
    bins: binCounts.get(name) || null,
    selected: state.selectedCards.includes(name),
    excluded: state.excludedCards.includes(name),
    };
});

cards.sort((a, b) => {
    if (a.selected !== b.selected) return Number(b.selected) - Number(a.selected);
    if (a.excluded !== b.excluded) return Number(b.excluded) - Number(a.excluded);
    const byInclusion = b.inclusion - a.inclusion;
    if (byInclusion !== 0) return byInclusion;
    if (a.type_rank !== b.type_rank) return a.type_rank - b.type_rank;
    return a.name.localeCompare(b.name);
});

return { cards, deckCount: totalMatches, axis };
}

function getCurrentDeckSet() {
if (!state.data || !state.data.decks) return new Set();

const deckMap = state.data.deck_map || {};
const cutoff = cutoffTs();

let deckSet = new Set();
state.data.decks.forEach((d, i) => {
    // Decks with no date are hidden while a date filter is active.
    if (cutoff == null || (d.date != null && d.date >= cutoff)) deckSet.add(i);
});

for (const name of state.selectedCards) {
    const matches = new Set(deckMap[name] || []);
    deckSet = new Set([...deckSet].filter(i => matches.has(i)));
}
for (const name of state.excludedCards) {
    const matches = new Set(deckMap[name] || []);
    deckSet = new Set([...deckSet].filter(i => !matches.has(i)));
}

return deckSet;
}

function renderCardGrid(cards, deckCount, axis) {
grid.innerHTML = "";

if (!cards.length) {
    const empty = document.createElement("div");
    empty.className = "error";
    empty.textContent = "No cards match the current deck filter.";
    grid.appendChild(empty);
    return;
}

const metaByName = new Map((state.data.cards || []).map(c => [c.name, c]));

const toggleInclude = name => {
    if (state.selectedCards.includes(name)) {
    state.selectedCards = state.selectedCards.filter(n => n !== name);
    } else {
    state.selectedCards = [...state.selectedCards, name];
    state.excludedCards = state.excludedCards.filter(n => n !== name);
    }
    renderCurrentDeckView();
};

const toggleExclude = name => {
    if (state.excludedCards.includes(name)) {
    state.excludedCards = state.excludedCards.filter(n => n !== name);
    } else {
    state.excludedCards = [...state.excludedCards, name];
    state.selectedCards = state.selectedCards.filter(n => n !== name);
    }
    renderCurrentDeckView();
};

cards.forEach(c => {
    const meta = metaByName.get(c.name) || {};
    const card = document.createElement("div");
    card.className = "card" + (c.selected ? " selected" : "") + (c.excluded ? " excluded" : "");
    card.dataset.cardName = c.name;
    card.title = c.selected
    ? `Included: click to remove, right-click to exclude "${c.name}"`
    : c.excluded
        ? `Excluded: right-click to remove, click to include "${c.name}"`
        : `Click: only decks with "${c.name}" · Right-click: only decks without it`;

    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = c.name;
    img.onload = () => img.classList.add("loaded");
    if (meta.image) img.src = meta.image;
    card.appendChild(img);

    const pct = document.createElement("div");
    pct.className = "pct";
    if (c.excluded) {
    pct.textContent = "Excluded";
    } else {
    pct.textContent = pctLabel(c.inclusion);
    if (c.avg_copies >= 1.5) {
        const small = document.createElement("small");
        small.textContent = "×" + c.avg_copies.toFixed(1);
        pct.appendChild(small);
    }
    }
    card.appendChild(pct);
    if (!c.excluded && c.decks) {
    const strip = document.createElement("span");
    strip.className = "strip";
    strip.style.background = stripGradient(c.bins, axis.totals);
    strip.addEventListener("mousemove", e => {
        const r = strip.getBoundingClientRect();
        const i = Math.max(0, Math.min(axis.n - 1,
        Math.floor((e.clientX - r.left) / r.width * axis.n)));
        const total = axis.totals[i];
        const got = c.bins ? c.bins[i] : 0;
        strip.title = total
        ? `${binLabel(axis, i)}: ${got} of ${total} decks (${pctLabel(got / total)})`
        : `${binLabel(axis, i)}: no decks`;
    });
    pct.appendChild(strip);
    }

    card.addEventListener("click", () => toggleInclude(c.name));
    card.addEventListener("contextmenu", e => {
    e.preventDefault();
    toggleExclude(c.name);
    });

    grid.appendChild(card);
});

const sub = document.getElementById("sub");
const when = state.maxAgeDays == null ? "" : ` from the last ${state.maxAgeDays} days`;
if (state.selectedCards.length || state.excludedCards.length) {
    const parts = [];
    if (state.selectedCards.length) parts.push(`with ${state.selectedCards.join(", ")}`);
    if (state.excludedCards.length) parts.push(`without ${state.excludedCards.join(", ")}`);
    sub.textContent = `${deckCount} decks${when} ${parts.join(" and ")}`;
} else {
    sub.textContent = `${deckCount} decks${when}, ${state.data.cards.length} distinct cards, highest inclusion first`;
}
}

function renderCurrentDeckView() {
if (!state.data) return;

const { cards, deckCount, axis } = buildFilteredCards();
renderCardGrid(cards, deckCount, axis);
}

function resetFilters() {
state.selectedCards = [];
state.excludedCards = [];
state.maxAgeDays = null;
state.hideLands = false;
ageInput.value = ageInput.max;
updateAgeLabel();
updateLandToggleButton();
renderCurrentDeckView();
}

async function load() {
if (!commander) {
    document.getElementById("sub").textContent =
    "Search for a commander to see its average deck.";
    return;
}

const res = await fetch("/api/average-deck?commander=" + encodeURIComponent(commander));
const data = await res.json();

if (!res.ok) {
    document.getElementById("sub").textContent = "";
    grid.className = "error";
    grid.textContent = data.error || "Could not load the average deck.";
    if (data.choices) {
    const ul = document.createElement("ul");

    const choices = [...data.choices].sort((a, b) => {
        const byCount = (b.decks ?? 0) - (a.decks ?? 0);
        return byCount !== 0
        ? byCount
        : (a.name || "").localeCompare(b.name || "");
    });

    choices.forEach(c => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = "?commander=" + encodeURIComponent(c.name);
        a.textContent = c.name;
        li.append(a, ` (${c.decks} decks)`);
        ul.appendChild(li);
    });

    grid.appendChild(ul);
    }
    return;
}

state.data = data;
state.selectedCards = [];
state.excludedCards = [];
state.maxAgeDays = null;
state.hideLands = false;
ageInput.max = data.days || 365;
ageInput.value = ageInput.max;
ageInput.disabled = false;
updateAgeLabel();
updateLandToggleButton();

document.getElementById("title").textContent = "Average deck: " + data.commander;
document.title = "Average deck: " + data.commander;

renderCurrentDeckView();
}

document.getElementById("q").value = commander;

document.getElementById("search").onsubmit = e => {
e.preventDefault();
const q = document.getElementById("q").value.trim();
location.search = q ? "?commander=" + encodeURIComponent(q) : "";
};

document.getElementById("reset-filters").addEventListener("click", () => {
resetFilters();
});

async function loadCommanders() {
const list = await (await fetch("/api/commanders")).json();
const dl = document.getElementById("cmds");
dl.innerHTML = "";

const sorted = [...list].sort((a, b) => {
    const byCount = (b.decks ?? 0) - (a.decks ?? 0);
    return byCount !== 0 ? byCount : (a.name || "").localeCompare(b.name || "");
});

sorted.forEach(c => {
    const o = document.createElement("option");
    o.value = c.name;
    o.label = `${c.name} (${c.decks})`;
    dl.appendChild(o);
});
}

async function loadInfo() {
const d = await (await fetch("/api/dataset-info")).json();
document.getElementById("info-tip").textContent =
    `Built from topdeck.gg tournament data: decks from players who scored at least ` +
    `half of the available points, in tournaments from the last ${d.days} days with ` +
    `at least ${d.min_players} players and at least ${d.min_swiss} Swiss rounds. ` +
    `${d.decks} decks from ${d.tournaments} tournaments.`;
}

loadInfo().catch(() => {});
loadCommanders().catch(() => {});
load();