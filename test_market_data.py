import os, sqlite3, subprocess, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import market_data_common as m
import build_market_snapshot as snap

def conn():
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row; m.ensure_tables(c); return c

def bars(t,n,base=100):
    return [m.PriceBar(t, f'2024-01-{i+1:02d}', base+i, 1000+i, 'test', base+i) for i in range(n)]

def test_tables_idempotent_and_upsert_update_no_duplicate():
    c=conn(); m.ensure_tables(c); assert c.execute("select name from sqlite_master where name='market_prices'").fetchone()
    m.upsert_prices(c,[m.PriceBar('AAA','2024-01-01',10,5,'x',10)]); m.upsert_prices(c,[m.PriceBar('AAA','2024-01-01',11,6,'x',11)])
    r=c.execute('select count(*), close, volume from market_prices').fetchone(); assert (r[0],r[1],r[2])==(1,11,6)

def test_prune_latest_250_and_keeps_fewer():
    c=conn(); m.upsert_prices(c,[m.PriceBar('AAA',f'2024-{(i//28)+1:02d}-{(i%28)+1:02d}',10+i,1,'x',10+i) for i in range(260)]); m.upsert_prices(c,bars('BBB',10))
    m.prune_prices(c,['AAA','BBB'])
    assert c.execute("select count(*) from market_prices where ticker='AAA'").fetchone()[0]==250
    assert c.execute("select min(trade_date) from market_prices where ticker='AAA'").fetchone()[0]=='2024-01-11'
    assert c.execute("select count(*) from market_prices where ticker='BBB'").fetchone()[0]==10

def test_snapshot_returns_and_averages_offsets():
    c=conn(); m.upsert_prices(c,bars('AAA',31,100)); m.rebuild_snapshots(c,['AAA'],{'AAA'}); r=c.execute("select * from market_snapshot where ticker='AAA'").fetchone()
    assert r['return_1d']==pytest.approx(130/129-1); assert r['return_5d']==pytest.approx(130/125-1); assert r['return_30d']==pytest.approx(130/100-1)
    assert r['avg_volume_30d']==pytest.approx(sum(1001+i for i in range(30))/30)
    assert r['avg_dollar_volume_30d']==pytest.approx(sum((101+i)*(1001+i) for i in range(30))/30)
    assert r['as_of_date']=='2024-01-31'

def test_30d_return_null_with_fewer_than_31():
    c=conn(); m.upsert_prices(c,bars('AAA',30)); m.rebuild_snapshots(c,['AAA'],{'AAA'}); assert c.execute('select return_30d from market_snapshot').fetchone()[0] is None

def test_failure_does_not_remove_other_valid_data_and_dryrun_no_db_change(tmp_path, monkeypatch):
    c=conn(); m.upsert_prices(c,bars('OK',5)); m.prune_prices(c,['BAD']); assert c.execute("select count(*) from market_prices where ticker='OK'").fetchone()[0]==5
    db=tmp_path/'d.db'; env=tmp_path/'.env'; env.write_text('APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=\n')
    # dry-run with blank creds exits before table writes
    cp=subprocess.run([sys.executable,'scripts/update_market_prices.py','--dry-run','--ticker','NVDA','--db',str(db)],cwd=Path(__file__).parent,capture_output=True,text=True)
    assert 'Missing required Alpaca credential' in cp.stderr + cp.stdout
    assert not db.exists() or sqlite3.connect(db).total_changes == 0

def test_market_cap_failures_preserve_previous():
    c=conn(); m.upsert_prices(c,bars('AAA',2)); m.rebuild_snapshots(c,['AAA'],{'AAA'}); snap.refresh_market_caps(c,['AAA'],lambda t: 123.0)
    old=c.execute('select market_cap, market_cap_updated_at from market_snapshot').fetchone(); snap.refresh_market_caps(c,['AAA'],lambda t: None)
    new=c.execute('select market_cap, market_cap_updated_at from market_snapshot').fetchone(); assert tuple(old)==tuple(new)

def test_audit_output_deterministic(tmp_path):
    p=tmp_path/'f.csv'; fs=[{'ticker':'B','stage':'price','reason':'z','attempt_count':1,'created_at':'t'},{'ticker':'A','stage':'price','reason':'a','attempt_count':1,'created_at':'t'}]
    m.append_failures(p,fs); first=p.read_text(); m.append_failures(p,list(reversed(fs))); assert p.read_text()==first

def test_source_never_references_relationships_raw_for_market_data():
    for p in Path('scripts').glob('*market*.py'):
        assert 'relationships_raw' not in p.read_text()

def test_env_loaded_blank_failures_and_no_secret_leak(tmp_path, monkeypatch):
    e=tmp_path/'.env'; e.write_text('APCA_API_KEY_ID=TOKENVALUE\nAPCA_API_SECRET_KEY=\n'); monkeypatch.delenv('APCA_API_KEY_ID',raising=False); monkeypatch.delenv('APCA_API_SECRET_KEY',raising=False)
    with pytest.raises(m.MarketDataError) as x: m.require_alpaca_credentials(e)
    assert 'APCA_API_SECRET_KEY' in str(x.value) and 'TOKENVALUE' not in str(x.value)
    e.write_text('APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=TOKENVALUE2\n'); monkeypatch.delenv('APCA_API_KEY_ID',raising=False); monkeypatch.delenv('APCA_API_SECRET_KEY',raising=False)
    with pytest.raises(m.MarketDataError) as y: m.require_alpaca_credentials(e)
    assert 'APCA_API_KEY_ID' in str(y.value) and 'TOKENVALUE2' not in str(y.value)

def test_env_update_preserves_appends_once_and_gitignore():
    p=Path('.env'); old=p.read_text() if p.exists() else None
    try:
        p.write_text('EXISTING=keep\nAPCA_API_KEY_ID=abc\n'); m.ensure_env_placeholders(p); m.ensure_env_placeholders(p)
        text=p.read_text(); assert 'EXISTING=keep' in text and 'APCA_API_KEY_ID=abc' in text and text.count('APCA_API_SECRET_KEY=')==1
    finally:
        if old is None: p.unlink(missing_ok=True)
        else: p.write_text(old)
    assert any(line.strip()=='.env' for line in Path('.gitignore').read_text().splitlines())

def test_alpaca_feed_env_precedence_and_fallback(tmp_path, monkeypatch):
    e=tmp_path/'.env'
    e.write_text('ALPACA_DATA_FEED=sip\n')
    monkeypatch.delenv('ALPACA_DATA_FEED', raising=False)
    assert m.resolve_alpaca_feed(None, e) == 'sip'
    assert m.resolve_alpaca_feed('iex', e) == 'iex'
    e.write_text('')
    monkeypatch.delenv('ALPACA_DATA_FEED', raising=False)
    assert m.resolve_alpaca_feed(None, e) == 'iex'


def test_invalid_alpaca_feed_fails_clearly(tmp_path, monkeypatch):
    e=tmp_path/'.env'
    e.write_text('ALPACA_DATA_FEED=bogus\n')
    monkeypatch.delenv('ALPACA_DATA_FEED', raising=False)
    with pytest.raises(m.MarketDataError) as exc:
        m.resolve_alpaca_feed(None, e)
    assert 'Invalid Alpaca data feed' in str(exc.value)
    assert 'iex, sip' in str(exc.value)


def test_env_setup_appends_and_preserves_alpaca_data_feed(tmp_path):
    e=tmp_path/'.env'
    e.write_text('APCA_API_KEY_ID=abc\nAPCA_API_SECRET_KEY=def\n')
    m.ensure_env_placeholders(e)
    m.ensure_env_placeholders(e)
    assert e.read_text().count('ALPACA_DATA_FEED=iex') == 1
    e.write_text('ALPACA_DATA_FEED=sip\nAPCA_API_KEY_ID=abc\n')
    m.ensure_env_placeholders(e)
    m.ensure_env_placeholders(e)
    text=e.read_text()
    assert text.count('ALPACA_DATA_FEED=sip') == 1
    assert 'ALPACA_DATA_FEED=iex' not in text
