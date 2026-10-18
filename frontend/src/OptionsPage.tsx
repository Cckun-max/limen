import { useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import './options.css';

type Row = {id:number;name:string;expiry:string;strike:number;right:'C'|'P';midpoint:number|null;iv:number|null;delta:number|null;gamma:number|null;vega:number|null;theta:number|null;volume:number|null;session_volume:number|null;oi:number|null;oi_date:string|null;gex:number|null};
type Target = 'ES'|'NQ';
type Data = {target:Target;multiplier?:number;underlying:string;underlying_price:number;bar_start:string;bar_end:string;collected_at:string;rate:{date:string;rate:number};rows:Row[];vx:{name:string;expiry:string;price:number|null}[];expiries:string[];expected_contracts:number;priced_contracts:number;oi_contracts:number;oi_dates:string[];oi_preliminary:boolean;strike_min:number;strike_max:number;strike_step:number};
type Status = {automatic:boolean;running:boolean;phase:string;next_update:string;last_attempt?:string;last_success?:string;error?:string;warnings:string[];progress?:number;total?:number};
type Payload = {status:Status;data:Data|null};
type Point = {x:number;y:number|null;label:string};
const fmt = (v:number|null|undefined,d=2) => v==null?'—':v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v:number|null|undefined) => v==null?'—':`${fmt(v*100)}%`;
const dateText = (d:string) => `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
const time = (d?:string) => d?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(d)):'—';
const expiryAxis = (d:string) => `${d.slice(4,6)}/${d.slice(6,8)}`;
const expiryTime = (d:string) => Date.parse(`${dateText(d)}T00:00:00Z`);

function Curve({points,label,unit='%',color='#b4a0ff'}:{points:Point[];label:string;unit?:string;color?:string}) {
  const values=points.flatMap(p=>p.y==null?[]:[p.y]);
  if(!values.length)return <p className="options-empty">暂无有效报价</p>;
  const min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.25,.1),low=min-pad,high=max+pad;
  const xmin=Math.min(...points.map(p=>p.x)),xmax=Math.max(...points.map(p=>p.x));
  const x=(p:Point)=>42+(p.x-xmin)/(xmax-xmin||1)*300,y=(v:number)=>115-(v-low)/(high-low)*90;
  let interrupted=true;
  const path=points.map(p=>{if(p.y==null){interrupted=true;return '';}const command=interrupted?'M':'L';interrupted=false;return `${command} ${x(p)} ${y(p.y)}`;}).join(' ');
  return <svg className="options-curve" viewBox="0 0 385 145" role="img" aria-label={label}>
    {[low,(low+high)/2,high].map(v=><g key={v}><line x1="40" x2="347" y1={y(v)} y2={y(v)} stroke="#2b2b32" strokeDasharray="3 4"/><text x="34" y={y(v)+4} textAnchor="end" fill="#8b8795" fontSize="10">{v.toFixed(1)}</text></g>)}
    <path d={path} fill="none" stroke={color} strokeWidth="2"/>
    {points.map((p,i)=><g key={p.x}>{(points.length<6||i%4===0||i===points.length-1)&&<text x={x(p)} y="138" textAnchor="middle" fill="#aaa3b8" fontSize="10">{p.label}</text>}{p.y!=null&&<circle cx={x(p)} cy={y(p.y)} r="3" fill={color}><title>{p.label}：{fmt(p.y)}{unit}</title></circle>}</g>)}
  </svg>;
}

function Distribution({rows,field}:{rows:Row[];field:'gex'|'oi'}) {
  const strikes=[...new Set(rows.map(r=>r.strike))].sort((a,b)=>a-b);
  const points=strikes.map(k=>{const at=rows.filter(r=>r.strike===k);const values=(side:string)=>at.filter(r=>r.right===side&&r[field]!=null).map(r=>r[field]!);const c=values('C'),p=values('P');return {k,c:c.length?c.reduce((a,b)=>a+b,0):null,p:p.length?p.reduce((a,b)=>a+b,0):null};});
  if(!points.some(p=>p.c!=null||p.p!=null))return <p className="options-empty">持仓报告更新后显示分布</p>;
  const scale=field==='gex'?1e6:1;
  const max=Math.max(1,...points.flatMap(p=>[Math.abs(p.c||0)/scale,Math.abs(p.p||0)/scale]));
  const zero=field==='gex'?106:177,range=field==='gex'?78:145;
  const x=(i:number)=>55+i*640/Math.max(1,points.length-1),y=(v:number)=>zero-v/scale/max*range;
  const width=Math.min(12,280/points.length);
  return <svg className="options-distribution" viewBox="0 0 750 220" role="img" aria-label={field==='gex'?'Gamma 敞口执行价分布':'持仓量执行价分布'}>
    <text x="18" y="16" fill="#9d96ad" fontSize="11">{field==='gex'?'百万美元 / 1% 价格变化':'手'}</text>
    {(field==='gex'?[-max,0,max]:[0,max/2,max]).map(v=><g key={v}><line x1="49" x2="712" y1={zero-v/max*range} y2={zero-v/max*range} stroke="#302b3a" strokeDasharray="3 4"/><text x="43" y={zero-v/max*range+4} textAnchor="end" fill="#93899f" fontSize="10">{fmt(v,field==='gex'?1:0)}</text></g>)}
    {points.map((p,i)=><g key={p.k}>{[p.c,p.p].map((v,j)=>v==null?null:<rect key={j} x={x(i)+(j?1:-width-1)} y={Math.min(zero,y(v))} width={width} height={Math.max(.7,Math.abs(y(v)-zero))} fill={j?'#e989a2':'#77d1ba'}><title>{p.k} · {j?'Put':'Call'}：{fmt(v/scale,field==='gex'?3:0)}{field==='gex'?' 百万美元':' 手'}</title></rect>)}{(i%2===0||i===points.length-1)&&<text x={x(i)} y="207" textAnchor="middle" fontSize="10" fill="#aaa2b8">{p.k}</text>}</g>)}
    {field==='gex'&&<path d={points.map((p,i)=>p.c==null||p.p==null?'':`${i===0||points[i-1].c==null||points[i-1].p==null?'M':'L'} ${x(i)} ${y(p.c+p.p)}`).join(' ')} fill="none" stroke="#c5acff" strokeWidth="2"/>}
  </svg>;
}

function paired(rows:Row[],field:'oi'|'volume'|'session_volume') {
  const pairs=new Map<number,Partial<Record<'C'|'P',number|null>>>();
  for(const row of rows){const p=pairs.get(row.strike)||{};p[row.right]=row[field];pairs.set(row.strike,p);}
  const complete=[...pairs.values()].filter(p=>p.C!=null&&p.P!=null);
  const c=complete.reduce((n,p)=>n+p.C!,0),p=complete.reduce((n,p)=>n+p.P!,0);
  return {c:complete.length?c:null,p:complete.length?p:null,ratio:c?p/c:null,n:complete.length,total:pairs.size};
}

export function OptionsPage(){
  const [target,setTarget]=useState<Target>(()=>{try{return localStorage.getItem('options-target')==='NQ'?'NQ':'ES';}catch{return 'ES';}});
  const choose=(value:Target)=>{setTarget(value);try{localStorage.setItem('options-target',value);}catch{/* Selection still works without storage. */}};
  return <OptionsBoard key={target} target={target} choose={choose}/>;
}

function OptionsBoard({target,choose}:{target:Target;choose:(value:Target)=>void}){
  const [payload,setPayload]=useState<Payload|null>(null),[error,setError]=useState(''),[expiry,setExpiry]=useState(''),[contractId,setContractId]=useState<number|null>(null),[sending,setSending]=useState(false);
  const endpoint=`/api/v1/options/${target.toLowerCase()}`;
  useEffect(()=>{
    const controller=new AbortController();let pending=false;
    const read=async()=>{if(pending)return;pending=true;try{const r=await fetch(endpoint,{signal:controller.signal,cache:'no-store'});if(!r.ok)throw Error();const value=await r.json();if(value.data&&value.data.target!==target)throw Error();if(!controller.signal.aborted){setPayload(value);setError('');}}catch{if(!controller.signal.aborted)setError('连接暂时中断，正在自动重连');}finally{pending=false;}};
    void read();const timer=window.setInterval(read,5000);
    const visible=()=>{if(document.visibilityState==='visible')void read();};document.addEventListener('visibilitychange',visible);
    return()=>{controller.abort();clearInterval(timer);document.removeEventListener('visibilitychange',visible);};
  },[endpoint,target]);
  const data=payload?.data,status=payload?.status;
  const active=data?.expiries.includes(expiry)?expiry:data?.expiries[0]||'';
  const rows=data?.rows.filter(r=>r.expiry===active)||[];
  const atm=data?Math.round(data.underlying_price/data.strike_step)*data.strike_step:0;
  const selected=rows.find(r=>r.id===contractId)||rows.find(r=>r.strike===atm&&r.right==='P')||rows[0];
  const oi=paired(rows,'oi'),volume=paired(rows,'volume'),session=paired(rows,'session_volume');
  const skew=rows.filter(r=>r.right===(r.strike<(data?.underlying_price||0)?'P':'C'));
  const term=data?.expiries.map(e=>data.rows.find(r=>r.expiry===e&&r.strike===atm&&r.right===(atm<data.underlying_price?'P':'C'))).filter((r):r is Row=>!!r)||[];
  const refresh=async()=>{setSending(true);try{const r=await fetch(`${endpoint}/refresh`,{method:'POST',headers:{'X-Local-Market':'1'}});if(!r.ok)throw Error();const state=await fetch(endpoint,{cache:'no-store'});if(state.ok)setPayload(await state.json());setError('');}catch{setError('暂时无法更新，稍后将自动重试');}finally{setSending(false);}};
  return <main className="options-page" aria-label={`${target} 期权看板`}>
    <div className="options-heading"><div><span className="options-eyebrow">FUTURES OPTIONS</span><h1>{target} 期权 <span>每 30 分钟更新</span></h1></div><div className="options-products" role="group" aria-label="期权标的"><button aria-pressed={target==='ES'} onClick={()=>choose('ES')}>ES 标普 500</button><button aria-pressed={target==='NQ'} onClick={()=>choose('NQ')}>NQ 纳斯达克 100</button></div><a className="options-back" href="#/trade"><ArrowLeft size={15}/>返回期货图表</a></div>
    <div className="options-status" role="status"><div><i className={status?.running?'updating':''}/><strong>{status?.running?`${status.phase}${status.total?` ${status.progress||0}/${status.total}`:''}`:status?.error?'等待重连':status?.automatic?'自动更新已开启':'正在连接'}</strong></div><span>最近检查 {time(status?.last_success)}</span><span>{status?.running?'本轮完成后安排下次更新':`下次更新 ${time(status?.next_update)}`}</span><button className="options-refresh" onClick={refresh} disabled={sending||status?.running||!!error}><RefreshCw size={14}/>{sending||status?.running?'更新中…':'立即更新'}</button></div>
    {(error||status?.error)&&<p className="options-error" role="alert">{error||status?.error}{data?' · 保留最近有效行情':''}</p>}
    {!!status?.warnings?.length&&<p className="options-warning">{status.warnings.join(' · ')}</p>}
    {!data?<div className="options-loading">正在获取 {target} 期权行情…<p>首次更新需要几分钟，完成后自动显示。</p></div>:<>
      <div className="options-controls"><label>到期日<select aria-label={`${target} 期权到期日`} value={active} onChange={e=>{setExpiry(e.target.value);setContractId(null);}}>{data.expiries.map(d=><option value={d} key={d}>{dateText(d)}</option>)}</select></label><div className="options-coverage"><strong>{data.underlying} {fmt(data.underlying_price)}</strong><span>{data.strike_min}–{data.strike_max} · 间隔 {data.strike_step} 点 · IV {data.priced_contracts}/{data.expected_contracts} · OI {data.oi_contracts}/{data.expected_contracts}</span></div><span className="options-asof">行情截至 {time(data.bar_end)} · 北京时间</span></div>
      <div className="options-grid">
        <section className="options-card options-gamma"><header><h2>Gamma 敞口分布</h2><span className="options-tag">{rows.filter(r=>r.gex!=null).length}/{rows.length} 合约</span></header><div className="options-legend"><span className="call">Call</span><span className="put">Put</span><span className="net">净值估算</span><span>OI {data.oi_dates.join(' / ')||'等待更新'}{data.oi_preliminary?' · 初步报告':''}</span></div><Distribution rows={rows} field="gex"/><p>按执行价汇总 · Call 正、Put 负；这是持仓假设下的估算，不代表做市商真实仓位。</p></section>
        <section className="options-card"><header><h2>价格、IV 与 Greeks</h2><span className="options-tag">30 分钟参考价</span></header><select className="options-contract" aria-label="期权合约" value={selected?.id||''} onChange={e=>setContractId(Number(e.target.value))}>{rows.map(r=><option key={r.id} value={r.id}>{r.strike} · {r.right==='C'?'Call 看涨':'Put 看跌'}</option>)}</select><div className="options-primary"><strong>{pct(selected?.iv)}</strong><span>IV · 中间价 {fmt(selected?.midpoint)} 点</span></div><dl className="options-greeks">{(['delta','gamma','vega','theta'] as const).map(k=><div key={k}><dt>{({delta:'Delta',gamma:'Gamma',vega:'Vega / 1%',theta:'Theta / 日'})[k]}</dt><dd>{fmt(selected?.[k],k==='gamma'?6:4)}</dd></div>)}</dl><p>延迟行情；报价取已完成的半小时收盘中间价。</p></section>
        <section className="options-card"><header><h2>波动率偏斜 Skew</h2><span className="options-tag">{expiryAxis(active)}</span></header><Curve label="虚值期权波动率偏斜" points={skew.map(r=>({x:r.strike,y:r.iv==null?null:r.iv*100,label:String(r.strike)}))}/><p>同一到期日 · 虚值 Put / Call · 执行价</p></section>
        <section className="options-card"><header><h2>IV 期限结构</h2><span className="options-tag">近似平值</span></header><Curve label="近似平值 IV 期限结构" points={term.map(r=>({x:expiryTime(r.expiry),y:r.iv==null?null:r.iv*100,label:expiryAxis(r.expiry)}))} color="#77d1ba"/><p>各到期日统一执行价 {atm} · 到期日</p></section>
        <section className="options-card"><header><h2>VIX 期货期限结构</h2><span className="options-tag">VX</span></header><Curve label="VIX 期货期限结构" points={data.vx.map(r=>({x:expiryTime(r.expiry),y:r.price,label:expiryAxis(r.expiry)}))} unit="" color="#e3b776"/><p>独立 VX 期货 · 同一半小时成交收盘价</p></section>
        <section className="options-card options-oi"><header><h2>持仓量分布</h2><span className="options-tag">日级 OI</span></header><Distribution rows={rows} field="oi"/><div className="options-ratios"><span>Put/Call <b>{fmt(oi.ratio)}</b></span><span>Call <b>{fmt(oi.c,0)}</b></span><span>Put <b>{fmt(oi.p,0)}</b></span></div><p>所选到期日、所示执行价 · {oi.n}/{oi.total} 对合约；不含未取得的持仓量。</p></section>
        <section className="options-card"><header><h2>成交量与 Put/Call</h2><span className="options-tag">所选范围</span></header><div className="options-primary"><strong>{fmt(volume.ratio)}</strong><span>半小时 Put/Call · {volume.n}/{volume.total} 对合约</span><span>所选合约当日累计 {fmt(selected?.session_volume,0)} 手</span></div><dl className="options-greeks"><div><dt>半小时 Call</dt><dd>{fmt(volume.c,0)}</dd></div><div><dt>半小时 Put</dt><dd>{fmt(volume.p,0)}</dd></div><div><dt>当日 Call</dt><dd>{fmt(session.c,0)}</dd></div><div><dt>当日 Put</dt><dd>{fmt(session.p,0)}</dd></div></dl><p>当日 Put/Call {fmt(session.ratio)} · 完整时段 {session.n}/{session.total} 对合约。缺少成交记录的合约不计入。</p></section>
      </div>
      <details className="options-details"><summary>合约明细 <span>{dateText(active)} · {rows.length} 个合约</span></summary><div className="options-table-wrap"><table><caption>参考价及 Greeks 以点计；「—」表示暂缺。</caption><thead><tr>{['合约','中间价','IV','Delta','Gamma','OI','半小时量'].map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{rows.map(r=><tr key={r.id}><th scope="row">{r.name}</th><td>{fmt(r.midpoint)}</td><td>{pct(r.iv)}</td><td>{fmt(r.delta,4)}</td><td>{fmt(r.gamma,6)}</td><td>{fmt(r.oi,0)}</td><td>{fmt(r.volume,0)}</td></tr>)}</tbody></table></div></details>
      <details className="options-method"><summary>数据与计算口径</summary><p>行情：IBKR 延迟行情的 30 分钟柱，每半小时检查一次；OI：<a href="https://www.cmegroup.com/market-data/daily-bulletin.html" target="_blank" rel="noreferrer">CME 每日持仓报告</a>。当前覆盖 {data.expiries.length} 个到期日、{data.expected_contracts} 个合约，IV 有效 {data.priced_contracts} 个，不代表全部 {target} 期权。</p><p>Black-76 · 欧式期货期权 · 年化利率采用 SOFR {pct(data.rate.rate)}（{data.rate.date}），按平坦利率估算。Gamma 敞口 = Gamma × OI × {data.multiplier??(target==='NQ'?20:50)} × 期货价格² × 1%；Put 按负号计。OI 每日公布，行情更新不会产生新的盘中 OI。</p><p>本机运行且联网时自动采集，休眠后恢复更新；保存最近 10 天。最近取数完成 {time(data.collected_at)}。市场休市时保留最近交易时段。</p></details>
    </>}
  </main>;
}
