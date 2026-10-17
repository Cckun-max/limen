import { memo, useEffect, useState } from 'react';
import { money, price, priceDigits } from './types';
import { openOrderBook, type Book, type Level, type Trade } from './orderBookStream';

export const OrderBook = memo(function OrderBook({ symbol, szDecimals }: { symbol: string; szDecimals: number }) {
  const [tab, setTab] = useState<'book' | 'trades'>('book');
  const [fast, setFast] = useState(true);
  const [book, setBook] = useState<Book | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [status, setStatus] = useState('正在连接 Hyperliquid…');
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setBook(null); setTrades([]);
    const stop = openOrderBook(symbol, fast, {
      book: setBook, status: setStatus,
      trades: incoming => setTrades(old => Array.from(new Map([...incoming, ...old]
        .map(t => [`${t.coin}:${t.time}:${t.tid}`, t])).values()).sort((a, b) => b.time - a.time).slice(0, 40)),
    });
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => { stop(); clearInterval(timer); };
  }, [symbol, fast]);
  const bids = book?.levels[0] ?? [], asks = book?.levels[1] ?? [];
  const max = Math.max(1, ...[...bids, ...asks].map(l => +l.sz));
  const spread = bids.length && asks.length ? +asks[0].px - +bids[0].px : null;
  const age = book ? Math.max(0, now - book.receivedAt) : null;
  const stale = !!status || age == null || age > (fast ? 3000 : 12_000);
  const rows = (items: Level[], side: string) => items.map(l => <div className={`book-row ${side}`} key={l.px}>
    <div className="depth-bar" style={{ width: `${+l.sz / max * 100}%` }}/><span>{price(+l.px)}</span>
    <span key={l.sz} className="book-size">{money(+l.sz, szDecimals)}</span>
  </div>);
  return <section className="orderbook" aria-label="订单簿与最新成交">
    <div className="panel-tabs"><button className={tab === 'book' ? 'active' : ''} onClick={() => setTab('book')}>订单簿</button><button className={tab === 'trades' ? 'active' : ''} onClick={() => setTab('trades')}>最新成交</button></div>
    <div className="book-stream-controls"><span className={stale ? 'amber' : 'positive'}>{stale ? '等待推送' : '实时推送'}</span><select aria-label="订单簿更新模式" value={fast ? 'fast' : 'deep'} onChange={e => setFast(e.target.value === 'fast')}><option value="fast">快速 · 5 档</option><option value="deep">深度 · 20 档（较慢）</option></select></div>
    <div className="book-freshness" aria-label="订单簿推送状态" data-sequence={book?.sequence ?? 0} data-received-at={book?.receivedAt ?? 0} data-source-time={book?.sourceTime ?? 0} data-interval-ms={book?.intervalMs ?? ''}>
      <span>{age == null ? '等待首条快照' : `上次更新 ${(age / 1000).toFixed(1)} 秒前`}</span><span title="最近 9 次实际接收间隔的中位数，不是网络延迟">{book?.intervalMs == null ? '' : `间隔约 ${(book.intervalMs / 1000).toFixed(1)} 秒`}</span>
    </div>
    <div className="book-labels"><span>价格 (USDC)</span><span>数量 ({symbol})</span></div>
    {stale && <p className="book-status" role="status">{status || '盘口更新延迟'}{book ? ' · 当前为缓存' : ''}</p>}
    {tab === 'book' ? <div className={`book-body ${stale ? 'book-stale' : ''}`}>
      <div className="ask-rows">{rows([...asks].reverse(), 'sell')}</div>
      <div className="book-spread"><span>价差 <b>{money(spread, priceDigits(bids[0] ? +bids[0].px : undefined))}</b></span><span>{spread != null && bids[0] ? `${(spread / +bids[0].px * 100).toFixed(3)}%` : '—'}</span></div>
      <div className="bid-rows">{rows(bids, 'buy')}</div>
      <div className="book-totals"><span>前 {asks.length || (fast ? 5 : 20)} 档卖单 <b className="negative">{asks.length ? money(asks.reduce((v, l) => v + +l.sz, 0), szDecimals) : '—'}</b></span><span>买单 <b className="positive">{bids.length ? money(bids.reduce((v, l) => v + +l.sz, 0), szDecimals) : '—'}</b></span></div>
    </div> : <div className="trade-rows">{trades.map(t => <div className={`book-row ${t.side === 'B' ? 'buy' : 'sell'}`} key={`${t.time}:${t.tid}`}><span>{price(+t.px)}</span><span>{money(+t.sz, szDecimals)}</span></div>)}{!trades.length && <p className="book-status">等待最新成交</p>}</div>}
  </section>;
});
