export type ExamStatus = 'ready' | 'running' | 'paused' | 'passed' | 'failed' | 'terminated';
export type ExamAction = 'start' | 'pause' | 'resume' | 'terminate' | 'restart';

export interface ExamPreset {
  id: string;
  name: string;
  version: number;
  initial_balance: number;
  profit_target: number;
  max_drawdown: number;
  daily_loss_limit: number | null;
  max_contracts: number;
  min_trading_days: number;
  consistency_pct: number | null;
  drawdown_mode: 'eod_trailing' | 'fixed';
  fee_per_contract: number;
  slippage_ticks: number;
  timezone?: string;
  session_close?: string;
  session_reopen?: string;
  description?: string;
  builtin?: boolean;
}

export interface ExamStrategy {
  id: string;
  name: string;
  version: string | number;
  description?: string;
  parameters?: Record<string, unknown>;
}

export interface ExamInstrument {
  symbol: string;
  display_symbol?: string;
  full_name?: string;
  name?: string;
  expiry?: string;
  data_mode?: string;
  multiplier?: string | number;
  tick_size?: number;
}

export interface ExamPosition {
  side?: string;
  direction?: string;
  quantity?: number;
  entry_price?: number;
  average_price?: number;
  entry_time?: string | number;
  unrealized_pnl?: number;
}

export interface ExamAccount {
  id: string;
  name: string;
  status: ExamStatus;
  symbol: string;
  display_symbol?: string;
  instrument?: ExamInstrument;
  timeframe: string;
  quantity: number;
  strategy: ExamStrategy;
  preset: ExamPreset;
  balance: number;
  equity: number;
  net_pnl: number;
  unrealized_pnl: number;
  drawdown_limit: number;
  daily_pnl: number;
  trading_days: number;
  trade_count: number;
  consistency_ratio?: number | null;
  best_day_pnl?: number;
  position?: ExamPosition | null;
  run_state?: string | Record<string, unknown>;
  last_error?: string | null;
  data_mode?: string;
  data_as_of?: string | number | null;
  created_at?: string | number;
  updated_at?: string | number;
  parent_account_id?: string | null;
}

export interface ExamTrade {
  id?: string | number;
  side?: string;
  direction?: string;
  quantity?: number;
  entry_price?: number;
  exit_price?: number | null;
  entry_time?: string | number;
  exit_time?: string | number | null;
  opened_at?: string | number;
  closed_at?: string | number | null;
  net_pnl?: number;
  pnl?: number;
  fees?: number;
  fee?: number;
  entry_fee?: number;
  exit_fee?: number;
  status?: string;
  reason?: string;
  exit_reason?: string;
}

export interface ExamEvent {
  id?: string | number;
  at?: string | number;
  created_at?: string | number;
  time?: string | number;
  kind?: string;
  type?: string;
  message?: string;
  detail?: string;
}

export interface ExamEquityPoint {
  at?: string | number;
  time?: string | number;
  timestamp?: string | number;
  equity: number;
  balance?: number;
}

export interface ExamDetail {
  account: ExamAccount;
  trades: ExamTrade[];
  events: ExamEvent[];
  equity_curve: ExamEquityPoint[];
  position?: ExamPosition | null;
}
