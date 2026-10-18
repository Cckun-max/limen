"""Synthetic IBKR fixtures. No login, trades, paid requests or model calls."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient
from ib_async import Stock, Ticker
from pydantic import ValidationError
import pytest

from server.app import create_app
from server.ai import snapshot_market, model_snapshot
from server.ibkr import Broker, IBMarket, ConnectionSettings, BAR_SIZES
from server.ibkr_sessions import Sessions, normalize_bars, timestamp, weekly_bars
from server.indicators import calculate
from server.models import TIMEFRAMES
from server.storage import Store

AT = timestamp(datetime(2026,10,17,12,tzinfo=timezone.utc))


def payload():
    rows=[]
    day=datetime(2026,6,1)
    while day < datetime(2026,10,17):
        if day.weekday()<5:
            rows.append({"date":day.strftime('%Y%m%d'),
                         "start":timestamp(day.replace(hour=9,minute=30),'America/New_York'),
                         "end":timestamp(day.replace(hour=16),'America/New_York')})
        day+=timedelta(days=1)
    return {"zone":"America/New_York","start":rows[0]["start"],"end":AT,"rows":rows,"fetched_at":AT}


def instrument(con_id=123, kind='stock'):
    c=Stock('AAPL','SMART','USD',conId=con_id)
    return {"symbol":f"ibkr:{con_id}","instrument_id":f"ibkr:{con_id}","provider":"ibkr","con_id":con_id,
            "display_symbol":"AAPL","native_symbol":"AAPL","asset_type":kind,"name":"Apple",
            "exchange":"SMART","currency":"USD","expiry":"","multiplier":"","tick_size":.01,
            "timezone":"America/New_York","session":"常规交易时段","volume_unit":"股",
            "contract":{k:getattr(c,k) for k in ('secType','conId','symbol','exchange','currency')}}


def raw_bars(sessions,tf):
    bars=[]
    for r in sessions.rows:
        if tf=='1d':
            times=[datetime.strptime(r['date'],'%Y%m%d').date()]
        else:
            times=[datetime.fromtimestamp(t/1000,timezone.utc) for t in range(r['start'],r['end'],TIMEFRAMES[tf])]
        bars.extend(NS(date=t,open=100.,high=102.,low=99.,close=101.,volume=1000.,barCount=10) for t in times)
    return bars[-500:]


def test_stock_overnight_preserves_indicators_and_internal_gap_does_not():
    s=Sessions(payload())
    raw=raw_bars(s,'30m')[-26:]
    bars=normalize_bars(raw,'30m','ibkr:123',s,AT-900000,AT)
    assert calculate(bars)[-1]['ema20'] is None
    assert calculate(bars,s.adjacent)[-1]['ema20']==101
    assert not s.gaps(bars,'30m',AT-900000)
    broken=bars[:20]+bars[21:]
    assert s.gaps(broken,'30m',AT-900000)
    assert calculate(broken,s.adjacent)[-1]['ema20'] is None


def test_opening_partial_bar_and_delayed_forming_bar():
    s=Sessions(payload()); start=s.rows[-1]['start']
    raw=[NS(date=datetime.fromtimestamp(t/1000,timezone.utc),open=100,high=101,low=99,close=100,volume=1,barCount=1)
         for t in (start,start+30*60000)]
    cutoff=start+45*60000
    bars=normalize_bars(raw,'1h','ibkr:123',s,cutoff,cutoff+900000)
    assert bars[0].end==bars[1].time
    assert bars[0].status=='confirmed'
    assert bars[1].status=='forming'
    assert s.gaps(bars,'1h',cutoff)==[]
    with pytest.raises(ValueError,match='截止'):
        normalize_bars(raw,'1h','ibkr:123',s,start-1,AT)


def test_timezone_dst_and_early_close():
    assert timestamp(datetime(2026,3,9,9,30),'America/New_York')-timestamp(datetime(2026,3,6,9,30),'America/New_York')==71*3600000
    p=payload();p['rows'][-1]['end']=p['rows'][-1]['start']+210*60000
    s=Sessions(p);bars=normalize_bars(raw_bars(s,'4h'),'4h','ibkr:123',s,AT,AT)
    assert bars[-1].end-bars[-1].time==210*60000
    assert not s.gaps(bars,'4h',AT)


def test_weekly_uses_exchange_dates_missing_day_is_not_confirmed():
    s=Sessions(payload());daily=normalize_bars(raw_bars(s,'1d'),'1d','ibkr:123',s,AT,AT)
    weeks=weekly_bars(daily,s,AT)
    assert weeks[-2].status=='confirmed'
    assert weeks[-1].status=='forming'  # conservative: current week not finalized before next Monday
    bad=weekly_bars([b for b in daily if b.time!=daily[-8].time],s,AT)
    assert bad[-2].status=='forming'
    assert s.gaps(daily[:-1],'1d',AT)


def test_multiple_session_segments_and_futures_reference_day():
    p={'zone':'America/Chicago','start':0,'end':AT,'rows':[
        {'date':'20261016','start':timestamp(datetime(2026,10,15,17),'America/Chicago'),'end':timestamp(datetime(2026,10,16,15,15),'America/Chicago')},
        {'date':'20261016','start':timestamp(datetime(2026,10,16,15,30),'America/Chicago'),'end':timestamp(datetime(2026,10,16,16),'America/Chicago')}]}
    s=Sessions(p)
    r=NS(date=datetime(2026,10,16).date(),open=6000,high=6010,low=5990,close=6005,volume=100,barCount=30)
    b=normalize_bars([r],'1d','ibkr:456',s,AT,AT)[0]
    assert b.time==p['rows'][0]['start'] and b.end==p['rows'][-1]['end']
    assert s.at(p['rows'][0]['end']+1000) is None


def test_cache_contract_isolation_and_ib_history_corrections(tmp_path):
    store=Store(tmp_path/'test.db');s=Sessions(payload())
    b=normalize_bars(raw_bars(s,'1d')[-1:],'1d','ibkr:123',s,AT,AT)[0]
    store.merge([b,replace(b,symbol='ibkr:456',close=100)])
    assert store.bars('1d',symbol='ibkr:456')[0].close==100
    store.merge([replace(b,close=100.5)])
    assert store.bars('1d',symbol='ibkr:123')[0].close==100.5
    store.merge([replace(b,status='forming',source='ws',close=99)])
    assert store.bars('1d',symbol='ibkr:123')[0].close==100.5
    store.close()


def test_snapshot_uses_stock_units_and_delay_and_blocks_bad_history(tmp_path,monkeypatch):
    monkeypatch.setattr('server.ibkr.now_ms',lambda:AT)
    store=Store(tmp_path/'test.db');broker=Broker(store)
    broker.ib=NS(isConnected=lambda:True);broker.upstream=True
    market=IBMarket(store,broker,instrument());market.sessions=Sessions(payload())
    market.generation=broker.generation
    for tf in ('15m','1h','4h','1d'):
        store.merge(normalize_bars(raw_bars(market.sessions,tf),tf,market.symbol,market.sessions,AT-900000,AT))
        market.last_rest[tf]=AT
    snap=snapshot_market(market,'1h')
    assert snap['ready'],snap['blocking_reasons']
    assert snap['units']['volume']=='股' and snap['units']['funding']=='不适用'
    assert snap['history_delay_minutes']==15 and snap['data_cutoff']==AT-900000
    assert model_snapshot(snap)['instrument']['display_symbol']=='AAPL'
    broker.upstream=False
    assert not snapshot_market(market,'1h')['ready']
    store.close()


def test_quote_unknown_values_are_not_zero_and_mode_is_actual(tmp_path):
    store=Store(tmp_path/'test.db');market=IBMarket(store,Broker(store),instrument())
    ticker=Ticker(contract=Stock('AAPL'))
    ticker.last,ticker.close,ticker.bid,ticker.ask,ticker.marketDataType=101,100,float('nan'),-1,3
    market._quote(ticker)
    assert market.quote['bid'] is None and market.quote['ask'] is None
    assert market.quote['change_pct']==pytest.approx(1)
    assert market.quote['data_mode']=='delayed'
    assert market.quote['source_time'] is None and market.quote['funding'] is None
    store.close()


def test_history_request_cutoff_is_delayed_no_live_subscription_and_partial_failures(tmp_path,monkeypatch):
    monkeypatch.setattr('server.ibkr.now_ms',lambda:AT)
    store=Store(tmp_path/'test.db');broker=Broker(store);market=IBMarket(store,broker,instrument())
    market.session_payload=payload();market.sessions=Sessions(payload())
    requests=[]
    async def history(contract,**kwargs):
        requests.append(kwargs)
        if kwargs['barSizeSetting']=='15 mins':
            raise RuntimeError('fixture permission denied')
        tf=next(k for k,v in BAR_SIZES.items() if v==kwargs['barSizeSetting'])
        return raw_bars(market.sessions,tf)
    async def no_sleep(_): pass
    monkeypatch.setattr('server.ibkr.asyncio.sleep',no_sleep)
    broker.ib=NS(reqHistoricalDataAsync=history)
    asyncio.run(market.reconcile())
    assert len(requests)==9
    assert all(int(r['endDateTime'].timestamp()*1000)==AT-900000 for r in requests)
    assert all(r['useRTH'] and r['whatToShow']=='TRADES' and not r['keepUpToDate'] for r in requests)
    assert market.last_rest['15m']==0 and market.last_rest['1h']==AT
    assert market.errors['15m'] and store.bars('1h',symbol=market.symbol)
    store.close()


def test_recent_listing_keeps_verified_schedule_when_older_page_has_no_data(tmp_path, monkeypatch):
    from ib_async.wrapper import RequestError
    monkeypatch.setattr('server.ibkr.now_ms', lambda: AT)
    async def no_sleep(_): pass
    monkeypatch.setattr('server.ibkr.asyncio.sleep', no_sleep)
    store = Store(tmp_path / 'schedule.db')
    broker = Broker(store)
    market = IBMarket(store, broker, instrument(771759702))
    calls = []
    async def schedule(contract, days, end, useRTH):
        calls.append((days, end, useRTH))
        if len(calls) == 3:
            raise RequestError(28, 162, r'\u5386\u53f2\u5e02\u573a\u6570\u636e\u67e5\u8be2\u672a\u8fd4\u56de\u6570\u636e:CRWV@SMART Schedule')
        date = '20261016' if len(calls) == 1 else '20251016'
        start, end_time = f'{date}-09:30:00', f'{date}-16:00:00'
        return NS(timeZone='America/New_York', startDateTime=start, endDateTime=end_time,
                  sessions=[NS(refDate=date, startDateTime=start, endDateTime=end_time)])
    broker.ib = NS(reqHistoricalScheduleAsync=schedule)
    asyncio.run(market._schedule())
    assert len(calls) == 3 and all(days == 365 and useRTH for days, _, useRTH in calls)
    assert len(market.sessions.rows) == 2
    assert store.get(f'sessions:{market.symbol}')['rows'] == market.session_payload['rows']
    assert market.session_payload['start'] < market.session_payload['end']
    store.close()


def test_schedule_no_data_is_not_confused_with_missing_permissions(tmp_path, monkeypatch):
    from ib_async.wrapper import RequestError
    monkeypatch.setattr('server.ibkr.now_ms', lambda: AT)
    async def no_sleep(_): pass
    monkeypatch.setattr('server.ibkr.asyncio.sleep', no_sleep)
    store = Store(tmp_path / 'schedule-errors.db')
    broker = Broker(store)
    market = IBMarket(store, broker, instrument())
    async def denied(*_args, **_kwargs):
        raise RequestError(3, 162, 'No market data permissions for NASDAQ STK')
    broker.ib = NS(reqHistoricalScheduleAsync=denied, isConnected=lambda:False)
    asyncio.run(market.reconcile())
    assert 'No market data permissions' in market.errors['calendar']
    assert market._quality('1h', [])['error'] == market.errors['calendar']
    assert not market.session_payload
    broker._on_error(3, 162, 'No market data permissions for NASDAQ STK')
    assert broker.error is None  # A symbol request does not poison the connection.
    broker._on_error(-1, 1100, 'connection lost')
    assert broker.error
    store.close()


def test_routes_no_credentials_order_endpoints_and_watchlist_persistence(tmp_path):
    with TestClient(create_app(tmp_path,live=False)) as c:
        status=c.get('/api/v1/ibkr/status').json()
        assert not status['connected'] and status['read_only']
        assert set(status)=={'connected','connecting','read_only','last_check','history_delay_minutes','instruments'}
        assert c.put('/api/v1/ibkr/config',json={}).status_code==404
        headers={'X-Local-Market':'1'}
        assert c.put('/api/v1/ibkr/config',headers=headers,json={'enabled':True,'port':4002}).status_code==404
        assert c.get('/api/v1/ibkr/config').status_code==404
        schema=c.get('/openapi.json').json()
        assert '/api/v1/ibkr/config' not in schema['paths']
        assert 'ConnectionSettings' not in schema['components']['schemas']
        unavailable=c.get('/api/v1/ibkr/search?q=AAPL')
        assert unavailable.status_code==503 and unavailable.json()['detail']=='行情暂不可用，请稍后重试'
        assert c.post('/api/v1/ibkr/instruments',headers=headers,json={'con_id':123}).status_code==422
        broker=c.app.state.hub.broker;broker.candidates[123]=instrument()
        assert broker.store.get('ibkr:settings') is None
        broker.error='TWS / IB Gateway: Socket API 127.0.0.1:7497 client_id=71'
        assert c.post('/api/v1/ibkr/instruments',headers=headers,json={'con_id':123}).status_code==200
        assert c.put('/api/v1/watchlists/default/items/ibkr:123',headers=headers).status_code==200
        assert c.get('/api/v1/chart/1h?symbol=ibkr:123').json()['instrument']['display_symbol']=='AAPL'
        overview=c.get('/api/v1/overview?symbol=ibkr:123')
        assert overview.json()['errors']['connection']=='行情暂不可用，请稍后重试'
        assert not any(detail in overview.text for detail in ('TWS','Gateway','127.0.0.1','7497','client_id'))
        assert 'error' not in c.get('/api/v1/ibkr/status').json()
        assert c.get('/api/v1/overview?symbol=BTC').status_code==200
        assert c.post('/api/v1/ibkr/orders',headers=headers,json={}).status_code==404
    with TestClient(create_app(tmp_path,live=False)) as c:
        assert c.get('/api/v1/ibkr/status').json()['instruments'][0]['symbol']=='ibkr:123'
        assert 'ibkr:123' in c.get('/api/v1/watchlists').json()[0]['symbols']


@pytest.mark.parametrize('invalid', [
    {'port':80}, {'port':65536}, {'client_id':0}, {'client_id':1000000},
    {'host':'remote.example'}, {'password':'fixture'}, {'unknown':True},
])
def test_private_connection_settings_validation(invalid):
    with pytest.raises(ValidationError):
        ConnectionSettings.model_validate(invalid)


def run_config_cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/configure_ibkr.py'), *args],
        env={**os.environ,'MARKET_DATA_DIR':str(tmp_path)}, capture_output=True, text=True)


def test_private_config_cli_read_only_and_preserves_unspecified_values(tmp_path):
    data=tmp_path/'data'; path=data/'btc-hyperliquid.sqlite3'
    initial=run_config_cli(data)
    assert initial.returncode==0,initial.stderr
    assert json.loads(initial.stdout)==ConnectionSettings().model_dump()
    assert not data.exists()
    changed=run_config_cli(data,'--port','4002','--client-id','83','--mode','live','--enable')
    assert changed.returncode==0,changed.stderr
    assert '重启' in changed.stderr
    expected={'port':4002,'client_id':83,'delayed':False,'enabled':True}
    assert json.loads(changed.stdout)==expected
    assert path.stat().st_mode & 0o777==0o600
    before=(path.read_bytes(),path.stat().st_mtime_ns)
    inspected=run_config_cli(data)
    assert inspected.returncode==0 and not inspected.stderr
    assert json.loads(inspected.stdout)==expected
    assert (path.read_bytes(),path.stat().st_mtime_ns)==before
    disabled=run_config_cli(data,'--disable')
    assert disabled.returncode==0,disabled.stderr
    assert json.loads(disabled.stdout)=={**expected,'enabled':False}
    store=Store(path)
    assert Broker(store).config.model_dump()=={**expected,'enabled':False}
    store.close()


@pytest.mark.parametrize('args', [
    ('--port','80'), ('--client-id','0'), ('--mode','invalid'), ('--enable','--disable'),
])
def test_private_config_cli_rejects_invalid_changes_without_creating_database(tmp_path,args):
    data=tmp_path/'data'
    result=run_config_cli(data,*args)
    assert result.returncode==2
    assert not data.exists()


def test_live_to_delayed_cache_and_reconnection_do_not_appear_fresh(tmp_path,monkeypatch):
    s=Sessions(payload()); now=s.rows[-1]['end']-60000
    monkeypatch.setattr('server.ibkr.now_ms',lambda:now)
    store=Store(tmp_path/'test.db'); broker=Broker(store)
    broker.ib=NS(isConnected=lambda:True);broker.upstream=True
    market=IBMarket(store,broker,instrument());market.sessions=s;market.generation=broker.generation
    # Cached completed bars were previously fetched in live mode.
    bars=normalize_bars(raw_bars(s,'1m'),'1m',market.symbol,s,s.rows[-1]['end'],now)
    store.merge(bars);market.last_rest['1m']=now
    shown,_=market.features('1m')
    assert shown and all(b.end<=now-900000-2000 for b in shown)
    assert market._quality('1m',shown)['status']=='delayed'
    broker._on_error(-1,1100,'fixture disconnect')
    assert market._quality('1m',shown)['status']=='stale'
    broker._on_error(-1,1101,'fixture reconnect')
    assert market._quality('1m',shown)['status']=='stale'  # must resync new generation
    store.close()


def test_connection_uses_only_socket_handshake_and_delayed_market_request(tmp_path,monkeypatch):
    store=Store(tmp_path/'test.db');broker=Broker(store);calls=[];connected=False
    async def connect(host,port,client_id,timeout):
        nonlocal connected
        calls.append(('connect',host,port,client_id,timeout));connected=True
    async def heartbeat(): calls.append(('heartbeat',))
    async def stop_sleep(_): raise asyncio.CancelledError()
    # Missing account/order/position APIs intentionally raise if ever accessed.
    broker.ib=NS(client=NS(connectAsync=connect),wrapper=NS(),isConnected=lambda:connected,
                 reqMarketDataType=lambda mode:calls.append(('mode',mode)),reqCurrentTimeAsync=heartbeat)
    monkeypatch.setattr('server.ibkr.asyncio.sleep',stop_sleep)
    with pytest.raises(asyncio.CancelledError): asyncio.run(broker._loop())
    assert calls==[('connect','127.0.0.1',7497,71,5),('mode',3),('heartbeat',)]
    assert broker.connected and broker.generation==1
    store.close()


def test_old_live_forming_candle_cannot_leak_through_delayed_refresh(tmp_path,monkeypatch):
    s=Sessions(payload());now=s.rows[-1]['end']-60000
    monkeypatch.setattr('server.ibkr.now_ms',lambda:now)
    store=Store(tmp_path/'test.db');broker=Broker(store)
    market=IBMarket(store,broker,instrument());market.sessions=s;market.generation=broker.generation
    b=normalize_bars(raw_bars(s,'1d')[-1:],'1d',market.symbol,s,now,now-1000)[0]
    store.merge([b]);market.last_rest['1d']=now;market.fetch_started['1d']=now
    assert market.features('1d')[0]==[]  # stale live forming bar omitted by this fetch
    delayed=replace(b,close=100,volume=100,received_at=now)
    store.merge([delayed]);market.revisions['1d']+=1
    visible=market.features('1d')[0]
    assert len(visible)==1 and visible[0].close==100 and visible[0].volume==100
    store.close()


@pytest.mark.parametrize('root,exchange,multiplier,tick', [('ES','CME','50',.25), ('MGC','COMEX','10',.1)])
def test_search_qualifies_and_persists_concrete_contract(tmp_path,monkeypatch,root,exchange,multiplier,tick):
    from ib_async import ContractDetails, Future
    store=Store(tmp_path/'test.db');broker=Broker(store);broker.upstream=True;queries=[]
    async def details(contract):
        queries.append(contract)
        return [ContractDetails(contract=Future(root,'209912',exchange,currency='USD',conId=456,localSymbol=f'{root}Z9',multiplier=multiplier),
                                realExpirationDate='20991218',timeZoneId='America/Chicago',minTick=tick,longName=f'Fixture {root}'),
                ContractDetails(contract=Future(root,'200012',exchange,currency='USD',conId=111),realExpirationDate='20001215')]
    async def no_sleep(_): pass
    broker.ib=NS(isConnected=lambda:True,reqContractDetailsAsync=details)
    monkeypatch.setattr('server.ibkr.asyncio.sleep',no_sleep)
    result=asyncio.run(broker.search(root.lower(),'FUT'))
    assert len(result)==1 and queries[0].exchange==exchange and queries[0].symbol==root
    added=broker.add(456)
    assert added['symbol']=='ibkr:456' and added['volume_unit']=='手' and added['expiry']=='20991218'
    assert added['asset_type']=='future' and added['multiplier']==multiplier and added['tick_size']==tick
    assert added['contract']['secType']=='FUT' and added['contract']['exchange']==exchange
    assert store.get('ibkr:instruments')['ibkr:456']['contract']['conId']==456
    store.close()


def test_discovery_filters_results_and_qualifies_only_selected_stock(tmp_path, monkeypatch):
    from ib_async import Contract, ContractDescription, ContractDetails
    store = Store(tmp_path/'search.db'); broker = Broker(store); broker.upstream = True
    calls = []
    nvda = Stock('NVDA', 'SMART', 'USD', conId=4815747, primaryExchange='NASDAQ', description='NVIDIA CORP')
    async def matching(query):
        calls.append(('matching', query))
        return [ContractDescription(contract=nvda), ContractDescription(contract=nvda),
                ContractDescription(contract=Stock('NVDA', 'LSE', 'USD', conId=2, primaryExchange='LSE')),
                ContractDescription(contract=Stock('NVDA', 'SMART', 'EUR', conId=3, primaryExchange='NASDAQ')),
                ContractDescription(contract=Contract(secType='CFD', symbol='NVDA', conId=4, currency='USD', primaryExchange='NASDAQ'))]
    async def details(contract):
        calls.append(('details', contract.conId, contract.exchange))
        return [ContractDetails(contract=nvda, longName='NVIDIA CORP', minTick=.01, timeZoneId='US/Eastern')]
    async def no_sleep(_): pass
    monkeypatch.setattr('server.ibkr.asyncio.sleep', no_sleep)
    broker.ib = NS(isConnected=lambda: True, reqMatchingSymbolsAsync=matching, reqContractDetailsAsync=details)
    async def run():
        hits = await broker.discover('nv')
        assert len(hits) == 1 and hits[0]['display_symbol'] == 'NVDA'
        assert calls == [('matching', 'NV')] and not broker.instruments and not broker.candidates
        saved = await broker.resolve_and_add(hits[0]['con_id'])
        assert saved['timezone'] == 'US/Eastern' and saved['tick_size'] == .01
        assert calls[-1] == ('details', 4815747, 'SMART')
        assert store.get('ibkr:instruments')['ibkr:4815747']['contract']['conId'] == 4815747
    asyncio.run(run()); store.close()


def test_discovery_paces_requests_and_distinguishes_timeout_from_no_results(tmp_path, monkeypatch):
    from fastapi import HTTPException
    store = Store(tmp_path/'search.db'); broker = Broker(store); broker.upstream = True
    sleeps = []; answers = [[], None]
    async def matching(_): return answers.pop(0)
    async def sleep(seconds): sleeps.append(seconds)
    monkeypatch.setattr('server.ibkr.asyncio.sleep', sleep)
    monkeypatch.setattr('server.ibkr.time.monotonic', lambda: 100.0)
    broker.ib = NS(isConnected=lambda: True, reqMatchingSymbolsAsync=matching)
    async def run():
        assert await broker.discover('NOTFOUND') == []
        with pytest.raises(HTTPException) as error: await broker.discover('NV')
        assert error.value.status_code == 502
        with pytest.raises(HTTPException) as error: await broker.resolve_and_add(999)
        assert error.value.status_code == 422
    asyncio.run(run())
    assert sleeps == [0, 1.05]
    store.close()


def test_selected_search_result_rejects_mismatched_contract(tmp_path):
    from fastapi import HTTPException
    from ib_async import ContractDetails
    store = Store(tmp_path/'search.db'); broker = Broker(store); broker.upstream = True
    broker.discovery_contracts[123] = Stock('NVDA', 'SMART', 'USD', conId=123, primaryExchange='NASDAQ')
    async def details(_):
        return [ContractDetails(contract=Stock('OTHER', 'SMART', 'USD', conId=999, primaryExchange='NASDAQ'))]
    broker.ib = NS(isConnected=lambda: True, reqContractDetailsAsync=details)
    with pytest.raises(HTTPException) as error: asyncio.run(broker.resolve_and_add(123))
    assert error.value.status_code == 502 and not broker.instruments
    store.close()
