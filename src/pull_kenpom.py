"""
Daily step on YOUR computer:
  1. Open https://kenpom.com in your browser and save the page (Cmd+S) to Downloads.
  2. Run: python3 src/pull_kenpom.py
Reads the newest saved KenPom page in ~/Downloads, saves data/kenpom.json, and pushes it.
The push triggers the GitHub workflow, which pulls odds and rebuilds the dashboard.
(kenpom.com blocks scripted requests, so the page has to come from your browser.)
"""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from edge import parse_kenpom  # noqa: E402

def git(*args):
    subprocess.run(["git", "-C", str(ROOT), *args], check=True)

def newest_saved_page():
    folder = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Downloads"
    cands = sorted([p for p in folder.glob("*.htm*") if time.time() - p.stat().st_mtime < 24 * 3600],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        html = p.read_text(errors="ignore")
        if "ratings-table" in html:
            return p, html
    sys.exit("No KenPom page saved in the last 24 hours. Open kenpom.com, press Cmd+S, "
             "save to Downloads (format: 'Web Page, HTML Only' or 'Page Source'), then rerun.")

git("pull", "--rebase", "--quiet")
path, html = newest_saved_page()
teams, through = parse_kenpom(html)
out = ROOT / "data" / "kenpom.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"pulled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "through": through, "teams": teams}, indent=1))
print(f"Read {path.name}: {len(teams)} teams (data through {through or 'n/a'}).")
git("add", "data/kenpom.json")
if subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"]).returncode:
    git("commit", "-m", f"KenPom ratings {datetime.now():%Y-%m-%d}")
    git("push", "--quiet")
    print("Pushed. Dashboard updates in about a minute: https://wilkszy.github.io/cbb-edge/")
else:
    print("Ratings unchanged since last pull; nothing to push.")
