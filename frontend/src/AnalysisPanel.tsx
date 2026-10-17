import { useEffect, useRef, useState } from 'react';
import { LABELS, money, price, type Timeframe } from './types';
import { aiRequest, dateTime, type AnalysisRun, type AnalysisSummary, type ModelSettings, type Snapshot, type Framework } from './aiTypes';
import { FrameworkEditor, FrameworkDetails } from './FrameworkEditor';
import './analysis.css';

const defaultQuestion = (symbol: string) => `请结合多周期趋势、EMA、RSI、MACD 和成交量分析 ${symbol}，说明关键价位、多空情景、触发条件与失效条件。`;
const STATUS = { running: '分析中', completed: '已完成', failed: '失败', cancelled: '已取消' };
const REGIME: Record<string, string> = { trending_up: '上行趋势', trending_down: '下行趋势', ranging: '区间震荡', unclear: '方向不明确' };

function Evidence({ refs, snapshot }: { refs: string[]; snapshot: Snapshot }) {
  function find(ref: string) {
    if (ref === 'quote') return snapshot.quote;
    for (const tf of Object.values(snapshot.timeframes)) {
      const item = [...tf.bars, ...tf.indicators].find(v => v.evidence_id === ref);
      if (item) return item;
    }
    return snapshot.levels.find(v => v.level_ref === ref);
  }
  return <details className="ai-evidence"><summary>查看依据 · {refs.join(' / ')}</summary>
    {refs.map(ref => <div key={ref}><strong>{ref}</strong><pre>{JSON.stringify(find(ref), null, 2)}</pre></div>)}
  </details>;
}

function SnapshotView({ snapshot, historical = false }: { snapshot: Snapshot; historical?: boolean }) {
  return <div className="ai-snapshot">
    <div className="ai-snapshot-title"><strong>{historical ? '本次分析的固定快照' : '待分析行情快照'}</strong><span>{dateTime(snapshot.as_of)}</span></div>
    <p className="ai-caption">{snapshot.instrument?.display_symbol??snapshot.symbol} · {snapshot.source} · 主周期 {LABELS[snapshot.timeframe]} · 仅已确认收盘 K 线 · 时间显示为本机时区</p>
    <div className="ai-table-wrap"><table><thead><tr><th>周期</th><th>输入 K 线</th><th>最近确认收盘</th><th>收盘价</th><th>EMA20 / 60</th><th>RSI14</th><th>MACD 柱</th></tr></thead>
      <tbody>{Object.entries(snapshot.timeframes).map(([tf, data]) => <tr key={tf} className={tf === snapshot.timeframe ? 'ai-focus-row' : ''}>
        <th>{LABELS[tf as Timeframe]}{tf === snapshot.timeframe && <small>主周期</small>}</th><td>{data.input_count} 根</td><td>{dateTime(data.last_closed_at)}</td><td>{price(data.last_close)}</td>
        <td>{price(data.latest_indicators?.ema20)} / {price(data.latest_indicators?.ema60)}</td><td>{money(data.latest_indicators?.rsi14)}</td><td>{price(data.latest_indicators?.histogram)}</td>
      </tr>)}</tbody></table></div>
    {snapshot.blocking_reasons.length > 0 && <div className="ai-warning" role="status">{snapshot.blocking_reasons.join(' ')}</div>}
    {snapshot.warnings.length > 0 && <p className="ai-caption amber">{snapshot.warnings.join(' ')}</p>}
    <details className="ai-more"><summary>查看输入数据与候选价位</summary><p>各周期最近最多 120 根确认 K 线、最近 20 组指标、报价与品种相关字段。指标使用完整缓存预热。</p>
      <div className="ai-level-chips">{snapshot.levels.map(level => <span key={level.level_ref}>{LABELS[level.timeframe]} · {level.kind} <b>{price(level.price)}</b></span>)}</div>
      <p className="ai-caption">这些价位是窗口高低点，是否形成支撑或阻力由分析结合上下文说明。</p><pre>{JSON.stringify(snapshot, null, 2)}</pre>
    </details>
  </div>;
}

function Settings({ config, onSaved }: { config: ModelSettings; onSaved: (value: ModelSettings) => void }) {
  const [baseUrl, setBaseUrl] = useState(config.base_url);
  const [model, setModel] = useState(config.model);
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [maxTokens, setMaxTokens] = useState(config.max_tokens);
  const [parameter, setParameter] = useState(config.token_parameter);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  async function save(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setMessage('');
    try {
      const result = await aiRequest<ModelSettings>('/config', { base_url: baseUrl, model, api_key: key, clear_key: clearKey, max_tokens: maxTokens, token_parameter: parameter }, 'PUT');
      onSaved(result); setKey(''); setClearKey(false);
      setMessage(result.ready ? '配置已保存在本机，尚未验证模型连接。点击生成分析时才会调用。' : '配置已保存，补齐地址、模型和密钥后即可分析。');
    } catch (error) { setMessage((error as Error).message); }
    finally { setSaving(false); }
  }
  return <form className="ai-settings-form" onSubmit={save}>
    <p className="ai-caption">支持 Chat Completions 兼容接口。保存只写入本机；点击生成分析后，行情快照与问题会发送到你配置的模型服务。</p>
    <div className="ai-settings-grid"><label>API 地址（Base URL）<input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder="https://你的服务商地址/v1" maxLength={500} autoComplete="off"/></label>
      <label>模型名称<input value={model} onChange={e => setModel(e.target.value)} placeholder="填写服务商提供的模型 ID" maxLength={160} autoComplete="off"/></label>
      <label>API Key<input type="password" value={key} onChange={e => setKey(e.target.value)} placeholder={config.has_key ? '已保存，留空保留原密钥' : '暂未配置'} maxLength={4096} autoComplete="new-password"/></label>
      <label>输出 token 上限<input type="number" value={maxTokens} onChange={e => setMaxTokens(Number(e.target.value))} min={512} max={16384} required/></label>
    </div>
    <div className="ai-settings-extra"><label>兼容参数<select value={parameter} onChange={e => setParameter(e.target.value)}><option value="max_completion_tokens">max_completion_tokens</option><option value="max_tokens">max_tokens（部分兼容服务）</option></select></label>
      {config.has_key && <label className="ai-checkbox"><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)}/>清除已保存密钥</label>}</div>
    <p className="ai-caption">密钥保存在本机受限权限文件中，不回显、不写入浏览器存储。更换 API 地址需要重新填写密钥。</p>
    <button className="ai-secondary" type="submit" disabled={saving}>{saving ? '保存中…' : '保存配置'}</button><span className="ai-save-message" role="status">{message}</span>
  </form>;
}

export function AnalysisPanel({ symbol, displaySymbol = symbol, timeframe, settingsRequest = 0 }: { symbol: string; displaySymbol?: string; timeframe: Timeframe; settingsRequest?: number }) {
  const [config, setConfig] = useState<ModelSettings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => { if (settingsRequest) setSettingsOpen(true); }, [settingsRequest]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [run, setRun] = useState<AnalysisRun | null>(null);
  const [history, setHistory] = useState<AnalysisSummary[]>([]);
  const [frameworks, setFrameworks] = useState<Framework[]>([]);
  const [frameworkId, setFrameworkId] = useState(() => { try { return localStorage.getItem('local-market-framework') || 'preset-multitimeframe'; } catch { return 'preset-multitimeframe'; } });
  const [editor, setEditor] = useState<'edit' | 'new' | 'copy' | null>(null);
  const [frameworkMessage, setFrameworkMessage] = useState('');
  const framework = frameworks.find(item => item.id === frameworkId) ?? frameworks.find(item => item.id === 'preset-multitimeframe') ?? frameworks[0];
  const [question, setQuestion] = useState(defaultQuestion(displaySymbol));
  const [followup, setFollowup] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [now, setNow] = useState(Date.now());
  const selection = useRef(0);
  const pendingRequest = useRef<{ fingerprint: string; id: string } | null>(null);
  const busy = run?.status === 'running' || history.some(item => item.status === 'running');

  async function loadHistory() { setHistory(await aiRequest<AnalysisSummary[]>('/analyses')); }
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([aiRequest<ModelSettings>('/config', undefined, 'GET', controller.signal), aiRequest<AnalysisSummary[]>('/analyses', undefined, 'GET', controller.signal), aiRequest<Framework[]>('/frameworks', undefined, 'GET', controller.signal)])
      .then(([settings, items, methods]) => { setConfig(settings); setHistory(items); setFrameworks(methods); })
      .catch(error => { if (error.name !== 'AbortError') setError('无法连接 AI 看盘服务，请刷新页面重试。'); });
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { controller.abort(); clearInterval(timer); };
  }, []);
  useEffect(() => { selection.current++; setSnapshot(null); }, [timeframe, symbol]);
  useEffect(() => { setRun(null); setQuestion(defaultQuestion(displaySymbol)); }, [symbol,displaySymbol]);
  const hasRunningHistory = history.some(item => item.status === 'running');
  useEffect(() => {
    if (!hasRunningHistory) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      aiRequest<AnalysisSummary[]>('/analyses', undefined, 'GET', controller.signal)
        .then(setHistory).catch(() => {});
    }, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [hasRunningHistory]);
  useEffect(() => {
    if (!run || run.status !== 'running') return;
    const controller = new AbortController();
    const runId = run.id;
    const poll = async () => {
      try {
        const next = await aiRequest<AnalysisRun>(`/analyses/${runId}`, undefined, 'GET', controller.signal);
        setRun(current => current?.id === runId ? next : current);
        if (next.status !== 'running') await loadHistory();
      } catch (error) { if ((error as Error).name !== 'AbortError') setError('暂时无法读取分析状态，正在重试。'); }
    };
    void poll();
    const timer = window.setInterval(poll, 2000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [run?.id, run?.status]);

  function selectFramework(id: string) {
    selection.current++; setFrameworkId(id); setSnapshot(null); setFrameworkMessage('');
    try { localStorage.setItem('local-market-framework', id); } catch { /* Selection still works without browser storage. */ }
  }
  function saveFramework(saved: Framework) {
    setFrameworks(old => [...old.filter(item => item.id !== saved.id), saved]);
    selectFramework(saved.id); setEditor(null);
    setFrameworkMessage(`已保存 ${saved.name} · v${saved.version}。读取新快照后开始分析。`);
  }

  async function capture() {
    const current = ++selection.current;
    setLoading(true); setError('');
    try {
      if (!framework) throw new Error('请先选择看盘框架。');
      const value = await aiRequest<Snapshot>('/snapshots', { timeframe, symbol, framework_id: framework.id, framework_version: framework.version });
      if (current === selection.current) { setSnapshot(value); setRun(null); }
    } catch (error) { setError((error as Error).message); }
    finally { setLoading(false); }
  }
  async function openRun(id: string) {
    const current = ++selection.current;
    setLoading(true); setError('');
    try {
      const value = await aiRequest<AnalysisRun>(`/analyses/${id}`);
      if (current === selection.current) { setRun(value); setFollowup(''); }
    } catch (error) { setError((error as Error).message); }
    finally { setLoading(false); }
  }
  async function analyze(isFollowup = false) {
    const current = selection.current;
    setLoading(true); setError('');
    const payload = { snapshot_id: isFollowup ? run?.snapshot_id : snapshot?.id,
      parent_id: isFollowup ? run?.id : null, question: (isFollowup ? followup : question).trim() };
    const fingerprint = JSON.stringify(payload);
    if (pendingRequest.current?.fingerprint !== fingerprint) {
      pendingRequest.current = { fingerprint, id: crypto.randomUUID() };
    }
    try {
      const item = await aiRequest<{ id: string }>('/analyses', { request_id: pendingRequest.current.id, ...payload });
      pendingRequest.current = null;
      if (current === selection.current) { await openRun(item.id); setFollowup(''); }
      await loadHistory();
    } catch (error) { setError((error as Error).message); void loadHistory().catch(() => {}); }
    finally { setLoading(false); }
  }
  async function cancel() {
    if (!run) return;
    setLoading(true); setError('');
    try { const value = await aiRequest<AnalysisRun>(`/analyses/${run.id}/cancel`, {}); setRun(current => current?.id === value.id ? value : current); await loadHistory(); }
    catch (error) { setError((error as Error).message); }
    finally { setLoading(false); }
  }
  const expired = snapshot ? now - snapshot.as_of > 120_000 : false;
  const activeSnapshot = run?.snapshot ?? snapshot;
  const result = run?.result;
  const frameworkMatches = !!framework && snapshot?.framework?.id === framework.id && snapshot?.framework?.version === framework.version;

  return <section id="ai-analysis" className="ai-panel" aria-label="AI 单品种看盘">
    <div className="ai-heading"><div><div className="eyebrow">AI MARKET ANALYSIS</div><h2>AI 单品种看盘 <span>{run?.snapshot.instrument?.display_symbol ?? run?.snapshot.symbol ?? displaySymbol}</span></h2><p>从多周期数据出发，让每一次判断有据可查。</p></div>
      <div className="ai-heading-actions"><span className={config?.ready ? 'positive' : 'amber'}>{config ? config.ready ? '模型已配置' : '待配置 API' : '正在读取配置'}</span><button className="ai-secondary" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(v => !v)}>{settingsOpen ? '收起模型配置' : '模型配置'}</button></div>
    </div>
    {settingsOpen && config && <Settings config={config} onSaved={setConfig}/>}
    {editor && framework && <FrameworkEditor framework={framework} mode={editor} onSaved={saveFramework} onClose={() => setEditor(null)}/>}
    <div className="ai-layout"><nav className="ai-history" aria-label="分析记录"><div className="ai-history-heading"><h3>分析记录</h3><button className="ai-text-button" disabled={loading || busy} onClick={() => { selection.current++; setRun(null); setError(''); }}>新分析 ＋</button></div>
      {history.length === 0 ? <div className="ai-history-empty"><span>◷</span><p>还没有分析记录</p><small>生成后的分析、问题与行情快照会自动保存在本机。</small></div> : <div className="ai-history-list">{history.map(item => <button key={item.id} className={`ai-history-item ${run?.id === item.id ? 'selected' : ''}`} disabled={loading} onClick={() => openRun(item.id)}>
        <span><strong>{item.symbol ?? 'BTC'} · {LABELS[item.timeframe]}{item.parent_id ? ' · 追问' : ''}</strong><small className={item.status === 'completed' ? 'positive' : item.status === 'failed' ? 'negative' : 'amber'}>{STATUS[item.status]}</small></span>
        <p>{item.question}</p><time>{dateTime(item.created_at)}</time>
      </button>)}</div>}
    </nav><div className="ai-main">
      {error && <div className="ai-warning" role="alert">{error}</div>}
      {!run && <div className="framework-picker"><label htmlFor="ai-framework">看盘框架</label><select id="ai-framework" disabled={loading || busy || !framework} value={framework?.id ?? ''} onChange={e => selectFramework(e.target.value)}>{!framework && <option value="">正在读取框架…</option>}{frameworks.map(item => <option key={item.id} value={item.id}>{item.name} · v{item.version}</option>)}</select>
        <div className="framework-picker-actions"><button disabled={loading || busy || !framework} onClick={() => setEditor('edit')}>编辑框架</button><button disabled={loading || busy || !framework} onClick={() => setEditor('new')}>新建框架</button><button disabled={loading || busy || !framework} onClick={() => setEditor('copy')}>复制框架</button></div>
        {framework && <details><summary>{framework.steps.length} 个检查步骤 · 查看当前方法</summary><FrameworkDetails framework={framework}/></details>}
        {frameworkMessage && <p role="status">{frameworkMessage}</p>}
      </div>}
      {!run && <><div className="ai-compose-top"><div><strong>01 <span>准备行情</span></strong><p>主周期跟随图表：<b>{LABELS[timeframe]}</b>，{framework?.timeframes.length ? '搭配框架指定的关注周期。' : '自动搭配相关高低周期。'}</p></div><button className="ai-secondary" disabled={loading || busy || !framework} onClick={capture}>{loading ? '处理中…' : snapshot ? '更新行情快照' : '读取行情快照'}</button></div>
        {!snapshot && <div className="ai-input-empty"><span>≋</span><div><strong>先读取当前 {displaySymbol} 行情</strong><p>预览 K 线、指标和数据时间。这一步只处理行情，不调用模型。</p></div></div>}</>}
      {activeSnapshot && <SnapshotView snapshot={activeSnapshot} historical={!!run}/>}
      {!run && <form className="ai-compose" onSubmit={event => { event.preventDefault(); void analyze(); }}><label htmlFor="ai-question"><strong>02 <span>你的看盘问题</span></strong></label><textarea id="ai-question" rows={3} value={question} maxLength={2000} onChange={e => setQuestion(e.target.value)}/>
        <div className="ai-submit-row"><p>{!config?.ready ? 'API 暂未配置，行情预览已可用。' : expired ? '快照已超过两分钟，请更新后再分析。' : !snapshot || !frameworkMatches ? '读取当前框架的行情快照后即可开始。' : !snapshot.ready ? '请先等待行情同步完成，再更新快照。' : `将通过 ${config.model} 按「${framework?.name}」v${framework?.version} 分析，完成后自动保存。`}</p>
          <button className="ai-primary" type="submit" disabled={loading || busy || !config?.ready || !snapshot?.ready || !frameworkMatches || expired || !question.trim() || snapshot.timeframe !== timeframe || snapshot.symbol !== symbol}>{busy ? '已有分析进行中' : !config?.ready ? '待配置 API' : '生成 AI 分析 ↗'}</button></div>
      </form>}
      {run && <><div className="ai-run-meta"><span>{STATUS[run.status]} · {run.model} · {dateTime(run.created_at)}</span><a href={`/api/v1/ai/analyses/${run.id}/export`} download>导出完整记录 ↓</a></div><p className="ai-user-question"><b>你的问题</b>{run.question}</p>
        {run.framework ? <details className="framework-frozen"><summary>本次框架：{run.framework.name} · v{run.framework.version}</summary><FrameworkDetails framework={run.framework}/><p className="ai-caption">已固定在这条记录中，追问继续沿用此版本。</p></details> : <p className="ai-caption">本次使用旧版默认多周期方法，未绑定自定义框架。</p>}
        {run.status === 'running' && <div className="ai-progress" role="status"><span className="loading-orbit"/><div><strong>{run.stage}</strong><p>读取同一份行情快照；结果检查完成后显示。</p></div><button className="ai-secondary" onClick={cancel} disabled={loading}>取消分析</button></div>}
        {run.error && <div className="ai-warning" role="status">{run.error}</div>}
        {result && <article className="ai-result"><div className="ai-result-title"><span>{REGIME[result.market_regime]}</span><small>基于 {dateTime(run.snapshot.as_of)} 的快照</small></div><p className="ai-thesis">{result.thesis}</p>
          {result.framework_checks && <><h3>按框架逐项检查</h3><p className="ai-caption">以下为 AI 对条件的判断；证据可展开核对。</p><div className="framework-checks">{result.framework_checks.map((check, index) => <section className={`framework-check ${check.status}`} key={check.step_id}><header><strong>{index + 1}. {check.title}</strong><span className={check.status === 'met' ? 'positive' : check.status === 'not_met' ? 'negative' : 'amber'}>{{ met: '满足', not_met: '未满足', insufficient_data: '数据不足' }[check.status]}</span></header><small>{check.condition}</small><p>{check.explanation}</p>{check.evidence_refs.length > 0 && <Evidence refs={check.evidence_refs} snapshot={run.snapshot}/>}</section>)}</div></>}
          <h3>多周期观察</h3><div className="ai-observations">{result.observations.map((item, index) => <div key={index}><p>{item.text}</p><Evidence refs={item.evidence_refs} snapshot={run.snapshot}/></div>)}</div>
          {result.key_levels.length > 0 && <><h3>关键价位</h3><div className="ai-levels">{result.key_levels.map((level, index) => <div key={index}><strong>{price(level.price)}</strong><small>{LABELS[level.timeframe]} · {level.kind}</small><p>{level.rationale}</p></div>)}</div></>}
          <h3>多空情景</h3><div className="ai-scenarios">{result.scenarios.map(item => <div key={item.direction}><strong className={item.direction === 'bullish' ? 'positive' : 'negative'}>{item.direction === 'bullish' ? '偏多情景' : '偏空情景'}</strong><p><b>观察条件</b>{item.trigger}</p><p><b>失效条件</b>{item.invalidation}</p><Evidence refs={item.evidence_refs} snapshot={run.snapshot}/></div>)}</div>
          <h3>不确定性与数据局限</h3><ul>{result.risks.map((risk, index) => <li key={index}>{risk}</li>)}</ul><p className="ai-caption">已检查结果格式、证据引用和关键价位来源；模型的文字推断仍需结合图表判断。</p>
        </article>}
        {run.status === 'failed' && run.raw_output && <details className="ai-more"><summary>查看未通过检查的模型原文</summary><pre>{run.raw_output}</pre></details>}
        {run.status === 'completed' && <form className="ai-followup" onSubmit={event => { event.preventDefault(); void analyze(true); }}><label htmlFor="ai-followup">继续追问</label><p className="ai-caption">沿用上方固定快照。要看最新行情，点击“新分析”并重新读取行情。</p>
          {result && <div className="ai-suggestions">{result.follow_up_questions.map((q, index) => <button key={index} type="button" onClick={() => setFollowup(q)}>{q}</button>)}</div>}
          <textarea id="ai-followup" rows={2} value={followup} onChange={e => setFollowup(e.target.value)} maxLength={2000} placeholder="例如：大周期与小周期的方向有哪些冲突？"/>
          <div className="ai-submit-row"><p>{run.usage.total_tokens ? `本次调用累计 ${run.usage.total_tokens.toLocaleString()} tokens` : '每次追问都会保存为一条关联记录。'}</p><button className="ai-primary" disabled={loading || busy || !config?.ready || !followup.trim()}>发送追问 ↗</button></div>
        </form>}
      </>}
    </div></div>
  </section>;
}
