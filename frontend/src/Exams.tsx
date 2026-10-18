import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ArrowRight, ArrowUpRight, Check, CheckCircle2, ChevronDown, ChevronRight, CircleHelp, Copy, Flag, Layers3, LoaderCircle, Pause, Play, Plus, RefreshCw, Settings2, ShieldCheck, Target, Trophy, X } from 'lucide-react';
import type { ExamAccount, ExamAction, ExamDetail, ExamEquityPoint, ExamInstrument, ExamPreset, ExamStatus, ExamStrategy } from './examTypes';
import './exams.css';

const statusNames: Record<ExamStatus, string> = { ready: '待开考', running: '考试中', paused: '已暂停', passed: '已通过', failed: '未通过', terminated: '已结束' };
const timeframes = [['1m', '1 分钟'], ['3m', '3 分钟'], ['5m', '5 分钟'], ['15m', '15 分钟'], ['30m', '30 分钟'], ['1h', '1 小时']] as const;
const usd = (value: number | null | undefined, digits = 0) => value == null || !Number.isFinite(value) ? '—' : `$${value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
const signed = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${usd(Math.abs(value), 2)}`;
const num = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString('en-US', { maximumFractionDigits: 4 });
const signClass = (value: number | null | undefined) => value == null || value === 0 ? '' : value > 0 ? 'ex-positive' : 'ex-negative';
const percent = (value: number) => Math.min(100, Math.max(0, Number.isFinite(value) ? value : 0));
const exitReasons: Record<string, string> = { strategy_reverse: '策略反向信号', maximum_drawdown: '触及最大亏损线', daily_loss_limit: '触及每日亏损限额', session_close: '每日收盘平仓', user_paused: '暂停考试平仓', user_terminated: '结束考试平仓' };
const displaySymbol = (account: ExamAccount) => account.instrument?.display_symbol ?? account.display_symbol ?? account.symbol;
function dateLabel(value?: string | number | null, compact = false) {
  if (value == null || value === '') return '—';
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  if (!Number.isFinite(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', ...(compact ? {} : { hour: '2-digit', minute: '2-digit', hour12: false }) });
}
function errorMessage(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请稍后重试。'; }
async function request<T>(path: string, options: { method?: string; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/exams${path}`, { method: options.method ?? 'GET', headers: { 'X-Local-Market': '1', 'Content-Type': 'application/json' }, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('本地服务暂时未连接，请确认服务运行后重试。');
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg ?? '请检查填写内容').join('；') : '暂时无法完成操作，请稍后重试。');
  }
  return body as T;
}
function listOf<T>(value: T[] | { items: T[] }) { return Array.isArray(value) ? value : value.items ?? []; }
function Status({ status }: { status: ExamStatus }) { return <span className={`ex-status ex-status-${status}`}><i/>{statusNames[status] ?? status}</span>; }

function Dialog({ title, children, onClose, busy = false }: { title: string; children: ReactNode; onClose: () => void; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const dialog = ref.current;
    const previous = document.activeElement as HTMLElement | null;
    dialog?.showModal();
    return () => { dialog?.close(); previous?.focus(); };
  }, []);
  return <dialog className="ex-dialog" ref={ref} aria-labelledby={titleId} onCancel={event => { event.preventDefault(); if (!busy) closeRef.current(); }} onClick={event => { if (event.target === ref.current && !busy) onClose(); }}>
    <div className="ex-dialog-header"><div><span className="ex-eyebrow">EXAM SETTINGS</span><h2 id={titleId}>{title}</h2></div><button type="button" className="ex-icon-button" aria-label="关闭对话框" disabled={busy} onClick={onClose}><X size={19}/></button></div>
    {children}
  </dialog>;
}

type PresetDraft = Omit<ExamPreset, 'id' | 'version'>;
function PresetEditor({ preset, onClose, onSaved }: { preset: ExamPreset; onClose: () => void; onSaved: (value: ExamPreset) => void }) {
  const [draft, setDraft] = useState<PresetDraft>({ name: `${preset.name} · 自定义`, description: preset.description, initial_balance: preset.initial_balance, profit_target: preset.profit_target, max_drawdown: preset.max_drawdown, daily_loss_limit: preset.daily_loss_limit, max_contracts: preset.max_contracts, min_trading_days: preset.min_trading_days, consistency_pct: preset.consistency_pct, drawdown_mode: preset.drawdown_mode, fee_per_contract: preset.fee_per_contract, slippage_ticks: preset.slippage_ticks, timezone: preset.timezone ?? 'America/Chicago', session_close: preset.session_close ?? '15:10', session_reopen: preset.session_reopen ?? '17:00' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const updateNumber = (key: keyof PresetDraft, value: string, nullable = false) => setDraft(old => ({ ...old, [key]: nullable && value === '' ? null : Number(value) }));
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try { onSaved(await request<ExamPreset>('/presets', { method: 'POST', body: { ...draft, name: draft.name.trim() } })); }
    catch (error) { setError(errorMessage(error)); setBusy(false); }
  }
  return <Dialog title="创建规则套餐" onClose={onClose} busy={busy}><form onSubmit={save} className="ex-preset-form">
    <p className="ex-dialog-note"><Copy size={15}/>从「{preset.name}」复制，保存为新套餐。已创建账号继续使用开考时的规则。</p>
    <label className="ex-field ex-full"><span>套餐名称</span><input autoFocus required maxLength={60} value={draft.name} onChange={e => setDraft(old => ({ ...old, name: e.target.value }))}/></label>
    <div className="ex-form-grid">
      <label className="ex-field"><span>初始资金 · USD</span><input required type="number" min="1000" max="10000000" step="1" value={draft.initial_balance} onChange={e => updateNumber('initial_balance', e.target.value)}/></label>
      <label className="ex-field"><span>盈利目标 · USD</span><input required type="number" min="1" max="1000000" step="1" value={draft.profit_target} onChange={e => updateNumber('profit_target', e.target.value)}/></label>
      <label className="ex-field"><span>最大亏损额度 · USD</span><input required type="number" min="1" max={Math.min(1000000, draft.initial_balance - 1)} step="1" value={draft.max_drawdown} onChange={e => updateNumber('max_drawdown', e.target.value)}/></label>
      <label className="ex-field"><span>每日亏损限额 · USD <small>可不设</small></span><input type="number" min="1" max={draft.max_drawdown} step="1" placeholder="不限制" value={draft.daily_loss_limit ?? ''} onChange={e => updateNumber('daily_loss_limit', e.target.value, true)}/></label>
      <label className="ex-field"><span>最大合约手数</span><input required type="number" min="1" max="100" step="1" value={draft.max_contracts} onChange={e => updateNumber('max_contracts', e.target.value)}/></label>
      <label className="ex-field"><span>最少交易日</span><input required type="number" min="1" max="365" step="1" value={draft.min_trading_days} onChange={e => updateNumber('min_trading_days', e.target.value)}/></label>
      <label className="ex-field"><span>单日盈利占比上限 · % <small>可不设</small></span><input type="number" min="10" max="100" step="1" placeholder="不限制" value={draft.consistency_pct == null ? '' : Math.round(draft.consistency_pct * 100)} onChange={e => setDraft(old => ({ ...old, consistency_pct: e.target.value === '' ? null : Number(e.target.value) / 100 }))}/></label>
      <label className="ex-field"><span>亏损线计算方式</span><select value={draft.drawdown_mode} onChange={e => setDraft(old => ({ ...old, drawdown_mode: e.target.value as ExamPreset['drawdown_mode'] }))}><option value="eod_trailing">日终追踪</option><option value="fixed">固定亏损线</option></select></label>
      <label className="ex-field"><span>每手单边手续费 · USD</span><input required type="number" min="0" max="1000" step="0.01" value={draft.fee_per_contract} onChange={e => updateNumber('fee_per_contract', e.target.value)}/></label>
      <label className="ex-field"><span>每次成交滑点 · tick</span><input required type="number" min="0" max="100" step="1" value={draft.slippage_ticks} onChange={e => updateNumber('slippage_ticks', e.target.value)}/></label>
      <label className="ex-field"><span>每日平仓时间 · 芝加哥</span><input required type="time" value={draft.session_close} onChange={e => setDraft(old => ({ ...old, session_close: e.target.value }))}/></label>
      <label className="ex-field"><span>每日恢复时间 · 芝加哥</span><input required type="time" value={draft.session_reopen} onChange={e => setDraft(old => ({ ...old, session_reopen: e.target.value }))}/></label>
    </div>
    <p className="ex-help">日终追踪：亏损线随日终账户高点上移；盘中浮亏也会触发亏损检查。具体规则在账号中固定保存。</p>
    {error && <p className="ex-error" role="alert">{error}</p>}
    <div className="ex-dialog-actions"><button type="button" className="ex-button" onClick={onClose} disabled={busy}>取消</button><button type="submit" className="ex-button ex-primary" disabled={busy}>{busy ? <LoaderCircle size={15} className="ex-spin"/> : <Check size={15}/>}保存新套餐</button></div>
  </form></Dialog>;
}

function EquityChart({ points, initial }: { points: ExamEquityPoint[]; initial: number }) {
  const id = useId().replace(/:/g, '');
  const values = points.filter(point => Number.isFinite(point.equity));
  if (!values.length) return <div className="ex-chart-empty"><Layers3 size={25}/><strong>净值曲线等待第一笔行情</strong><span>开考后持续记录账户净值与浮动盈亏。</span></div>;
  const width = 780, height = 206, left = 12, right = 76, top = 16, bottom = 26;
  const rawMin = Math.min(initial, ...values.map(point => point.equity)), rawMax = Math.max(initial, ...values.map(point => point.equity));
  const pad = Math.max((rawMax - rawMin) * .18, Math.abs(initial) * .001, 1);
  const min = rawMin - pad, max = rawMax + pad;
  const x = (index: number) => left + index / Math.max(1, values.length - 1) * (width - left - right);
  const y = (value: number) => top + (max - value) / (max - min) * (height - top - bottom);
  const line = values.map((point, index) => `${index ? 'L' : 'M'}${x(index).toFixed(2)},${y(point.equity).toFixed(2)}`).join(' ');
  const end = values[values.length - 1];
  const positive = end.equity >= initial;
  const color = positive ? '#8bdbb8' : '#f08497';
  const axisPoints = [max, (max + min) / 2, min];
  return <div className="ex-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`账户净值曲线，当前净值 ${usd(end.equity, 2)}`}>
    <defs><linearGradient id={`ex-fill-${id}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={color} stopOpacity=".16"/><stop offset="100%" stopColor={color} stopOpacity="0"/></linearGradient></defs>
    {axisPoints.map((value, index) => <g key={index}><line x1={left} y1={y(value)} x2={width - right} y2={y(value)} stroke="#25232c" strokeDasharray="3 5"/><text x={width - right + 12} y={y(value) + 3} fill="#767180" fontSize="10">{usd(value)}</text></g>)}
    <line x1={left} y1={y(initial)} x2={width - right} y2={y(initial)} stroke="#9684ce" strokeOpacity=".45" strokeDasharray="4 5"/>
    <path d={`${line} L${x(values.length - 1)},${height - bottom} L${left},${height - bottom} Z`} fill={`url(#ex-fill-${id})`}/><path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"/>
    <circle cx={x(values.length - 1)} cy={y(end.equity)} r="3" fill={color}/>
    <text x={left} y={height - 4} fill="#767180" fontSize="10">{dateLabel(values[0].at ?? values[0].time ?? values[0].timestamp)}</text>
    {values.length > 1 && <text x={width - right} y={height - 4} textAnchor="end" fill="#767180" fontSize="10">{dateLabel(end.at ?? end.time ?? end.timestamp)}</text>}
  </svg></div>;
}

function AccountDetail({ detail, account, busy, loading, error, onAction }: { detail: ExamDetail | null; account: ExamAccount; busy: boolean; loading: boolean; error: string; onAction: (account: ExamAccount, action: ExamAction) => void }) {
  const [tab, setTab] = useState<'trades' | 'events'>('trades');
  const preset = account.preset;
  const position = detail?.position ?? account.position;
  const remaining = account.equity - account.drawdown_limit;
  const pnl = account.balance - preset.initial_balance;
  const progress = percent(pnl / preset.profit_target * 100);
  const state = typeof account.run_state === 'string' ? account.run_state : typeof account.run_state?.phase === 'string' ? account.run_state.phase : '';
  const stateLabels: Record<string, string> = { waiting_for_feed: '等待行情连接', waiting_for_data: '等待新行情', waiting_for_start_data: '等待开考后的行情', waiting_for_post_start_data: '等待延迟行情追上开考时间', closing: '等待新鲜行情平仓', recovering: '恢复行情与账本校验', daily_loss_pause: '已达每日亏损限额，等待下一交易日', data_gap: '行情存在缺口，暂停新开仓', waiting_history: '等待历史行情', waiting_data: '等待新行情', warming_up: '策略准备中', ready: '等待策略信号', running: '策略运行中', market_closed: '交易时段外', delayed: '延迟行情', disconnected: '行情暂时中断', stale: '等待新行情', waiting_bar: '等待下一根 K 线', paused: '已暂停', idle: '等待开考' };
  const actions: [ExamAction, string, ReactNode][] = account.status === 'ready' ? [['start', '开始考试', <Play size={14}/>]] : account.status === 'running' ? [['pause', '暂停', <Pause size={14}/>]] : account.status === 'paused' ? [['resume', '继续考试', <Play size={14}/>]] : [];
  function recordTabKey(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'trades' : event.key === 'End' ? 'events' : tab === 'trades' ? 'events' : 'trades';
    setTab(next);
    event.currentTarget.querySelector<HTMLButtonElement>(`#ex-${next}-tab`)?.focus();
  }
  return <section className="ex-detail ex-panel" aria-label={`${account.name} 账号详情`} aria-busy={loading}>
    <div className="ex-detail-heading"><div><span className="ex-eyebrow">ACCOUNT DETAIL</span><h2>{account.name}<Status status={account.status}/></h2><p>{displaySymbol(account)} <i/> {timeframes.find(([key]) => key === account.timeframe)?.[1] ?? account.timeframe} <i/> {account.strategy.name} · v{account.strategy.version}</p></div><div className="ex-detail-actions">{actions.map(([action, title, icon]) => <button key={action} className={`ex-button ${action === 'pause' ? '' : 'ex-primary'}`} disabled={busy} onClick={() => onAction(account, action)}>{busy ? <LoaderCircle size={14} className="ex-spin"/> : icon}{title}</button>)}{['ready', 'running', 'paused'].includes(account.status) && <button className="ex-button ex-quiet" disabled={busy} onClick={() => onAction(account, 'terminate')}><Flag size={14}/>结束本轮</button>}{['passed', 'failed', 'terminated'].includes(account.status) && <button className="ex-button" disabled={busy} onClick={() => onAction(account, 'restart')}><RefreshCw size={14}/>重新开考</button>}</div></div>
    {(account.last_error || error) && <div className="ex-inline-warning" role="alert"><CircleHelp size={15}/><span>{error || account.last_error}</span></div>}
    <div className="ex-detail-stats"><div><span>账户净值</span><strong>{usd(account.equity, 2)}</strong><small>余额 {usd(account.balance, 2)}</small></div><div><span>累计已实现盈亏</span><strong className={signClass(pnl)}>{signed(pnl)}</strong><small>浮动 {signed(account.unrealized_pnl)}</small></div><div><span>剩余亏损空间</span><strong className={remaining <= preset.max_drawdown * .2 ? 'ex-negative' : ''}>{usd(Math.max(0, remaining), 2)}</strong><small>亏损线 {usd(account.drawdown_limit, 2)}</small></div><div><span>今日盈亏</span><strong className={signClass(account.daily_pnl)}>{signed(account.daily_pnl)}</strong><small>{account.trading_days} / {preset.min_trading_days} 个交易日 · {account.trade_count} 笔成交</small></div></div>
    <div className="ex-goal-row"><span><Target size={14}/>盈利目标 <b>{usd(preset.profit_target)}</b></span><div className="ex-progress-track" role="progressbar" aria-label="盈利目标完成度" aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${progress}%` }}/></div><strong>{Math.round(progress)}%</strong></div>
    <div className="ex-pass-conditions"><span className={account.trading_days >= preset.min_trading_days ? 'met' : ''}><CheckCircle2 size={12}/>交易日 {account.trading_days} / {preset.min_trading_days}</span>{preset.consistency_pct != null && <span className={account.consistency_ratio != null && account.consistency_ratio <= preset.consistency_pct ? 'met' : ''}><CheckCircle2 size={12}/>单日盈利占比 {account.consistency_ratio == null ? '—' : `${Math.round(account.consistency_ratio * 100)}%`} / ≤ {Math.round(preset.consistency_pct * 100)}%</span>}<span>平仓后检查通过条件</span></div>
    <div className="ex-chart-heading"><h3>账户净值</h3><span><i/>{preset.name} · 规则 v{preset.version}</span></div>
    {loading && !detail ? <div className="ex-chart-empty"><LoaderCircle className="ex-spin" size={23}/><span>正在读取考试记录…</span></div> : <EquityChart points={detail?.equity_curve ?? []} initial={preset.initial_balance}/>}
    <div className="ex-runtime"><span className={`ex-runtime-dot ${account.status === 'running' ? 'live' : ''}`}/><span>{account.status === 'running' ? stateLabels[state] ?? '跟随后续行情运行' : statusNames[account.status]}</span><span className="ex-runtime-data">{account.data_mode === 'delayed' ? '延迟行情' : 'K 线模拟'}{account.data_as_of && <><i/>行情截至 {dateLabel(account.data_as_of)}</>}<i/>更新于 {dateLabel(account.updated_at)}</span></div>
    <div className="ex-position"><h3>当前持仓</h3>{position && Number(position.quantity ?? 0) !== 0 ? <div className="ex-position-values"><strong className={(position.side ?? position.direction) === 'short' ? 'ex-negative' : 'ex-positive'}>{['short', 'sell'].includes(position.side ?? position.direction ?? '') ? '空' : '多'} {Math.abs(position.quantity ?? 0)} 手</strong><span>开仓均价 <b>{num(position.entry_price ?? position.average_price)}</b></span><span>浮动盈亏 <b className={signClass(account.unrealized_pnl)}>{signed(account.unrealized_pnl)}</b></span></div> : <span className="ex-position-empty">当前空仓</span>}</div>
    <div className="ex-record-tabs" role="tablist" aria-label="考试记录" onKeyDown={recordTabKey}><button id="ex-trades-tab" role="tab" tabIndex={tab === 'trades' ? 0 : -1} aria-selected={tab === 'trades'} aria-controls="ex-trades-panel" onClick={() => setTab('trades')}>交易记录 <span>{detail?.trades?.length ?? 0}</span></button><button id="ex-events-tab" role="tab" tabIndex={tab === 'events' ? 0 : -1} aria-selected={tab === 'events'} aria-controls="ex-events-panel" onClick={() => setTab('events')}>运行事件 <span>{detail?.events?.length ?? 0}</span></button></div>
    {tab === 'trades' ? <div id="ex-trades-panel" role="tabpanel" aria-labelledby="ex-trades-tab" className="ex-record-panel">{detail?.trades?.length ? <div className="ex-table-scroll"><table className="ex-trades-table"><thead><tr><th>开仓 / 平仓时间</th><th>方向 · 手数</th><th>开仓价 / 平仓价</th><th>净盈亏</th><th>平仓原因</th></tr></thead><tbody>{detail.trades.map((trade, index) => <tr key={trade.id ?? index}><td>{dateLabel(trade.entry_time ?? trade.opened_at)}<small>{dateLabel(trade.exit_time ?? trade.closed_at)}</small></td><td><span className={['short', 'sell'].includes(trade.side ?? trade.direction ?? '') ? 'ex-negative' : 'ex-positive'}>{['short', 'sell'].includes(trade.side ?? trade.direction ?? '') ? '空' : '多'}</span> · {trade.quantity ?? '—'}</td><td>{num(trade.entry_price)}<small>{num(trade.exit_price)}</small></td><td className={signClass(trade.net_pnl ?? trade.pnl)}>{signed(trade.net_pnl ?? trade.pnl)}<small>费用 {usd(trade.fees ?? trade.fee ?? (trade.entry_fee != null ? trade.entry_fee + (trade.exit_fee ?? 0) : undefined), 2)}</small></td><td>{trade.status === 'open' ? '持仓中' : exitReasons[trade.exit_reason ?? trade.reason ?? ''] ?? trade.exit_reason ?? trade.reason ?? '—'}</td></tr>)}</tbody></table></div> : <div className="ex-record-empty"><Layers3 size={20}/><p>还没有交易记录</p><span>策略产生信号并完成模拟成交后，记录将出现在这里。</span></div>}</div> : <div id="ex-events-panel" role="tabpanel" aria-labelledby="ex-events-tab" className="ex-record-panel">{detail?.events?.length ? <ol className="ex-events">{detail.events.map((event, index) => <li key={event.id ?? index}><i/><div><p>{event.message?.startsWith('模拟平仓：') ? `模拟平仓：${exitReasons[event.message.slice(5)] ?? event.message.slice(5)}` : event.message ?? event.detail ?? event.kind ?? event.type ?? '状态更新'}</p><time>{dateLabel(event.at ?? event.created_at ?? event.time)}</time></div></li>)}</ol> : <div className="ex-record-empty"><ShieldCheck size={20}/><p>还没有运行事件</p><span>开考、暂停、行情中断及规则触发都会留下记录。</span></div>}</div>}
    <details className="ex-snapshot"><summary>查看本轮规则快照<ChevronDown size={14}/></summary><dl><div><dt>最大亏损额度</dt><dd>{usd(preset.max_drawdown)}</dd></div><div><dt>亏损线方式</dt><dd>{preset.drawdown_mode === 'eod_trailing' ? '日终追踪' : '固定亏损线'}</dd></div><div><dt>每日亏损限额</dt><dd>{preset.daily_loss_limit ? usd(preset.daily_loss_limit) : '不限制'}</dd></div><div><dt>单日盈利占比</dt><dd>{preset.consistency_pct ? `≤ ${Math.round(preset.consistency_pct * 100)}%` : '不限制'}</dd></div><div><dt>仓位限制</dt><dd>{preset.max_contracts} 手</dd></div><div><dt>手续费 / 滑点</dt><dd>{usd(preset.fee_per_contract, 2)} 单边 / {preset.slippage_ticks} tick</dd></div><div><dt>每日平仓 / 恢复</dt><dd>{preset.session_close ?? '15:10'} / {preset.session_reopen ?? '17:00'}（芝加哥）</dd></div></dl></details>
  </section>;
}

export function ExamsPage({ active = true }: { active?: boolean }) {
  const [presets, setPresets] = useState<ExamPreset[]>([]);
  const [strategies, setStrategies] = useState<ExamStrategy[]>([]);
  const [instruments, setInstruments] = useState<ExamInstrument[]>([]);
  const [accounts, setAccounts] = useState<ExamAccount[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<ExamDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [metadataError, setMetadataError] = useState('');
  const [metadataLoading, setMetadataLoading] = useState(true);
  const [detailError, setDetailError] = useState('');
  const [writeError, setWriteError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [filter, setFilter] = useState('all');
  const [createOpen, setCreateOpen] = useState(false);
  const [presetId, setPresetId] = useState('');
  const [strategyId, setStrategyId] = useState('');
  const [symbol, setSymbol] = useState('');
  const [timeframe, setTimeframe] = useState('5m');
  const [quantity, setQuantity] = useState(1);
  const [name, setName] = useState('');
  const [editingPreset, setEditingPreset] = useState<ExamPreset | null>(null);
  const [confirmation, setConfirmation] = useState<{ account: ExamAccount; action: ExamAction } | null>(null);
  const [notice, setNotice] = useState('');
  const writing = useRef(false);
  const mutationEpoch = useRef(0);
  const nameInput = useRef<HTMLInputElement>(null);
  const createRef = useRef<HTMLElement>(null);
  const preset = presets.find(item => item.id === presetId);
  const instrument = instruments.find(item => item.symbol === symbol);
  const selected = detail?.account.id === selectedId ? detail.account : accounts.find(item => item.id === selectedId) ?? null;

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let current = true;
    async function load() {
      const results = await Promise.allSettled([request<ExamPreset[] | { items: ExamPreset[] }>('/presets', { signal: controller.signal }), request<ExamStrategy[] | { items: ExamStrategy[] }>('/strategies', { signal: controller.signal }), request<ExamInstrument[] | { items: ExamInstrument[] }>('/instruments', { signal: controller.signal })]);
      if (!current) return;
      const [presetResult, strategyResult, instrumentResult] = results;
      if (presetResult.status === 'fulfilled') { const items = listOf(presetResult.value); setPresets(items); setPresetId(old => items.some(item => item.id === old) ? old : items[0]?.id ?? ''); }
      if (strategyResult.status === 'fulfilled') { const items = listOf(strategyResult.value); setStrategies(items); setStrategyId(old => items.some(item => item.id === old) ? old : items[0]?.id ?? ''); }
      if (instrumentResult.status === 'fulfilled') { const items = listOf(instrumentResult.value); setInstruments(items); setSymbol(old => items.some(item => item.symbol === old) ? old : items[0]?.symbol ?? ''); }
      const rejected = results.find(item => item.status === 'rejected');
      setMetadataError(rejected?.status === 'rejected' ? errorMessage(rejected.reason) : '');
      setMetadataLoading(false);
    }
    void load();
    return () => { current = false; controller.abort(); };
  }, [active, revision]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let current = true, pending = false;
    async function load() {
      if (pending || writing.current) return;
      pending = true;
      const epoch = mutationEpoch.current;
      try {
        const next = listOf(await request<ExamAccount[] | { items: ExamAccount[] }>('/accounts', { signal: controller.signal }));
        if (!current || epoch !== mutationEpoch.current) return;
        setAccounts(next); setLoadError('');
        setSelectedId(old => next.some(item => item.id === old) ? old : next[0]?.id ?? '');
      } catch (error) { if (current) setLoadError(errorMessage(error)); }
      finally { pending = false; if (current) setLoading(false); }
    }
    void load(); const timer = window.setInterval(load, 4000);
    return () => { current = false; controller.abort(); window.clearInterval(timer); };
  }, [active, revision]);

  useEffect(() => {
    setDetail(null); setDetailError('');
    if (!active || !selectedId) return;
    const controller = new AbortController();
    let current = true, pending = false;
    setDetailLoading(true);
    async function load() {
      if (pending || writing.current) return;
      pending = true;
      const epoch = mutationEpoch.current;
      try {
        const next = await request<ExamDetail>(`/accounts/${encodeURIComponent(selectedId)}`, { signal: controller.signal });
        if (current && epoch === mutationEpoch.current) { setDetail(next); setDetailError(''); }
      } catch (error) { if (current) setDetailError(errorMessage(error)); }
      finally { pending = false; if (current) setDetailLoading(false); }
    }
    void load(); const timer = window.setInterval(load, 4000);
    return () => { current = false; controller.abort(); window.clearInterval(timer); };
  }, [active, selectedId, revision]);

  const totals = useMemo(() => ({ running: accounts.filter(item => item.status === 'running').length, passed: accounts.filter(item => item.status === 'passed').length, completed: accounts.filter(item => item.status === 'passed' || item.status === 'failed').length, pnl: accounts.reduce((sum, item) => sum + (item.balance - item.preset.initial_balance), 0), trades: accounts.reduce((sum, item) => sum + (item.trade_count ?? 0), 0) }), [accounts]);
  const rows = accounts.filter(item => filter === 'all' || (filter === 'finished' ? ['passed', 'failed', 'terminated'].includes(item.status) : item.status === filter));
  function openCreate(nextPreset?: string) {
    if (nextPreset) setPresetId(nextPreset);
    setCreateOpen(true); setWriteError('');
    window.setTimeout(() => { createRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }); nameInput.current?.focus({ preventScroll: true }); }, 60);
  }
  async function createAccount(event: FormEvent) {
    event.preventDefault();
    if (writing.current) return;
    writing.current = true; mutationEpoch.current++; setBusy(true); setWriteError(''); setNotice('');
    try {
      const account = await request<ExamAccount>('/accounts', { method: 'POST', body: { name: name.trim(), preset_id: presetId, strategy_id: strategyId, symbol, timeframe, quantity } });
      setAccounts(old => [account, ...old]); setSelectedId(account.id); setFilter('all'); setName(''); setCreateOpen(false); setNotice('考试号已创建。检查配置后，点击「开始考试」运行策略。'); setRevision(old => old + 1);
    } catch (error) { setWriteError(errorMessage(error)); }
    finally { writing.current = false; setBusy(false); }
  }
  async function performAction(account: ExamAccount, action: ExamAction) {
    if (writing.current) return;
    writing.current = true; mutationEpoch.current++; setBusy(true); setWriteError(''); setNotice('');
    try {
      const saved = await request<ExamAccount>(`/accounts/${encodeURIComponent(account.id)}/actions`, { method: 'POST', body: { action } });
      if (saved?.id) { setAccounts(old => old.some(item => item.id === saved.id) ? old.map(item => item.id === saved.id ? saved : item) : [saved, ...old]); setSelectedId(saved.id); }
      setConfirmation(null); setNotice(saved.run_state === 'closing' ? `已请求${action === 'pause' ? '暂停' : '结束'}，等待下一份新鲜行情平仓后完成。` : action === 'restart' ? '已创建新一轮考试号，原考试记录保留。' : action === 'start' || action === 'resume' ? '考试已启动，策略将跟随后续行情自动模拟交易。' : action === 'pause' ? '考试已暂停；恢复时不会补做暂停期间的交易。' : '本轮考试已结束，记录已保留。'); setRevision(old => old + 1);
    } catch (error) { setWriteError(errorMessage(error)); }
    finally { writing.current = false; setBusy(false); }
  }
  function onAction(account: ExamAccount, action: ExamAction) { if (action === 'terminate' || action === 'restart') { setWriteError(''); setConfirmation({ account, action }); } else void performAction(account, action); }
  function savedPreset(next: ExamPreset) { setPresets(old => [...old, next]); setPresetId(next.id); setEditingPreset(null); setNotice(`已保存「${next.name}」，可用于创建新考试号。`); }

  return <main className="exams-page" hidden={!active} aria-label="模拟考试">
    <header className="ex-page-heading"><div><span className="ex-eyebrow">STRATEGY PROVING GROUND</span><h1>模拟考试<span className="ex-heading-tag">本地策略考场</span></h1><p>让策略先走完一场考试，再决定下一步。</p></div><div className="ex-header-actions"><button className="ex-button ex-quiet" aria-label="刷新考试数据" disabled={busy} onClick={() => setRevision(old => old + 1)}><RefreshCw size={15}/><span>刷新</span></button><button className="ex-button ex-primary" onClick={() => openCreate()} disabled={!presets.length || busy}><Plus size={16}/>创建考试号</button></div></header>
    {(loadError || metadataError) && <div className="ex-error ex-page-error" role="alert"><CircleHelp size={16}/><span>{loadError || metadataError}</span><button onClick={() => setRevision(old => old + 1)}>重试</button></div>}
    {notice && <div className="ex-success" role="status"><CheckCircle2 size={16}/><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice('')}><X size={15}/></button></div>}
    {writeError && !createOpen && !confirmation && <div className="ex-error ex-page-error" role="alert">{writeError}</div>}
    <section className="ex-overview" aria-label="考试总览"><article><span><Layers3 size={15}/>考试账户</span><strong>{loading ? '—' : accounts.length}<small>个</small></strong><p>每轮规则、策略独立记录</p></article><article><span><span className="ex-overview-dot"/>正在考试</span><strong>{loading ? '—' : totals.running}<small>个</small></strong><p>跟随后续行情自动运行</p></article><article><span><Trophy size={15}/>通过考试</span><strong>{loading ? '—' : totals.passed}<small>个</small></strong><p>{totals.completed ? `已完成 ${totals.completed} 轮 · 通过率 ${Math.round(totals.passed / totals.completed * 100)}%` : '等待第一轮考试成绩'}</p></article><article><span><ArrowUpRight size={16}/>累计已实现盈亏</span><strong className={signClass(totals.pnl)}>{loading ? '—' : signed(totals.pnl)}</strong><p>全部账户 · {totals.trades} 笔成交</p></article></section>
    <section className="ex-packages-section" aria-label="选择账户规则套餐"><div className="ex-section-heading"><div><h2>选择你的考试规格</h2><p>选一个规则套餐，把策略放到真实行情中检验。</p></div><span className="ex-section-kicker"><ShieldCheck size={14}/>规则在开考时固定</span></div>
      <div className="ex-packages">{presets.map(item => <article key={item.id} className={`ex-package ${item.id === presetId ? 'selected' : ''}`}><button className="ex-package-select" aria-pressed={item.id === presetId} onClick={() => { setPresetId(item.id); if (quantity > item.max_contracts) setQuantity(item.max_contracts); }}><div className="ex-package-title"><span>{item.initial_balance >= 1000 ? `${num(item.initial_balance / 1000)}K` : usd(item.initial_balance)}</span><span className="ex-package-check">{item.id === presetId ? <Check size={13}/> : null}</span></div><h3>{item.name}</h3><p>{item.drawdown_mode === 'eod_trailing' ? '日终追踪亏损线' : '固定亏损线'}<span>模拟评估账户</span></p><dl><div><dt>盈利目标</dt><dd>{usd(item.profit_target)}</dd></div><div><dt>最大亏损额度</dt><dd>{usd(item.max_drawdown)}</dd></div><div><dt>最大仓位</dt><dd>{item.max_contracts} 手</dd></div><div><dt>最少交易日</dt><dd>{item.min_trading_days} 天</dd></div></dl></button><div className="ex-package-footer"><span>{item.daily_loss_limit ? `每日亏损 ≤ ${usd(item.daily_loss_limit)}` : '不设每日亏损限额'}</span><button aria-label={`调整 ${item.name} 规则`} onClick={() => setEditingPreset(item)}><Settings2 size={13}/>调整规则</button></div></article>)}</div>
      {!presets.length && <div className="ex-package-placeholder">{metadataLoading ? <><LoaderCircle className="ex-spin" size={18}/>正在读取规则套餐…</> : <><ShieldCheck size={20}/>暂时没有可用套餐，请连接本地服务后重试。</>}</div>}
    </section>
    <section className={`ex-create ex-panel ${createOpen ? 'is-open' : ''}`} ref={createRef} aria-label="创建考试号"><button className="ex-create-toggle" aria-expanded={createOpen} aria-controls="ex-create-form" onClick={() => setCreateOpen(old => !old)}><span className="ex-create-icon"><Plus size={18}/></span><span><strong>创建一个模拟考试号</strong><small>{preset ? `${preset.name} · 盈利目标 ${usd(preset.profit_target)} · 最大亏损 ${usd(preset.max_drawdown)}` : '选择规则套餐，绑定策略，即可开始'}</small></span><ChevronDown size={18}/></button>
      {createOpen && <form id="ex-create-form" className="ex-create-form" onSubmit={createAccount}><div className="ex-create-grid"><label className="ex-field"><span>考试号名称</span><input ref={nameInput} required placeholder="给本轮考试起个名字" maxLength={80} value={name} onChange={e => setName(e.target.value)}/></label><label className="ex-field"><span>规则套餐</span><select required value={presetId} onChange={e => { setPresetId(e.target.value); const next = presets.find(item => item.id === e.target.value); if (next && quantity > next.max_contracts) setQuantity(next.max_contracts); }}><option value="" disabled>选择套餐</option>{presets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label className="ex-field"><span>策略</span><select required value={strategyId} onChange={e => setStrategyId(e.target.value)}><option value="" disabled>选择策略</option>{strategies.map(item => <option key={item.id} value={item.id}>{item.name} · v{item.version}</option>)}</select></label><label className="ex-field ex-contract-field"><span>期货合约</span><select required value={symbol} onChange={e => setSymbol(e.target.value)}><option value="" disabled>{instruments.length ? '选择具体合约' : '暂无已添加的可用合约'}</option>{instruments.map(item => <option key={item.symbol} value={item.symbol}>{item.display_symbol ?? item.symbol}{item.expiry ? ` · ${item.expiry}` : ''}{item.full_name || item.name ? ` · ${item.full_name ?? item.name}` : ''}</option>)}</select></label><label className="ex-field"><span>信号周期</span><select value={timeframe} onChange={e => setTimeframe(e.target.value)}>{timeframes.map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select></label><label className="ex-field"><span>每次交易手数 <small>最多 {preset?.max_contracts ?? '—'} 手</small></span><input required type="number" min="1" step="1" max={preset?.max_contracts ?? 1} value={quantity} onChange={e => setQuantity(Number(e.target.value))}/></label></div>
        {!instruments.length && <p className="ex-inline-note"><CircleHelp size={14}/>先在市场添加一个未到期的美元期货合约。<a href="#/markets">去添加合约<ArrowRight size={13}/></a></p>}
        <div className="ex-strategy-note"><CircleHelp size={15}/><span>{strategies.find(item => item.id === strategyId)?.description || '选择策略后，系统将跟随后续行情执行模拟。'}<small>内置策略用于验证流程，未经回测验证，不代表已筛选出的有效策略。{instrument?.data_mode === 'delayed' ? ' 当前合约使用延迟行情。' : ''}</small></span></div>
        {writeError && <p className="ex-error" role="alert">{writeError}</p>}<div className="ex-create-submit"><span>创建后处于待开考状态，可在详情中启动。</span><button type="submit" className="ex-button ex-primary" disabled={busy || !presetId || !strategyId || !symbol}>{busy ? <LoaderCircle size={15} className="ex-spin"/> : <Plus size={15}/>}创建考试号</button></div>
      </form>}
    </section>
    <div className="ex-workspace"><section className="ex-accounts ex-panel" aria-label="考试账号列表"><div className="ex-accounts-heading"><h2>我的考试号 <span>{accounts.length}</span></h2><select aria-label="筛选考试状态" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部状态</option><option value="running">考试中</option><option value="ready">待开考</option><option value="paused">已暂停</option><option value="finished">已完成</option></select></div>
      {loading ? <div className="ex-accounts-empty"><LoaderCircle size={24} className="ex-spin"/><strong>正在读取考试号</strong></div> : !rows.length ? <div className="ex-accounts-empty"><span className="ex-empty-icon"><Flag size={25}/></span><strong>{accounts.length ? '这个状态下没有考试号' : '策略的下一场考试，从这里开始'}</strong><p>{accounts.length ? '切换筛选，查看其他考试账号。' : '创建独立账号，记录每一笔交易，也记录策略能否走到终点。'}</p>{!accounts.length && <button className="ex-text-button" onClick={() => openCreate()} disabled={!presets.length}>创建第一个考试号<ArrowRight size={14}/></button>}</div> : <div className="ex-account-list">{rows.map(account => <button className={`ex-account-card ${selectedId === account.id ? 'selected' : ''}`} key={account.id} aria-pressed={selectedId === account.id} onClick={() => setSelectedId(account.id)}><div className="ex-account-card-top"><strong>{account.name}</strong><Status status={account.status}/></div><p>{account.instrument?.display_symbol ?? account.display_symbol ?? instruments.find(item => item.symbol === account.symbol)?.display_symbol ?? account.symbol}<i/>{account.strategy.name}</p><div className="ex-account-card-values"><div><span>净盈亏</span><b className={signClass(account.net_pnl)}>{signed(account.net_pnl)}</b></div><div><span>目标进度</span><b>{Math.round(percent((account.balance - account.preset.initial_balance) / account.preset.profit_target * 100))}%</b></div><ChevronRight size={16}/></div><div className="ex-account-progress"><i style={{ width: `${percent((account.balance - account.preset.initial_balance) / account.preset.profit_target * 100)}%` }}/></div><div className="ex-account-card-bottom"><span>{account.preset.name}</span><span>{account.trade_count} 笔 · {account.trading_days} 天</span></div></button>)}</div>}
    </section>
    {selected ? <AccountDetail key={selected.id} detail={detail?.account.id === selectedId ? detail : null} account={selected} loading={detailLoading} busy={busy} error={detailError} onAction={onAction}/> : <section className="ex-detail ex-panel ex-detail-empty"><div className="ex-detail-empty-visual"><div/><Target size={34}/><span><ArrowUpRight size={18}/></span></div><span className="ex-eyebrow">ONE STRATEGY. ONE ACCOUNT.</span><h2>把每一次考验，变成可以复盘的记录。</h2><p>选中一个考试号，查看净值、持仓、交易和规则检查。</p><div className="ex-detail-empty-steps"><span><b>01</b>选择规则</span><ChevronRight size={14}/><span><b>02</b>绑定策略</span><ChevronRight size={14}/><span><b>03</b>观察成绩</span></div></section>}
    </div>
    <aside className="ex-method-note"><ShieldCheck size={16}/><p><strong>本地模拟 · 独立账本</strong><span>按 K 线模拟成交并计入手续费与滑点，延迟行情会标注。后台服务运行时，关闭页面仍会继续；电脑休眠会中断行情。套餐为可配置的本地评估规则。</span></p></aside>
    {editingPreset && <PresetEditor preset={editingPreset} onClose={() => setEditingPreset(null)} onSaved={savedPreset}/>}
    {confirmation && <Dialog title={confirmation.action === 'restart' ? '开始新一轮考试' : '结束本轮考试'} busy={busy} onClose={() => { setConfirmation(null); setWriteError(''); }}><div className="ex-confirm-content"><p>{confirmation.action === 'restart' ? `为「${confirmation.account.name}」复制相同规则和策略，创建新的考试账号。当前账号和历史交易记录都会保留。` : `结束「${confirmation.account.name}」的本轮考试。系统将处理当前持仓并停止后续策略执行，历史记录保留。`}</p>{writeError && <p className="ex-error" role="alert">{writeError}</p>}<div className="ex-dialog-actions"><button className="ex-button" disabled={busy} onClick={() => setConfirmation(null)}>取消</button><button className={`ex-button ${confirmation.action === 'terminate' ? 'ex-danger' : 'ex-primary'}`} disabled={busy} onClick={() => void performAction(confirmation.account, confirmation.action)}>{busy && <LoaderCircle size={15} className="ex-spin"/>}{confirmation.action === 'restart' ? '创建新一轮' : '结束本轮'}</button></div></div></Dialog>}
  </main>;
}
