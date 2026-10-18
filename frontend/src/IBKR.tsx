import { money, price, clock, type Overview } from './types';
import './ibkr.css';

export const DATA_MODES: Record<string,string> = { live:'实时报价', delayed:'延迟报价', frozen:'收盘冻结报价', delayed_frozen:'延迟冻结报价', unknown:'等待报价类型', unavailable:'等待行情' };

export function IBQuotePanel({ overview }: {overview:Overview|null}) {
  const q=overview?.quote; const i=overview?.instrument;
  return <section className="orderbook ibkr-quote" aria-label="盈透买卖报价"><div className="panel-tabs"><button className="active">买卖报价</button></div>
    <div className="book-stream-controls"><span className={overview?.quote_stale?'amber':'positive'}>{overview?.quote_stale?'报价暂未更新':DATA_MODES[q?.data_mode??'unavailable']}</span></div>
    <div className="book-labels"><span>价格 ({i?.currency??'USD'})</span><span>数量 ({i?.volume_unit??'—'})</span></div>
    <div className="book-row sell"><span>卖一 {price(q?.ask)}</span><span>{money(q?.ask_size,0)}</span></div>
    <div className="book-row buy"><span>买一 {price(q?.bid)}</span><span>{money(q?.bid_size,0)}</span></div>
    <div className="ibkr-quote-info"><p>报价接收：{clock(q?.received_at)}</p><p>成交时间：{q?.source_time?new Date(q.source_time).toLocaleString('zh-CN'):'行情源暂未提供'}</p><p>K 线：{overview?.history_delay_minutes?`延迟至少 ${overview.history_delay_minutes} 分钟`:'实时行情'}，约每分钟同步。</p><p>当前提供买一卖一。深度订单簿与逐笔成交尚未接入。</p>{overview?.errors&&Object.values(overview.errors).map((e,n)=><p key={n} className="amber">{e}</p>)}</div>
  </section>;
}
