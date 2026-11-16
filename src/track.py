"""
Track record: lock each game's lines at the last refresh before tip, pull final
scores, and grade the KenPom projection vs. the book.

Ledger lives at docs/data/ledger.json (one entry per Odds API event id).
Env: ODDS_API_KEY, or SCORES_JSON=path to a saved /scores response for testing.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/New_York"))
LOCK_FIELDS = ["home", "away", "home_conf", "away_conf", "tip", "tip_iso", "neutral",
               "book_spread", "proj_spread", "diff", "pick", "kp_home_wp", "mkt_home_wp",
               "ml_home", "ml_away", "ml_edge", "best_line",
               "book_total", "proj_total", "tot_diff", "ou_pick", "model_home_wp", "ml_min_edge"]
BUCKETS = [("Under 2", 0, 2), ("2 to 3", 2, 3), ("3 to 5", 3, 5), ("5+", 5, 999)]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_scores() -> list:
    if os.getenv("SCORES_JSON"):
        return json.loads(Path(os.environ["SCORES_JSON"]).read_text())
    r = requests.get("https://api.the-odds-api.com/v4/sports/basketball_ncaab/scores",
                     params={"apiKey": os.environ["ODDS_API_KEY"], "daysFrom": 3}, timeout=30)
    r.raise_for_status()
    print(f"Scores pulled. Odds API credits remaining: {r.headers.get('x-requests-remaining')}")
    return r.json()


def lock_lines(ledger: dict, games: list, now: datetime) -> int:
    """Overwrite lines for games that haven't tipped; tipped games stay frozen."""
    n = 0
    for g in games:
        tip = _dt(g["tip_iso"])
        if tip <= now:
            continue
        prev = ledger.get(g["id"], {})
        if prev.get("final"):
            continue
        entry = {k: g.get(k) for k in LOCK_FIELDS}
        entry["date"] = tip.astimezone(TZ).strftime("%Y-%m-%d")
        entry["locked_at"] = now.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        entry["opened_spread"] = prev.get("opened_spread", g["book_spread"])
        entry["opened_total"] = prev.get("opened_total", g.get("book_total"))
        ledger[g["id"]] = entry
        n += 1
    return n


def add_scores(ledger: dict, now: datetime) -> int:
    pending = [k for k, v in ledger.items() if not v.get("final") and _dt(v["tip_iso"]) < now]
    if not pending:
        return 0
    try:
        events = fetch_scores()
    except Exception as e:  # never let a scores hiccup block the dashboard
        print(f"Scores fetch failed: {e}")
        return 0
    n = 0
    for ev in events:
        e = ledger.get(ev["id"])
        if not e or e.get("final") or not ev.get("completed") or not ev.get("scores"):
            continue
        pts = {s["name"]: int(s["score"]) for s in ev["scores"]}
        if ev["home_team"] not in pts or ev["away_team"] not in pts:
            continue
        e["home_score"], e["away_score"] = pts[ev["home_team"]], pts[ev["away_team"]]
        e["final"] = True
        n += 1
    # Odds API keeps scores for 3 days; flag anything older that never got one.
    for k in pending:
        if not ledger[k].get("final") and now - _dt(ledger[k]["tip_iso"]) > timedelta(days=3):
            ledger[k]["no_result"] = True
    return n


def grade(e: dict) -> dict:
    actual = e["home_score"] - e["away_score"]
    proj_m, book_m = -e["proj_spread"], -e["book_spread"]
    pick_home = e["pick"] == e["home"]
    cover = actual + e["book_spread"] if pick_home else -(actual + e["book_spread"])
    g = {
        "actual": actual,
        "proj_err": abs(proj_m - actual),
        "book_err": abs(book_m - actual),
        "proj_right": None if proj_m == 0 or actual == 0 else (proj_m > 0) == (actual > 0),
        "book_right": None if book_m == 0 or actual == 0 else (book_m > 0) == (actual > 0),
        "ats": "W" if cover > 0 else "L" if cover < 0 else "P",
        "gap": abs(e["diff"]),
        "big": abs(e["book_spread"]) > 20,
    }
    if e.get("book_total") is not None and e.get("proj_total") is not None:
        tot = e["home_score"] + e["away_score"]
        side = (tot > e["book_total"]) - (tot < e["book_total"])  # 1 over, -1 under, 0 push
        want = 1 if e["ou_pick"] == "Over" else -1
        g["tot"] = {"actual": tot, "proj_err": abs(e["proj_total"] - tot),
                    "book_err": abs(e["book_total"] - tot), "gap": abs(e["tot_diff"]),
                    "res": "P" if side == 0 else ("W" if side == want else "L")}
    mkt = e.get("mkt_home_wp")
    if mkt is not None and e.get("ml_edge") is not None:
        mkt_pick = mkt if pick_home else 1 - mkt
        price = e.get("ml_home") if pick_home else e.get("ml_away")
        if e["ml_edge"] >= max(e.get("ml_min_edge") or 0, 1e-9) and 0.15 <= mkt_pick <= 0.85 and price:
            won = (actual > 0) == pick_home and actual != 0
            g["ml"] = {"won": won, "price": price, "mkt": mkt_pick,
                       "units": (price / 100 if price > 0 else 100 / -price) if won else -1.0}
    return g


def _ats(rows):
    w = sum(r["ats"] == "W" for r in rows); l = sum(r["ats"] == "L" for r in rows)
    p = sum(r["ats"] == "P" for r in rows)
    return {"w": w, "l": l, "p": p, "pct": round(w / (w + l), 3) if w + l else None}


def _tot(rows):
    t = [r["tot"] for r in rows if "tot" in r]
    if not t:
        return None
    w = sum(x["res"] == "W" for x in t); l = sum(x["res"] == "L" for x in t); p = sum(x["res"] == "P" for x in t)
    big = [x for x in t if x["gap"] >= 4]
    bw = sum(x["res"] == "W" for x in big); bl = sum(x["res"] == "L" for x in big)
    return {"n": len(t), "proj_mae": round(sum(x["proj_err"] for x in t) / len(t), 2),
            "book_mae": round(sum(x["book_err"] for x in t) / len(t), 2),
            "rec": {"w": w, "l": l, "p": p, "pct": round(w / (w + l), 3) if w + l else None},
            "rec4": {"w": bw, "l": bl, "p": 0, "pct": round(bw / (bw + bl), 3) if bw + bl else None}}


def _summ(rows):
    if not rows:
        return None
    ml = [r["ml"] for r in rows if "ml" in r]
    pr = [r["proj_right"] for r in rows if r["proj_right"] is not None]
    br = [r["book_right"] for r in rows if r["book_right"] is not None]
    return {
        "n": len(rows),
        "proj_mae": round(sum(r["proj_err"] for r in rows) / len(rows), 2),
        "book_mae": round(sum(r["book_err"] for r in rows) / len(rows), 2),
        "proj_closer": round(sum(r["proj_err"] < r["book_err"] for r in rows) / len(rows), 3),
        "proj_win": round(sum(pr) / len(pr), 3) if pr else None,
        "book_win": round(sum(br) / len(br), 3) if br else None,
        "ats": _ats(rows),
        "ats_value": _ats([r for r in rows if r["gap"] >= 3 and not r["big"]]),
        "ats_big": _ats([r for r in rows if r["big"]]),
        "big_book_bias": (lambda b: round(sum(abs(r["actual"]) - abs(r["book_spread"]) for r in b) / len(b), 1) if b else None)(
            [r for r in rows if r["big"]]),
        "tot": _tot(rows),
        "ml": {"n": len(ml), "w": sum(m["won"] for m in ml),
               "units": round(sum(m["units"] for m in ml), 2),
               "exp": round(sum(m["mkt"] for m in ml) / len(ml), 3) if ml else None},
    }


def update_track(games: list, now: datetime, path: Path) -> dict:
    ledger = json.loads(path.read_text()) if path.exists() else {}
    locked = lock_lines(ledger, games, now)
    scored = add_scores(ledger, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=1, sort_keys=True))
    print(f"Ledger: {locked} lines locked/updated, {scored} games scored, {len(ledger)} total.")
    return summarize(ledger)


def summarize(ledger: dict) -> dict:
    graded = []
    for gid, e in ledger.items():
        if e.get("final"):
            graded.append({**e, "id": gid, **grade(e)})
    graded.sort(key=lambda r: r["tip_iso"], reverse=True)

    months = sorted({r["date"][:7] for r in graded}, reverse=True)
    return {
        "started": min((e["date"] for e in ledger.values()), default=None),
        "pending": sum(1 for e in ledger.values() if not e.get("final") and not e.get("no_result")),
        "no_result": sum(1 for e in ledger.values() if e.get("no_result")),
        "overall": _summ(graded),
        "months": [{"month": m, **_summ([r for r in graded if r["date"][:7] == m])} for m in months],
        "buckets": [{"label": lab, "all": _ats([r for r in graded if lo <= r["gap"] < hi]),
                     "comp": _ats([r for r in graded if lo <= r["gap"] < hi and not r["big"]]),
                     "big": _ats([r for r in graded if lo <= r["gap"] < hi and r["big"]])}
                    for lab, lo, hi in BUCKETS],
        "recent": [{k: r.get(k) for k in ("date", "tip", "away", "home", "away_score", "home_score",
                                           "book_spread", "proj_spread", "pick", "ats", "proj_err",
                                           "book_err", "gap", "big", "book_total", "ou_pick")}
                   | ({"tot_res": r["tot"]["res"], "tot_actual": r["tot"]["actual"]} if "tot" in r else {})
                   | ({"ml_won": r["ml"]["won"], "ml_price": r["ml"]["price"]} if "ml" in r else {})
                   for r in graded[:60]],
    }
