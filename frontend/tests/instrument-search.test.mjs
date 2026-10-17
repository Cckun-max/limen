import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeInstrumentSearch, instrumentMatches, brokerSearchKind } from '../src/instrumentSearch.ts';

test('Chinese aliases still find a stock after it has been added', () => {
  const stock = {symbol: 'ibkr:4815747', display_symbol: 'NVDA', name: 'NVIDIA CORP'};
  for (const query of ['nv', 'NVDA', 'NVIDIA', '英伟达', ' 英偉達 ']) assert.equal(instrumentMatches(stock, query), true);
  assert.equal(instrumentMatches(stock, '苹果'), false);
  assert.equal(normalizeInstrumentSearch('nv'), 'NV');
});

test('asset filter selects the right search source without breaking crypto pair searches', () => {
  assert.equal(brokerSearchKind('NV', 'all'), 'STK');
  assert.equal(brokerSearchKind('英伟达', 'stock'), 'STK');
  assert.equal(brokerSearchKind('ES', 'all'), 'FUT');
  assert.equal(brokerSearchKind('ES', 'stock'), 'STK');
  assert.equal(brokerSearchKind('ES', 'future'), 'FUT');
  assert.equal(brokerSearchKind('mgc', 'all'), 'FUT');
  assert.equal(brokerSearchKind('微型黄金', 'all'), 'FUT');
  assert.equal(brokerSearchKind('MGC', 'future'), 'FUT');
  assert.equal(brokerSearchKind('MGC', 'stock'), 'STK'); // The identically named ETF remains searchable.
  assert.equal(instrumentMatches({symbol: 'ibkr:123', display_symbol: 'MGCZ6', name: 'E-Micro Gold'}, '微型黄金'), true);
  assert.equal(brokerSearchKind('BTC', 'perp'), null);
  assert.equal(brokerSearchKind(' ', 'all'), null);
  assert.equal(instrumentMatches({symbol: 'BTC'}, 'btc/usdc'), true);
});
