"""Rebuild docs/index.html from the latest slate + ledger without calling any API."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from track import summarize
d = json.loads((ROOT / "docs/data/latest.json").read_text())
d["track"] = summarize(json.loads((ROOT / "docs/data/ledger.json").read_text()))
(ROOT / "docs/data/latest.json").write_text(json.dumps(d, indent=2))
tpl = (ROOT / "src/template.html").read_text()
(ROOT / "docs/index.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(d)))
print("Rebuilt page:", d["track"]["overall"]["n"], "graded games;", "big-spread ATS", d["track"]["overall"]["ats_big"])
