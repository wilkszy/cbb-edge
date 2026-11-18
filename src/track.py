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
               "book_total", "proj_total", "tot_diff", "ou_pick", "model_home_wp", "ml_min_edge",
               "proj_home_pts", "proj_away_pts", "imp_home", "imp_away", "top_play", "top_plays", "top5_dropped"]
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
    tts = []
    for side in ("home", "away"):
        imp, proj = e.get(f"imp_{side}"), e.get(f"proj_{side}_pts")
        sign = -1 if side == "home" else 1   # home pts = (T - S)/2, away = (T + S)/2, S = home spread
        if imp is None and e.get("book_total") is not None:
            imp = round((e["book_total"] + sign * e["book_spread"]) / 2, 1)
        if proj is None and e.get("proj_total") is not None:
            proj = round((e["proj_total"] + sign * e["proj_spread"]) / 2, 1)
        if imp is None or proj is None:
            continue
        act = e[f"{side}_score"]
        want = 1 if proj > imp else -1
        hit = (act > imp) - (act < imp)
        tts.append({"team": e[side], "imp": imp, "proj": proj, "actual": act, "gap": abs(proj - imp),
                    "lean": "Over" if want > 0 else "Under",
                    "proj_err": abs(proj - act), "book_err": abs(imp - act),
                    "res": "P" if hit == 0 else ("W" if hit == want else "L")})
    if tts:
        g["tt"] = tts
    def grade_plays(plays):
      out = []
      for tp in plays:
        hs, as_ = e["home_score"], e["away_score"]
        if tp["type"] == "Spread":
            res = g["ats"]
        elif tp["type"] == "Total":
            res = g["tot"]["res"] if "tot" in g else None
        elif tp["type"] == "Team total":
            pts = hs if tp["team"] == e["home"] else as_
            hit = (pts > tp["line"]) - (pts < tp["line"]); want = 1 if tp["lean"] == "Over" else -1
            res = "P" if hit == 0 else ("W" if hit == want else "L")
        else:
            res = "W" if (hs > as_) == (tp["side"] == e["home"]) else "L"
        if res:
            out.append({"type": tp["type"], "res": res, "prob": tp["prob"], "price": tp.get("price")})
      return out
    gp = grade_plays(e.get("top_plays") or ([e["top_play"]] if e.get("top_play") else []))
    if gp:
        g["plays"] = gp
    gd = grade_plays(e.get("top5_dropped") or [])
    if gd:
        g["dropped_plays"] = gd
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


def _tt(rows):
    t = [x for r in rows for x in r.get("tt", [])]
    if not t:
        return None
    def rec(xs):
        w = sum(x["res"] == "W" for x in xs); l = sum(x["res"] == "L" for x in xs); p = sum(x["res"] == "P" for x in xs)
        return {"w": w, "l": l, "p": p, "pct": round(w / (w + l), 3) if w + l else None}
    return {"n": len(t), "proj_mae": round(sum(x["proj_err"] for x in t) / len(t), 2),
            "book_mae": round(sum(x["book_err"] for x in t) / len(t), 2),
            "rec": rec(t), "rec3": rec([x for x in t if x["gap"] >= 3]),
            "over": rec([x for x in t if x["lean"] == "Over"]), "under": rec([x for x in t if x["lean"] == "Under"])}


def _plays(rows, key="plays"):
    ps = [p for r in rows for p in r.get(key, [])]
    if not ps:
        return None
    def rec(xs):
        w = sum(x["res"] == "W" for x in xs); l = sum(x["res"] == "L" for x in xs); p = sum(x["res"] == "P" for x in xs)
        return {"w": w, "l": l, "p": p, "pct": round(w / (w + l), 3) if w + l else None}
    def units(x):
        if x["res"] == "P": return 0.0
        pr = x["price"] if x["price"] is not None else -110
        return (pr / 100 if pr > 0 else 100 / -pr) if x["res"] == "W" else -1.0
    types = sorted({x["type"] for x in ps})
    return {"rec": rec(ps), "exp": round(sum(x["prob"] for x in ps) / len(ps), 3),
            "units": round(sum(units(x) for x in ps), 2),
            "by_type": {t: rec([x for x in ps if x["type"] == t]) for t in types}}


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
        "ats_big_value": _ats([r for r in rows if r["big"] and r["gap"] >= 3]),
        "big_book_bias": (lambda b: round(sum(abs(r["actual"]) - abs(r["book_spread"]) for r in b) / len(b), 1) if b else None)(
            [r for r in rows if r["big"]]),
        "tot": _tot(rows),
        "tt": _tt(rows),
        "plays": _plays(rows),
        "dropped_plays": _plays(rows, "dropped_plays"),
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


def team_trends(graded: list) -> list:
    """Each team's record against its KenPom projection, across every graded game."""
    T = {}
    for r in graded:
        hs, as_ = r["home_score"], r["away_score"]
        ph = (r["proj_total"] - r["proj_spread"]) / 2 if r.get("proj_total") is not None else None
        pa = (r["proj_total"] + r["proj_spread"]) / 2 if r.get("proj_total") is not None else None
        for side, opp, pts, opts, proj_pts, sign in (("home", "away", hs, as_, ph, 1), ("away", "home", as_, hs, pa, -1)):
            t = T.setdefault(r[side], {"team": r[side], "g": 0, "w": 0, "l": 0, "kp_w": 0, "kp_l": 0, "upsets": 0,
                                       "upset_losses": 0, "beat": 0, "short": 0, "margin_vs": 0.0, "pts_over": 0,
                                       "pts_under": 0, "pts_vs": 0.0, "pts_n": 0, "tot_over": 0, "tot_under": 0,
                                       "ats_w": 0, "ats_l": 0, "last": None, "games": [],
                                       "tot_vs": 0.0, "tot_n": 0, "tb_over": 0, "tb_under": 0, "tb_vs": 0.0, "tb_n": 0,
                                       "imp_over": 0, "imp_under": 0, "imp_vs": 0.0, "imp_n": 0})
            margin = (hs - as_) * sign
            proj_margin = -r["proj_spread"] * sign
            book_line = r["book_spread"] * sign          # this team's spread
            won, kp_fav = margin > 0, proj_margin > 0
            t["g"] += 1; t["w"] += won; t["l"] += not won
            t["kp_w"] += kp_fav; t["kp_l"] += not kp_fav
            t["upsets"] += won and not kp_fav; t["upset_losses"] += (not won) and kp_fav
            d = margin - proj_margin
            t["margin_vs"] += d; t["beat"] += d > 0; t["short"] += d < 0
            if proj_pts is not None:
                t["pts_vs"] += pts - proj_pts; t["pts_n"] += 1
                t["pts_over"] += pts > proj_pts; t["pts_under"] += pts < proj_pts
                tot = hs + as_
                t["tot_over"] += tot > r["proj_total"]; t["tot_under"] += tot < r["proj_total"]
                t["tot_vs"] += tot - r["proj_total"]; t["tot_n"] += 1
            if r.get("book_total") is not None:
                tot = hs + as_
                t["tb_over"] += tot > r["book_total"]; t["tb_under"] += tot < r["book_total"]
                t["tb_vs"] += tot - r["book_total"]; t["tb_n"] += 1
                imp = (r["book_total"] - sign * r["book_spread"]) / 2
                t["imp_over"] += pts > imp; t["imp_under"] += pts < imp
                t["imp_vs"] += pts - imp; t["imp_n"] += 1
            c = margin + book_line
            t["ats_w"] += c > 0; t["ats_l"] += c < 0
            t["last"] = max(t["last"] or r["date"], r["date"])
            t["games"].append({"date": r["date"], "opp": r[opp], "at": "vs." if side == "home" else "@",
                               "score": f"{pts}-{opts}", "won": won, "proj": round(proj_margin, 1),
                               "margin": margin, "line": book_line})
    out = []
    for t in T.values():
        t["margin_vs"] = round(t["margin_vs"] / t["g"], 1)
        t["pts_vs"] = round(t["pts_vs"] / t["pts_n"], 1) if t["pts_n"] else None
        t["tot_vs"] = round(t["tot_vs"] / t["tot_n"], 1) if t["tot_n"] else None
        t["tb_vs"] = round(t["tb_vs"] / t["tb_n"], 1) if t["tb_n"] else None
        t["imp_vs"] = round(t["imp_vs"] / t["imp_n"], 1) if t["imp_n"] else None
        t["games"].sort(key=lambda x: x["date"], reverse=True)
        out.append(t)
    return sorted(out, key=lambda t: -t["margin_vs"])


BE = 0.524
CATS = [
    # key, label, bet kind, membership on a graded row
    ("small_gap", "Gap under 2 pts", "spread", lambda r: r["gap"] < 2),
    ("mid_gap", "Gap 2 to 5 pts", "spread", lambda r: 2 <= r["gap"] < 5),
    ("comp_pick", "Competitive picks", "spread", lambda r: r["gap"] >= 3 and not r["big"]),
    ("big_pick", "Big-spread picks", "spread", lambda r: r["gap"] >= 3 and r["big"]),
    ("road", "Road team", "spread", lambda r: r["pick"] == r["away"]),
    ("home", "Home team", "spread", lambda r: r["pick"] == r["home"]),
    ("dog", "Underdog", "spread", lambda r: (r["pick"] == r["home"]) == (r["book_spread"] > 0)),
    ("fav", "Favorite", "spread", lambda r: (r["pick"] == r["home"]) != (r["book_spread"] > 0)),
    ("mid_spread", "Spread 10 to 20", "spread", lambda r: 10 <= abs(r["book_spread"]) <= 20),
    ("under", "Under lean", "total", lambda r: r.get("ou_pick") == "Under"),
    ("over", "Over lean", "total", lambda r: r.get("ou_pick") == "Over"),
    ("tot_small", "Total gap under 4", "total", lambda r: "tot" in r and r["tot"]["gap"] < 4),
    ("tot_big", "Total gap 4+", "total", lambda r: "tot" in r and r["tot"]["gap"] >= 4),
    ("ml", "Moneyline value", "ml", lambda r: "ml" in r),
]


def _status(all_pct, rec_pct):
    if all_pct is None or rec_pct is None:
        return None
    if all_pct >= 0.54 and rec_pct >= BE:
        return "green"
    if all_pct < BE and rec_pct < BE:
        return "red"
    if (all_pct >= BE) != (rec_pct >= BE):
        return "yellow"
    return None


def category_trends(graded: list, recent_days: int = 3) -> list:
    days = sorted({r["date"] for r in graded})
    recent = set(days[-recent_days:])
    out = []
    for key, label, kind, f in CATS:
        rows = [r for r in graded if f(r)]
        if kind == "ml":
            res = lambda r: "W" if r["ml"]["won"] else "L"
        elif kind == "total":
            rows = [r for r in rows if "tot" in r]
            res = lambda r: r["tot"]["res"]
        else:
            res = lambda r: r["ats"]
        def rec(xs):
            w = sum(res(r) == "W" for r in xs); l = sum(res(r) == "L" for r in xs)
            return {"w": w, "l": l, "pct": round(w / (w + l), 3) if w + l else None}
        a, rc = rec(rows), rec([r for r in rows if r["date"] in recent])
        entry = {"key": key, "label": label, "kind": kind, "all": a, "recent": rc, "status": None}
        if kind == "ml":
            u_all = round(sum(r["ml"]["units"] for r in rows), 2)
            u_rec = round(sum(r["ml"]["units"] for r in rows if r["date"] in recent), 2)
            entry.update(units_all=u_all, units_recent=u_rec)
            if len(rows) >= 20:
                entry["status"] = ("green" if u_all > 0 and u_rec > 0 else "red" if u_all < 0 and u_rec < 0 else "yellow")
        elif a["w"] + a["l"] >= 20 and rc["w"] + rc["l"] >= 5:
            entry["status"] = _status(a["pct"], rc["pct"])
        out.append(entry)
    return out


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
        "teams": team_trends(graded),
        "categories": category_trends(graded),
        "recent": [{k: r.get(k) for k in ("date", "tip", "away", "home", "away_score", "home_score",
                                           "book_spread", "proj_spread", "pick", "ats", "proj_err",
                                           "book_err", "gap", "big", "book_total", "ou_pick")}
                   | ({"tot_res": r["tot"]["res"], "tot_actual": r["tot"]["actual"]} if "tot" in r else {})
                   | ({"ml_won": r["ml"]["won"], "ml_price": r["ml"]["price"]} if "ml" in r else {})
                   for r in graded[:60]],
    }
