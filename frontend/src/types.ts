export const TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'] as const;
export type Timeframe = typeof TIMEFRAMES[number];
export const LABELS: Record<Timeframe, string> = {
  '1m': '1 分钟', '3m': '3 分钟', '5m': '5 分钟', '15m': '15 分钟', '30m': '30 分钟',
  '1h': '1 小时', '2h': '2 小时', '4h': '4 小时', '1d': '1 日', '1w': '1 周',
};
export const TIMEFRAME_GROUPS: { label: string; values: readonly Timeframe[] }[] = [
  { label: '分钟', values: ['1m', '3m', '5m', '15m', '30m'] },
  { label: '小时', values: ['1h', '2h', '4h'] },
  { label: '日 / 周', values: ['1d', '1w'] },
];
export interface Bar {
  time: number; end: number; open: number; high: number; low: number; close: number;
  volume: number; trades: number; status: 'forming' | 'confirmed'; revision: number; received_at: number;
}
export interface Indicator {
  time: number; ema20: number | null; ema60: number | null; rsi14: number | null;
  macd: number | null; signal: number | null; histogram: number | null;
  provisional: boolean; samples: number;
}
export interface Quality {
  status: string; gaps: { start: number; end: number; bars: number }[]; pending: number;
  count: number; last_confirmed_end: number | null; last_rest: number | null; error: string | null;
}
export interface ChartData {
  instrument?: Instrument;
  symbol: string; instrument_id: string;
  timeframe: Timeframe; bars: Bar[]; indicators: Indicator[]; revision: number;
  quality: Quality; as_of: number; coverage_start: number | null; algorithm_version: string;
}
export interface Quote {
  mark: number | null; mid: number | null; change_pct: number | null; volume_usd: number | null;
  oi_base: number | null; funding: number | null; received_at: number; source: string;
  volume?: number | null; bid?: number | null; ask?: number | null; bid_size?: number | null; ask_size?: number | null;
  data_mode?: string; source_time?: number | null;
}
export interface Instrument {
  symbol: string; instrument_id: string; provider: string; display_symbol: string; native_symbol: string;
  name: string; asset_type: string; currency: string; exchange: string; expiry: string; multiplier: string;
  tick_size: number; timezone: string; session: string; volume_unit: string; con_id: number;
}
export interface Overview {
  instrument?: Instrument; data_cutoff?: number; history_delay_minutes?: number;
  symbol: string; instrument_id: string;
  quote: Quote | null; quote_stale: boolean; transport: 'live' | 'polling' | 'offline';
  ws_connected: boolean; last_ws: number | null; syncing: boolean; server_time: number; reconnects: number;
  errors: Record<string, string>;
  timeframes: Record<Timeframe, { revision: number; quality: Quality; confirmed_indicators: Indicator | null; last_close: number | null }>;
}
export type Visibility = { ema20: boolean; ema60: boolean; rsi: boolean; macd: boolean };
export const money = (n?: number | null, digits = 1) => n == null ? '—' : n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const compact = (n?: number | null) => n == null ? '—' : n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(2)}K` : money(n);
export const clock = (n?: number | null) => n ? new Date(n).toLocaleTimeString('zh-CN', { hour12: false }) : '—';

export const priceDigits = (n?: number | null) => !n ? 2 : Math.min(10, Math.max(2, 5 - Math.floor(Math.log10(Math.abs(n)))));
export const price = (n?: number | null) => money(n, priceDigits(n));
