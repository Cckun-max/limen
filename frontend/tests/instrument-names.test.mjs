import test from 'node:test';
import assert from 'node:assert/strict';
import { instrumentDescription } from '../src/instrumentNames.ts';
import { instrumentMatches, normalizeInstrumentSearch } from '../src/instrumentSearch.ts';

const stock = (symbol, name) => ({symbol: `ibkr:${symbol}`, display_symbol: symbol, name, asset_type: 'stock'});

test('company names distinguish the underlying stock from daily leveraged ETFs', () => {
  assert.match(instrumentDescription(stock('CRWV', 'COREWEAVE INC-CL A')), /CoreWeave/);
  const short = instrumentDescription(stock('CORD', 'T-REX 2X INV CRWV DA TAR ETF'));
  const long = instrumentDescription(stock('CRWU', 'T-REX 2X LONG CRWV DA TA ETF'));
  assert.match(short, /做空|反向/);
  assert.match(long, /做多/);
  assert.notEqual(short, long);
  assert.match(instrumentDescription(stock('NVDA', 'NVIDIA CORP')), /英伟达/);
  assert.equal(instrumentDescription(stock('CRWU', 'T-REX 3X LONG CRWV ETF')), 'T-REX 3X LONG CRWV ETF');
});

test('market type keeps identically named ETFs and futures separate', () => {
  const etf = instrumentDescription(stock('MGC', 'VANGUARD MEGA CAP ETF'));
  const future = instrumentDescription({symbol:'ibkr:123', display_symbol:'MGCZ6', native_symbol:'MGC', name:'E-Micro Gold', asset_type:'future'});
  assert.doesNotMatch(etf, /黄金/);
  assert.match(future, /黄金/);
  assert.notEqual(etf, future);
  assert.doesNotMatch(instrumentDescription(stock('ES', 'EVERSOURCE ENERGY')), /标普/);
});

test('Chinese and project names search the same provider identity without replacing its code', () => {
  const bitcoin = {symbol:'BTC'};
  const curve = {symbol:'CRV'};
  const pepe = {symbol:'kPEPE'};
  assert.match(instrumentDescription(bitcoin), /比特币/);
  assert.match(instrumentDescription(curve), /Curve/);
  assert.equal(instrumentMatches(bitcoin, '比特币'), true);
  assert.equal(instrumentMatches(curve, 'curve'), true);
  assert.equal(instrumentMatches(pepe, 'pepe'), true);
  assert.equal(instrumentMatches(stock('NVDA', 'NVIDIA CORP'), '英伟达'), true);
  assert.equal(normalizeInstrumentSearch('英伟达'), 'NVDA');
  assert.equal(normalizeInstrumentSearch('赛富时'), 'CRM');
  assert.equal(normalizeInstrumentSearch('PEPE'), 'kPEPE');
  assert.equal(pepe.symbol, 'kPEPE');
});

test('unknown securities keep the provider name and never borrow a crypto project name', () => {
  assert.equal(instrumentDescription(stock('ZZUNKNOWN', 'EXAMPLE COMPANY INC')), 'EXAMPLE COMPANY INC');
  assert.equal(instrumentDescription({symbol:'ZZUNKNOWN'}), '');
  assert.notEqual(instrumentDescription(stock('SOL', 'EMEREN GROUP LTD')), instrumentDescription({symbol:'SOL'}));
});
