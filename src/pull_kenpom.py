"""
Run on YOUR computer (KenPom blocks GitHub's servers).
Pulls the public kenpom.com ratings table, saves data/kenpom.json, and pushes it.
The push triggers the GitHub workflow, which pulls odds and rebuilds the dashboard.
"""
import json, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from edge import fetch_kenpom_html, parse_kenpom  # noqa: E402

def git(*args):
    subprocess.run(["git", "-C", str(ROOT), *args], check=True)

git("pull", "--rebase", "--quiet")
teams, through = parse_kenpom(fetch_kenpom_html())
out = ROOT / "data" / "kenpom.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"pulled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "through": through, "teams": teams}, indent=1))
print(f"Saved {len(teams)} teams (data through {through or 'n/a'}).")
git("add", "data/kenpom.json")
if subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"]).returncode:
    git("commit", "-m", f"KenPom ratings {datetime.now():%Y-%m-%d}")
    git("push", "--quiet")
    print("Pushed. GitHub will rebuild the dashboard in about a minute.")
else:
    print("Ratings unchanged since last pull; nothing to push.")
