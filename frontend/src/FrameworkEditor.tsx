import { useEffect, useRef, useState } from 'react';
import { aiRequest, dateTime, type Framework, type FrameworkDraft } from './aiTypes';
import { LABELS, type Timeframe } from './types';
import './frameworks.css';

export const INDICATORS: Record<string, string> = { ema20: 'EMA 20', ema60: 'EMA 60', rsi14: 'RSI 14', macd: 'MACD', volume: '成交量' };
export function FrameworkDetails({ framework }: { framework: Framework }) {
  return <div className="framework-details"><strong>{framework.name} · v{framework.version}</strong>
    <p>{framework.description || '自定义分析方法'}</p>
    <p>关注周期：{framework.timeframes.length ? framework.timeframes.map(tf => LABELS[tf]).join(' / ') + '，并包含图表主周期' : '跟随图表自动搭配'}</p>
    <p>重点指标：{framework.indicators.map(i => INDICATORS[i] ?? i).join(' / ') || '不限'}</p>
    <ol>{framework.steps.map(step => <li key={step.id}><b>{step.title}</b><p>{step.condition}</p></li>)}</ol>
    <p>输出偏好：{framework.output_style || '简洁说明判断及依据'}</p>
  </div>;
}
function copyDraft(value: FrameworkDraft): FrameworkDraft {
  return { name: value.name, description: value.description, timeframes: [...value.timeframes], indicators: [...value.indicators],
    steps: value.steps.map(step => ({ ...step })), output_style: value.output_style };
}
const fresh = (): FrameworkDraft => ({ name: '', description: '', timeframes: [], indicators: ['ema20', 'ema60', 'rsi14', 'macd', 'volume'],
  steps: [{ id: crypto.randomUUID(), title: '趋势方向', condition: '' }], output_style: '简洁中文，先给结论，再说明依据、分歧与失效条件。' });

export function FrameworkEditor({ framework, mode, onSaved, onClose }: {
  framework: Framework; mode: 'edit' | 'new' | 'copy'; onSaved: (framework: Framework) => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState<FrameworkDraft>(() => mode === 'new' ? fresh() : { ...copyDraft(framework), name: framework.name + (mode === 'copy' ? ' · 副本' : '') });
  const [versions, setVersions] = useState<Framework[]>([]);
  const [preview, setPreview] = useState<Framework | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { const modal = dialog.current; modal?.showModal(); return () => modal?.close(); }, []);
  useEffect(() => {
    if (mode !== 'edit') return;
    const controller = new AbortController();
    aiRequest<Framework[]>(`/frameworks/${framework.id}/versions`, undefined, 'GET', controller.signal).then(setVersions)
      .catch(e => { if (e.name !== 'AbortError') setError('暂时无法加载版本记录。'); });
    return () => controller.abort();
  }, [framework.id, mode]);
  function move(index: number, direction: number) {
    setDraft(old => { const steps = [...old.steps]; [steps[index], steps[index + direction]] = [steps[index + direction], steps[index]]; return { ...old, steps }; });
  }
  async function save(event: React.FormEvent) {
    event.preventDefault(); setError('');
    if (draft.timeframes.length === 1 || draft.timeframes.length > 4) { setError('请选择 2–4 个关注周期，或全部取消以跟随图表自动搭配。'); return; }
    if (!draft.name.trim() || draft.steps.some(step => !step.title.trim() || !step.condition.trim())) { setError('请填写框架名称及每个步骤的标题、判断条件。'); return; }
    setSaving(true);
    try {
      const saved = await aiRequest<Framework>(mode === 'edit' ? `/frameworks/${framework.id}` : '/frameworks',
        mode === 'edit' ? { ...draft, expected_version: framework.version } : draft, mode === 'edit' ? 'PUT' : 'POST');
      onSaved(saved);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  }
  return <dialog ref={dialog} className="framework-dialog" aria-labelledby="framework-heading" onCancel={e => { e.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={save}>
      <header><div><small>自定义看盘框架</small><h2 id="framework-heading">{mode === 'edit' ? `编辑 ${framework.name}` : mode === 'copy' ? '复制为新框架' : '创建看盘框架'}</h2></div><button type="button" className="framework-close" onClick={onClose} disabled={saving} aria-label="关闭框架编辑器">×</button></header>
      <div className="framework-editor-body">
        {mode === 'edit' && <label className="framework-version-picker">版本记录<select aria-label="查看框架版本" value={preview?.version ?? ''} onChange={e => setPreview(versions.find(v => v.version === Number(e.target.value)) ?? null)}><option value="">当前草稿 · 保存为 v{framework.version + 1}</option>{versions.map(v => <option value={v.version} key={v.version}>v{v.version} · {dateTime(v.created_at)}</option>)}</select></label>}
        {preview ? <><p className="framework-hint">正在查看已保存版本，内容不会被修改。</p><FrameworkDetails framework={preview}/><button className="framework-secondary" type="button" onClick={() => { setDraft(copyDraft(preview)); setPreview(null); }}>以此版本继续编辑</button></> : <fieldset disabled={saving} className="framework-fields">
          <div className="framework-basics"><label>框架名称<input autoFocus required maxLength={80} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} placeholder="例如：我的趋势回踩方法"/></label>
            <label>方法说明<textarea rows={2} maxLength={600} value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} placeholder="适合什么行情，重点观察什么"/></label></div>
          <fieldset><legend>关注周期</legend><p className="framework-hint">留空时跟随图表自动搭配；自选 2–4 个周期时，会同时包含图表主周期。</p><div className="framework-options">{(Object.keys(LABELS) as Timeframe[]).map(tf => <label key={tf}><input type="checkbox" checked={draft.timeframes.includes(tf)} onChange={e => setDraft({ ...draft, timeframes: e.target.checked ? [...draft.timeframes, tf] : draft.timeframes.filter(t => t !== tf) })}/>{LABELS[tf]}</label>)}</div></fieldset>
          <fieldset><legend>重点指标</legend><div className="framework-options">{Object.entries(INDICATORS).map(([key, label]) => <label key={key}><input type="checkbox" checked={draft.indicators.includes(key)} onChange={e => setDraft({ ...draft, indicators: e.target.checked ? [...draft.indicators, key] : draft.indicators.filter(i => i !== key) })}/>{label}</label>)}</div><p className="framework-hint">使用现有指标参数。规则涉及未提供的指标或数据时，AI 应标记“数据不足”。</p></fieldset>
          <div className="framework-step-heading"><h3>按顺序检查</h3><span>{draft.steps.length} / 12 项</span></div>
          <div className="framework-steps">{draft.steps.map((step, index) => <section key={step.id} className="framework-step"><div className="framework-step-actions"><b>{String(index + 1).padStart(2, '0')}</b><button type="button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`步骤 ${index + 1} 上移`}>↑</button><button type="button" disabled={index === draft.steps.length - 1} onClick={() => move(index, 1)} aria-label={`步骤 ${index + 1} 下移`}>↓</button><button type="button" disabled={draft.steps.length === 1} onClick={() => setDraft({ ...draft, steps: draft.steps.filter(s => s.id !== step.id) })} aria-label={`删除步骤 ${index + 1}`}>移除</button></div>
            <label>步骤 {index + 1} 标题<input aria-label={`步骤 ${index + 1} 标题`} required maxLength={80} value={step.title} onChange={e => setDraft({ ...draft, steps: draft.steps.map(s => s.id === step.id ? { ...s, title: e.target.value } : s) })}/></label>
            <label>步骤 {index + 1} 判断条件<textarea aria-label={`步骤 ${index + 1} 判断条件`} required rows={3} maxLength={1200} value={step.condition} placeholder="写清满足条件，例如：1 小时收盘价高于 EMA20，且 EMA20 高于 EMA60" onChange={e => setDraft({ ...draft, steps: draft.steps.map(s => s.id === step.id ? { ...s, condition: e.target.value } : s) })}/></label>
          </section>)}</div>
          <button type="button" className="framework-secondary" disabled={draft.steps.length >= 12} onClick={() => setDraft({ ...draft, steps: [...draft.steps, { id: crypto.randomUUID(), title: '', condition: '' }] })}>＋ 添加检查步骤</button>
          <label className="framework-output">输出偏好<textarea rows={2} maxLength={1200} value={draft.output_style} onChange={e => setDraft({ ...draft, output_style: e.target.value })}/></label>
        </fieldset>}
      </div>
      <footer>{error && <p className="framework-error" role="alert">{error}</p>}<p>保存后用于新的分析；历史分析和追问保留原版本。</p><div><button type="button" className="framework-secondary" disabled={saving} onClick={onClose}>取消编辑</button><button className="framework-primary" type="submit" disabled={saving || !!preview}>{saving ? '保存中…' : mode === 'edit' ? `保存为 v${framework.version + 1}` : '保存框架'}</button></div></footer>
    </form>
  </dialog>;
}
