import { CRYPTO_NAMES, CRYPTO_SEARCH_ALIASES } from './cryptoNames.ts';
import { STOCK_SEARCH_ALIASES, stockDescription } from './stockNames.ts';

export interface NamedInstrument {
  symbol: string;
  display_symbol?: string;
  native_symbol?: string;
  name?: string;
  provider?: string;
  asset_type?: string;
}

// This is display/search metadata only. The provider's symbol and contract ID
// remain the identity used to open a chart or save a watchlist item.
export function instrumentDescription(item: NamedInstrument): string {
  const symbol = item.native_symbol || item.display_symbol || item.symbol;
  const isBroker = item.provider === 'ibkr' || item.symbol.startsWith('ibkr:')
    || item.asset_type === 'stock' || item.asset_type === 'future';
  const description = isBroker
    ? stockDescription(symbol, item.name, item.asset_type)
    : CRYPTO_NAMES[symbol];
  if (description) return description;
  return item.name && item.name.toUpperCase() !== symbol.toUpperCase() ? item.name : '';
}

const SEARCH_ALIASES: Record<string, string> = Object.fromEntries(
  Object.entries({ ...CRYPTO_SEARCH_ALIASES, ...STOCK_SEARCH_ALIASES }).map(([alias, symbol]) => [alias.toUpperCase(), symbol]),
);
const PROJECT_ALIASES = Object.fromEntries(Object.entries(CRYPTO_NAMES).map(([symbol, name]) => [name.toUpperCase(), symbol]));

export function instrumentSearchAlias(query: string): string {
  const value = query.trim();
  return SEARCH_ALIASES[value.toUpperCase()] ?? PROJECT_ALIASES[value.toUpperCase()] ?? value.toUpperCase();
}
