import type { Bar, Indicator, Quality, Quote, Timeframe, Instrument } from './types';

export interface ModelSettings {
  base_url: string; model: string; max_tokens: number;
  token_parameter: string; has_key: boolean; ready: boolean;
}
export interface FrameworkDraft {
  name: string; description: string; timeframes: Timeframe[];
  indicators: string[]; steps: { id: string; title: string; condition: string }[]; output_style: string;
}
export interface Framework extends FrameworkDraft { id: string; version: number; created_at: number }
export interface Snapshot {
  instrument?: Instrument; history_delay_minutes?: number;
  id: string; symbol: string; timeframe: Timeframe; as_of: number; source: string;
  quote: Quote | null; ready: boolean; warnings: string[]; blocking_reasons: string[];
  timeframes: Partial<Record<Timeframe, {
    bars: (Bar & { evidence_id: string })[]; indicators: (Indicator & { evidence_id: string })[];
    input_count: number; last_closed_at: number | null; last_close: number | null;
    latest_indicators: Indicator | null; quality: Quality;
  }>>;
  levels: { level_ref: string; timeframe: Timeframe; price: number; kind: string }[];
  framework?: Framework;
}
export interface AnalysisResult {
  market_regime: string; thesis: string;
  observations: { text: string; evidence_refs: string[] }[];
  key_levels: { level_ref: string; timeframe: Timeframe; price: number; kind: string; rationale: string }[];
  scenarios: { direction: string; trigger: string; invalidation: string; evidence_refs: string[] }[];
  risks: string[]; follow_up_questions: string[];
  framework_checks?: { step_id: string; title: string; condition: string; status: 'met' | 'not_met' | 'insufficient_data'; explanation: string; evidence_refs: string[] }[];
}
export interface AnalysisSummary {
  symbol: string;
  id: string; snapshot_id: string; parent_id: string | null; created_at: number;
  status: 'running' | 'completed' | 'failed' | 'cancelled'; timeframe: Timeframe;
  question: string; error: string | null; model: string;
}
export interface AnalysisRun extends AnalysisSummary {
  snapshot: Snapshot; result: AnalysisResult | null; stage: string;
  raw_output: string | null; usage: Record<string, number>;
  framework?: Framework | null; framework_version?: string;
}

export async function aiRequest<T>(path: string, body?: unknown, method = 'POST', signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/v1/ai${path}`, body === undefined
    ? { signal }
    : { method, signal, headers: { 'Content-Type': 'application/json', 'X-Local-Market': '1' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未成功，请稍后重试。');
  return data;
}
export const dateTime = (time?: number | null) => time ? new Date(time).toLocaleString('zh-CN', { hour12: false }) : '—';
