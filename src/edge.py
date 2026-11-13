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
  MARGIN_SD        std dev of game margin for win prob (default 11.0)
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
MARGIN_SD = float(os.getenv("MARGIN_SD", "11.0"))
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
        params={"apiKey": key, "regions": "us", "markets": "spreads,h2h",
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
    s = s.lower().replace("&", "and").replace("'", "").replace(".", "")
    s = re.sub(r"\bstate\b", "st", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Matcher:
    def __init__(self, kp_names):
        self.aliases = json.loads(ALIASES_FILE.read_text()) if ALIASES_FILE.exists() else {}
        self.by_norm = {norm(n): n for n in kp_names}
        self.unmatched = set()

    def __call__(self, odds_name: str):
        if odds_name in self.aliases:
            target = self.by_norm.get(norm(self.aliases[odds_name]))
            if target:
                return target
        words = norm(odds_name).split()
        # Odds API = "<School> <Mascot>". Take the longest prefix that is a KenPom team.
        for i in range(len(words), 0, -1):
            cand = " ".join(words[:i])
            if cand in self.by_norm:
                return self.by_norm[cand]
        self.unmatched.add(odds_name)
        return None


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
    match = Matcher(kp.keys())
    today = now.astimezone(TZ).date()
    games = []
    for ev in odds:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.astimezone(TZ).date() != today:
            continue
        h_name, a_name = ev["home_team"], ev["away_team"]
        h_kp, a_kp = match(h_name), match(a_name)
        if not h_kp or not a_kp:
            continue
        home_spreads, ml_home_p, books = {}, [], []
        best_home = best_away = None
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                outs = {o["name"]: o for o in mk["outcomes"]}
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
        if not home_spreads:
            continue
        neutral = ev["id"] in neutral_ids
        pts_h, pts_a, margin, poss, wp = project(kp[h_kp], kp[a_kp], avg_o, avg_t, neutral)
        book_spread = statistics.median(home_spreads.values())  # home line, e.g. -6.5
        book_margin = -book_spread                               # book's implied home margin
        diff = round(margin - book_margin, 1)                    # + = value on home
        pick_home = diff > 0
        mkt_wp = statistics.mean(ml_home_p) if ml_home_p else None
        best = best_home if pick_home else best_away
        games.append({
            "id": ev["id"],
            "tip": start.astimezone(TZ).strftime("%-I:%M %p"),
            "tip_iso": start.isoformat(),
            "home": h_kp, "away": a_kp,
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
            "ml_edge": None if mkt_wp is None else round(
                (wp - mkt_wp) if pick_home else ((1 - wp) - (1 - mkt_wp)), 3),
        })
    games.sort(key=lambda g: abs(g["diff"]), reverse=True)
    return {"games": games, "unmatched": sorted(match.unmatched), "avg_o": round(avg_o, 1),
            "avg_t": round(avg_t, 1)}


KP_FILE = ROOT / "data" / "kenpom.json"


def load_kenpom():
    """In GitHub Actions, use the ratings snapshot pushed from your computer."""
    if os.getenv("KENPOM_HTML") or not KP_FILE.exists():
        return parse_kenpom(fetch_kenpom_html())
    snap = json.loads(KP_FILE.read_text())
    print(f"Using KenPom snapshot pulled {snap['pulled_at']}")
    return snap["teams"], snap["through"]


def main():
    now = datetime.now(timezone.utc)
    kp, through = load_kenpom()
    result = build(kp, fetch_odds(), now)
    payload = {
        "slate_date": now.astimezone(TZ).strftime("%Y-%m-%d"),
        "generated": now.astimezone(TZ).strftime("%b %-d, %Y %-I:%M %p ET"),
        "kenpom_through": through,
        "hca": HCA, "margin_sd": MARGIN_SD, **result,
    }
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / f"{payload['slate_date']}.json").write_text(json.dumps(payload, indent=2))
    (DOCS / "data" / "latest.json").write_text(json.dumps(payload, indent=2))
    tpl = (ROOT / "src" / "template.html").read_text()
    (DOCS / "index.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(payload)))
    print(f"{len(payload['games'])} games written. Unmatched names: {payload['unmatched'] or 'none'}")


if __name__ == "__main__":
    main()
