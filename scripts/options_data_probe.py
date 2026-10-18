#!/usr/bin/env python3
"""Small, market-data-only IBKR access probe; no trading/account requests.

Uses a separate client connection and never sends paid regulatory snapshots.
The output is evidence of field availability, not a trading signal or full chain.
Run with the existing limen virtual environment; --target SPX or ES.
"""
import argparse
import asyncio
import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone, timedelta
import json
import logging
import math
from pathlib import Path
import re
import resource
import time

from ib_async import IB, Contract, Index, Future, Option, FuturesOption


def clean(value):
    if is_dataclass(value):
        return clean(asdict(value))
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 1e100):
        return None
    return value


def valid(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 1e100


def contract_row(c):
    return {k: getattr(c, k) for k in (
        'conId', 'symbol', 'secType', 'exchange', 'currency', 'localSymbol',
        'tradingClass', 'lastTradeDateOrContractMonth', 'strike', 'right', 'multiplier')}


def ticker_row(t, observed_mode):
    fields = ('bid', 'ask', 'last', 'close', 'bidSize', 'askSize', 'volume',
              'openInterest', 'callOpenInterest', 'putOpenInterest',
              'callVolume', 'putVolume', 'lastTimestamp', 'delayedLastTimestamp',
              'bidGreeks', 'askGreeks', 'lastGreeks', 'modelGreeks')
    return clean({'contract': contract_row(t.contract),
                  'observed_market_data_type': observed_mode,
                  'received_at': t.time,
                  'note': 'received_at is local receipt time, NOT exchange quote time',
                  **{f: getattr(t, f, None) for f in fields}})


async def run(args):
    started = time.monotonic()
    cpu_start = time.process_time()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'target': args.target,
              'scope': 'sample, not full-chain aggregation', 'errors': [], 'batches': [],
              'paid_regulatory_snapshots': False, 'orders_requested': False,
              'refresh_interval_target_seconds': 1800}
    ib = IB()
    ib.RaiseRequestErrors = True
    ib.wrapper.clientId = args.client_id
    observed_modes = {}
    original_mode = ib.wrapper.marketDataType

    def market_data_type(req_id, mode):
        observed_modes[req_id] = mode
        original_mode(req_id, mode)
    ib.wrapper.marketDataType = market_data_type

    def on_error(req_id, code, message, contract=None):
        if code in {2104, 2106, 2107, 2108, 2158}:
            return
        decoded = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), message)
        report['errors'].append({'request_id': req_id, 'code': code,
                                 'message': decoded[:1000],
                                 'contract': contract_row(contract) if contract else None})
    ib.errorEvent += on_error

    async def batch(name, contracts, mode=4, generic=''):
        t0 = time.monotonic()
        ib.reqMarketDataType(mode)
        tickers = []
        for source in contracts:
            # Fresh objects prevent earlier batches' cached values appearing as new data.
            c = copy.deepcopy(source)
            t = ib.reqMktData(c, genericTickList=generic, snapshot=False, regulatorySnapshot=False)
            tickers.append((c, t, ib.wrapper.ticker2ReqId['mktData'][t]))
            await asyncio.sleep(0.08)
        await asyncio.sleep(args.wait)
        rows = [ticker_row(t, observed_modes.get(req_id)) for c, t, req_id in tickers]
        for c, t, req_id in tickers:
            ib.cancelMktData(c)
        entry = {'name': name, 'requested_mode': mode, 'generic_ticks': generic,
                 'seconds': round(time.monotonic() - t0, 3), 'rows': rows}
        report['batches'].append(entry)
        print(json.dumps({'stage': name, 'seconds': entry['seconds'], 'contracts': len(rows),
                          'with_bid_ask': sum(valid(r['bid']) and valid(r['ask']) for r in rows),
                          'with_last_or_close': sum(valid(r['last']) or valid(r['close']) for r in rows),
                          'with_model_greeks': sum(r['modelGreeks'] is not None for r in rows),
                          'observed_modes': sorted(set(r['observed_market_data_type'] for r in rows if r['observed_market_data_type'] is not None))}), flush=True)
        return rows

    try:
        # Direct socket handshake avoids IB.connectAsync's account/positions sync.
        await ib.client.connectAsync('127.0.0.1', args.port, args.client_id, timeout=8)
        now = await asyncio.wait_for(ib.reqCurrentTimeAsync(), 8)
        report['server_time'] = now.isoformat()
        report['server_version'] = ib.client.serverVersion()
        today = now.strftime('%Y%m%d')
        template = Index('SPX', 'CBOE', 'USD') if args.target == 'SPX' else Future('ES', exchange='CME', currency='USD')
        ds = await asyncio.wait_for(ib.reqContractDetailsAsync(template), 25)
        if args.target == 'ES':
            ds = sorted([d for d in ds if d.contract.lastTradeDateOrContractMonth >= today], key=lambda d: d.contract.lastTradeDateOrContractMonth)
        underlying = ds[0].contract
        report['underlying'] = contract_row(underlying)
        report['trading_hours'] = ds[0].tradingHours
        report['timezone'] = ds[0].timeZoneId
        vx_ds = await asyncio.wait_for(ib.reqContractDetailsAsync(Future('VIX', exchange='CFE', currency='USD')), 25)
        vx = sorted([d.contract for d in vx_ds if d.contract.tradingClass == 'VX' and d.contract.lastTradeDateOrContractMonth >= today], key=lambda c: c.lastTradeDateOrContractMonth)[:9]
        base_rows = await batch('underlying_and_vx_delayed_frozen', [underlying] + vx)
        urow = base_rows[0]
        center = next((urow[f] for f in ('last', 'close', 'bid') if valid(urow[f])), None)
        if not center:
            # A dated daily close is ONLY a strike-selection reference, never a fresh quote.
            try:
                bars = await ib.reqHistoricalDataAsync(underlying, '', '3 D', '1 day',
                                                       'TRADES', False, formatDate=2, timeout=20)
                report['underlying_daily_bars'] = [
                    {'date': str(b.date), 'close': clean(b.close), 'volume': clean(b.volume)}
                    for b in bars]
                if bars and valid(bars[-1].close):
                    center = bars[-1].close
            except Exception as error:
                report['underlying_history_error'] = f'{type(error).__name__}: {str(error)[:300]}'
        chains = await asyncio.wait_for(ib.reqSecDefOptParamsAsync(underlying.symbol, 'CME' if args.target == 'ES' else '', underlying.secType, underlying.conId), 45)
        report['chains'] = clean(chains)
        print(json.dumps({'stage': 'option_chains', 'count': len(chains), 'center_reference': center,
                          'classes': sorted({c.tradingClass for c in chains})}), flush=True)
        if not center:
            # Still test field entitlement on listed contracts, without calculating IV.
            strikes = sorted({s for ch in chains for s in ch.strikes if valid(s)})
            if not strikes:
                report['option_sample_blocker'] = 'No underlying price or listed strikes.'
                return
            center = strikes[len(strikes)//2]
            report['center_fallback'] = 'Median of listed strikes, NOT an underlying market price; sample may not be ATM.'
        if args.target == 'SPX':
            selected = [c for c in chains if c.exchange == 'SMART' and c.tradingClass == 'SPXW']
        else:
            selected = [c for c in chains if c.exchange == 'CME']
        report['sample_selection'] = {'center_reference': center, 'center_is_not_current_quote': True,
                                      'expiry_targets_days': [7, 30, 60], 'strikes_per_expiry': 9,
                                      'rights': ['C', 'P']}
        candidates = []
        for ch in selected:
            for expiry in sorted(ch.expirations):
                if expiry >= today:
                    candidates.append((expiry, ch))
        selected_pairs = []
        for days in (7, 30, 60):
            target = (now + timedelta(days=days)).strftime('%Y%m%d')
            choices = sorted(candidates, key=lambda p: abs((datetime.strptime(p[0], '%Y%m%d') - datetime.strptime(target, '%Y%m%d')).days))
            if choices and choices[0] not in selected_pairs:
                selected_pairs.append(choices[0])
        options = []
        for expiry, ch in selected_pairs:
            near = sorted(ch.strikes, key=lambda s: abs(s-center))[:9]
            for strike in sorted(near):
                for right in ('C', 'P'):
                    cls = Option if args.target == 'SPX' else FuturesOption
                    options.append(cls(underlying.symbol, expiry, strike, right, ch.exchange,
                                       multiplier=ch.multiplier, currency='USD', tradingClass=ch.tradingClass))
        qualified = []
        for start in range(0, len(options), 9):
            results = await asyncio.gather(*(asyncio.wait_for(ib.reqContractDetailsAsync(c), 20) for c in options[start:start+9]), return_exceptions=True)
            for c, result in zip(options[start:start+9], results):
                if isinstance(result, Exception):
                    report.setdefault('qualification_errors', []).append({'contract': contract_row(c), 'error': str(result)[:200]})
                elif len(result) == 1:
                    qualified.append(result[0].contract)
        report['sample_contracts_requested'] = len(options)
        report['sample_contracts_qualified'] = len(qualified)
        print(json.dumps({'stage': 'qualified_options', 'count': len(qualified)}), flush=True)
        for start in range(0, len(qualified), 27):
            await batch(f'options_delayed_frozen_{start}', qualified[start:start+27], mode=4, generic='100,101')
        # Plain delayed streaming control separates generic-tick limitations from quote access.
        if qualified:
            await batch('plain_delayed_control', [underlying] + qualified[:4] + vx[:2], mode=3)
    except Exception as error:
        report['fatal_error'] = f'{type(error).__name__}: {str(error)[:500]}'
        print(report['fatal_error'], flush=True)
    finally:
        ib.disconnect()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['cpu_seconds'] = round(time.process_time() - cpu_start, 3)
        report['peak_process_rss_bytes_macos'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        print(json.dumps({'output': str(output), 'elapsed_seconds': report['elapsed_seconds'],
                          'cpu_seconds': report['cpu_seconds'], 'errors': len(report['errors'])}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['SPX', 'ES'], default='SPX')
    parser.add_argument('--port', type=int, default=7496)
    parser.add_argument('--client-id', type=int, default=92717)
    parser.add_argument('--wait', type=float, default=20)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    logging.getLogger('ib_async').setLevel(logging.CRITICAL)
    asyncio.run(run(args))
