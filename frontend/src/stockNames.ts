// Display/search metadata; never use these labels as a contract identifier.
// Chinese labels are common names or explanations, not purported official
// translations. Unknown symbols intentionally fall back to the provider name.
const STOCK_NAMES: Record<string, string> = {
  AAPL: '苹果', MSFT: '微软', NVDA: '英伟达', AMZN: '亚马逊',
  GOOG: '谷歌 Alphabet（C 类股）', GOOGL: '谷歌 Alphabet（A 类股）',
  META: 'Meta（脸书母公司）', TSLA: '特斯拉', AVGO: '博通',
  TSM: '台积电', AMD: '超威半导体 AMD', INTC: '英特尔', MU: '美光科技',
  QCOM: '高通', ARM: '安谋 ARM', ASML: '阿斯麦', AMAT: '应用材料',
  LRCX: '泛林集团', KLAC: '科磊', MRVL: '迈威尔科技', TXN: '德州仪器',
  ADI: '亚德诺半导体', NXPI: '恩智浦半导体', ON: '安森美半导体',
  CRWV: 'CoreWeave（AI 云计算公司）', NBIS: 'Nebius（AI 云基础设施）',
  ORCL: '甲骨文', CRM: '赛富时', ADBE: '奥多比', NOW: 'ServiceNow（企业工作流软件）',
  SNOW: 'Snowflake（云数据平台）', PLTR: 'Palantir（数据分析软件）',
  CRWD: 'CrowdStrike（网络安全）', PANW: '派拓网络', ZS: 'Zscaler（云安全）',
  NET: 'Cloudflare（网络与云服务）', DDOG: 'Datadog（云监控）',
  MDB: 'MongoDB（数据库软件）', TEAM: 'Atlassian（协作软件）',
  SHOP: 'Shopify（电商平台）', UBER: '优步', ABNB: '爱彼迎', BKNG: 'Booking（在线旅行）',
  NFLX: '奈飞', DIS: '迪士尼', SPOT: 'Spotify（流媒体音乐）', RBLX: 'Roblox（游戏平台）',
  RDDT: 'Reddit（社交平台）', PINS: 'Pinterest（图片社交平台）', SNAP: 'Snap（Snapchat 母公司）',
  BABA: '阿里巴巴', JD: '京东', PDD: '拼多多', BIDU: '百度', NTES: '网易',
  BILI: '哔哩哔哩', TME: '腾讯音乐', NIO: '蔚来', XPEV: '小鹏汽车', LI: '理想汽车',
  TCOM: '携程集团', YUMC: '百胜中国', BEKE: '贝壳', EDU: '新东方', TAL: '好未来',
  COIN: 'Coinbase（加密货币交易平台）', MSTR: 'Strategy（比特币储备公司）',
  HOOD: 'Robinhood（在线券商）', CRCL: 'Circle（USDC 发行商）',
  MARA: 'MARA（比特币挖矿）', RIOT: 'Riot Platforms（比特币挖矿）',
  CLSK: 'CleanSpark（比特币挖矿）', IREN: 'IREN（数据中心与比特币挖矿）',
  HUT: 'Hut 8（能源与数字基础设施）', BITF: 'Bitfarms（比特币挖矿）',
  CORZ: 'Core Scientific（数据中心）', CIFR: 'Cipher Mining（比特币挖矿）',
  SQ: 'Block（支付科技）', XYZ: 'Block（支付科技）', PYPL: '贝宝', SOFI: 'SoFi（金融科技）',
  V: '维萨 Visa', MA: '万事达', JPM: '摩根大通', BAC: '美国银行',
  WFC: '富国银行', C: '花旗集团', GS: '高盛', MS: '摩根士丹利',
  BLK: '贝莱德', SCHW: '嘉信理财', IBKR: '盈透证券', AXP: '美国运通',
  'BRK.B': '伯克希尔哈撒韦（B 类股）', 'BRK B': '伯克希尔哈撒韦（B 类股）',
  'BRK.A': '伯克希尔哈撒韦（A 类股）', 'BRK A': '伯克希尔哈撒韦（A 类股）',
  LLY: '礼来', NVO: '诺和诺德', JNJ: '强生', PFE: '辉瑞', MRK: '默沙东',
  ABBV: '艾伯维', UNH: '联合健康', AMGN: '安进', GILD: '吉利德科学', MRNA: '莫德纳',
  ISRG: '直觉外科', TMO: '赛默飞世尔', ABT: '雅培', CVS: 'CVS Health（医药零售与保险）',
  WMT: '沃尔玛', COST: '开市客', TGT: '塔吉特', HD: '家得宝', LOW: '劳氏',
  KO: '可口可乐', PEP: '百事', PG: '宝洁', CL: '高露洁棕榄',
  NKE: '耐克', SBUX: '星巴克', MCD: '麦当劳', YUM: '百胜餐饮', CMG: 'Chipotle（墨式餐饮）',
  XOM: '埃克森美孚', CVX: '雪佛龙', COP: '康菲石油', OXY: '西方石油',
  SLB: '斯伦贝谢', EOG: 'EOG Resources（油气勘探）',
  GE: 'GE 航空航天', BA: '波音', CAT: '卡特彼勒', DE: '迪尔',
  HON: '霍尼韦尔', LMT: '洛克希德马丁', RTX: '雷神技术 RTX',
  GD: '通用动力', NOC: '诺斯罗普格鲁曼', UPS: '联合包裹', FDX: '联邦快递',
  F: '福特汽车', GM: '通用汽车', RIVN: 'Rivian（电动汽车）', LCID: 'Lucid（电动汽车）',
  RKLB: 'Rocket Lab（火箭与航天）', ASTS: 'AST SpaceMobile（卫星通信）',
  SMR: 'NuScale Power（小型模块化核电）', OKLO: 'Oklo（先进核能）',
  IONQ: 'IonQ（量子计算）', RGTI: 'Rigetti（量子计算）', QBTS: 'D-Wave（量子计算）',
  SMCI: '超微电脑', DELL: '戴尔科技', HPE: '慧与', HPQ: '惠普',
  CSCO: '思科', IBM: '国际商业机器 IBM', ACN: '埃森哲', SAP: '思爱普 SAP',
  VZ: '威瑞森', T: '美国电话电报 AT&T', TMUS: 'T-Mobile（移动通信）',
  ES: 'Eversource Energy（能源公用事业）', NEE: '新纪元能源', DUK: '杜克能源',
  SO: '南方电力', D: '道明尼能源', AEP: '美国电力',
};

const ETF_NAMES: Record<string, string> = {
  SPY: 'SPDR 标普 500 ETF', VOO: '先锋标普 500 ETF', IVV: 'iShares 标普 500 ETF',
  QQQ: '景顺纳斯达克 100 ETF', QQQM: '景顺纳斯达克 100 ETF',
  DIA: 'SPDR 道琼斯工业平均指数 ETF', IWM: 'iShares 罗素 2000 ETF',
  VTI: '先锋美国全市场 ETF', VT: '先锋全球股票 ETF',
  MGC: '先锋美国超大盘股 ETF', VEA: '先锋发达市场 ETF', VWO: '先锋新兴市场 ETF',
  EEM: 'iShares 新兴市场 ETF', FXI: 'iShares 中国大盘股 ETF', KWEB: '中概互联网 ETF',
  ASHR: '沪深 300 中国 A 股 ETF', GLD: 'SPDR 黄金 ETF', IAU: 'iShares 黄金 ETF',
  SLV: 'iShares 白银 ETF', GDX: '金矿股 ETF', USO: '美国原油 ETF',
  TLT: 'iShares 20 年以上美国国债 ETF', IEF: 'iShares 7–10 年美国国债 ETF',
  SHY: 'iShares 1–3 年美国国债 ETF', BND: '先锋美国债券市场 ETF',
  SGOV: 'iShares 0–3 个月美国国债 ETF', HYG: 'iShares 高收益公司债 ETF',
  LQD: 'iShares 投资级公司债 ETF', SCHD: '嘉信美国股息股票 ETF',
  JEPI: '摩根大通股票收益 ETF', JEPQ: '摩根大通纳斯达克股票收益 ETF',
  XLK: '科技行业 ETF', XLF: '金融行业 ETF', XLE: '能源行业 ETF',
  XLV: '医疗保健行业 ETF', XLI: '工业行业 ETF', XLP: '日常消费行业 ETF',
  XLY: '可选消费行业 ETF', XLU: '公用事业行业 ETF', XLB: '原材料行业 ETF',
  XLRE: '房地产行业 ETF', XLC: '通信服务行业 ETF',
  SMH: 'VanEck 半导体 ETF', SOXX: 'iShares 半导体 ETF', ARKK: 'ARK 创新 ETF',
  IBIT: '贝莱德 iShares 比特币 ETF', FBTC: '富达比特币 ETF',
  GBTC: '灰度比特币信托 ETF', BITB: 'Bitwise 比特币 ETF', ARKB: 'ARK 21Shares 比特币 ETF',
  ETHA: '贝莱德 iShares 以太坊 ETF', FETH: '富达以太坊 ETF',
  ETHE: '灰度以太坊信托 ETF',
};

const FUTURE_NAMES: Record<string, string> = {
  ES: 'E-mini 标普 500 指数期货', MES: '微型 E-mini 标普 500 指数期货',
  NQ: 'E-mini 纳斯达克 100 指数期货', MNQ: '微型 E-mini 纳斯达克 100 指数期货',
  RTY: 'E-mini 罗素 2000 指数期货', M2K: '微型 E-mini 罗素 2000 指数期货',
  YM: 'E-mini 道琼斯指数期货', MYM: '微型 E-mini 道琼斯指数期货',
  GC: '黄金期货', MGC: '微型黄金期货', SI: '白银期货', SIL: '微型白银期货',
  CL: 'WTI 原油期货', MCL: '微型 WTI 原油期货', NG: '天然气期货',
  HG: '铜期货', ZB: '美国长期国债期货', ZN: '美国 10 年期国债期货',
};

// Product descriptions retain issuer, daily multiplier and direction so an ETF
// cannot be mistaken for its underlying stock. These are explanatory labels.
// Verified against issuer product pages on 2026-10-18:
// https://www.rexshares.com/cord/ | https://www.rexshares.com/crwu/
// https://leverageshares.com/us/etfs/leverage-shares-2x-long-crwv-daily-etf
// https://corgiinvest.com/crwx | https://www.tradretfs.com/cwvx
// https://graniteshares.com/etfs/cwy/
// CRWV business description: https://www.coreweave.com/
const SPECIAL_ETFS: Record<string, { name: string; underlying: RegExp; inverse?: boolean }> = {
  CORD: { name: 'T-REX CoreWeave 每日两倍做空 ETF', underlying: /CRWV|COREWEAVE/i, inverse: true },
  CRWG: { name: 'Leverage Shares CoreWeave 每日两倍做多 ETF', underlying: /CRWV|COREWEAVE/i },
  CRWU: { name: 'T-REX CoreWeave 每日两倍做多 ETF', underlying: /CRWV|COREWEAVE/i },
  CRWX: { name: 'Corgi CoreWeave 每日两倍做多 ETF', underlying: /CRWV|COREWEAVE/i },
  CWVX: { name: 'Tradr CoreWeave 每日两倍做多 ETF', underlying: /CRWV|COREWEAVE/i },
  CWY: { name: 'GraniteShares CoreWeave 期权收益策略 ETF', underlying: /CRWV|COREWEAVE/i },
};

export function stockDescription(symbol: string, originalName = '', assetType = ''): string {
  const key = symbol.trim().toUpperCase();
  const type = assetType.toLowerCase();
  if (type === 'future' || type === 'fut') return FUTURE_NAMES[key] ?? '';
  // Without a type, only an explicit futures name can disambiguate shared roots.
  if (!type && /\bFUTURE(S)?\b|期货|E-MINI|MICRO GOLD/i.test(originalName)) {
    return FUTURE_NAMES[key] ?? '';
  }
  const special = SPECIAL_ETFS[key];
  if (special) {
    if (originalName && !special.underlying.test(originalName)) return '';
    const multiplier = originalName.match(/\b(\d+(?:\.\d+)?)\s*X\b/i);
    if (key !== 'CWY' && multiplier && Number(multiplier[1]) !== 2) return '';
    const isInverse = /\bINV(?:ERSE)?\b|\bSHORT\b|做空|反向/i.test(originalName);
    const isLong = /\bLONG\b|做多/i.test(originalName);
    if ((special.inverse && isLong) || (!special.inverse && isInverse)) return '';
    return special.name;
  }
  return STOCK_NAMES[key] ?? ETF_NAMES[key] ?? '';
}

const STOCK_LABEL_ALIASES: Record<string, string> = Object.fromEntries(
  Object.entries(STOCK_NAMES).map(([symbol, name]) => [name, symbol]),
);
const primaryLabels = Object.entries(STOCK_NAMES).map(([symbol, name]) => [name.replace(/（.*$/, ''), symbol]);
for (const [label, symbol] of primaryLabels) {
  // Shared company names with different share classes need an explicit choice.
  if (primaryLabels.filter(([otherLabel]) => otherLabel === label).length === 1) {
    STOCK_LABEL_ALIASES[label] = symbol;
  }
}

export const STOCK_SEARCH_ALIASES: Record<string, string> = {
  ...STOCK_LABEL_ALIASES,
  '英伟达': 'NVDA', '英偉達': 'NVDA', '辉达': 'NVDA', '輝達': 'NVDA',
  '苹果': 'AAPL', '蘋果': 'AAPL', '微软': 'MSFT', '微軟': 'MSFT',
  '特斯拉': 'TSLA', '亚马逊': 'AMZN', '亞馬遜': 'AMZN', '谷歌': 'GOOGL',
  '脸书': 'META', '臉書': 'META', '博通': 'AVGO', '台积电': 'TSM', '台積電': 'TSM',
  '超威半导体': 'AMD', '英特尔': 'INTC', '英特爾': 'INTC', '美光': 'MU', '美光科技': 'MU',
  '高通': 'QCOM', '阿斯麦': 'ASML', '阿斯麥': 'ASML', '甲骨文': 'ORCL',
  '帕兰提尔': 'PLTR', '帕蘭提爾': 'PLTR', 'Palantir': 'PLTR',
  '奈飞': 'NFLX', '奈飛': 'NFLX', '迪士尼': 'DIS', '优步': 'UBER', '優步': 'UBER',
  '爱彼迎': 'ABNB', '愛彼迎': 'ABNB', '阿里巴巴': 'BABA', '京东': 'JD', '京東': 'JD',
  '拼多多': 'PDD', '百度': 'BIDU', '网易': 'NTES', '網易': 'NTES', '哔哩哔哩': 'BILI',
  '蔚来': 'NIO', '蔚來': 'NIO', '小鹏': 'XPEV', '小鵬': 'XPEV', '理想汽车': 'LI',
  '携程': 'TCOM', '騰訊音樂': 'TME', '腾讯音乐': 'TME',
  '贝宝': 'PYPL', '摩根大通': 'JPM', '高盛': 'GS', '美国银行': 'BAC', '美國銀行': 'BAC',
  '贝莱德': 'BLK', '伯克希尔': 'BRK.B', '伯克希尔哈撒韦': 'BRK.B',
  '礼来': 'LLY', '禮來': 'LLY', '诺和诺德': 'NVO',
  '强生': 'JNJ', '輝瑞': 'PFE', '辉瑞': 'PFE', '沃尔玛': 'WMT', '沃爾瑪': 'WMT',
  '开市客': 'COST', '好市多': 'COST', '可口可乐': 'KO', '百事': 'PEP',
  '宝洁': 'PG', '寶潔': 'PG', '耐克': 'NKE', '星巴克': 'SBUX', '麦当劳': 'MCD',
  '波音': 'BA', '卡特彼勒': 'CAT', '福特': 'F', '通用汽车': 'GM',
  '标普500': 'SPY', '标普 500': 'SPY', '纳斯达克100': 'QQQ', '纳斯达克 100': 'QQQ',
  '黄金ETF': 'GLD', '黄金etf': 'GLD', '白银ETF': 'SLV',
  '微型黄金': 'MGC', '微型黄金期货': 'MGC', '微型黃金': 'MGC', '微型黃金期貨': 'MGC',
};
