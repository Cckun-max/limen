import test from 'node:test';
import assert from 'node:assert/strict';
import { openOrderBook } from '../src/orderBookStream.ts';

function harness(fast = true) {
  let time = 100000, next = 0;
  const tasks = new Map(), sockets = [], books = [], statuses = [], trades = [];
  const add = (fn, ms, period = 0) => { const id = ++next; tasks.set(id, { fn, at: time + ms, period }); return id; };
  const env = {
    now: () => time,
    later: (fn, ms) => add(fn, ms), every: (fn, ms) => add(fn, ms, ms),
    clearLater: id => tasks.delete(id), clearEvery: id => tasks.delete(id),
    socket: () => {
      const socket = { readyState: 0, closed: false, sent: [], onopen: null, onmessage: null, onerror: null, onclose: null,
        send(data) { this.sent.push(JSON.parse(data)); }, close() { this.closed = true; this.readyState = 3; this.onclose?.({}); },
        open() { this.readyState = 1; this.onopen?.({}); }, emit(data) { this.onmessage?.({ data: JSON.stringify(data) }); } };
      sockets.push(socket); return socket;
    },
  };
  const stop = openOrderBook('DOGE', fast, { book: x => books.push(x), trades: x => trades.push(x), status: x => statuses.push(x) }, env);
  const tick = ms => {
    const end = time + ms;
    while (true) {
      const due = [...tasks].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, t] = due; time = t.at;
      if (t.period) t.at += t.period; else tasks.delete(id);
      t.fn();
    }
    time = end;
  };
  return { sockets, books, trades, statuses, stop, tick, tasks, now: () => time };
}
function book(time, levels = 5, coin = 'DOGE') {
  return { channel: 'l2Book', data: { coin, time, levels: [
    Array.from({ length: levels }, (_, i) => ({ px: String(.08 - i * .000001), sz: '1234' })),
    Array.from({ length: levels }, (_, i) => ({ px: String(.081 + i * .000001), sz: '5678' })),
  ] } };
}

test('explicit fast subscription applies each snapshot immediately and measures received cadence', () => {
  const h = harness(), s = h.sockets[0]; s.open();
  assert.deepEqual(s.sent[0].subscription, { type: 'l2Book', coin: 'DOGE', fast: true });
  s.emit(book(h.now())); assert.equal(h.books.length, 1);
  h.tick(530); s.emit(book(h.now()));
  assert.equal(h.books.at(-1).intervalMs, 530);
  assert.equal(h.books.at(-1).sequence, 2);
  h.stop();
});

test('depth mode replaces all 20 levels; fast mode rejects deep and wrong-symbol snapshots', () => {
  const h = harness(false), s = h.sockets[0]; s.open();
  assert.equal(s.sent[0].subscription.fast, false);
  s.emit(book(h.now(), 20)); assert.equal(h.books[0].levels[0].length, 20);
  h.tick(5400); s.emit(book(h.now(), 1)); assert.equal(h.books.at(-1).levels[0].length, 1);
  h.stop();
  const f = harness(); f.sockets[0].open();
  f.sockets[0].emit(book(f.now(), 20)); f.sockets[0].emit(book(f.now(), 5, 'BTC'));
  assert.equal(f.books.length, 0); f.stop();
});

test('pongs and duplicates do not hide a silent order book; reconnect and resubscribe', () => {
  const h = harness(), s = h.sockets[0]; s.open(); s.emit(book(h.now()));
  const delayed = s.onmessage, original = h.now();
  h.tick(4000); s.emit({ channel: 'pong' }); s.emit(book(original));
  h.tick(4500);
  assert.equal(s.closed, true); assert.ok(h.statuses.at(-1).includes('停滞'));
  h.tick(1000); assert.equal(h.sockets.length, 2);
  const current = h.sockets[1]; current.open();
  assert.equal(current.sent[0].subscription.fast, true);
  delayed({ data: JSON.stringify(book(h.now())) }); assert.equal(h.books.length, 1);
  current.emit(book(h.now())); assert.equal(h.books.length, 2);
  h.stop();
});

test('deep feed tolerates 5 second snapshots; older timestamps never refresh freshness', () => {
  const h = harness(false), s = h.sockets[0]; s.open(); s.emit(book(h.now(), 20));
  const first = h.now();
  h.tick(5500); s.emit(book(h.now(), 20));
  h.tick(5500); s.emit(book(h.now(), 20));
  const received = h.books.at(-1).receivedAt;
  s.emit(book(first, 20));
  assert.equal(h.books.at(-1).receivedAt, received);
  assert.equal(s.closed, false);
  h.stop();
});

test('no initial snapshot triggers recovery; disposing clears all timers and old callbacks', () => {
  const h = harness(), s = h.sockets[0]; s.open();
  const delayed = s.onmessage;
  h.tick(12500); assert.equal(s.closed, true);
  h.stop(); h.tick(60000);
  delayed({ data: JSON.stringify(book(h.now())) });
  assert.equal(h.books.length, 0); assert.equal(h.sockets.length, 1); assert.equal(h.tasks.size, 0);
});

test('malformed books do not replace good data and trades remain symbol-scoped', () => {
  const h = harness(), s = h.sockets[0]; s.open(); s.emit(book(h.now()));
  const bad = book(h.now() + 1); bad.data.levels[0][0].px = 'NaN'; s.emit(bad);
  assert.equal(h.books.length, 1);
  const trade = { coin: 'DOGE', px: '.08', sz: '1', time: h.now(), tid: 3, side: 'B' };
  s.emit({ channel: 'trades', data: [trade, { ...trade, coin: 'BTC' }] });
  assert.deepEqual(h.trades, [[trade]]); h.stop();
});
