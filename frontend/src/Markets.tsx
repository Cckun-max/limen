import { useEffect, useMemo, useRef, useState } from 'react';
import { Search, Star, X, Plus, Pencil, Trash2, ArrowUpDown, History } from 'lucide-react';
import { compact, clock, money, price } from './types';
import type { Instrument } from './types';
import { brokerSearchKind, instrumentMatches, normalizeInstrumentSearch } from './instrumentSearch';
import { useSearchHistory } from './useSearchHistory';
import './markets.css';

export interface Market {
  provider?: string; display_symbol?: string; name?: string; asset_type?: string; expiry?: string; stale?: boolean; data_mode?: string;
  symbol: string; instrument_id: string; mark: number | null; change_pct: number | null;
  volume_usd: number | null; oi_usd: number | null; funding: number | null;
  delisted: boolean; sz_decimals: number; received_at: number;
}
export interface Catalog { items: Market[]; stale: boolean; received_at: number | null; error: string | null; scope: string }
export interface Watchlist { id: string; name: string; symbols: string[] }
export async function marketRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { method, headers: { 'X-Local-Market': '1', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || '请求未成功');
  return value;
}

export function useMarkets() {
  const [reloadVersion, setReloadVersion] = useState(0);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [groups, setGroups] = useState<Watchlist[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const writing = useRef(false);
  const writeVersion = useRef(0);
  useEffect(() => {
    let active = true;
    const load = async () => {
      const version = writeVersion.current;
      try {
        const [data, saved] = await Promise.all([marketRequest<Catalog>('/markets'), marketRequest<Watchlist[]>('/watchlists')]);
        if (active) { setCatalog(data); if (!writing.current && version === writeVersion.current) setGroups(saved); setError(''); }
      } catch { if (active) { setError('本地行情服务未连接'); setCatalog(old => old ? { ...old, stale: true } : old); } }
    };
    void load();
    const timer = setInterval(load, 10_000);
    return () => { active = false; clearInterval(timer); };
  }, [reloadVersion]);
  async function mutate(path: string, method: string, body?: unknown) {
    if (writing.current) return false;
    writing.current = true; writeVersion.current++; setBusy(true); setError('');
    try { const saved = await marketRequest<Watchlist[]>(`/watchlists${path}`, method, body); setGroups(saved); return saved; }
    catch (e) { setError((e as Error).message); return false; }
    finally { writing.current = false; setBusy(false); }
  }
  return { catalog, groups, error, busy, mutate, reload: () => setReloadVersion(v=>v+1) };
}
type MarketsState = ReturnType<typeof useMarkets>;
type Sort = 'symbol' | 'mark' | 'change_pct' | 'volume_usd' | 'oi_usd' | 'funding';
type BrokerHit = Pick<Instrument, 'symbol' | 'con_id' | 'display_symbol' | 'name' | 'exchange' | 'currency' | 'asset_type' | 'expiry'>;

export function MarketsDialog({ state, symbol, initialTab, onChoose, onClose, onBrokerSettings }: {
  state: MarketsState; symbol: string; initialTab: 'all' | 'watch'; onChoose: (symbol: string) => void; onClose: () => void; onBrokerSettings: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState(initialTab);
  const [groupId, setGroupId] = useState('default');
  const [search, setSearch] = useState('');
  const searchInput = useRef<HTMLInputElement>(null);
  const history = useSearchHistory(search);
  const [filter, setFilter] = useState('all');
  const [assetType,setAssetType] = useState('all');
  const [volume, setVolume] = useState('0');
  const [sort, setSort] = useState<Sort>('volume_usd');
  const [ascending, setAscending] = useState(false);
  const [editing, setEditing] = useState<'new' | 'rename' | null>(null);
  const [name, setName] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [brokerHits, setBrokerHits] = useState<BrokerHit[]>([]);
  const [brokerPhase, setBrokerPhase] = useState<'idle' | 'loading' | 'ready' | 'disconnected' | 'error'>('idle');
  const [brokerError, setBrokerError] = useState('');
  const [retrySearch, setRetrySearch] = useState(0);
  const [adding, setAdding] = useState<number | null>(null);
  const [addError, setAddError] = useState('');
  const searchKind = tab === 'all' ? brokerSearchKind(search, assetType) : null;
  useEffect(() => {
    let active = true;
    setBrokerHits([]); setBrokerError(''); setAddError('');
    if (!searchKind) { setBrokerPhase('idle'); return; }
    setBrokerPhase('loading');
    const timer = setTimeout(async () => {
      try {
        const status = await marketRequest<{connected: boolean}>('/ibkr/status');
        if (!active) return;
        if (!status.connected) { setBrokerPhase('disconnected'); return; }
        const query = normalizeInstrumentSearch(search);
        const path = searchKind === 'FUT' ? `/ibkr/search?q=${encodeURIComponent(query)}&kind=FUT` : `/ibkr/discover?q=${encodeURIComponent(query)}`;
        const hits = await marketRequest<BrokerHit[]>(path);
        if (active) { setBrokerHits(hits); setBrokerPhase('ready'); }
      } catch (e) {
        if (active) { setBrokerError((e as Error).message); setBrokerPhase('error'); }
      }
    }, 650);
    return () => { active = false; clearTimeout(timer); };
  }, [search, searchKind, assetType, tab, retrySearch]);

  async function addBrokerHit(hit: BrokerHit) {
    if (adding !== null) return;
    setAdding(hit.con_id); setAddError('');
    try {
      const saved = await marketRequest<Instrument>('/ibkr/instruments', 'POST', {con_id: hit.con_id});
      state.reload(); chooseInstrument(saved.symbol);
    } catch (e) { setAddError((e as Error).message); }
    finally { setAdding(null); }
  }
  function chooseInstrument(next: string) {
    history.remember(search);
    onChoose(next); onClose();
  }
  const { catalog, groups, error, busy, mutate } = state;
  const group = groups.find(g => g.id === groupId) ?? groups[0];
  useEffect(() => { const modal = dialog.current; modal?.showModal(); return () => modal?.close(); }, []);
  const rows = useMemo(() => {
    const catalogRows = catalog?.items ?? [];
    const missing = (group?.symbols ?? []).filter(s => !catalogRows.some(r => r.symbol === s));
    const all = [...catalogRows, ...missing.map(symbol => ({ symbol, delisted: true, mark: null, change_pct: null, volume_usd: null, oi_usd: null, funding: null } as Market))];
    return all.filter(row => (tab === 'watch' ? group?.symbols.includes(row.symbol) : !row.delisted)
      && instrumentMatches(row, search)
      && (assetType==='all'||(row.asset_type??'perp')===assetType)
      && (filter === 'all' || (row.change_pct != null && (filter === 'up' ? row.change_pct > 0 : row.change_pct < 0)))
      && (Number(volume) === 0 || (row.volume_usd != null && row.volume_usd >= Number(volume))))
      .sort((a, b) => {
        const x = sort==='symbol'?(a.display_symbol??a.symbol):a[sort], y = sort==='symbol'?(b.display_symbol??b.symbol):b[sort];
        if (x == null || y == null) return x == null ? y == null ? a.symbol.localeCompare(b.symbol) : 1 : -1;
        const diff = typeof x === 'string' ? x.localeCompare(String(y)) : x - Number(y);
        return diff * (ascending ? 1 : -1) || a.symbol.localeCompare(b.symbol);
      });
  }, [catalog, group, tab, search, filter, assetType, volume, sort, ascending]);
  function chooseSort(next: Sort) { if (next === sort) setAscending(v => !v); else { setSort(next); setAscending(next === 'symbol'); } }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    const saved = await mutate(editing === 'rename' ? `/${group?.id}` : '', editing === 'rename' ? 'PATCH' : 'POST', { name: name.trim() });
    if (saved) { const next = saved.find(g => g.name === name.trim()); if (next) setGroupId(next.id); setEditing(null); }
  }
  return <dialog ref={dialog} className="markets-dialog" onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="markets-header"><div><span className="markets-eyebrow">MARKET EXPLORER</span><h2>市场与自选</h2><p>{catalog?.scope ?? 'Hyperliquid 原生永续'} · 行情时效取决于数据源 · 自选保存在本机</p></div><button aria-label="关闭市场列表" onClick={onClose}><X size={21}/></button></div>
    <div className="markets-tabs"><button className={tab === 'all' ? 'selected' : ''} onClick={() => setTab('all')}>全部行情</button><button className={tab === 'watch' ? 'selected' : ''} onClick={() => setTab('watch')}><Star size={14}/>我的自选</button><span className={catalog?.stale ? 'amber' : ''}>{catalog?.stale ? '缓存行情 · ' : '更新于 '}{clock(catalog?.received_at)}</span></div>
    <div className="markets-controls"><label className="market-search"><Search size={16}/><input ref={searchInput} aria-label="搜索品种" placeholder="搜索代码或名称，如 BTC、NVDA、英伟达、MGC" value={search} onChange={e => setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); history.remember(search); } }} maxLength={40}/>{search && <button aria-label="清空搜索" onClick={() => setSearch('')}><X size={14}/></button>}</label><select aria-label="市场类型" value={assetType} onChange={e=>setAssetType(e.target.value)}><option value="all">全部市场</option><option value="perp">加密永续</option><option value="stock">美股 / ETF</option><option value="future">期货</option></select><select aria-label="涨跌筛选" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部涨跌</option><option value="up">仅上涨</option><option value="down">仅下跌</option></select><select aria-label="成交额筛选" value={volume} onChange={e => setVolume(e.target.value)}><option value="0">全部成交额</option><option value="1000000">24h ≥ 100 万 USD</option><option value="10000000">24h ≥ 1,000 万 USD</option><option value="100000000">24h ≥ 1 亿 USD</option></select></div>
    <section className="market-search-history" aria-label="搜索历史">
      <span className="search-history-label"><History size={13}/>最近搜索</span>
      <div className="search-history-items">
        {history.items.length ? history.items.map(item => <span className="search-history-chip" key={item}>
          <button title={item} aria-label={`搜索历史：${item}`} onClick={() => { setSearch(item); history.remember(item); searchInput.current?.focus(); }}>{item}</button>
          <button aria-label={`删除搜索历史：${item}`} onClick={() => history.remove(item)}><X size={12}/></button>
        </span>) : <span className="search-history-empty">暂无搜索记录</span>}
      </div>
      {!!history.items.length && <button className="search-history-clear" onClick={history.clear}>清空历史</button>}
    </section>
    <div className="group-controls"><span>{tab === 'watch' ? '当前分组' : '星标保存到'}</span><select aria-label="自选分组" value={group?.id ?? ''} onChange={e => { setGroupId(e.target.value); setEditing(null); setDeleteOpen(false); }}>{groups.map(g => <option key={g.id} value={g.id}>{g.name}（{g.symbols.length}）</option>)}</select><button onClick={() => { setEditing('new'); setName(''); setDeleteOpen(false); }}><Plus size={14}/>新建分组</button><button disabled={!group} aria-label="重命名分组" onClick={() => { setEditing('rename'); setName(group?.name ?? ''); setDeleteOpen(false); }}><Pencil size={14}/></button><button disabled={!group || group.id === 'default'} aria-label="删除分组" onClick={() => { setDeleteOpen(true); setEditing(null); }}><Trash2 size={14}/></button><span className="market-count">{rows.length} 个品种</span></div>
    {editing && <form className="group-editor" onSubmit={save}><input autoFocus aria-label="分组名称" placeholder="分组名称" maxLength={30} value={name} onChange={e => setName(e.target.value)}/><button disabled={busy || !name.trim()} type="submit">保存分组</button><button type="button" onClick={() => setEditing(null)}>取消</button></form>}
    {deleteOpen && <div className="group-editor"><span>删除「{group?.name}」？组内币种会移入默认自选。</span><button disabled={busy} onClick={async () => { if (await mutate(`/${group?.id}`, 'DELETE')) { setGroupId('default'); setDeleteOpen(false); } }}>确认删除分组</button><button onClick={() => setDeleteOpen(false)}>取消</button></div>}
    {(error || catalog?.error) && <p className="market-error" role="alert">{error || catalog?.error}</p>}
    {searchKind && <section className="market-discovery" aria-label="盈透搜索结果" aria-busy={brokerPhase === 'loading'}>
      <div className="market-discovery-title"><strong>搜索更多{searchKind === 'FUT' ? '期货合约' : '美股 / ETF'}</strong><span>IBKR · {normalizeInstrumentSearch(search)}</span></div>
      {brokerPhase === 'loading' && <p role="status">正在查询盈透品种…</p>}
      {brokerPhase === 'disconnected' && <p role="status">连接盈透后可搜索未添加的品种。<button onClick={onBrokerSettings}>打开盈透连接设置</button></p>}
      {brokerPhase === 'error' && <p role="alert">{brokerError} <button onClick={() => setRetrySearch(v => v + 1)}>重新查询</button></p>}
      {brokerPhase === 'ready' && !brokerHits.length && <p role="status">盈透没有返回匹配品种，请尝试完整证券代码或英文名称。</p>}
      {brokerPhase === 'ready' && !!brokerHits.length && <><p>{searchKind === 'FUT' ? '选择具体到期合约后加载行情；同名 ETF 请切换“美股 / ETF”。' : '选择品种后加载行情；涨跌和成交额筛选仅用于下方已添加列表。'}</p><div className="market-discovery-results">{brokerHits.map(hit => {
        const saved = catalog?.items.some(row => row.symbol === hit.symbol);
        return <div className="market-discovery-row" key={hit.con_id}>
          <div><strong>{hit.display_symbol}</strong><span>{hit.name} · {hit.exchange} · {hit.currency}{hit.expiry ? ` · 到期 ${hit.expiry}` : ''}</span></div>
          <button disabled={adding !== null} aria-label={`${saved ? '查看' : '添加并看盘'} ${hit.display_symbol}`} onClick={() => { if (saved) chooseInstrument(hit.symbol); else void addBrokerHit(hit); }}>{adding === hit.con_id ? '正在添加…' : saved ? '查看图表' : '添加并看盘'}</button>
        </div>;
      })}</div></>}
      {addError && <p role="alert" className="negative">{addError}</p>}
    </section>}
    <div className="markets-table-wrap"><table className="markets-table"><thead><tr><th aria-label="自选"/><>{([['symbol', '品种'], ['mark', '价格 · USD'], ['change_pct', '涨跌幅'], ['volume_usd', '24h 成交额 · USD'], ['oi_usd', '持仓名义价值 · USD'], ['funding', '资金费率 · 1h']] as [Sort, string][]).map(([key, label]) => <th key={key} aria-sort={sort === key ? ascending ? 'ascending' : 'descending' : 'none'}><button onClick={() => chooseSort(key)}>{label}{sort === key ? <span>{ascending ? '↑' : '↓'}</span> : <ArrowUpDown size={11}/>}</button></th>)}</></tr></thead><tbody>{rows.map(row => {
      const starred = !!group?.symbols.includes(row.symbol);
      return <tr key={row.symbol} className={`${row.symbol === symbol ? 'current' : ''} ${row.delisted ? 'delisted' : ''}`}><td><button className={`market-star ${starred ? 'starred' : ''}`} disabled={busy || !group || (row.delisted && !starred)} aria-label={`${starred ? '移除' : '添加'} ${row.symbol} ${group?.name ?? '自选'}`} aria-pressed={starred} onClick={() => void mutate(`/${group?.id}/items/${encodeURIComponent(row.symbol)}`, starred ? 'DELETE' : 'PUT')}><Star size={17}/></button></td><td><button className="market-symbol-button" disabled={row.delisted} onClick={() => chooseInstrument(row.symbol)}><strong>{row.display_symbol??row.symbol}</strong><small>{row.provider==='ibkr'?`IBKR · ${row.expiry||'美股'} · ${row.stale?'缓存 / 未连接':row.data_mode==='live'?'实时报价':'延迟或冻结报价'}`:row.delisted ? '已下架 / 暂不可用' : row.symbol === symbol ? '当前图表' : '永续 · 查看图表'}</small></button></td><td>{price(row.mark)}</td><td className={(row.change_pct ?? 0) >= 0 ? 'positive' : 'negative'}>{row.change_pct == null ? '—' : `${row.change_pct >= 0 ? '+' : ''}${row.change_pct.toFixed(2)}%`}</td><td>{compact(row.volume_usd)}</td><td>{compact(row.oi_usd)}</td><td>{row.funding == null ? '—' : `${money(row.funding * 100, 4)}%`}</td></tr>;
    })}</tbody></table>{!rows.length && <div className="markets-empty"><Search size={25}/><strong>{searchKind ? '已添加列表中没有匹配品种' : search || filter !== 'all' || volume !== '0' ? '没有符合条件的品种' : tab === 'watch' ? '这个分组还没有自选' : '正在读取市场目录'}</strong><p>{tab === 'watch' ? '到全部行情点击星标，即可加入当前分组。' : searchKind ? '可从上方盈透搜索结果添加新股票或期货。' : '试试调整搜索条件，或等待行情连接。'}</p></div>}</div>
    <div className="markets-footnote">永续：标记价与 24h 涨跌；IBKR：成交价与前收盘涨跌。盈透列表仅更新正在查看的品种，其他显示缓存。</div>
  </dialog>;
}
