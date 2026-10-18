import { instrumentDescription, instrumentSearchAlias, type NamedInstrument } from './instrumentNames.ts';

export function normalizeInstrumentSearch(query: string) {
  return instrumentSearchAlias(query);
}

export function instrumentMatches(row: NamedInstrument, query: string) {
  const text = `${row.symbol} ${row.display_symbol ?? ''} ${row.name ?? ''} ${instrumentDescription(row)}`.toUpperCase();
  const values = [query.trim().toUpperCase(), normalizeInstrumentSearch(query)]
    .map(value => value.toUpperCase().replace(/\/(USDC|USD|USDT)$/, ''));
  return values.some(value => text.includes(value));
}

export function brokerSearchKind(query: string, assetType: string) {
  const value = normalizeInstrumentSearch(query);
  if (!value || assetType === 'perp') return null;
  if (assetType === 'future' || (assetType === 'all' && ['ES', 'NQ', 'MES', 'MNQ', 'RTY', 'M2K', 'MGC'].includes(value))) return 'FUT';
  return 'STK';
}
