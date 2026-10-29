import importlib.util
import sqlite3
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'dashboard' / 'generate.py'
spec = importlib.util.spec_from_file_location('keeper_dashboard', MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_missing_inputs():
    result = mod.build(None, None, None)
    assert 'No evaluation results JSON supplied' in result
    assert 'Emails ingested' in result
    assert '<html' in result

def test_router_db_and_escaping(tmp_path):
    db=tmp_path/'router.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE calls (model TEXT, served_model TEXT, status TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, cost_usd REAL)')
        c.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?)', ('<script>alert(1)</script>', None,'ok',20,10,100,None))
    result=mod.build(None,db,None)
    assert '&lt;script&gt;' in result
    assert '<script>alert(1)</script>' not in result
    assert 'Not recorded' in result

def test_empty_tables(tmp_path):
    db=tmp_path/'keeper.db'
    with sqlite3.connect(db) as c: c.execute('CREATE TABLE opportunities (eligibility TEXT)')
    assert 'Opportunity eligibility' in mod.build(db,None,None)
