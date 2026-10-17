import { useCallback, useEffect, useRef, useState } from 'react';
import { normalizeInstrumentSearch } from './instrumentSearch';

const STORAGE_KEY = 'local-market:market-search-history:v1';
const LIMIT = 10;

function readHistory(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    if (!Array.isArray(saved)) return [];
    const seen = new Set<string>();
    return saved.filter((item): item is string => {
      if (typeof item !== 'string' || !item.trim() || item.length > 40) return false;
      const key = normalizeInstrumentSearch(item);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, LIMIT).map(item => item.trim());
  } catch { return []; }
}

export function useSearchHistory(query: string) {
  const [items, setItems] = useState(readHistory);
  const latest = useRef(items);
  const pending = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cancelPending = useCallback(() => { clearTimeout(pending.current); }, []);
  const save = useCallback((next: string[]) => {
    latest.current = next;
    // Persist before opening a result unmounts the market dialog.
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); }
    catch { /* Search stays usable when browser storage is unavailable. */ }
    setItems(next);
  }, []);
  const remember = useCallback((query: string) => {
    cancelPending();
    const value = query.trim().slice(0, 40);
    if (!value) return;
    const key = normalizeInstrumentSearch(value);
    save([value, ...latest.current.filter(item => normalizeInstrumentSearch(item) !== key)].slice(0, LIMIT));
  }, [cancelPending, save]);

  useEffect(() => {
    if (query.trim()) pending.current = setTimeout(() => remember(query), 1200);
    return cancelPending;
  }, [query, remember, cancelPending]);

  function remove(query: string) {
    cancelPending();
    save(latest.current.filter(item => item !== query));
  }
  function clear() {
    cancelPending();
    save([]);
  }
  return { items, remember, remove, clear };
}
