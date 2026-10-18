import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, RefreshCw, Star } from 'lucide-react';
import { clock, price as fmtPrice } from './types';

type Bucket = { price: number; long: number | null; short: number | null };

interface Position {
  address: string; coin: string; cohort_label?: string | null;
  size: number; leverage: number; liq_price: number | null; entry_px: number | null;
  unrealized_pnl: number | null; account_value: number | null; total_pnl: number | null;
  tracked?: boolean;
}

interface FillEvent {
  id: number; address: string; coin: string; ts: number;
  side: string | null; direction: string | null; event: string;
  size: number | null; price: number | null; closed_pnl: number | null;
  watch_label?: string | null;
}

interface Coverage {
  status: 'unverified' | 'sampled_pass' | 'sampled_fail' | string;
  samples?: number; within_threshold?: number; max_abs_pct?: number | null;
  checked_at?: number | null; note?: string | null; scope?: string;
}

interface Status {
  configured: boolean; mode: 'normal' | 'degraded' | 'paused' | 'unconfigured' | string;
  reasons: string[]; snapshot_age_seconds: number | null;
  quota: { used: number; limit: number; hard_cap: number; warning: boolean; exhausted: boolean };
  storage: { used_bytes: number; cap_bytes: number; used_ratio: number };
  watch_count: number; last_error: { message?: string } | null;
  levels_interval_seconds: number;
}

interface Overview {
  symbol: string; available: boolean; reason: string | null;
  snapshot_time: string | null; fetched_at: string | null;
  levels: Bucket[]; coverage: Coverage; positions: Position[];
  positions_fetched_at: string | null; events: FillEvent[];
  watchlist: string[]; status: Status;
}

const EVENT_LABEL: Record<string, string> = {
  open: '开仓', add: '加仓', reduce: '减仓', close: '平仓', flip: '反手',
};
const REASON_LABEL: Record<string, string> = {
  no_key: '未配置 LiqFlow key',
  unauthorized: 'LiqFlow key 失效（401），采集已暂停',
  rate_limited: 'LiqFlow 限流或额度耗尽（429），采集已暂停',
  upstream_error: '上游连续失败，已降级为 15 分钟低频',
  quota_warning: '本月额度已用 80%',
  quota_exhausted: '本地额度硬顶触发，采集已停止',
};

function ageText(iso: string | null, now: number): string {
  if (!iso) return '—';
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
  return `${Math.floor(seconds / 86400)} 天`;
}

function compactNum(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(2)}K`;
  return `${sign}${abs.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

function shortAddress(address: string): string {
  return address.length > 12 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}

function sideOf(position: Position): 'long' | 'short' {
  return position.size < 0 ? 'short' : 'long';
}

function eventLabel(event: FillEvent): string {
  const base = EVENT_LABEL[event.event] ?? event.event;
  if (event.event === 'flip') return `${base}${event.direction === 'short' ? '为多' : '为空'}`;
  return `${base}${event.direction === 'long' ? '多' : '空'}`;
}

function binLevels(levels: Bucket[], spot: number | null, widthPct = 0.25, count = 44) {
  if (!spot || !levels.length) return [];
  const low = spot * (1 - widthPct);
  const high = spot * (1 + widthPct);
  const width = (high - low) / count;
  const bins = Array.from({ length: count }, (_, index) => ({
    low: low + index * width, high: low + (index + 1) * width,
    long: 0, short: 0, rows: 0,
  }));
  for (const level of levels) {
    if (level.price < low || level.price > high) continue;
    const index = Math.min(count - 1, Math.max(0, Math.floor((level.price - low) / width)));
    const bin = bins[index];
    if (level.long != null) bin.long += level.long;
    if (level.short != null) bin.short += level.short;
    bin.rows += 1;
  }
  return bins.filter(bin => bin.rows > 0).reverse();
}

function coverageLabel(coverage: Coverage): string {
  return coverage.status === 'sampled_pass' ? '抽样通过'
    : coverage.status === 'sampled_fail' ? '抽样失败' : '未验证';
}

function CoverageBadge({ coverage }: { coverage: Coverage }) {
  const label = coverageLabel(coverage);
  const detail = coverage.status === 'unverified'
    ? '尚未用官方 liquidationPx 抽样核对'
    : `${coverage.samples ?? 0} 个仓位 · 阈值内 ${coverage.within_threshold ?? 0} 个`
      + (coverage.max_abs_pct != null ? ` · 最大偏差 ${coverage.max_abs_pct.toFixed(3)}%` : '')
      + (coverage.scope === 'global_top25' ? '（基于 top25 持仓抽样）' : '');
  return (
    <span className={`intel-coverage ${coverage.status}`} title={detail}>
      {label}
    </span>
  );
}

export function IntelPanel({ symbol, spot }: { symbol: string; spot: number | null }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [busyAddress, setBusyAddress] = useState('');
  const [notice, setNotice] = useState('');
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    let alive = true;
    async function load(showSpinner = false) {
      if (showSpinner) setLoading(true);
      try {
        const response = await fetch(`/api/v1/intel/overview?symbol=${encodeURIComponent(symbol)}`,
          { signal: abort.signal });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${response.status}`);
        }
        const payload = await response.json() as Overview;
        if (alive) { setData(payload); setError(''); }
      } catch (reason) {
        if (reason instanceof Error && reason.name === 'AbortError') return;
        if (alive) setError(reason instanceof Error ? reason.message : '情报服务暂不可用');
      } finally {
        if (alive) setLoading(false);
      }
    }
    setData(null);
    void load(true);
    const timer = window.setInterval(() => void load(false), 20000);
    return () => { alive = false; abort.abort(); window.clearInterval(timer); };
  }, [symbol]);

  const bins = useMemo(() => binLevels(data?.levels ?? [], spot), [data?.levels, spot]);
  const maxFlow = useMemo(
    () => Math.max(1, ...bins.flatMap(bin => [bin.long, bin.short])), [bins]);
  const barsRef = useRef<HTMLDivElement>(null);
  const spotRef = useRef<HTMLDivElement>(null);

  // Centre the spot row once per symbol/snapshot; quote ticks must not steal the scroll.
  useEffect(() => {
    const container = barsRef.current;
    const row = spotRef.current;
    if (!container || !row) return;
    const delta = row.getBoundingClientRect().top - container.getBoundingClientRect().top;
    const target = container.scrollTop + delta - container.clientHeight / 2 + row.clientHeight / 2;
    container.scrollTop = Math.max(0, target);
  }, [symbol, data?.snapshot_time, spot != null]);

  async function toggleTrack(position: Position) {
    if (busyAddress) return;
    setBusyAddress(position.address);
    setNotice('');
    try {
      const response = await fetch(
        position.tracked ? `/api/v1/intel/watchlist/${position.address}` : '/api/v1/intel/watchlist',
        {
          method: position.tracked ? 'DELETE' : 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Local-Market': '1' },
          body: position.tracked ? undefined : JSON.stringify({ address: position.address, label: position.cohort_label }),
        });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      setNotice(position.tracked ? '已取消跟踪' : '已加入大户跟踪');
      setData(current => current ? {
        ...current,
        positions: current.positions.map(item => item.address === position.address
          ? { ...item, tracked: !position.tracked } : item),
        status: { ...current.status, watch_count: body.count ?? current.status.watch_count },
      } : current);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : '操作失败');
    } finally {
      setBusyAddress('');
    }
  }

  async function manualRefresh() {
    if (loading) return;
    setLoading(true);
    setNotice('');
    try {
      const response = await fetch('/api/v1/intel/refresh',
        { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Market': '1' } });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      setNotice('已请求刷新，采集完成后自动更新');
      window.setTimeout(() => { void fetch(`/api/v1/intel/overview?symbol=${encodeURIComponent(symbol)}`)
        .then(r => r.ok ? r.json() : null)
        .then(payload => { if (payload) setData(payload as Overview); }); }, 5000);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : '刷新失败');
    } finally {
      setLoading(false);
    }
  }

  const status = data?.status;
  const mode = status?.mode ?? 'unconfigured';
  const reasons = (status?.reasons ?? []).map(reason => REASON_LABEL[reason] ?? reason);
  const degraded = mode === 'degraded' || mode === 'paused';
  const quota = status?.quota;
  const quotaWarning = quota && quota.used / Math.max(1, quota.limit) >= 0.8;

  return (
    <div className="intel-panel">
      <div className="intel-head">
        <div className="intel-title">
          <strong>清算与大户</strong>
          <span className="intel-source">LiqFlow</span>
          <span className="intel-source official">官方 HL</span>
        </div>
        <div className="intel-meta">
          {data?.snapshot_time && (
            <span title="LiqFlow 上游约 10 分钟刷新一次；本地每 5 分钟拉取一次。">快照 {clock(new Date(data.snapshot_time).getTime())} · {ageText(data.snapshot_time, now)}前</span>
          )}
          {data && <CoverageBadge coverage={data.coverage} />}
          <button className="intel-refresh" onClick={() => void manualRefresh()} disabled={loading}
            title="立即刷新（消耗一次额度）">
            <RefreshCw size={12} className={loading ? 'spin' : ''} />刷新
          </button>
        </div>
      </div>

      {data && (degraded || quotaWarning || !status?.configured) && (
        <div className={`intel-banner ${mode === 'paused' ? 'danger' : ''}`}>
          <AlertTriangle size={13} />
          <span>
            {!status?.configured ? 'LiqFlow key 未配置，面板仅展示历史缓存。'
              : mode === 'paused' ? `采集已暂停：${reasons.join('；')}`
                : mode === 'degraded' ? `采集降级中：${reasons.join('；') || '上游异常'}`
                  : `额度提醒：${quota?.used}/${quota?.limit}`}
          </span>
          {quota && <em>本月已用 {quota.used.toLocaleString()} / {quota.limit.toLocaleString()}</em>}
        </div>
      )}

      {error && <div className="intel-error" role="status">情报接口暂时不可用：{error}（保留最近一次成功数据）</div>}
      {notice && <div className="intel-notice">{notice}</div>}

      {!data && !error && <div className="intel-empty">正在读取市场情报…</div>}

      {data && !data.available && (
        <div className="intel-unavailable">
          <strong>{data.reason || '暂无可用的清算数据'}</strong>
          <span>数据缺失时不做聚合、不补零。切回 Hyperliquid 永续标的可查看清算分布。</span>
        </div>
      )}

      {data?.available && (
        <div className="intel-grid">
          <section className="intel-card intel-liquidation" aria-label="清算分布">
            <header>
              <h3>清算分布 · {data.symbol}</h3>
              <span>
                LiqFlow · 快照 {data.snapshot_time ? clock(new Date(data.snapshot_time).getTime()) : '—'}
                {' · '}{coverageLabel(data.coverage)}
              </span>
            </header>
            <div className="intel-bars" ref={barsRef}>
              <div className="intel-bar-head"><span>多单清算</span><b>价格</b><span>空单清算</span></div>
              {bins.map(bin => {
                const isSpot = spot != null && spot >= bin.low && spot < bin.high;
                return (
                <div className={`intel-bar-row ${isSpot ? 'spot' : ''}`}
                  key={`${bin.low}-${bin.high}`} ref={isSpot ? spotRef : undefined}>
                  <div className="intel-side long">
                    {bin.long > 0 && (
                      <i style={{ width: `${Math.min(100, bin.long / maxFlow * 100)}%` }}
                        title={`多单 ${compactNum(bin.long)}`} />
                    )}
                  </div>
                  <b>{isSpot ? '现价 ' : ''}{fmtPrice((bin.low + bin.high) / 2)}</b>
                  <div className="intel-side short">
                    {bin.short > 0 && (
                      <i style={{ width: `${Math.min(100, bin.short / maxFlow * 100)}%` }}
                        title={`空单 ${compactNum(bin.short)}`} />
                    )}
                  </div>
                </div>
              );
              })}
            </div>
            <footer>±25% 价格带 · 现价 {fmtPrice(spot)} · 仅显示有数据的桶；空桶不补零。桶宽随价格比例变化。</footer>
          </section>

          <section className="intel-card intel-positions" aria-label="持仓榜">
            <header>
              <h3>持仓榜 top {data.positions.length}</h3>
              <span>LiqFlow · {data.positions_fetched_at ? clock(new Date(data.positions_fetched_at).getTime()) : '—'} 抓取</span>
            </header>
            <div className="intel-table">
              <div className="intel-table-head">
                <span>地址</span><span>币</span><span>方向</span><span>杠杆</span>
                <span>清算价</span><span>浮盈</span>
              </div>
              {data.positions.map(position => (
                <button className={`intel-table-row ${position.tracked ? 'tracked' : ''}`}
                  key={`${position.address}-${position.coin}`}
                  onClick={() => void toggleTrack(position)}
                  disabled={busyAddress === position.address}
                  title={`${position.address} · ${position.cohort_label ?? '未知分组'}`}>
                  <span className="mono">{shortAddress(position.address)}</span>
                  <span>{position.coin}</span>
                  <span className={sideOf(position) === 'long' ? 'positive' : 'negative'}>
                    {sideOf(position) === 'long' ? '多' : '空'}
                  </span>
                  <span>{position.leverage ? `${position.leverage}x` : '—'}</span>
                  <span>{fmtPrice(position.liq_price)}</span>
                  <span className={(position.unrealized_pnl ?? 0) >= 0 ? 'positive' : 'negative'}>
                    {compactNum(position.unrealized_pnl)}
                  </span>
                  <Star size={11} className="intel-star" fill={position.tracked ? 'currentColor' : 'none'} />
                </button>
              ))}
            </div>
            <footer>LiqFlow top25 榜单，非全市场仓位；点击行加入跟踪（{status?.watch_count ?? 0}/50）。</footer>
          </section>

          <section className="intel-card intel-events" aria-label="大户动向">
            <header>
              <h3>大户动向</h3>
              <span>
                官方 HL · 最近 {data.events[0] ? clock(data.events[0].ts) : '—'}
                {' · '}仅跟踪地址
              </span>
            </header>
            {data.events.length === 0
              ? <div className="intel-empty small">暂无事件。跟踪地址后，新成交会在这里推导为开/加/减/平。</div>
              : (
                <div className="intel-event-list">
                  {data.events.map(event => (
                    <div className="intel-event" key={event.id}>
                      <time>{clock(event.ts)}</time>
                      <span className="mono">{shortAddress(event.address)}</span>
                      <b>{event.coin}</b>
                      <em className={`event ${event.event}`}>{eventLabel(event)}</em>
                      <span>{compactNum(event.size)} @ {fmtPrice(event.price)}</span>
                      {event.closed_pnl ? (
                        <span className={event.closed_pnl >= 0 ? 'positive' : 'negative'}>
                          {event.closed_pnl >= 0 ? '+' : ''}{compactNum(event.closed_pnl)}
                        </span>
                      ) : <span />}
                    </div>
                  ))}
                </div>
              )}
          </section>
        </div>
      )}

      <div className="intel-foot">
        <span>
          数据：LiqFlow（快照 {data?.snapshot_time ? ageText(data.snapshot_time, now) : '—'}前）
          · 官方 HL（持仓核对/事件）
          {quota ? ` · 额度 ${quota.used.toLocaleString()}/${quota.limit.toLocaleString()}` : ''}
          {data?.coverage.scope === 'global_top25' ? ' · 覆盖率为 top25 抽样口径' : ''}
        </span>
      </div>
    </div>
  );
}
