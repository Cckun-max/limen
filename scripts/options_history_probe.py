#!/usr/bin/env python3
"""Follow-up read-only checks for the sample produced by options_data_probe.py."""
import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import time

from ib_async import IB, Contract
from options_data_probe import clean, contract_row, ticker_row


async def run(args):
    source = json.loads(Path(args.input).read_text())
    underlying = Contract.create(**source['underlying'])
    option_rows = [r for b in source['batches'] if b['name'].startswith('options_') for r in b['rows']]
    center = source['sample_selection']['center_reference']
    selected = []
    for expiry in sorted({r['contract']['lastTradeDateOrContractMonth'] for r in option_rows}):
        for right in ('C', 'P'):
            choices = [r for r in option_rows if r['contract']['right'] == right and r['contract']['lastTradeDateOrContractMonth'] == expiry]
            selected.append(Contract.create(**min(choices, key=lambda r: abs(r['contract']['strike']-center))['contract']))
    vx_rows = source['batches'][0]['rows'][1:]
    vx = [Contract.create(**r['contract']) for r in vx_rows[:3]]
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'target': source['target'],
              'scope': '6 option contracts + underlying + 3 VX months; not whole chain',
              'paid_regulatory_snapshots': False, 'errors': [], 'quote_controls': [], 'history': []}
    ib = IB()
    ib.RaiseRequestErrors = True
    ib.wrapper.clientId = args.client_id
    modes = {}
    original_mode = ib.wrapper.marketDataType
    def mode_event(req_id, mode):
        modes[req_id] = mode
        original_mode(req_id, mode)
    ib.wrapper.marketDataType = mode_event
    def error_event(req_id, code, message, contract=None):
        if code not in {2104, 2106, 2107, 2108, 2158}:
            report['errors'].append({'request_id': req_id, 'code': code, 'message': message[:1000],
                                     'contract': contract_row(contract) if contract else None})
    ib.errorEvent += error_event
    started = time.monotonic()
    try:
        await ib.client.connectAsync('127.0.0.1', args.port, args.client_id, timeout=8)
        now = await ib.reqCurrentTimeAsync()
        # Mode 1/2 checks use existing entitlements, never purchase data.
        for mode in (1, 2):
            ib.reqMarketDataType(mode)
            pairs = []
            for template in [underlying] + selected[:2] + vx[:1]:
                c = copy.deepcopy(template)
                t = ib.reqMktData(c, '100,101' if c.secType == 'FOP' else '', False, False)
                pairs.append((c, t, ib.wrapper.ticker2ReqId['mktData'][t]))
                await asyncio.sleep(0.1)
            await asyncio.sleep(12)
            rows = [ticker_row(t, modes.get(req)) for c, t, req in pairs]
            report['quote_controls'].append({'requested_mode': mode, 'rows': rows})
            for c, t, req in pairs:
                ib.cancelMktData(c)
            print(json.dumps({'stage': 'quote_control', 'mode': mode, 'errors_so_far': len(report['errors'])}), flush=True)
        sem = asyncio.Semaphore(2)
        async def history(c, field):
            async with sem:
                t0 = time.monotonic()
                row = {'contract': contract_row(c), 'whatToShow': field,
                       'bar_size': '30 mins', 'end_cutoff': (now-timedelta(minutes=20)).isoformat()}
                try:
                    bars = await ib.reqHistoricalDataAsync(c, now-timedelta(minutes=20), '2 D', '30 mins',
                                                          field, False, formatDate=2, timeout=25)
                    row['bars'] = clean(list(bars))
                    row['count'] = len(bars)
                except Exception as e:
                    row['error'] = f'{type(e).__name__}: {str(e)[:400]}'
                row['seconds'] = round(time.monotonic()-t0, 3)
                report['history'].append(row)
                print(json.dumps({'stage': 'history', 'symbol': c.localSymbol, 'field': field,
                                  'count': row.get('count'), 'error': row.get('error'), 'seconds': row['seconds']}), flush=True)
        requests = [(underlying, 'TRADES')]
        requests += [(c, 'MIDPOINT') for c in selected]
        requests += [(c, 'TRADES') for c in selected[:2]]
        requests += [(c, 'TRADES') for c in vx]
        await asyncio.gather(*(history(c, field) for c, field in requests))
    finally:
        ib.disconnect()
        report['elapsed_seconds'] = round(time.monotonic()-started, 3)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        Path(args.output).write_text(json.dumps(clean(report), ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        print(json.dumps({'output': args.output, 'elapsed_seconds': report['elapsed_seconds']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--port', type=int, default=7496)
    parser.add_argument('--client-id', type=int, default=92719)
    args = parser.parse_args()
    logging.getLogger('ib_async').setLevel(logging.CRITICAL)
    asyncio.run(run(args))
