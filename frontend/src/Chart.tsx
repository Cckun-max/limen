import { useEffect, useRef, useState } from 'react';
import {
  createChart, CandlestickSeries, LineSeries, HistogramSeries, ColorType, CrosshairMode,
  LineStyle, type IChartApi, type ISeriesApi, type UTCTimestamp,
} from 'lightweight-charts';
import { money, price, priceDigits, type ChartData, type Indicator, type Timeframe, type Visibility } from './types';

const seconds = (n: number) => n / 1000 as UTCTimestamp;
const COLORS = { up: '#64d6a0', down: '#ec536e', ema20: '#dfb76c', ema60: '#9c9bdf', rsi: '#a9a7e9', dif: '#79b6dc', signal: '#dfb76c' };
type Lines = Partial<Record<keyof Indicator, ISeriesApi<'Line'>>>;

export function MarketChart({ symbol, data, timeframe, visible, resetKey }: {
  symbol: string; data?: ChartData; timeframe: Timeframe; visible: Visibility; resetKey: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const api = useRef<IChartApi | null>(null);
  const candle = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null);
  const histogram = useRef<ISeriesApi<'Histogram'> | null>(null);
  const lines = useRef<Lines>({});
  const current = useRef(data);
  const initial = useRef(true);
  const count = useRef(0);
  const [hover, setHover] = useState<number | null>(null);
  current.current = data;
  const extraHeight = (visible.rsi ? 105 : 0) + (visible.macd ? 110 : 0);

  useEffect(() => {
    if (!container.current) return;
    const chart = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#121212' }, textColor: '#969696',
        fontFamily: 'Arial, sans-serif', fontSize: 11,
        attributionLogo: true, panes: { separatorColor: '#292929', separatorHoverColor: '#485867', enableResize: true } },
      grid: { vertLines: { color: '#202020' }, horzLines: { color: '#202020' } },
      rightPriceScale: { borderColor: '#292929', minimumWidth: 74 },
      timeScale: { borderColor: '#292929', timeVisible: !['1d', '1w'].includes(timeframe), secondsVisible: false, rightOffset: 7, barSpacing: 7,
        tickMarkFormatter: (time: number, tickType: number) => new Date(time * 1000).toLocaleString('zh-CN', ['1d', '1w'].includes(timeframe) || tickType < 3
          ? { month: '2-digit', day: '2-digit' }
          : { hour: '2-digit', minute: '2-digit', hour12: false }) },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: '#617382', labelBackgroundColor: '#324653' }, horzLine: { color: '#617382', labelBackgroundColor: '#324653' } },
      localization: { locale: 'zh-CN', timeFormatter: (time: number) => new Date(time * 1000).toLocaleString('zh-CN', { hour12: false }) },
    });
    api.current = chart;
    candle.current = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.up, downColor: COLORS.down, borderVisible: false,
      wickUpColor: COLORS.up, wickDownColor: COLORS.down, priceFormat: { type: 'price', precision: 6, minMove: 0.000001 },
    });
    lines.current = {};
    for (const key of ['ema20', 'ema60'] as const) {
      lines.current[key] = chart.addSeries(LineSeries, { color: COLORS[key], lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    }
    volume.current = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1);
    let paneIndex = 2;
    if (visible.rsi) {
      const rsi = chart.addSeries(LineSeries, { color: COLORS.rsi, lineWidth: 1, priceLineVisible: false,
        priceFormat: { type: 'price', precision: 1, minMove: 0.1 }, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) }, paneIndex++);
      for (const price of [30, 70]) rsi.createPriceLine({ price, color: '#435066', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
      lines.current.rsi14 = rsi;
    }
    histogram.current = null;
    if (visible.macd) {
      histogram.current = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, paneIndex);
      lines.current.macd = chart.addSeries(LineSeries, { color: COLORS.dif, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, paneIndex);
      lines.current.signal = chart.addSeries(LineSeries, { color: COLORS.signal, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, paneIndex);
    }
    // Ratios survive the initial ResizeObserver callback; sequential setHeight calls do not.
    chart.panes()[0].setStretchFactor(3.5);
    chart.panes()[1].setStretchFactor(0.65);
    for (let i = 2; i < chart.panes().length; i++) chart.panes()[i].setStretchFactor(1.05);
    chart.subscribeCrosshairMove(event => setHover(typeof event.time === 'number' ? event.time * 1000 : null));
    initial.current = true;
    count.current = 0;
    return () => { chart.remove(); api.current = null; };
  }, [timeframe, visible.rsi, visible.macd]);

  useEffect(() => {
    if (!api.current || !candle.current || !data?.bars.length) return;
    const chart = api.current;
    const tick = data.instrument?.tick_size;
    const digits = tick && tick>0 ? Math.min(10,Math.max(0,Math.ceil(-Math.log10(tick)))) : priceDigits(data.bars.at(-1)?.close);
    const priceFormat = { type: 'price' as const, precision: tick?.toString().includes('.')?Math.max(digits,tick.toString().split('.')[1].length):digits, minMove: tick && tick>0?tick:10 ** -digits };
    candle.current.applyOptions({ priceFormat });
    for (const key of ['ema20', 'ema60', 'macd', 'signal'] as const) lines.current[key]?.applyOptions({ priceFormat });
    histogram.current?.applyOptions({ priceFormat });
    const range = chart.timeScale().getVisibleLogicalRange();
    const atRight = range == null || range.to >= count.current - 2;
    candle.current.setData(data.bars.map(b => ({ time: seconds(b.time), open: b.open, high: b.high, low: b.low, close: b.close })));
    volume.current?.setData(data.bars.map(b => ({ time: seconds(b.time), value: b.volume, color: b.close >= b.open ? '#64d6a055' : '#ec536e55' })));
    for (const [name, line] of Object.entries(lines.current)) {
      line?.setData(data.indicators.map(i => {
        const value = i[name as keyof Indicator];
        return typeof value === 'number' ? { time: seconds(i.time), value } : { time: seconds(i.time) };
      }));
    }
    histogram.current?.setData(data.indicators.map(i => i.histogram == null
      ? { time: seconds(i.time) }
      : { time: seconds(i.time), value: i.histogram, color: i.histogram >= 0 ? '#86d9b47a' : '#ed8c927a' }));
    if (initial.current) {
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, data.bars.length - 115), to: data.bars.length + 5 });
      initial.current = false;
    } else if (range) {
      if (atRight && data.bars.length > count.current) chart.timeScale().scrollToRealTime();
      else chart.timeScale().setVisibleLogicalRange(range);
    }
    count.current = data.bars.length;
  }, [data, timeframe, visible.rsi, visible.macd]);

  useEffect(() => {
    lines.current.ema20?.applyOptions({ visible: visible.ema20 });
    lines.current.ema60?.applyOptions({ visible: visible.ema60 });
  }, [visible, timeframe]);

  useEffect(() => {
    const total = current.current?.bars.length ?? 0;
    if (total) api.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - 115), to: total + 5 });
  }, [resetKey]);

  const bar = data?.bars.find(b => b.time === hover) ?? data?.bars.at(-1);
  const indicator = data?.indicators.find(i => i.time === bar?.time);
  return <>
    <div className="ohlc-row">
      <span className="ohlc-date">{bar ? new Date(bar.time).toLocaleString('zh-CN', { year: ['1d', '1w'].includes(timeframe) ? 'numeric' : undefined, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '等待行情'}</span>
      {[['开', bar?.open], ['高', bar?.high], ['低', bar?.low], ['收', bar?.close]].map(([name, value]) => <span key={name}><em>{name}</em> <b className={bar && bar.close >= bar.open ? 'positive' : 'negative'}>{price(value as number | undefined)}</b></span>)}
      <span className="bar-state">{bar?.status === 'confirmed' ? '已收盘' : bar ? '形成中' : ''}</span>
    </div>
    <div className="chart-shell" style={{ height: `calc(100vh - 350px + ${extraHeight}px)` }}>
      <div ref={container} className="chart-canvas" role="img" aria-label={`${symbol} ${timeframe} K线、成交量与技术指标图表`} />
      <div className="chart-label main-label">
        {visible.ema20 && <span style={{ color: COLORS.ema20 }}>EMA 20 <b>{price(indicator?.ema20)}</b></span>}
        {visible.ema60 && <span style={{ color: COLORS.ema60 }}>EMA 60 <b>{price(indicator?.ema60)}</b></span>}
      </div>
      {!data?.bars.length && <div className="empty-chart"><span className="loading-orbit"/><h3>正在载入真实行情</h3><p>首次连接会获取历史 K 线并保存在本机</p></div>}
    </div>
    <div className="indicator-readout">
      <span>VOL <b>{money(bar?.volume, data?.instrument?0:3)} {data?.instrument?.volume_unit??symbol}</b></span>
      {visible.rsi && <span>RSI 14 <b style={{ color: COLORS.rsi }}>{money(indicator?.rsi14, 2)}</b></span>}
      {visible.macd && <><span>DIF <b style={{ color: COLORS.dif }}>{price(indicator?.macd)}</b></span><span>DEA <b style={{ color: COLORS.signal }}>{price(indicator?.signal)}</b></span><span>HIST <b>{price(indicator?.histogram)}</b></span></>}
      <small>{bar?.status === 'forming' ? '当前柱指标为临时值' : '指标基于所选 K 线'}</small>
    </div>
  </>;
}
