// These are search aliases, not a market catalog. Results still come from IBKR.
const SEARCH_ALIASES: Record<string, string> = {
  '英伟达': 'NVDA', '英偉達': 'NVDA', '辉达': 'NVDA', '輝達': 'NVDA',
  '苹果': 'AAPL', '蘋果': 'AAPL', '微软': 'MSFT', '微軟': 'MSFT',
  '特斯拉': 'TSLA', '亚马逊': 'AMZN', '亞馬遜': 'AMZN', '谷歌': 'GOOGL',
  '微型黄金': 'MGC', '微型黄金期货': 'MGC', '微型黃金': 'MGC',
};

export function normalizeInstrumentSearch(query: string) {
  const value = query.trim();
  return SEARCH_ALIASES[value] ?? value.toUpperCase();
}

export function instrumentMatches(row: {symbol: string; display_symbol?: string; name?: string}, query: string) {
  const value = normalizeInstrumentSearch(query).replace(/\/(USDC|USD|USDT)$/, '');
  return `${row.symbol} ${row.display_symbol ?? ''} ${row.name ?? ''}`.toUpperCase().includes(value);
}

export function brokerSearchKind(query: string, assetType: string) {
  const value = normalizeInstrumentSearch(query);
  if (!value || assetType === 'perp') return null;
  if (assetType === 'future' || (assetType === 'all' && ['ES', 'NQ', 'MES', 'MNQ', 'RTY', 'M2K', 'MGC'].includes(value))) return 'FUT';
  return 'STK';
}
