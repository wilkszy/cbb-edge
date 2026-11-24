"""
CBB Edge: daily college basketball spread discrepancies vs. KenPom ratings.

1. Pulls public KenPom ratings (AdjEM, AdjO, AdjD, AdjT) from kenpom.com.
2. Pulls today's spreads + moneylines from The Odds API (US books).
3. Projects each game's margin from the ratings (KenPom-style formula).
4. Ranks games by the gap between the book spread and the projection.
5. Writes docs/index.html (dashboard) + docs/data/<date>.json.

Env vars:
  ODDS_API_KEY     required (free key at the-odds-api.com)
  HCA              home-court advantage in points (default 3.2)
  MARGIN_SD        std dev of game margin for win prob (default 13.0)
  WP_BLEND         weight on the KenPom win prob when blending with the market (default 0.5)
  ML_MIN_EDGE      minimum blended edge for a moneyline value pick (default 0.03)
  ESPN_JSON        optional path to a saved ESPN scoreboard response (skips fetch)
  TZ_NAME          slate timezone (default America/New_York)
  KENPOM_HTML      optional path to a saved kenpom.com page (skips fetch)
  ODDS_JSON        optional path to a saved odds response (skips fetch)
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ALIASES_FILE = ROOT / "src" / "aliases.json"
NEUTRAL_FILE = ROOT / "src" / "neutral_sites.json"

HCA = float(os.getenv("HCA", "3.2"))
MARGIN_SD = float(os.getenv("MARGIN_SD", "13.0"))
WP_BLEND = float(os.getenv("WP_BLEND", "0.5"))
ML_MIN_EDGE = float(os.getenv("ML_MIN_EDGE", "0.03"))
# Top plays: outcome spread around the projection, measured on graded games (Nov 13-15)
SD_MARGIN, SD_TOTAL, SD_TEAM = 12.0, 16.6, 10.2
SHRINK = 0.5        # pull model probabilities halfway toward the market's 50/50
ML_MAX_FAV = -300   # skip moneylines shorter than this
TOP_N = 5
LUCK_FLAG = float(os.getenv("LUCK_FLAG", "0.15"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/New_York"))
BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "espnbet", "betrivers", "fanatics"]
BOOK_LABELS = {"draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "williamhill_us": "CZR",
               "espnbet": "ESPN", "betrivers": "BR", "fanatics": "FAN"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- KenPom ---
def fetch_kenpom_html() -> str:
    if os.getenv("KENPOM_HTML"):
        return Path(os.environ["KENPOM_HTML"]).read_text()
    r = requests.get("https://kenpom.com/", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def _num(txt: str) -> float:
    return float(txt.replace("+", "").strip())


def parse_kenpom(html: str) -> tuple[dict, str]:
    """Return ({team: ratings}, data_through_text)."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="ratings-table")
    if table is None:
        raise RuntimeError("KenPom ratings table not found (layout change or blocked request).")
    teams = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        try:
            name_cell = tds[1]
            a = name_cell.find("a")
            name = (a.get_text() if a else name_cell.get_text()).strip()
            # Homepage columns: Rk, Team, Conf, W-L, NetRtg, ORtg, rk, DRtg, rk, AdjT, rk, ...
            teams[name] = {
                "rank": int(tds[0].get_text().strip()),
                "conf": tds[2].get_text().strip(),
                "record": tds[3].get_text().strip(),
                "adj_em": _num(tds[4].get_text()),
                "adj_o": _num(tds[5].get_text()),
                "adj_d": _num(tds[7].get_text()),
                "adj_t": _num(tds[9].get_text()),
                "luck": _num(tds[11].get_text()) if len(tds) > 11 else 0.0,
            }
        except (ValueError, IndexError):
            continue
    if len(teams) < 300:
        raise RuntimeError(f"Only parsed {len(teams)} KenPom teams; expected ~360. Check the parser.")
    # sanity: AdjEM should be ~ AdjO - AdjD
    bad = [t for t, v in teams.items() if abs(v["adj_o"] - v["adj_d"] - v["adj_em"]) > 0.6]
    if len(bad) > 10:
        raise RuntimeError("KenPom columns look shifted (AdjO - AdjD != AdjEM). Check the parser.")
    through = ""
    m = re.search(r"Data through games of\s*(?:<a[^>]*>)?([^<\n]+)", html)
    if m:
        through = m.group(1).strip()
    else:   # mid-update wording: "Data includes 58 of 59 games played on Friday, November 20"
        m = re.search(r"Data includes (\d+) of (\d+) games played on\s*(?:<a[^>]*>)?([^<\n]+)", html)
        if m:
            through = f"{m.group(3).strip()} ({m.group(1)} of {m.group(2)} games)"
    return teams, through


# ------------------------------------------------------------- Odds API ---
def fetch_odds() -> list:
    if os.getenv("ODDS_JSON"):
        return json.loads(Path(os.environ["ODDS_JSON"]).read_text())
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY is not set.")
    r = requests.get(
        "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds",
        params={"apiKey": key, "regions": "us", "markets": "spreads,h2h,totals",
                "oddsFormat": "american", "bookmakers": ",".join(BOOKS)},
        timeout=30,
    )
    r.raise_for_status()
    print(f"Odds API credits remaining: {r.headers.get('x-requests-remaining')}")
    return r.json()


def implied(american: float) -> float:
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


# --------------------------------------------------------- Name matching ---
def norm(s: str) -> str:
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.lower().replace("&", "and").replace("'", "").replace(".", "")
    s = re.sub(r"\bstate\b", "st", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Matcher:
    """Map Odds API names ("<School> <Mascot>") to KenPom names.

    Built once over every name in the odds feed so collisions can be caught:
    "South Carolina Upstate Spartans" must not land on "South Carolina".
    If two feed names map to the same KenPom team, only the closest one keeps it
    (alias > fewest leftover words); the rest are reported as unmatched.
    """
    MAX_MASCOT_WORDS = 2

    def __init__(self, kp_names, feed_names):
        self.aliases = json.loads(ALIASES_FILE.read_text()) if ALIASES_FILE.exists() else {}
        self.by_norm = {norm(n): n for n in kp_names}
        self.map, self.unmatched, self.collisions = {}, set(), {}
        cands = {}
        for name in set(feed_names):
            hit = self._raw(name)
            if hit:
                cands[name] = hit
            else:
                self.unmatched.add(name)
        claimed = {}
        for name, (kp, left) in cands.items():
            claimed.setdefault(kp, []).append((left, name))
        for kp, lst in claimed.items():
            lst.sort()
            if len(lst) > 1 and lst[0][0] == lst[1][0]:
                for _, n in lst:      # tie: can't tell which is right, drop all
                    self.unmatched.add(n)
                self.collisions[kp] = [n for _, n in lst]
                continue
            self.map[lst[0][1]] = kp
            for _, n in lst[1:]:
                self.unmatched.add(n)
            if len(lst) > 1:
                self.collisions[kp] = [n for _, n in lst]

    def _raw(self, odds_name):
        if odds_name in self.aliases:
            target = self.by_norm.get(norm(self.aliases[odds_name]))
            if target:
                return target, -1
        words = norm(odds_name).split()
        for i in range(len(words), 0, -1):
            cand = " ".join(words[:i])
            if cand in self.by_norm:
                left = len(words) - i
                return (self.by_norm[cand], left) if left <= self.MAX_MASCOT_WORDS else None
        return None

    def __call__(self, odds_name):
        return self.map.get(odds_name)


# ------------------------------------------------------------ Neutral sites ---
ESPN_MISSED: list = []


def fetch_espn(day) -> list:
    if os.getenv("ESPN_JSON"):
        return json.loads(Path(os.environ["ESPN_JSON"]).read_text()).get("events", [])
    r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
                     params={"dates": day.strftime("%Y%m%d"), "groups": 50, "limit": 500},
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json().get("events", [])


def espn_neutral(kp_names, day) -> dict:
    """{frozenset({kp_team_a, kp_team_b}): neutral_bool} for the day's D-I games on ESPN."""
    try:
        events = fetch_espn(day)
    except Exception as e:
        print(f"ESPN scoreboard fetch failed ({e}); neutral sites only from neutral_sites.json")
        return {}
    by_norm = {norm(n): n for n in kp_names}
    teams = []
    for ev in events:
        for comp in ev.get("competitions", [])[:1]:
            names = []
            for c in comp.get("competitors", []):
                t = c.get("team", {})
                names.append((t.get("location", ""), t.get("displayName", "")))
            teams.append((names, bool(comp.get("neutralSite"))))
    m = Matcher(kp_names, [d for names, _ in teams for _, d in names])
    out, miss, missed = {}, 0, []
    for names, neutral in teams:
        kps = [by_norm.get(norm(loc)) or m(disp) for loc, disp in names]
        if len(kps) == 2 and all(kps):
            out[frozenset(kps)] = neutral
        else:
            miss += 1
            missed += [disp for (loc, disp), k in zip(names, kps) if not k]
    ESPN_MISSED[:] = sorted(set(missed))
    print(f"ESPN: {len(events)} games, {sum(out.values())} neutral-site, {miss} not matched to KenPom")
    return out


# ------------------------------------------------------------ Top plays ---
def _cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _breakeven(price):
    if price is None:
        price = -110
    return -price / (-price + 100) if price < 0 else 100 / (price + 100)


def candidate_plays(g):
    """Every bet on this game the model rates above break-even, with a shrunk win probability."""
    out = []
    shrink = lambda p: 0.5 + SHRINK * (p - 0.5)
    m = -g["proj_spread"]                     # projected home margin
    pick_home = g["pick"] == g["home"]
    bl = g.get("best_line")
    if bl:
        edge = (m + bl["point"]) if pick_home else (-m + bl["point"])
        p = shrink(_cdf(edge / SD_MARGIN))
        out.append({"type": "Spread", "side": g["pick"], "line": bl["point"], "price": bl["price"],
                    "book": bl["book"], "prob": p, "be": _breakeven(bl["price"]),
                    "why": f"KenPom {(g['proj_spread'] if pick_home else -g['proj_spread']):+.1f} vs. line {bl['point']:+g}"})
    ob = g.get("ou_best")
    if ob and g.get("book_total") is not None:
        d = (g["proj_total"] - ob["point"]) * (1 if g["ou_pick"] == "Over" else -1)
        out.append({"type": "Total", "side": g["ou_pick"], "line": ob["point"], "price": ob["price"],
                    "book": ob["book"], "prob": shrink(_cdf(d / SD_TOTAL)), "be": _breakeven(ob["price"]),
                    "why": f"KenPom total {g['proj_total']:.1f} vs. {ob['point']:g}"})
    for side in ("home", "away"):
        imp, proj = g.get(f"imp_{side}"), g.get(f"proj_{side}_pts")
        if imp is None or proj is None:
            continue
        lean = "Over" if proj > imp else "Under"
        out.append({"type": "Team total", "side": f"{g[side]} {lean}", "line": imp, "price": None,
                    "book": "implied", "prob": shrink(_cdf(abs(proj - imp) / SD_TEAM)), "be": _breakeven(-110),
                    "why": f"KenPom {proj:.1f} vs. implied {imp:g}", "team": g[side], "lean": lean})
    if g.get("mkt_home_wp") is not None and g.get("model_home_wp") is not None:
        for side, wp, price in (("home", g["model_home_wp"], g.get("ml_home")), ("away", 1 - g["model_home_wp"], g.get("ml_away"))):
            if price is None or price < ML_MAX_FAV:
                continue
            out.append({"type": "Moneyline", "side": g[side], "line": None, "price": price, "book": "median",
                        "prob": wp, "be": _breakeven(price),
                        "why": f"Model {wp:.0%} vs. market {(g['mkt_home_wp'] if side == 'home' else 1 - g['mkt_home_wp']):.0%}"})
    return [dict(c, game=g["id"], prob=round(c["prob"], 3), be=round(c["be"], 3)) for c in out if c["prob"] > c["be"]]


def trend_status(trend, name):
    """green / red when the team's trend holds and its latest game agreed; yellow when the latest game flipped."""
    t = trend.get(name)
    if not t or t["g"] < 2 or abs(t["margin_vs"]) < 12 / math.sqrt(t["g"]):
        return None
    sign = 1 if t["margin_vs"] > 0 else -1
    last = t["games"][0] if t.get("games") else None
    if last is not None:
        d = last["margin"] - last["proj"]
        if d * sign < 0:
            return "yellow"
    return "green" if sign > 0 else "red"


def trend_dir(trend, name):
    st = trend_status(trend, name)
    return 1 if st == "green" else -1 if st == "red" else 0


def red_flag(c, g, trend):
    """Same rule as the dashboard: backing a team the model has been overrating,
    or fading one it has been underrating."""
    if c["type"] in ("Spread", "Moneyline"):
        backed = c["side"]; faded = g["away"] if backed == g["home"] else g["home"]
        reasons = []
        if trend_dir(trend, backed) < 0: reasons.append(f"{backed} has been trailing its KenPom projection")
        if trend_dir(trend, faded) > 0: reasons.append(f"{faded} has been beating its KenPom projection")
        return "; ".join(reasons)
    if c["type"] == "Team total":
        d = trend_dir(trend, c["team"])
        if c["lean"] == "Over" and d < 0: return f"{c['team']} has been trailing its KenPom projection"
        if c["lean"] == "Under" and d > 0: return f"{c['team']} has been beating its KenPom projection"
    return ""


def top_plays(games, trend=None):
    """Most likely plays of the day: highest win probability, any bet type, any game.
    Red-flagged plays are dropped; plays from the same game are allowed and tagged."""
    trend = trend or {}
    by_id = {g["id"]: g for g in games}
    ranked = sorted((c for g in games for c in candidate_plays(g)), key=lambda c: -c["prob"])
    chosen, dropped = [], []
    for i, c in enumerate(ranked):
        if len(chosen) == TOP_N:
            break
        flag = red_flag(c, by_id[c["game"]], trend)
        if flag:
            dropped.append(dict(c, red_flag=flag, would_rank=i + 1))
        else:
            chosen.append(c)
    counts = {}
    for c in chosen:
        counts[c["game"]] = counts.get(c["game"], 0) + 1
    for c in chosen + dropped:
        g = by_id[c["game"]]
        c.update({"matchup": f"{g['away']} @ {g['home']}", "tip": g["tip"], "neutral": g["neutral"],
                  "same_game": counts.get(c["game"], 0) > 1})
    for c in chosen:
        by_id[c["game"]].setdefault("top_plays", []).append(c)
    for c in dropped:
        by_id[c["game"]].setdefault("top5_dropped", []).append(c)
    return chosen, dropped


# ------------------------------------------------------------ Green cards ---
CAT_STATUS: dict = {}


def member_cats(g, kind):
    """Same category rules as track.CATS, applied to today's game (mirrors the page)."""
    if kind == "ml":
        return ["ml"]
    if kind == "total":
        out = ["under" if g.get("ou_pick") == "Under" else "over"]
        if g.get("tot_diff") is not None:
            out.append("tot_small" if abs(g["tot_diff"]) < 4 else "tot_big")
        return out
    gap, big, ph = abs(g["diff"]), abs(g["book_spread"]) > 20, g["pick"] == g["home"]
    out = []
    if gap < 2: out.append("small_gap")
    elif gap < 5: out.append("mid_gap")
    if gap >= 3: out.append("big_pick" if big else "comp_pick")
    out.append("home" if ph else "road")
    out.append("dog" if ph == (g["book_spread"] > 0) else "fav")
    if 10 <= abs(g["book_spread"]) <= 20: out.append("mid_spread")
    return out


def green_plays(games, trend):
    """Today's picks in at least one green category, with no red category and no red team card."""
    out = []
    for g in games:
        opts = [("Spread", "spread", g["pick"], g.get("best_line"))]
        mkt = g.get("mkt_home_wp")
        if mkt is not None and g.get("ml_edge") is not None and g["ml_edge"] >= ML_MIN_EDGE:
            mp = mkt if g["pick"] == g["home"] else 1 - mkt
            if 0.15 <= mp <= 0.85:
                opts.append(("Moneyline", "ml", g["pick"], None))
        if g.get("book_total") is not None and g.get("ou_pick"):
            opts.append(("Total", "total", g["ou_pick"], g.get("ou_best")))
        for typ, kind, side, line in opts:
            cats = member_cats(g, kind)
            st = {c: CAT_STATUS.get(c) for c in cats}
            greens = [c for c, v in st.items() if v == "green"]
            if not greens or "red" in st.values():
                continue
            if typ in ("Spread", "Moneyline"):
                faded = g["away"] if side == g["home"] else g["home"]
                if trend_status(trend, side) == "red" or trend_status(trend, faded) == "green":
                    continue
            play = {"type": typ, "side": side, "game": g["id"], "greens": greens,
                    "yellows": [c for c, v in st.items() if v == "yellow"],
                    "matchup": f"{g['away']} @ {g['home']}", "tip": g["tip"], "tip_iso": g["tip_iso"]}
            if typ == "Spread" and line:
                play.update(line=line["point"], price=line["price"], book=line["book"])
            elif typ == "Moneyline":
                play.update(price=g["ml_home"] if side == g["home"] else g["ml_away"])
            elif typ == "Total" and line:
                play.update(line=line["point"], price=line["price"], book=line["book"])
            play["prob"] = 0.5
            out.append(play)
            g.setdefault("green_plays", []).append(play)
    out.sort(key=lambda p: (-len(p["greens"]), len(p["yellows"]), p["tip_iso"]))
    return out


# ------------------------------------------------------------ Projection ---
def project(home: dict, away: dict, avg_o: float, avg_t: float, neutral: bool):
    poss = home["adj_t"] * away["adj_t"] / avg_t
    pts_h = home["adj_o"] * away["adj_d"] / avg_o * poss / 100
    pts_a = away["adj_o"] * home["adj_d"] / avg_o * poss / 100
    hca = 0.0 if neutral else HCA
    pts_h += hca / 2
    pts_a -= hca / 2
    margin = pts_h - pts_a
    wp = 0.5 * (1 + math.erf(margin / (MARGIN_SD * math.sqrt(2))))
    return round(pts_h, 1), round(pts_a, 1), round(margin, 1), round(poss, 1), wp


def build(kp: dict, odds: list, now: datetime) -> dict:
    avg_o = statistics.mean(t["adj_o"] for t in kp.values())
    avg_t = statistics.mean(t["adj_t"] for t in kp.values())
    neutral_ids = set(json.loads(NEUTRAL_FILE.read_text())) if NEUTRAL_FILE.exists() else set()
    espn = espn_neutral(list(kp.keys()), now.astimezone(TZ).date())
    match = Matcher(kp.keys(), [n for ev in odds for n in (ev["home_team"], ev["away_team"])])
    today = now.astimezone(TZ).date()
    games = []
    today_names = set()
    for ev in odds:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.astimezone(TZ).date() != today:
            continue
        h_name, a_name = ev["home_team"], ev["away_team"]
        today_names.update((h_name, a_name))
        h_kp, a_kp = match(h_name), match(a_name)
        if not h_kp or not a_kp:
            continue
        home_spreads, ml_home_p, books = {}, [], []
        ml_prices_h, ml_prices_a = [], []
        totals, best_over, best_under = {}, None, None
        best_home = best_away = None
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                outs = {o["name"]: o for o in mk["outcomes"]}
                if mk["key"] == "totals" and "Over" in outs and "Under" in outs:
                    pt = outs["Over"]["point"]
                    totals[bk["key"]] = pt
                    if best_over is None or pt < best_over[0]:
                        best_over = (pt, outs["Over"]["price"], bk["key"])
                    if best_under is None or outs["Under"]["point"] > best_under[0]:
                        best_under = (outs["Under"]["point"], outs["Under"]["price"], bk["key"])
                    continue
                if h_name not in outs or a_name not in outs:
                    continue
                if mk["key"] == "spreads":
                    hp, ap = outs[h_name]["point"], outs[a_name]["point"]
                    home_spreads[bk["key"]] = hp
                    if best_home is None or hp > best_home[0]:
                        best_home = (hp, outs[h_name]["price"], bk["key"])
                    if best_away is None or ap > best_away[0]:
                        best_away = (ap, outs[a_name]["price"], bk["key"])
                elif mk["key"] == "h2h":
                    ph, pa = implied(outs[h_name]["price"]), implied(outs[a_name]["price"])
                    ml_home_p.append(ph / (ph + pa))  # vig removed
                    ml_prices_h.append(outs[h_name]["price"]); ml_prices_a.append(outs[a_name]["price"])
        if not home_spreads:
            continue
        espn_flag = espn.get(frozenset((h_kp, a_kp)))
        neutral = ev["id"] in neutral_ids or bool(espn_flag)
        pts_h, pts_a, margin, poss, wp = project(kp[h_kp], kp[a_kp], avg_o, avg_t, neutral)
        book_spread = statistics.median(home_spreads.values())  # home line, e.g. -6.5
        book_margin = -book_spread                               # book's implied home margin
        diff = round(margin - book_margin, 1)                    # + = value on home
        pick_home = diff > 0
        mkt_wp = statistics.mean(ml_home_p) if ml_home_p else None
        model_wp = wp if mkt_wp is None else WP_BLEND * wp + (1 - WP_BLEND) * mkt_wp
        best = best_home if pick_home else best_away
        games.append({
            "id": ev["id"],
            "tip": start.astimezone(TZ).strftime("%-I:%M %p"),
            "tip_iso": start.isoformat(),
            "home": h_kp, "away": a_kp, "home_raw": h_name, "away_raw": a_name,
            "home_rank": kp[h_kp]["rank"], "away_rank": kp[a_kp]["rank"],
            "home_conf": kp[h_kp]["conf"], "away_conf": kp[a_kp]["conf"],
            "neutral": neutral,
            "book_spread": book_spread,
            "books": {BOOK_LABELS.get(k, k): v for k, v in home_spreads.items()},
            "proj_spread": round(-margin, 1),
            "proj_score": f"{a_kp} {pts_a:.0f}, {h_kp} {pts_h:.0f}",
            "poss": poss,
            "diff": diff,
            "pick": h_kp if pick_home else a_kp,
            "best_line": None if best is None else
                {"point": best[0], "price": best[1], "book": BOOK_LABELS.get(best[2], best[2])},
            "kp_home_wp": round(wp, 3),
            "mkt_home_wp": None if mkt_wp is None else round(mkt_wp, 3),
            "model_home_wp": round(model_wp, 3),
            "ml_min_edge": ML_MIN_EDGE,
            "neutral_src": "espn" if espn_flag else ("manual" if ev["id"] in neutral_ids else None),
            "home_luck": kp[h_kp].get("luck"), "away_luck": kp[a_kp].get("luck"),
            "proj_total": round(pts_h + pts_a, 1),
            "proj_home_pts": round(pts_h, 1), "proj_away_pts": round(pts_a, 1),
            "imp_home": None if not totals else round((statistics.median(totals.values()) - book_spread) / 2, 1),
            "imp_away": None if not totals else round((statistics.median(totals.values()) + book_spread) / 2, 1),
            "book_total": statistics.median(totals.values()) if totals else None,
            "totals_books": {BOOK_LABELS.get(k, k): v for k, v in totals.items()},
            "tot_diff": round(pts_h + pts_a - statistics.median(totals.values()), 1) if totals else None,
            "ou_pick": None if not totals else ("Over" if pts_h + pts_a > statistics.median(totals.values()) else "Under"),
            "ou_best": None if not totals else (lambda b: None if b is None else
                {"point": b[0], "price": b[1], "book": BOOK_LABELS.get(b[2], b[2])})(
                best_over if pts_h + pts_a > statistics.median(totals.values()) else best_under),
            "ml_home": statistics.median(ml_prices_h) if ml_prices_h else None,
            "ml_away": statistics.median(ml_prices_a) if ml_prices_a else None,
            "ml_edge": None if mkt_wp is None else round(
                (model_wp - mkt_wp) if pick_home else (mkt_wp - model_wp), 3),
        })
    games.sort(key=lambda g: abs(g["diff"]), reverse=True)
    upcoming = [g for g in games if datetime.fromisoformat(g["tip_iso"]) > now]
    top5, dropped = top_plays(upcoming, TREND)
    greens = green_plays(upcoming, TREND)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / "name_map.json").write_text(json.dumps(
        {"matched": dict(sorted(match.map.items())), "unmatched": sorted(match.unmatched),
         "collisions": match.collisions, "espn_unmatched": ESPN_MISSED}, indent=1))
    if match.collisions:
        print(f"Name collisions (closest kept, others unmatched): {match.collisions}")
    return {"games": games, "top5": top5, "top5_dropped": dropped, "green_plays": greens, "unmatched": sorted(n for n in match.unmatched if n in today_names), "avg_o": round(avg_o, 1),
            "avg_t": round(avg_t, 1)}


KP_FILE = ROOT / "data" / "kenpom.json"
KP_META: dict = {}


def load_kenpom():
    """In GitHub Actions, use the ratings snapshot pushed from your computer."""
    if os.getenv("KENPOM_HTML") or not KP_FILE.exists():
        return parse_kenpom(fetch_kenpom_html())
    snap = json.loads(KP_FILE.read_text())
    print(f"Using KenPom snapshot pulled {snap['pulled_at']}")
    KP_META["pulled_at"] = snap.get("pulled_at", "")
    return snap["teams"], snap["through"]


TREND: dict = {}


def freeze_picks(payload):
    """Top 5, red-card drops and Green cards are picked once per day, at the first refresh.
    Later refreshes reuse them, so the record matches what was shown that morning."""
    path = DOCS / "data" / "picks" / f"{payload['slate_date']}.json"
    keys = ("top5", "top5_dropped", "green_plays")
    if path.exists():
        frozen = json.loads(path.read_text())
        for g in payload["games"]:
            for f in ("top_plays", "top5_dropped", "green_plays"):
                g.pop(f, None)
        by_id = {g["id"]: g for g in payload["games"]}
        for k, f in zip(keys, ("top_plays", "top5_dropped", "green_plays")):
            payload[k] = frozen.get(k, [])
            for p in payload[k]:
                if p["game"] in by_id:
                    by_id[p["game"]].setdefault(f, []).append(p)
        print(f"Using picks frozen at {frozen['frozen_at']}")
    elif KP_META.get("pulled_at", "")[:10] == payload["slate_date"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"frozen_at": payload["generated"], **{k: payload.get(k, []) for k in keys}}, indent=1))
        print("Froze today's picks")
    else:
        # e.g. a delayed evening run that crosses midnight: it still uses yesterday's ratings,
        # so don't lock in today's picks yet; show none until today's KenPom file arrives
        for g in payload["games"]:
            for f in ("top_plays", "top5_dropped", "green_plays"):
                g.pop(f, None)
        for k in keys:
            payload[k] = []
        print("Picks not frozen: KenPom snapshot is not from today")


def main():
    now = datetime.now(timezone.utc)
    led = DOCS / "data" / "ledger.json"
    if led.exists():
        from track import summarize
        _s = summarize(json.loads(led.read_text()))
        TREND.update({t["team"]: t for t in _s["teams"]})
        CAT_STATUS.update({c["key"]: c["status"] for c in _s.get("categories", [])})
    kp, through = load_kenpom()
    result = build(kp, fetch_odds(), now)
    payload = {
        "slate_date": now.astimezone(TZ).strftime("%Y-%m-%d"),
        "generated": now.astimezone(TZ).strftime("%b %-d, %Y %-I:%M %p ET"),
        "kenpom_through": through,
        "hca": HCA, "margin_sd": MARGIN_SD, "luck_flag": LUCK_FLAG,
        "wp_blend": WP_BLEND, "ml_min_edge": ML_MIN_EDGE, **result,
    }
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    from track import update_track
    freeze_picks(payload)
    payload["track"] = update_track(payload["games"], now, DOCS / "data" / "ledger.json")
    payload["team_info"] = {n: {"rank": v["rank"], "luck": v.get("luck"), "record": v.get("record"),
                                "conf": v.get("conf"), "adj_em": v.get("adj_em")} for n, v in kp.items()}
    (DOCS / "data" / f"{payload['slate_date']}.json").write_text(json.dumps(payload, indent=2))
    (DOCS / "data" / "latest.json").write_text(json.dumps(payload, indent=2))
    tpl = (ROOT / "src" / "template.html").read_text()
    (DOCS / "index.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(payload)))
    print(f"{len(payload['games'])} games written. Unmatched names: {payload['unmatched'] or 'none'}")


if __name__ == "__main__":
    main()
