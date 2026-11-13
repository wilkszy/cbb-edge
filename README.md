# CBB Edge: Book vs. KenPom

A daily dashboard of college basketball games where the sportsbook spread differs most from a projection built on KenPom's public ratings. Runs on GitHub Actions and publishes to GitHub Pages: no server, no cost.

## How it runs

KenPom blocks GitHub's servers, so the ratings pull happens on your computer:

1. **Your computer, each morning:** `python src/pull_kenpom.py` saves the public KenPom ratings to `data/kenpom.json` and pushes them.
2. **GitHub, automatically:** that push triggers the workflow, which pulls odds and rebuilds the dashboard. Lines refresh again at about 4:17 PM ET using the morning's ratings.

Dashboard: https://wilkszy.github.io/cbb-edge/

## One-time setup on your computer

Needs Python 3 and Git, signed in to GitHub.

    git clone https://github.com/wilkszy/cbb-edge.git
    cd cbb-edge
    pip3 install -r requirements.txt
    python3 src/pull_kenpom.py        # test run

**Schedule it on a Mac** (runs 9:05 AM daily if the Mac is awake): run `crontab -e` and add

    5 9 * * * cd ~/cbb-edge && /usr/bin/python3 src/pull_kenpom.py >> ~/cbb-edge-pull.log 2>&1

**Schedule it on Windows** (catches up after sleep): in Command Prompt from the cbb-edge folder,

    schtasks /create /tn "CBB Edge KenPom" /sc daily /st 09:05 /tr "cmd /c cd /d %CD% && python src\pull_kenpom.py"

## How the numbers work

- **Projection:** possessions = home AdjT x away AdjT / D-I average tempo. Each team's points = its AdjO x opponent AdjD / D-I average efficiency x possessions / 100. The home team gets 3.2 points (`HCA`), split evenly.
- **Book line:** median home spread across DraftKings, FanDuel, BetMGM, Caesars, ESPN BET, BetRivers, Fanatics.
- **Spread gap:** projected home margin minus the book's implied home margin. The sign tells you the value side.
- **ML edge:** projected win probability (normal distribution, 11-pt std. dev.) minus the market's no-vig probability.

This is a ratings-based projection, **not** KenPom's own game predictions (those need a KenPom API subscription). It knows nothing about injuries or lineup news.

## Maintenance

- **Neutral-site games:** the odds feed doesn't flag them. Add the game's Odds API event `id` (shown in `docs/data/latest.json`) to `src/neutral_sites.json` to drop home-court advantage for it.
- **Unmatched team names:** listed at the bottom of the dashboard. Add a line to `src/aliases.json`: `"Odds API name": "KenPom name"`.
- **If a run fails:** the Actions tab shows the error. If the local pull fails, kenpom.com may have changed its page layout; the script stops rather than publishing bad numbers.
- **Archive:** each day's slate is saved to `docs/data/YYYY-MM-DD.json`, so you can grade results later.

## Local test

    pip install -r requirements.txt
    python tests/make_fixtures.py   # synthetic data, for testing only
    KENPOM_HTML=tests/fixtures/kenpom.html ODDS_JSON=tests/fixtures/odds.json python src/edge.py
