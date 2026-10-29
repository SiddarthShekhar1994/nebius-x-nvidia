# Generate a self-contained, read-only Keeper evidence dashboard.
from __future__ import annotations
import argparse
import collections
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TABLES = {"email_records", "triage_results", "opportunities", "applications", "application_status_changes", "security_events", "calls"}

def rows(db: Path | None, table: str) -> list[dict]:
    if table not in TABLES:
        raise ValueError("Unknown table")
    if not db or not db.is_file():
        return []
    uri = db.resolve().as_uri() + '?mode=ro'
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            return [dict(r) for r in conn.execute('SELECT * FROM "' + table + '"')]
    except sqlite3.DatabaseError:
        return []

def load_eval(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (ValueError, OSError):
        return {}

def fmt(v):
    return html.escape(str(v if v is not None else '—'), quote=True)

def table(headers, data):
    if not data:
        return '<p class="muted">No recorded data available.</p>'
    head=''.join('<th>'+fmt(x)+'</th>' for x in headers)
    body=''.join('<tr>'+''.join('<td>'+fmt(x)+'</td>' for x in row)+'</tr>' for row in data)
    return '<div class="tablewrap"><table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table></div>'

def count_table(items, name):
    counts=collections.Counter(str(x.get(name) or 'unknown') for x in items)
    return table(['Category','Count'], sorted(counts.items(),key=lambda x:(-x[1],x[0])))

def build(keeper: Path | None, router: Path | None, evaluation: Path | None) -> str:
    emails=rows(keeper,'email_records'); triage=rows(keeper,'triage_results')
    opportunities=rows(keeper,'opportunities'); apps=rows(keeper,'applications')
    history=rows(keeper,'application_status_changes'); sec=rows(keeper,'security_events')
    calls=rows(router,'calls'); ev=load_eval(evaluation)
    model_stats={}
    for c in calls:
        key=(c.get('served_model') or c.get('model') or 'unknown')
        x=model_stats.setdefault(key,{'calls':0,'errors':0,'tokens':0,'cost':0.,'cost_count':0,'latencies':[]})
        x['calls']+=1;x['errors']+=c.get('status')!='ok'
        x['tokens']+=(c.get('prompt_tokens') or 0)+(c.get('completion_tokens') or 0)
        if c.get('cost_usd') is not None: x['cost']+=c['cost_usd'];x['cost_count']+=1
        if isinstance(c.get('latency_ms'),(float,int)):x['latencies'].append(c['latency_ms'])
    models=table(['Served model (or requested if missing)','Calls','Errors','Tokens','Mean latency','Logged cost'],[
        (name,s['calls'],s['errors'],s['tokens'],str(round(sum(s['latencies'])/len(s['latencies'])))+' ms' if s['latencies'] else '—',f"${s['cost']:.5f}" if s['cost_count'] else 'Not recorded') for name,s in sorted(model_stats.items())])
    safe_sec=[]
    for e in sec[-12:]:
        # Do not display raw destination, source emails, or evidence references: may contain sensitive strings.
        safe_sec.append((e.get('occurred_at','—'),e.get('category','—'),e.get('outcome','—'),'Details withheld'))
    def card(label,value): return '<div class="metric"><span>'+fmt(label)+'</span><strong>'+fmt(value)+'</strong></div>'
    metrics=''.join([card('Emails ingested',len(emails)),card('Triage results',len(triage)),card('Opportunities',len(opportunities)),card('Applications',len(apps)),card('Router calls',len(calls)),card('Security events',len(sec))])
    ev_section='<p class="muted">No evaluation results JSON supplied. No accuracy claim can be made.</p>'
    if ev:
        # Deliberately show source metadata without interpreting arbitrary nested result formats.
        keys=['sample_size','total','correct','accuracy','career_to_junk','unknown_count','model','dataset','source','notes']
        available=[(k,json.dumps(ev[k],ensure_ascii=False)[:300] if isinstance(ev[k],(dict,list)) else ev[k]) for k in keys if k in ev]
        ev_section=table(['Evaluation field','Recorded value'],available) if available else '<p class="muted">Evaluation JSON present, but no recognized top-level metric fields. Inspect the JSON schema before making claims.</p>'
    status='Fixture or real data depends on supplied database provenance. Counts are not a performance benchmark.'
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Keeper | Evidence Dashboard</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1120;color:#e8edf8;font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}.wrap{max-width:1100px;margin:auto;padding:38px 24px 70px}h1{font-size:38px;letter-spacing:-1.2px;margin:0}h2{font-size:19px;margin:0 0 14px}.eyebrow{color:#8fb6ff;font-weight:700;text-transform:uppercase;letter-spacing:2px;font-size:12px}.muted{color:#9eacc5}.hero{margin-bottom:28px}.pill{display:inline-block;border:1px solid #445576;border-radius:100px;padding:4px 12px;color:#c6d7f7;font-size:12px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.metric,.panel{border:1px solid #28344e;background:#141e31;border-radius:14px;padding:20px}.metric span{display:block;color:#9eacc5;font-size:13px}.metric strong{display:block;font-size:30px;margin-top:4px}.sections{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px}.panel.wide{grid-column:1/-1}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #2a3650;vertical-align:top}th{color:#9eb6df;font-size:12px}footer{margin-top:24px;color:#9eacc5;font-size:12px}.note{border-left:3px solid #8fb6ff;padding-left:14px;color:#c3d0e7}@media(max-width:700px){.metrics{grid-template-columns:repeat(2,1fr)}.sections{grid-template-columns:1fr}.panel.wide{grid-column:auto}h1{font-size:30px}}@media print{:root{color-scheme:light}body{background:white;color:#14203a}.panel,.metric{background:white;color:#14203a;border-color:#ccd5e3}.muted,.metric span{color:#46536c}}
</style></head><body><main class="wrap"><header class="hero"><div class="eyebrow">Keeper / Offline evidence report</div><h1>From inbox noise to action.</h1><p class="muted">Generated '''+fmt(now)+''' · Local, read-only, self-contained dashboard</p><span class="pill">Evidence-first · No invented metrics</span></header><section class="metrics">'''+metrics+'''</section><div class="sections"><section class="panel"><h2>Inbox triage</h2>'''+count_table(triage,'bucket')+'''</section><section class="panel"><h2>Opportunity eligibility</h2>'''+count_table(opportunities,'eligibility')+'''</section><section class="panel"><h2>Application stages</h2>'''+count_table(apps,'status')+'''</section><section class="panel"><h2>Application history</h2>'''+count_table(history,'status')+'''</section><section class="panel wide"><h2>Model usage and costs</h2><p class="note">Only router-logged calls are included. Hermes-native chat traffic may not appear here. Missing cost data is not treated as $0.</p>'''+models+'''</section><section class="panel wide"><h2>Evaluation evidence</h2>'''+ev_section+'''</section><section class="panel wide"><h2>Security audit (sanitized)</h2>'''+table(['Timestamp','Category','Outcome','Evidence'],safe_sec)+'''</section><section class="panel wide"><h2>Replay / counterfactual</h2><p class="muted">No measured replay provided. Do not claim observed missed deadlines or time saved. A two-week without/with Keeper comparison must be explicitly labeled as a fixture-driven scenario.</p></section></div><footer>'''+fmt(status)+''' Source paths and personal identifiers are deliberately excluded from this report.</footer></main></body></html>'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--keeper-db',type=Path)
    p.add_argument('--router-db',type=Path)
    p.add_argument('--eval-json',type=Path)
    p.add_argument('--output',type=Path,default=Path('dashboard/report.html'))
    args=p.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(build(args.keeper_db,args.router_db,args.eval_json),encoding='utf-8')
    print(f'Generated {args.output}')
if __name__=='__main__': main()
