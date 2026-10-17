export type Level = { px: string; sz: string };
export type Trade = Level & { coin: string; side: string; time: number; tid: number };
export type Book = { levels: Level[][]; sourceTime: number; receivedAt: number; intervalMs: number | null; sequence: number };
type Socket = Pick<WebSocket, 'readyState' | 'onopen' | 'onmessage' | 'onerror' | 'onclose' | 'send' | 'close'>;
type Runtime = {
  socket: () => Socket; now: () => number;
  every: (callback: () => void, ms: number) => number;
  later: (callback: () => void, ms: number) => number;
  clearEvery: (id: number) => void; clearLater: (id: number) => void;
};
type Sink = { book: (book: Book) => void; trades: (trades: Trade[]) => void; status: (message: string) => void };
const runtime: Runtime = {
  socket: () => new WebSocket('wss://api.hyperliquid.xyz/ws'), now: Date.now,
  every: (fn, ms) => window.setInterval(fn, ms), later: (fn, ms) => window.setTimeout(fn, ms),
  clearEvery: id => window.clearInterval(id), clearLater: id => window.clearTimeout(id),
};

export function validBook(data: unknown, symbol: string, fast: boolean): data is { coin: string; time: number; levels: Level[][] } {
  if (!data || typeof data !== 'object') return false;
  const d = data as { coin?: string; time?: number; levels?: Level[][] };
  return d.coin === symbol && Number.isFinite(d.time) && (d.time ?? 0) > 0 && Array.isArray(d.levels) && d.levels.length === 2
    && d.levels.every(side => Array.isArray(side) && side.length <= (fast ? 5 : 20) && side.every(level =>
      typeof level?.px === 'string' && typeof level.sz === 'string' && Number.isFinite(+level.px)
      && +level.px > 0 && Number.isFinite(+level.sz) && +level.sz >= 0));
}

/** Each complete snapshot replaces the book; fast and deep data never mix. */
export function openOrderBook(symbol: string, fast: boolean, sink: Sink, env: Runtime = runtime) {
  let stopped = false, current: Socket | null = null, retry: number | undefined;
  let failures = 0, sequence = 0, sourceTime = 0;
  const schedule = () => {
    if (stopped || retry !== undefined) return;
    retry = env.later(() => { retry = undefined; connect(); }, Math.min(15_000, 1000 * 2 ** Math.min(failures++, 4)));
  };
  const detach = (socket: Socket) => {
    socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
    socket.close();
  };
  let openedAt = 0, lastBook = 0, lastPing = 0;
  let intervals: number[] = [];
  const reconnect = (message: string) => {
    if (stopped) return;
    sink.status(message);
    if (current) { const old = current; current = null; detach(old); }
    schedule();
  };
  function connect() {
    if (stopped) return;
    sink.status('正在连接 Hyperliquid…');
    openedAt = env.now(); lastBook = 0; lastPing = 0; intervals = [];
    let socket: Socket;
    try { socket = env.socket(); current = socket; }
    catch { reconnect('连接失败，正在重连'); return; }
    const active = () => !stopped && current === socket;
    socket.onopen = () => {
      if (!active()) return;
      lastPing = env.now();
      try {
        socket.send(JSON.stringify({ method: 'subscribe', subscription: { type: 'l2Book', coin: symbol, fast } }));
        socket.send(JSON.stringify({ method: 'subscribe', subscription: { type: 'trades', coin: symbol } }));
      } catch { reconnect('订阅发送失败，正在重连'); }
    };
    socket.onmessage = event => {
      if (!active()) return;
      try {
        const message = JSON.parse(String(event.data));
        if (message.channel === 'error') { reconnect('盘口订阅失败，正在重连'); return; }
        if (message.channel === 'l2Book' && validBook(message.data, symbol, fast)) {
          const data = message.data;
          // Duplicates and delayed frames cannot make frozen data look fresh.
          if (data.time <= sourceTime) return;
          const receivedAt = env.now();
          if (lastBook) intervals = [...intervals.slice(-8), receivedAt - lastBook];
          lastBook = receivedAt; sourceTime = data.time; failures = 0;
          const sorted = [...intervals].sort((a, b) => a - b);
          sink.status('');
          sink.book({ levels: data.levels, sourceTime, receivedAt, sequence: ++sequence,
            intervalMs: sorted.length ? sorted[Math.floor(sorted.length / 2)] : null });
        } else if (message.channel === 'trades' && Array.isArray(message.data)) {
          const trades = message.data.filter((t: Trade) => t.coin === symbol && Number.isFinite(t.time) && Number.isFinite(t.tid)
            && Number.isFinite(+t.px) && +t.px > 0 && Number.isFinite(+t.sz) && +t.sz >= 0);
          if (trades.length) sink.trades(trades);
        }
      } catch { /* The watchdog still checks progress after malformed frames. */ }
    };
    socket.onerror = () => { if (active()) reconnect('连接异常，正在重连'); };
    socket.onclose = () => { if (active()) reconnect('连接中断，正在重连'); };
  }
  connect();
  const timer = env.every(() => {
    if (stopped || !current) return;
    const now = env.now();
    // Pongs and trades do not count as progress of the order book itself.
    if (now - (lastBook || openedAt) > (lastBook ? fast ? 8000 : 20_000 : 12_000)) {
      reconnect('盘口推送停滞，正在重连'); return;
    }
    if (current.readyState === 1 && now - lastPing >= 20_000) {
      try { current.send(JSON.stringify({ method: 'ping' })); lastPing = now; }
      catch { reconnect('心跳发送失败，正在重连'); }
    }
  }, 500);
  return () => {
    stopped = true;
    env.clearEvery(timer);
    if (retry !== undefined) env.clearLater(retry);
    if (current) { const old = current; current = null; detach(old); }
  };
}
