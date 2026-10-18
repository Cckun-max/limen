import { useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import './options.css';

type OptionRow = {
  id: number; name: string; expiry: string; strike: number; right: 'C' | 'P';
  midpoint: number | null; iv: number | null; delta: number | null; gamma: number | null;
  vega: number | null; theta: number | null; volume: number | null;
};
type Sample = {
  mode: 'historical_sample'; target: string; underlying: string; interval_minutes: number;
  collected_at: string; bar_start: string; bar_end: string; available_times: string[];
  interest_rate: number; underlying_price: number | null; expiries: string[];
  expected_contracts: number; priced_contracts: number; trade_contracts: number;
  rows: OptionRow[]; skew: OptionRow[]; term: OptionRow[];
  vx: { name: string; expiry: string; price: number | null }[];
};
type Point = { x: number; y: number | null; label: string };
const fmt = (v: number | null | undefined, digits = 2) => v == null ? '—' : v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const percent = (v: number | null | undefined) => v == null ? '—' : `${fmt(v * 100)}%`;
const expiryText = (d: string) => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
const expiryAxis = (d: string) => `${d.slice(4, 6)}/${d.slice(6, 8)}`;
const expiryTime = (d: string) => Date.parse(`${expiryText(d)}T00:00:00Z`);
const chinaTime = (d: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
}).format(new Date(d));

function Curve({ points, label, unit = '%', color = '#b4a0ff' }: { points: Point[]; label: string; unit?: string; color?: string }) {
  const values = points.flatMap(p => p.y == null ? [] : [p.y]);
  if (!values.length) return <p className="options-empty">所选时间缺少数据</p>;
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max - min) * .38, .25);
  const low = min - pad, high = max + pad;
  const xMin = Math.min(...points.map(p => p.x)), xMax = Math.max(...points.map(p => p.x));
  const x = (p: Point) => 44 + (p.x - xMin) / (xMax - xMin || 1) * 300;
  const y = (v: number) => 112 - (v - low) / (high - low) * 86;
  let interrupted = true;
  const path = points.map(p => {
    if (p.y == null) { interrupted = true; return ''; }
    const command = interrupted ? 'M' : 'L'; interrupted = false;
    return `${command} ${x(p)} ${y(p.y)}`;
  }).join(' ');
  return <svg className="options-curve" viewBox="0 0 390 145" role="img" aria-label={label}>
    {[low, (low + high) / 2, high].map(v => <g key={v}><line x1="40" x2="354" y1={y(v)} y2={y(v)} stroke="#2b2b32" strokeDasharray="3 4"/><text x="33" y={y(v) + 4} textAnchor="end" fill="#777782" fontSize="10">{v.toFixed(1)}</text></g>)}
    <path d={path} fill="none" stroke={color} strokeWidth="2"/>
    {points.map(p => <g key={p.x}>
      <text x={x(p)} y="136" textAnchor="middle" fill="#9999a6" fontSize="11">{p.label}</text>
      {p.y != null && <><circle cx={x(p)} cy={y(p.y)} r="4" fill={color}><title>{p.label}：{fmt(p.y)}{unit}</title></circle><text x={x(p)} y={y(p.y) - 10} textAnchor="middle" fill={color} fontSize="11">{fmt(p.y)}{unit}</text></>}
    </g>)}
  </svg>;
}

export function OptionsPage() {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [selectedTime, setSelectedTime] = useState('');
  const data = samples.find(sample => sample.bar_start === selectedTime) || samples.at(-1) || null;
  const [expiry, setExpiry] = useState('');
  const [contractId, setContractId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    fetch('/assets/options/es-sample.json', { signal: controller.signal, cache: 'no-store' })
      .then(async response => {
        const value = await response.json();
        if (!response.ok) throw new Error(value.detail || '期权样本暂不可读');
        if (!Array.isArray(value.samples) || !value.samples.length) throw new Error('本地期权样本为空');
        return value.samples as Sample[];
      }).then(value => { if (!controller.signal.aborted) setSamples(value); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message || '本地服务暂未连接'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refreshKey]);
  const activeExpiry = data?.expiries.includes(expiry) ? expiry : data?.expiries[0] || '';
  const rows = data?.rows.filter(row => row.expiry === activeExpiry) || [];
  const selected = rows.find(row => row.id === contractId) || rows.find(row => row.strike === 7700 && row.right === 'P') || rows[0];
  return <main className="options-page" aria-label="ES 期权样本看板">
    <div className="options-heading"><div><span className="options-eyebrow">ES FUTURES OPTIONS</span><h1>ES 期权 <span>历史试验</span></h1></div><a href="#/trade" className="options-back"><ArrowLeft size={15}/>返回期货图表</a></div>
    <div className="options-notice" role="status"><strong>30 分钟历史样本 · 盘中自动采集尚未启用</strong><span>展示已取到的真实历史数据与本地试算。当前买卖报价、OI、全链成交量与 Put/Call 比尚未取得。</span></div>
    {error && <div className="options-error" role="alert">{error}<button onClick={() => setRefreshKey(v => v + 1)}>重试读取</button>{data && <span>下面仍显示上一次成功读取的历史样本。</span>}</div>}
    {!data ? <div className="options-loading">{loading ? '正在读取本地期权试验结果…' : '尚无可展示的期权样本。'}</div> : <>
      <div className="options-controls">
        <label>样本时段（北京时间）<select aria-label="期权样本时段" disabled={loading} value={data.bar_start} onChange={e => setSelectedTime(e.target.value)}>{[...data.available_times].reverse().map(t => <option value={t} key={t}>{chinaTime(t)}–{chinaTime(new Date(Date.parse(t) + 1800000).toISOString()).slice(-5)}</option>)}</select></label>
        <label>ES 到期日<select aria-label="ES 期权到期日" value={activeExpiry} onChange={e => { setExpiry(e.target.value); setContractId(null); }}>{data.expiries.map(d => <option key={d} value={d}>{expiryText(d)}</option>)}</select></label>
        <div className="options-coverage"><strong>{data.priced_contracts}/{data.expected_contracts}</strong><span>合约有 IV / Greeks · 三个执行价</span></div>
        <button className="options-refresh" disabled={loading} onClick={() => setRefreshKey(v => v + 1)} title="重新读取本地试验文件，不请求行情商"><RefreshCw size={14}/>{loading ? '读取中…' : '重读本地结果'}</button>
      </div>
      <div className="options-grid" aria-busy={loading}>
        <section className="options-card"><header><h2><span>01</span>价格、IV 与 Greeks</h2><b className="options-tag">历史试算</b></header>
          <select className="options-contract" aria-label="期权样本合约" value={selected?.id || ''} onChange={e => setContractId(Number(e.target.value))}>{rows.map(row => <option key={row.id} value={row.id}>{row.strike} · {row.right === 'C' ? '看涨 Call' : '看跌 Put'}</option>)}</select>
          <div className="options-primary"><strong>{percent(selected?.iv)}</strong><span>IV · 中间价收盘 {fmt(selected?.midpoint)} 点</span></div>
          <dl className="options-greeks"><div><dt>Delta</dt><dd>{fmt(selected?.delta, 4)}</dd></div><div><dt>Gamma</dt><dd>{fmt(selected?.gamma, 6)}</dd></div><div><dt>Vega / 1%</dt><dd>{fmt(selected?.vega, 4)}</dd></div><div><dt>Theta / 日</dt><dd>{fmt(selected?.theta, 4)}</dd></div></dl>
          <p>当前买卖报价未取得；缺失值以「—」显示。</p>
        </section>
        <section className="options-card"><header><h2><span>02</span>波动率偏斜 Skew</h2><b className="options-tag">三个点</b></header>
          <Curve label={`${expiryText(activeExpiry)} 虚值期权 IV 偏斜样本`} points={data.skew.filter(r => r.expiry === activeExpiry).map(r => ({ x: r.strike, y: r.iv == null ? null : r.iv * 100, label: `${r.strike}` }))}/>
          <p>{expiryText(activeExpiry)} · 取虚值 Put / Call；横轴为执行价。</p>
        </section>
        <section className="options-card"><header><h2><span>03</span>IV 期限结构</h2><b className="options-tag">三个到期日</b></header>
          <Curve label="固定执行价 7700 的 IV 期限结构样本" points={data.term.map(r => ({ x: expiryTime(r.expiry), y: r.iv == null ? null : r.iv * 100, label: expiryAxis(r.expiry) }))} color="#6ed6be"/>
          <p>固定执行价 7700；横轴为到期日，未做恒定 Delta 插值。</p>
        </section>
        <section className="options-card"><header><h2><span>04</span>VIX 期货期限结构</h2><b className="options-tag">VX 历史成交</b></header>
          <Curve label="独立 VX 期货三个到期日的历史成交收盘价" points={data.vx.map(r => ({ x: expiryTime(r.expiry), y: r.price, label: expiryAxis(r.expiry) }))} unit="" color="#e3b776"/>
          <p>独立的 VX 期货数据 · 与所选 ES 样本时段对齐。</p>
        </section>
        <section className="options-card options-unavailable"><header><h2><span>05</span>OI 分布、持仓 Put/Call</h2><b className="options-tag missing">未取得</b></header>
          <div className="options-primary"><strong>—</strong><span>本次样本没有 OI</span></div><p className="options-explanation">CME 每日日报的导入尚未接入。需要有效的 OI 日期与完整合约范围，才能计算分布和持仓比。</p><p>这是日级持仓数据，半小时刷新不会使它变为实时 OI。</p>
        </section>
        <section className="options-card"><header><h2><span>06</span>成交量、成交量 Put/Call</h2><b className="options-tag missing">仅单合约</b></header>
          <div className="options-primary"><strong>{fmt(selected?.volume, 0)} <small>手</small></strong><span>{selected?.name} · 所选半小时成交量</span></div><p className="options-explanation">该时段 {data.trade_contracts}/{data.expected_contracts} 个样本合约有成交量。全 ES 当日累计量和 Put/Call 比尚未取得。</p><p>跟随 01 的合约选择；未返回的成交柱不补为 0。</p>
        </section>
      </div>
      <details className="options-details"><summary>查看合约明细 <span>{rows.length} 个合约 · {expiryText(activeExpiry)}</span></summary><div className="options-table-wrap"><table><caption>所选时段的历史中间价与本地模型试算；「—」表示缺失。</caption><thead><tr>{['合约', '历史中间价', 'IV', 'Delta', 'Gamma', 'Vega / 1%', 'Theta / 日', '半小时量'].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.id}><th scope="row">{row.name}</th><td>{fmt(row.midpoint)}</td><td>{percent(row.iv)}</td><td>{fmt(row.delta, 4)}</td><td>{fmt(row.gamma, 6)}</td><td>{fmt(row.vega, 4)}</td><td>{fmt(row.theta, 4)}</td><td>{fmt(row.volume, 0)}</td></tr>)}</tbody></table></div></details>
      <div className="options-method"><strong>试算口径</strong><p>IBKR 历史 30 分钟柱 · Black-76 · 年化利率暂设 {percent(data.interest_rate)}，未接入实际利率。Greeks 按期权价格点计，未乘 50 的合约乘数。样本对应 {data.underlying}，期货中间价收盘 {fmt(data.underlying_price)} 点。同一柱时段不保证最后报价完全同步。</p><span>本次取数完成：{chinaTime(data.collected_at)}（北京时间）。重读页面只读取本地文件，不会产生新的市场数据。</span></div>
    </>}
  </main>;
}
