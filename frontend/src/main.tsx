import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MarketChart } from './Chart';
import { TimeframePicker } from './TimeframePicker';
import { AnalysisPanel } from './AnalysisPanel';
import { TIMEFRAMES, LABELS, money, price, compact, clock, type ChartData, type Overview, type Timeframe, type Visibility } from './types';
import { RefreshCw, Maximize, Search, Settings2, Star, Sparkles, FileText, SlidersHorizontal } from 'lucide-react';
import { OrderBook } from './OrderBook';
import { MarketsDialog, useMarkets } from './Markets';
import { IBKRDialog, IBQuotePanel } from './IBKR';
import './style.css';
import './terminal.css';

const QUALITY: Record<string, string> = { warming: '预热中', stale: '数据过期', gap: '正在补齐缺口', recovering: '恢复校验中', pending: '等待收盘确认', fresh: '数据完整', delayed:'延迟行情', closed:'休市 · 最近交易时段数据' };

function App() {
  const [symbol, setSymbol] = useState(() => localStorage.getItem('local-market:symbol') || 'BTC');
  const [marketTab, setMarketTab] = useState<'all' | 'watch' | null>(null);
  const markets = useMarkets();
  const [ibkrOpen,setIbkrOpen] = useState(false);
  function chooseSymbol(next: string) {
    if (next === symbol) return;
    localStorage.setItem('local-market:symbol', next);
    setOverview(null); setCharts({}); setClientConnected(false); setLastSeen(0); setSymbol(next);
  }
  const defaultGroup = markets.groups.find(g => g.id === 'default');
  const starred = !!defaultGroup?.symbols.includes(symbol);
  const watchSymbols = [...new Set(markets.groups.flatMap(g => g.symbols))];
  const asset = markets.catalog?.items.find(m => m.symbol === symbol);

  const [timeframe, setTimeframe] = useState<Timeframe>(() => {
    const saved = localStorage.getItem('local-market:timeframe');
    return TIMEFRAMES.includes(saved as Timeframe) ? saved as Timeframe : '15m';
  });
  const [overview, setOverview] = useState<Overview | null>(null);
  const [charts, setCharts] = useState<Partial<Record<Timeframe, ChartData>>>({});
  const [visible, setVisible] = useState<Visibility>({ ema20: true, ema60: true, rsi: false, macd: false });
  const [clientConnected, setClientConnected] = useState(false);
  const [lastSeen, setLastSeen] = useState(0);
  const [time, setTime] = useState(Date.now());
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [resetKey, setResetKey] = useState(0);
  const [aiOpen, setAiOpen] = useState(false);
  const [settingsRequest, setSettingsRequest] = useState(0);
  function openSettings() { setAiOpen(true); setSettingsRequest(n=>n+1); }
  const [lowerTab, setLowerTab] = useState('market');

  function chooseTimeframe(next: Timeframe) {
    // Persist the user's selection immediately, not on every live candle update.
    localStorage.setItem('local-market:timeframe', next);
    setTimeframe(next);
  }

  useEffect(() => {
    const abort = new AbortController();
    fetch(`/api/v1/overview?symbol=${encodeURIComponent(symbol)}`,  { signal: abort.signal }).then(r => {
      if (!r.ok) return r.json().then(v => { throw new Error(v.detail); }); return r.json();
    }).then(v => { if (!abort.signal.aborted && v.symbol === symbol) setOverview(v); }).catch(e => { if (e.name !== 'AbortError') setError(e.message); });
    const events = new EventSource(`/api/v1/events?symbol=${encodeURIComponent(symbol)}`);
    events.onmessage = event => {
      const value = JSON.parse(event.data);
      if (abort.signal.aborted || value.symbol !== symbol) return;
      setOverview(value); setLastSeen(Date.now()); setClientConnected(true);
    };
    events.addEventListener('service-error', event => { setError(JSON.parse((event as MessageEvent).data).detail); events.close(); });
    events.onerror = () => setClientConnected(false);
    const timer = window.setInterval(() => setTime(Date.now()), 1000);
    return () => { abort.abort(); events.close(); clearInterval(timer); };
  }, [symbol]);

  const revision = overview?.timeframes[timeframe]?.revision;
  useEffect(() => {
    const abort = new AbortController();
    setError('');
    fetch(`/api/v1/chart/${timeframe}?symbol=${encodeURIComponent(symbol)}`, { signal: abort.signal }).then(r => {
      if (!r.ok) return r.json().then(v => { throw new Error(v.detail); }); return r.json();
    }).then((chart: ChartData) => { if (!abort.signal.aborted && chart.symbol === symbol) setCharts(old => ({ ...old, [timeframe]: chart })); })
      .catch(e => { if (e.name !== 'AbortError') setError(e.message || '图表数据暂时不可用，请重新同步。'); });
    return () => abort.abort();
  }, [symbol, timeframe, revision, resetKey]);

  async function refresh() {
    setRefreshing(true);
    try {
      const response = await fetch(`/api/v1/refresh?symbol=${encodeURIComponent(symbol)}`,  { method: 'POST', headers: { 'X-Local-Market': '1' } });
      if (!response.ok) throw new Error();
      setResetKey(k => k + 1); setError('');
    } catch { setError('本地服务未连接。请检查服务是否正在运行。'); }
    finally { setRefreshing(false); }
  }

  const connected = clientConnected && time - lastSeen < 8_000;
  const mode = connected ? overview?.transport ?? 'offline' : 'offline';
  const modeLabel = mode === 'live' ? '实时连接' : mode === 'polling' ? '轮询更新' : overview ? '连接恢复中' : '正在连接';
  const quote = overview?.quote;
  const isIB = symbol.startsWith('ibkr:');
  const instrument = overview?.instrument;
  const displaySymbol = instrument?.display_symbol || asset?.display_symbol || symbol;
  const sourceName = isIB ? 'IBKR' : 'Hyperliquid';
  const currency = isIB ? instrument?.currency ?? 'USD' : 'USDC';
  const chart = charts[timeframe];
  const rawQuality = overview?.timeframes[timeframe]?.quality;
  const quality = rawQuality && !connected ? { ...rawQuality, status: 'stale' } : rawQuality;
  const lastBar = chart?.bars.at(-1);
  const serverTime = overview ? overview.server_time + Math.max(0, time - lastSeen) : time;
  const remaining = lastBar ? Math.max(0, Math.floor((lastBar.end - serverTime) / 1000)) : 0;
  const days = Math.floor(remaining / 86400);
  const timer = `${days ? `${days} 天 ` : ''}${String(Math.floor(remaining % 86400 / 3600)).padStart(2, '0')}:${String(Math.floor(remaining % 3600 / 60)).padStart(2, '0')}:${String(remaining % 60).padStart(2, '0')}`;
  const overviewTimeframes = Array.from(new Set<Timeframe>([timeframe, '15m', '1h', '4h']));
  const stale = !connected || overview?.quote_stale;

  return <div className="app terminal">
    <header className="terminal-header">
      <a className="terminal-brand" href="/" aria-label="tide 首页"><svg viewBox="12 24 100 79" width={30} height={24} aria-hidden="true"><path d="M22 52H38C58 52 61 24 84 24H110L100 42H88C68 42 65 70 42 70H22Z" fill="#00D8F5"/><path d="M12 85H47C67 85 70 57 93 57H112L102 75H97C77 75 74 103 51 103H12Z" fill="#00D8F5"/></svg><span>tide</span></a>
      <nav aria-label="主导航"><button onClick={() => setMarketTab('all')}>市场</button><a href="#chart" className="active">交易</a><button onClick={() => setAiOpen(true)}>AI 看盘</button></nav>
      <button className="symbol-search" onClick={() => setMarketTab('all')} aria-label="搜索与切换品种"><Search size={16}/><span>{displaySymbol} / {currency}</span><span className="search-source">搜索品种</span></button>
      <div className="terminal-account"><button className="ibkr-entry" onClick={()=>setIbkrOpen(true)}>盈透 IBKR</button><span className={`connection ${mode}`}><i/>{modeLabel}</span><button onClick={refresh} disabled={refreshing || overview?.syncing} title="重新同步行情"><RefreshCw size={17}/></button><button onClick={openSettings} title="AI 模型配置"><Settings2 size={18}/></button></div>
    </header>
    <div className="watch-strip"><button className="watch-manage" onClick={() => setMarketTab('watch')}><Star size={16}/>自选</button><span className="strip-divider"/>{watchSymbols.map(s => { const item = markets.catalog?.items.find(m => m.symbol === s); return <button className={`watch-chip ${s === symbol ? 'selected' : ''}`} key={s} disabled={!item || item.delisted} onClick={() => chooseSymbol(s)}>{item?.display_symbol??s}<b>{price(item?.mark)}</b><span className={(item?.change_pct ?? 0) >= 0 ? 'positive' : 'negative'}>{item?.change_pct == null ? '—' : `${item.change_pct > 0 ? '+' : ''}${item.change_pct.toFixed(2)}%`}</span></button>; })}{!watchSymbols.length && <span className="watch-empty">点击币种旁的星标，添加你的第一个自选</span>}{markets.catalog?.stale && <span className="amber">缓存行情</span>}{markets.error && <span className="watch-error" role="status">{markets.error}</span>}</div>
    <main className="terminal-main">
      <div className="market-column">
        <section className={`quote-strip ${stale?'stale':''}`} aria-label={`${displaySymbol} 行情概览`}>
          <div className="quote-symbol"><button className="quote-star" aria-label={`${starred ? '移出' : '加入'}默认自选`} aria-pressed={starred} disabled={markets.busy || !defaultGroup} onClick={() => void markets.mutate(`/default/items/${encodeURIComponent(symbol)}`, starred ? 'DELETE' : 'PUT')}><Star size={19}/></button><span className="coin">{symbol === 'BTC' ? '₿' : displaySymbol.slice(0, 2)}</span><button className="symbol-switch" onClick={() => setMarketTab('all')}><strong>{displaySymbol}{isIB?'':'/USDC'} ⌄</strong><small>{isIB?(instrument?.asset_type==='future'?`${instrument.exchange} 期货`:'美股 / ETF'):`${symbol} 永续`}</small></button></div>
          <div className="quote-price"><strong className={(quote?.change_pct??0)>=0?'positive':'negative'}>{price(quote?.mark)}</strong><small className={(quote?.change_pct??0)>=0?'positive':'negative'}>{quote?.change_pct!=null?`${quote.change_pct>=0?'+':''}${quote.change_pct.toFixed(2)}%`:'—'}</small></div>
          <div><small>{isIB?`当日成交量 · ${instrument?.volume_unit??'—'}`:'成交额 · 24h USD'}</small><strong>{compact(isIB?quote?.volume:quote?.volume_usd)}</strong></div>
          <div><small>{isIB?'交易时段':`持仓量 · ${symbol}`}</small><strong>{isIB?instrument?.session??'等待品种信息':compact(quote?.oi_base)}</strong></div>
          <div><small>{isIB?'K 线时效':'资金费 · 1h'}</small><strong className={isIB?'amber':(quote?.funding??0)>=0?'positive':'negative'}>{isIB?(overview?.history_delay_minutes?`延迟 ≥ ${overview.history_delay_minutes} 分钟`:'实时权限模式'):quote?.funding==null?'—':`${(quote.funding*100).toFixed(4)}%`}</strong></div>
        </section>
        {(error || (overview && !connected) || quality?.status === 'stale' || quality?.status === 'gap') && <div className="notice" role="status">{error || (!connected ? '本地连接中断，正在恢复。当前为缓存行情。' : quality?.status === 'gap' ? '行情存在缺口，正在补齐。' : '行情已过期，正在重新同步。')}</div>}
        <section className="chart-card" id="chart" aria-label="多周期图表">
          <div className="terminal-toolbar"><button className="chart-symbol" onClick={() => setMarketTab('all')}><Search size={16}/> {displaySymbol}</button><TimeframePicker value={timeframe} onChange={chooseTimeframe}/><SlidersHorizontal size={16}/><div className="indicator-toggles">{(['ema20','ema60','rsi','macd'] as const).map(key=><label key={key} className={`toggle ${key}`}><input type="checkbox" checked={visible[key]} onChange={()=>setVisible(v=>({...v,[key]:!v[key]}))}/><span>{({ema20:'EMA 20',ema60:'EMA 60',rsi:'RSI',macd:'MACD'})[key]}</span></label>)}</div><button className="icon-button" aria-label="重置图表视野" onClick={()=>setResetKey(k=>k+1)}><Maximize size={17}/></button></div>
          <div className="chart-caption">{displaySymbol} · {LABELS[timeframe]} · {sourceName}{instrument?.expiry?` · 到期 ${instrument.expiry}`:''}</div>
          <MarketChart key={symbol} symbol={displaySymbol} data={chart} timeframe={timeframe} visible={visible} resetKey={resetKey}/>
          <div className="chart-bottom"><div className="quick-periods">{(['1m','5m','15m','1h','4h','1d','1w'] as Timeframe[]).map(tf=><button key={tf} className={tf===timeframe?'active':''} onClick={()=>chooseTimeframe(tf)}>{tf}</button>)}</div><span>{isIB?(lastBar?.status==='forming'?'尚未收盘':'最近确认柱'):lastBar?.status==='forming'?`形成中 ${timer}`:'已收盘'} · {clock(time)}</span></div>
        </section>
        <section id="market-data" className="market-data"><div className="panel-tabs"><button className={lowerTab==='market'?'active':''} onClick={()=>setLowerTab('market')}>市场数据</button><button className={lowerTab==='status'?'active':''} onClick={()=>setLowerTab('status')}>数据状态</button></div>
          {lowerTab==='market'?<div className="timeframe-cards">{overviewTimeframes.map(tf=>{const summary=overview?.timeframes[tf];const i=summary?.confirmed_indicators;return <button key={tf} className={`timeframe-card ${tf===timeframe?'selected':''}`} onClick={()=>chooseTimeframe(tf)}><strong>{LABELS[tf]}</strong><span>{price(summary?.last_close)}</span><small>RSI {money(i?.rsi14,1)} · MACD {price(i?.histogram)}</small></button>})}</div>:<div className="terminal-data-status"><span>当前周期 {LABELS[timeframe]}</span><span>本地 K 线 {quality?.count??0} 根</span><span>{QUALITY[quality?.status??'warming']}</span><span>行情接收 {clock(quote?.received_at)}</span><span>历史校验 {clock(quality?.last_rest)}</span><span>{isIB?`交易日历：${instrument?.timezone??'IBKR'} · ${QUALITY[quality?.status??'warming']}`:timeframe==='1w'?'周界 UTC 周四 00:00':'时间显示：本机时区'}</span>{quality?.error&&<p className="negative">{quality.error}</p>}</div>}
        </section>
      </div>
      {isIB?<IBQuotePanel overview={overview} onSettings={()=>setIbkrOpen(true)}/>:<OrderBook key={symbol} symbol={symbol} szDecimals={asset?.sz_decimals ?? 4}/>}
      <section className="assistant-column" aria-label="AI 问答面板"><div className="assistant-tabs"><button className={!aiOpen?'active':''} onClick={()=>setAiOpen(false)}>AI 问答</button><button className={aiOpen?'active':''} onClick={()=>setAiOpen(true)}>分析工作台</button></div>
        <div className="ai-landing" hidden={aiOpen}><button className="ask-ai" onClick={()=>setAiOpen(true)}><Sparkles size={19}/>Ask Market AI</button><button onClick={()=>setAiOpen(true)}><FileText size={18}/>AI 研报与记录</button><button onClick={openSettings}><Settings2 size={18}/>模型配置</button><p>读取当前行情，生成单轮看盘分析。</p></div>
        <div className="ai-workbench" hidden={!aiOpen}><AnalysisPanel symbol={symbol} displaySymbol={displaySymbol} timeframe={timeframe} settingsRequest={settingsRequest}/></div>
      </section>
    </main>
    {marketTab && <MarketsDialog state={markets} symbol={symbol} initialTab={marketTab} onChoose={chooseSymbol} onClose={() => setMarketTab(null)} onBrokerSettings={() => {setMarketTab(null);setIbkrOpen(true);}}/>}
    {ibkrOpen&&<IBKRDialog onClose={()=>setIbkrOpen(false)} onChoose={s=>{markets.reload();chooseSymbol(s);setIbkrOpen(false);}}/>}
    <footer className="terminal-footer"><span><i/>{modeLabel} · {sourceName}</span><span>TradingView Lightweight Charts™ · <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView, Inc.</a></span></footer>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
