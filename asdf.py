import json
d = json.load(open("data/standings_14d_p16_swiss3.json"))
t = d[0]
print({k: v for k, v in t.items() if k != "standings"})
print(json.dumps(t["standings"][:2], indent=2)[:2500])
