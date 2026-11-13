"""Builds SYNTHETIC test fixtures (fake ratings + fake odds) for exercising the pipeline."""
import json, random
from datetime import datetime, timezone, timedelta
random.seed(7)
real = ["Duke","Connecticut","Houston","Saint Mary's","N.C. State","Michigan St.","Miami FL","St. John's","Loyola Chicago","Kansas"]
names = real + [f"Team {i}" for i in range(355)]
rows = []
for i, n in enumerate(names, 1):
    o = round(random.uniform(95, 125), 1); d = round(random.uniform(92, 118), 1); t = round(random.uniform(62, 74), 1)
    rows.append(f'<tr><td>{i}</td><td><a href="team.php?team={n}">{n}</a> <span class="seed"></span></td><td>ACC</td><td>2-0</td>'
                f'<td>{o-d:+.2f}</td><td>{o}</td><td class="td-left"><span>1</span></td><td>{d}</td><td><span>1</span></td>'
                f'<td>{t}</td><td><span>1</span></td><td>+.01</td></tr>')
open("tests/fixtures/kenpom.html","w").write('<html><body>Data through games of Thursday, November 12<table id="ratings-table"><thead><tr><th>Rk</th></tr></thead><tbody>'+"".join(rows)+"</tbody></table></body></html>")
now = datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0)
pairs = [("Duke Blue Devils","Kansas Jayhawks",-2.5),("UConn Huskies","St. John's Red Storm",-4.5),
         ("Houston Cougars","Saint Mary's Gaels",-6.0),("NC State Wolfpack","Michigan St Spartans",1.5),
         ("Miami Hurricanes","Loyola (Chi) Ramblers",-3.0),("Gonzaga Bulldogs","Fake Opponent",-20)]
ev = []
for k,(h,a,s) in enumerate(pairs):
    bms = []
    for b,j in [("draftkings",0),("fanduel",0.5),("betmgm",-0.5)]:
        bms.append({"key":b,"markets":[{"key":"spreads","outcomes":[{"name":h,"point":s+j,"price":-110},{"name":a,"point":-(s+j),"price":-110}]},
                                       {"key":"h2h","outcomes":[{"name":h,"price":-150 if s<0 else 120},{"name":a,"price":130 if s<0 else -140}]}]})
    ev.append({"id":f"g{k}","commence_time":(now - timedelta(hours=3)).isoformat().replace("+00:00","Z"),"home_team":h,"away_team":a,"bookmakers":bms})
json.dump(ev, open("tests/fixtures/odds.json","w"))
