import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { LABELS, TIMEFRAMES, TIMEFRAME_GROUPS, type Timeframe } from './types';

export function TimeframePicker({ value, onChange }: { value: Timeframe; onChange: (value: Timeframe) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<Partial<Record<Timeframe, HTMLButtonElement | null>>>({});

  useEffect(() => {
    if (!open) return;
    items.current[value]?.focus({ preventScroll: true });
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', dismiss);
    return () => document.removeEventListener('pointerdown', dismiss);
  }, [open, value]);

  function choose(tf: Timeframe) {
    onChange(tf);
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  }

  function move(event: KeyboardEvent, tf: Timeframe) {
    const index = TIMEFRAMES.indexOf(tf);
    const next = event.key === 'ArrowDown' ? (index + 1) % TIMEFRAMES.length
      : event.key === 'ArrowUp' ? (index + TIMEFRAMES.length - 1) % TIMEFRAMES.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? TIMEFRAMES.length - 1 : null;
    if (next !== null) { event.preventDefault(); items.current[TIMEFRAMES[next]]?.focus(); }
  }

  return <div className="period-picker" ref={root}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}
    onKeyDown={event => {
      if (event.key === 'Escape' && open) { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
    }}>
    <button ref={trigger} className="period-trigger" aria-label={`图表周期：${LABELS[value]}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? 'timeframe-menu' : undefined}
      onClick={() => setOpen(old => !old)}
      onKeyDown={event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true); }
      }}>
      <span>{LABELS[value]}</span><span className="period-hint">切换周期</span>
    </button>
    {open && <div id="timeframe-menu" className="period-menu" role="menu" aria-label="选择图表周期">
      {TIMEFRAME_GROUPS.map(group => <div key={group.label} role="group" aria-label={group.label} className="period-group">
        <div className="period-group-label" aria-hidden="true">{group.label}</div>
        {group.values.map(tf => <button key={tf} type="button" role="menuitemradio" aria-checked={tf === value}
          tabIndex={tf === value ? 0 : -1} ref={node => { items.current[tf] = node; }}
          onKeyDown={event => move(event, tf)} onClick={() => choose(tf)}>
          <span>{LABELS[tf]}</span>{tf === value && <small>当前</small>}
        </button>)}
      </div>)}
    </div>}
  </div>;
}
