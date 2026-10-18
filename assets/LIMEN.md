# LIMEN 品牌资产

2026-10-18 采用用户选定的第 2 版：将 LIMEN 的 I 融合成一根 K 线，上影线穿过短水平价位线，暗含市场触及、穿越关键价位。

- `limen-wordmark.svg`：透明底横向字标，导航栏与文档使用。
- `limen-icon.svg`：同一字标中的 K 线图标，透明底。
- `limen-wordmark.png`：1600 像素宽透明 PNG。
- `limen-icon.png`：512 × 512 透明 PNG。
- `limen-concept-v1.png`：用户选定的原始概念图。
- 浏览器图标：`../frontend/public/favicon.svg`，暗底衬托薄荷绿 K 线。

字标白色为 `#e1e9ef`，K 线薄荷绿为 `#96e0bd`。前端复用同一份图形，避免用普通字体重新拼出字标。小尺寸需保留价位线与上下影线。

## 生成与导出

概念由内置 ImageGen 生成；生产 SVG 通过 Potrace 从已选概念图的原始轮廓自动描摹，保留字母结构并统一实色填充；PNG 由同一份 SVG 导出。透明图初稿存在的抠图杂点不用于页面。

生成提示方向：Create one premium custom wordmark for LIMEN. Replace only I with one mint Japanese candlestick whose upper wick crosses a short key-price line. Preserve readable LIMEN, geometric sans-serif letters, soft-square corners, off-white letters and a mint candle on a dark presentation canvas. Show the wordmark and its matching standalone glyph. No additional arrows, currency signs, slogans, gradients or 3D.

## 兼容既有数据

应用对外名称、源码目录与数据目录统一为 LIMEN / `limen`。旧数据目录 `tide`（更早 `LocalMarket`）在服务首次启动时自动迁移为 `Application Support/limen`；情报日志名为 `limen.intel`，环境变量 `TIDE_INTEL_*` 仍作为回退读取。浏览器存储键、`X-Local-Market` 请求头等旧标识保留，以继续读取原有自选、历史行情与模型配置。
