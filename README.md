# CBB Edge: Book vs. KenPom

A daily dashboard of college basketball games where the sportsbook spread differs most from a projection built on KenPom's public ratings. Runs on GitHub Actions and publishes to GitHub Pages: no server, no cost.

## Setup (about 10 minutes)

1. **Get an odds key.** Sign up for the free plan at the-odds-api.com (500 credits/month). Each run uses about 2 credits, so two runs a day comes to about 120 a month.
2. **Create a GitHub repo** (private is fine for Actions; Pages on a private repo needs a paid GitHub plan, so use a public repo if you're on the free plan). Upload everything in this folder, including the hidden `.github` folder.
3. **Add the key.** Repo > Settings > Secrets and variables > Actions > New repository secret. Name: `ODDS_API_KEY`.
4. **Turn on Pages.** Repo > Settings > Pages > Source: "Deploy from a branch", Branch: `main`, folder `/docs`.
5. **Run it once.** Actions tab > "Daily CBB Edge refresh" > Run workflow. Your dashboard is live at `https://<username>.github.io/<repo>/` within a minute or two.

After that it refreshes automatically at about 9:17 AM and 4:17 PM ET. Edit the `cron` lines in `.github/workflows/daily.yml` to change the times.

## How the numbers work

- **Projection:** possessions = home AdjT x away AdjT / D-I average tempo. Each team's points = its AdjO x opponent AdjD / D-I average efficiency x possessions / 100. The home team gets 3.2 points (`HCA`), split evenly.
- **Book line:** median home spread across DraftKings, FanDuel, BetMGM, Caesars, ESPN BET, BetRivers, Fanatics.
- **Spread gap:** projected home margin minus the book's implied home margin. The sign tells you the value side.
- **ML edge:** projected win probability (normal distribution, 11-pt std. dev.) minus the market's no-vig probability.

This is a ratings-based projection, **not** KenPom's own game predictions (those need a KenPom API subscription). It knows nothing about injuries or lineup news.

## Maintenance

- **Neutral-site games:** the odds feed doesn't flag them. Add the game's Odds API event `id` (shown in `docs/data/latest.json`) to `src/neutral_sites.json` to drop home-court advantage for it.
- **Unmatched team names:** listed at the bottom of the dashboard. Add a line to `src/aliases.json`: `"Odds API name": "KenPom name"`.
- **If a run fails:** the Actions tab shows the error. The most likely cause is kenpom.com blocking GitHub's servers or changing its page layout; the script stops rather than publishing bad numbers.
- **Archive:** each day's slate is saved to `docs/data/YYYY-MM-DD.json`, so you can grade results later.

## Local test

    pip install -r requirements.txt
    python tests/make_fixtures.py   # synthetic data, for testing only
    KENPOM_HTML=tests/fixtures/kenpom.html ODDS_JSON=tests/fixtures/odds.json python src/edge.py
