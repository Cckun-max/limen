import { useEffect, useRef, useState } from 'react';
import { X, PlugZap, Search } from 'lucide-react';
import { marketRequest } from './Markets';
import { money, price, clock, type Instrument, type Overview } from './types';
import './ibkr.css';

type Config = { enabled: boolean; port: number; client_id: number; delayed: boolean };
type Status = { config: Config; connected: boolean; connecting: boolean; error: string | null; instruments: Instrument[]; history_delay_minutes: number };
export const DATA_MODES: Record<string,string> = { live:'实时报价', delayed:'延迟报价', frozen:'收盘冻结报价', delayed_frozen:'延迟冻结报价', unknown:'等待报价类型', unavailable:'等待行情' };

export function IBKRDialog({ onClose, onChoose }: { onClose: () => void; onChoose: (symbol: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [config, setConfig] = useState<Config>({enabled:false,port:7497,client_id:71,delayed:true});
  const [kind,setKind] = useState('STK');
  const [query,setQuery] = useState('AAPL');
  const [results,setResults] = useState<Instrument[]>([]);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [message,setMessage] = useState('');
  useEffect(() => { const d=dialog.current; d?.showModal(); return () => d?.close(); },[]);
  useEffect(() => {
    let active=true;
    const load=async (initial=false) => {
      try { const data=await marketRequest<Status>('/ibkr/status'); if(active) { setStatus(data); if(initial) setConfig(data.config); } }
      catch(e) { if(active) setError((e as Error).message); }
    };
    void load(true); const timer=setInterval(()=>void load(),3000);
    return ()=>{ active=false; clearInterval(timer); };
  },[]);
  async function connect(enabled:boolean) {
    setBusy(true);setError('');setMessage('');
    try { const data=await marketRequest<Status>('/ibkr/config','PUT',{...config,enabled}); setStatus(data);setConfig(data.config);setMessage(enabled?'配置已保存，连接状态见上方。':'已断开盈透连接，历史缓存仍保留。'); }
    catch(e){setError((e as Error).message);} finally{setBusy(false);}
  }
  async function search(e:React.FormEvent) {
    e.preventDefault();setBusy(true);setError('');setResults([]);
    try { const items=await marketRequest<Instrument[]>(`/ibkr/search?q=${encodeURIComponent(query)}&kind=${kind}`);setResults(items);if(!items.length)setMessage('未找到可用合约，请检查证券代码。'); }
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function add(item:Instrument) {
    setBusy(true);setError('');
    try { const saved=await marketRequest<Instrument>('/ibkr/instruments','POST',{con_id:item.con_id}); onChoose(saved.symbol); }
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <dialog ref={dialog} className="ibkr-dialog" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <div className="ibkr-title"><div><span className="markets-eyebrow">MARKET CONNECTION</span><h2><PlugZap size={22}/> 盈透 IBKR</h2><p>美股、ETF、股指与微型黄金期货 · 行情只读接入</p></div><button aria-label="关闭盈透设置" onClick={onClose}><X/></button></div>
    <div className={`ibkr-status ${status?.connected?'positive':'amber'}`} role="status">● {status?.connected?'已连接官方客户端':status?.connecting?'正在连接…':status?.config.enabled?'等待 TWS / IB Gateway':'尚未启用'}<span>{status?.connected?'可以查询并添加品种':status?.error}</span></div>
    <details className="ibkr-help" open={!status?.connected}><summary>连接前的准备</summary><ol><li>在这台电脑的官方 TWS 或 IB Gateway 登录盈透并完成手机验证。</li><li>在 API 设置启用 Socket 客户端，勾选「Read-Only API / 只读 API」，仅允许本机连接。</li><li>核对实际 API 端口，再点击连接。这里无需填写盈透密码。</li></ol></details>
    <div className="ibkr-grid"><label>客户端端口<select aria-label="盈透客户端端口" value={[7496,7497,4001,4002].includes(config.port)?config.port:0} onChange={e=>setConfig({...config,port:Number(e.target.value)||7497})}><option value={7497}>TWS 模拟账户 · 7497</option><option value={7496}>TWS 实盘账户 · 7496</option><option value={4002}>IB Gateway 模拟 · 4002</option><option value={4001}>IB Gateway 实盘 · 4001</option>{![7496,7497,4001,4002].includes(config.port)&&<option value={0}>自定义端口</option>}</select></label>
      <label>实际端口<input aria-label="实际 API 端口" type="number" min={1024} max={65535} value={config.port} onChange={e=>setConfig({...config,port:Number(e.target.value)})}/></label>
      <label>连接编号<input aria-label="盈透连接编号" type="number" min={1} max={999999} value={config.client_id} onChange={e=>setConfig({...config,client_id:Number(e.target.value)})}/></label>
      <label>行情方式<select aria-label="盈透行情方式" value={config.delayed?'delayed':'live'} onChange={e=>setConfig({...config,delayed:e.target.value==='delayed'})}><option value="delayed">延迟模式 · 历史截止至少 15 分钟前</option><option value="live">实时模式 · 需要对应行情权限</option></select></label></div>
    <p className="ibkr-hint">报价按账户实际权限返回；延迟模式每分钟补取历史 K 线。美股使用常规交易时段，期货使用全部交易时段。缺少权限会显示错误，不自动订阅付费行情。</p>
    <div className="ibkr-actions"><button className="ibkr-primary" disabled={busy} onClick={()=>void connect(true)}>保存并连接</button><button disabled={busy||!status?.config.enabled} onClick={()=>void connect(false)}>断开连接</button></div>
    <div className="ibkr-divider"/><h3>添加美股 / 期货</h3>
    <form className="ibkr-search" onSubmit={search}><select aria-label="盈透品种类型" value={kind} onChange={e=>{setKind(e.target.value);setQuery(e.target.value==='FUT'?'ES':'AAPL');setResults([]);}}><option value="STK">美股 / ETF</option><option value="FUT">期货（CME / COMEX）</option></select><input aria-label="盈透证券代码" value={query} onChange={e=>setQuery(e.target.value.toUpperCase())} maxLength={20} placeholder={kind==='FUT'?'ES / NQ / MES / MNQ / RTY / M2K / MGC':'AAPL / NVDA / SPY'}/><button type="submit" disabled={busy||!status?.connected}><Search size={16}/> 查询合约</button></form>
    {error&&<p role="alert" className="negative">{error}</p>}{message&&!error&&<p role="status" className="ibkr-hint">{message}</p>}
    <div className="ibkr-results">{results.map(item=><div key={item.con_id}><div><strong>{item.display_symbol}</strong><span>{item.name} · {item.exchange} {item.expiry&&`· 到期 ${item.expiry}`}</span></div><button disabled={busy} onClick={()=>void add(item)}>添加并看盘</button></div>)}</div>
    {!!status?.instruments.length&&<><h3>已添加品种</h3><div className="ibkr-saved">{status.instruments.map(item=><button key={item.symbol} onClick={()=>onChoose(item.symbol)}>{item.display_symbol}<small>{item.expiry||item.exchange}</small></button>)}</div></>}
  </dialog>;
}

export function IBQuotePanel({ overview, onSettings }: {overview:Overview|null;onSettings:()=>void}) {
  const q=overview?.quote; const i=overview?.instrument;
  return <section className="orderbook ibkr-quote" aria-label="盈透买卖报价"><div className="panel-tabs"><button className="active">买卖报价</button></div>
    <div className="book-stream-controls"><span className={overview?.quote_stale?'amber':'positive'}>{overview?.quote_stale?'报价暂未更新':DATA_MODES[q?.data_mode??'unavailable']}</span></div>
    <div className="book-labels"><span>价格 ({i?.currency??'USD'})</span><span>数量 ({i?.volume_unit??'—'})</span></div>
    <div className="book-row sell"><span>卖一 {price(q?.ask)}</span><span>{money(q?.ask_size,0)}</span></div>
    <div className="book-row buy"><span>买一 {price(q?.bid)}</span><span>{money(q?.bid_size,0)}</span></div>
    <div className="ibkr-quote-info"><p>报价接收：{clock(q?.received_at)}</p><p>成交时间：{q?.source_time?new Date(q.source_time).toLocaleString('zh-CN'):'行情源暂未提供'}</p><p>K 线：{overview?.history_delay_minutes?`延迟至少 ${overview.history_delay_minutes} 分钟`:'实时权限模式'}，约每分钟同步。</p><p>当前提供买一卖一。深度订单簿与逐笔成交尚未接入。</p>{overview?.errors&&Object.values(overview.errors).map((e,n)=><p key={n} className="amber">{e}</p>)}<button onClick={onSettings}>盈透连接设置</button></div>
  </section>;
}
