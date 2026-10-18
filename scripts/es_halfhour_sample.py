#!/usr/bin/env python3
"""ES European-option 30-minute history and explicitly labelled model estimates.

Small read-only feasibility sample. No orders, paid snapshots, account queries,
or persistent background worker. Historical bar close is not a live quote.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import resource
import time
from zoneinfo import ZoneInfo

from ib_async import IB, Contract, FuturesOption
from options_data_probe import clean, contract_row


def black76(f, k, t, sigma, r, right):
    if min(f, k, t, sigma) <= 0:
        raise ValueError('positive inputs required')
    root_t = math.sqrt(t)
    d1 = (math.log(f/k) + sigma*sigma*t/2)/(sigma*root_t)
    d2 = d1 - sigma*root_t
    norm = lambda x: (1 + math.erf(x/math.sqrt(2)))/2
    density = math.exp(-d1*d1/2)/math.sqrt(2*math.pi)
    disc = math.exp(-r*t)
    sign = 1 if right == 'C' else -1
    price = disc*sign*(f*norm(sign*d1)-k*norm(sign*d2))
    return {'price': price, 'delta_future': disc*sign*norm(sign*d1),
            'gamma_future': disc*density/(f*sigma*root_t),
            'vega_per_1pct': disc*f*density*root_t/100,
            'theta_per_calendar_day': (r*price-disc*f*density*sigma/(2*root_t))/365}


def solve_iv(price, f, k, t, r, right):
    if not all(math.isfinite(x) for x in (price, f, k, t, r)) or min(price, f, k, t) <= 0:
        return None
    disc = math.exp(-r*t)
    intrinsic = disc*max((f-k) if right == 'C' else (k-f), 0)
    upper = disc*(f if right == 'C' else k)
    if price <= intrinsic or price >= upper:
        return None
    lo, hi = 0.00001, 5.0
    if black76(f,k,t,hi,r,right)['price'] < price:
        return None
    for _ in range(80):
        mid = (lo+hi)/2
        if black76(f,k,t,mid,r,right)['price'] < price:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


async def run(args):
    root = Path(args.output)
    root.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(Path(args.input).read_text())
    underlying = Contract.create(**previous['underlying'])
    # All three are European PM exercise series; American quarterly ES is excluded.
    selections = [('E4A', '20261026'), ('EW', '20261030'), ('EW3', '20261120')]
    strikes = [7600.0, 7700.0, 7800.0]
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'target': 'ES futures options, not SPX or SPY', 'strikes': strikes,
              'expiry_classes': selections, 'bar_size': '30 mins',
              'scope': '18 selected European option contracts; not entire ES chain',
              'interest_rate_annual': args.rate,
              'interest_rate_status': 'user/test parameter; not sourced or verified',
              'model': 'Black-76, underlying futures sensitivity, per option point, no contract multiplier',
              'data_limitations': ['Historical midpoint bar close is a reference, not a current executable bid/ask.',
                                   'Same bar end does not prove simultaneous quote timestamps.',
                                   'No OI, no live option-flow reconstruction, no full-chain PCR.',
                                   'Interest rate and quote freshness must be verified before production use.'],
              'contracts': [], 'history': [], 'errors': []}
    ib = IB()
    ib.RaiseRequestErrors = True
    ib.wrapper.clientId = 92720
    def on_error(req, code, message, contract=None):
        if code not in {2104, 2106, 2107, 2108, 2158}:
            report['errors'].append({'code': code, 'message': message[:800],
                                     'contract': contract_row(contract) if contract else None})
    ib.errorEvent += on_error
    started = time.monotonic()
    cpu_start = time.process_time()
    try:
        await ib.client.connectAsync('127.0.0.1', args.port, 92720, timeout=8)
        now = await ib.reqCurrentTimeAsync()
        report['server_time'] = now.isoformat()
        qualified = []
        for tc, expiry in selections:
            for strike in strikes:
                for right in ('C', 'P'):
                    c = FuturesOption('ES', expiry, strike, right, 'CME', multiplier='50', currency='USD', tradingClass=tc)
                    details = await asyncio.wait_for(ib.reqContractDetailsAsync(c), 15)
                    if len(details) == 1 and details[0].underConId == underlying.conId:
                        d = details[0]
                        qualified.append(d.contract)
                        report['contracts'].append({'contract': contract_row(d.contract),
                                                    'underlying_con_id': d.underConId,
                                                    'real_expiration_date': d.realExpirationDate,
                                                    'last_trade_time': d.lastTradeTime,
                                                    'time_zone': d.timeZoneId})
        sem = asyncio.Semaphore(2)
        async def history(c, field):
            async with sem:
                t0 = time.monotonic()
                row = {'contract': contract_row(c), 'field': field}
                try:
                    bars = await ib.reqHistoricalDataAsync(c, now-timedelta(minutes=20), '2 D', '30 mins', field, False, formatDate=2, timeout=20)
                    row['bars'] = clean(list(bars))
                except Exception as e:
                    row['error'] = str(e)[:500]
                row['seconds'] = round(time.monotonic()-t0, 3)
                report['history'].append(row)
                print(json.dumps({'contract': c.localSymbol, 'field': field,
                                  'bars': len(row.get('bars', [])), 'seconds': row['seconds']}), flush=True)
        # 37 bounded requests; max 2 in flight. Midpoint volume=-1 is not trade volume.
        await history(underlying, 'MIDPOINT')
        await asyncio.gather(*(history(c, field) for c in qualified for field in ('MIDPOINT', 'TRADES')))
        by_underlying = {b['date']: b for row in report['history'] if row['contract']['secType']=='FUT' for b in row.get('bars', [])}
        estimates = []
        chicago = ZoneInfo('America/Chicago')
        for row in report['history']:
            c = row['contract']
            if c['secType'] != 'FOP' or row['field'] != 'MIDPOINT':
                continue
            expiry = datetime.strptime(c['lastTradeDateOrContractMonth'], '%Y%m%d').replace(hour=15, tzinfo=chicago)
            points = []
            for bar in row.get('bars', []):
                u = by_underlying.get(bar['date'])
                if not u:
                    continue
                asof = datetime.fromisoformat(bar['date'])+timedelta(minutes=30)
                t = (expiry-asof).total_seconds()/(365*86400)
                sigma = solve_iv(bar['close'], u['close'], c['strike'], t, args.rate, c['right'])
                if sigma is None:
                    continue
                points.append({'bar_start': bar['date'], 'bar_end': asof.isoformat(),
                               'option_midpoint_close': bar['close'], 'underlying_midpoint_close': u['close'],
                               'iv': sigma, **black76(u['close'],c['strike'],t,sigma,args.rate,c['right'])})
            estimates.append({'contract': c, 'points': points})
        report['model_estimates'] = estimates
        latest_common = sorted(set.intersection(*[{p['bar_start'] for p in r['points']} for r in estimates if r['points']])) if any(r['points'] for r in estimates) else []
        if latest_common:
            last = latest_common[-1]
            report['latest_common_bar_start'] = last
            report['latest_surface_sample'] = [{'contract': r['contract'], 'estimate': next(p for p in r['points'] if p['bar_start']==last)} for r in estimates if any(p['bar_start']==last for p in r['points'])]
        report['successful_midpoint_contracts'] = sum(bool(r.get('bars')) for r in report['history'] if r['contract']['secType']=='FOP' and r['field']=='MIDPOINT')
        report['successful_trade_contracts'] = sum(bool(r.get('bars')) for r in report['history'] if r['contract']['secType']=='FOP' and r['field']=='TRADES')
        report['derived_contracts'] = sum(bool(r['points']) for r in estimates)
        report['derived_points'] = sum(len(r['points']) for r in estimates)
    finally:
        ib.disconnect()
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        report['cpu_seconds'] = round(time.process_time()-cpu_start, 3)
        report['peak_process_rss_bytes_macos'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        root.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        print(json.dumps({k: report.get(k) for k in ('successful_midpoint_contracts','successful_trade_contracts','derived_contracts','derived_points','elapsed_seconds','cpu_seconds','peak_process_rss_bytes_macos')}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--rate', type=float, required=True, help='Explicit test assumption, annual continuously compounded rate.')
    p.add_argument('--port', type=int, default=7496)
    args = p.parse_args()
    logging.getLogger('ib_async').setLevel(logging.CRITICAL)
    asyncio.run(run(args))
