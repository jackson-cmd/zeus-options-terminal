/* ZEUS help: plain-language cards that say what a button or a number is, how it is calculated and
   how to use it.
   · A small ? beside a column, a score or a card opens that one card.
   · The ? in the top bar (a round ? at the bottom right on pages without the top bar) opens a guide
     to every control on the page being viewed, with a search box and "show me on the page".
   · Press ? (outside a text box) for the guide; Escape closes it.
   The text is written here in Chinese and English, never machine-translated, and follows the
   interface language: 中文, 双语 (the English under the Chinese) or English.
   Loaded by zeus.js on every page that loads zeus.js; it changes nothing on the pages themselves
   except the ? icons it adds. */
(function () {
  'use strict';
  if (window.zeusHelp) return;

  // ================================================================== registry
  var REG = {}, LIST = [], GROUPS = {};
  // a heading in the page guide; page '*' = a heading every page can use
  function G(page, key, zh, en) { GROUPS[page + '/' + key] = [zh, en]; }
  // one card. pages: where the guide lists it. at: CSS selector of the element it explains, with an
  // optional "|text" the element must contain ('' = no single element). zh / en: [title, what it is,
  // how it is calculated, how to use it]; the last two may be ''. q: put a ? on the element
  // (true = inside it at the end, or right after a menu, box or button; 'after' = right after it).
  function E(pages, group, id, at, zh, en, q) {
    var e = { id: id, pages: [].concat(pages), group: group, at: at || '', zh: zh, en: en, q: q || false };
    REG[id] = e; LIST.push(e);
  }
  var PICKER = ['picker:kline', 'picker:heatmap', 'picker:brief', 'picker:rot', 'picker:opt'];
  var PAGE_NAME = {
    'positions': ['持仓', 'Positions'], 'picker:kline': ['选股 · K 线图墙', 'Picker · K-line chart wall'],
    'picker:heatmap': ['选股 · 热力图', 'Picker · Heatmap'], 'picker:brief': ['选股 · 市场判断', 'Picker · Market brief'],
    'picker:rot': ['选股 · 行业轮动', 'Picker · Sector rotation'], 'picker:opt': ['选股 · 期权情绪', 'Picker · Options sentiment'],
    'digest': ['Digest 新闻', 'Digest'], 'events': ['Events 事件', 'Events'], 'journal': ['交易日记', 'Trade Journal'],
    'paper': ['模拟盘', 'Paper Trade'], 'ta': ['TA 扫描', 'TA scanner'], 'checkup': ['开单前体检', 'Pre-trade checkup'],
    'review': ['日记复盘', 'Journal review'], 'top': ['顶栏', 'Top bar']
  };

  // ================================================================== groups
  G('top', 'bar', '顶栏（每个页面都有）', 'Top bar (on every page)');
  G('positions', 'acct', '账户概览', 'Account');
  G('positions', 'exit', '出场状态', 'Exit status');
  G('positions', 'tools', '工具栏', 'Toolbar');
  G('positions', 'cols', '持仓表的列', 'Table columns');
  G('*', 'views', '视图', 'Views');
  G('*', 'tools', '工具栏', 'Toolbar');
  G('*', 'score', '评分', 'Score');
  G('picker:kline', 'cards', '卡片上的标记', 'On each card');
  G('picker:heatmap', 'map', '热力图', 'Heatmap');
  G('picker:brief', 'brief', '市场判断', 'Market brief');
  G('picker:rot', 'rot', '行业轮动', 'Sector rotation');
  G('picker:opt', 'opt', '期权情绪', 'Options sentiment');
  G('digest', 'modes', '视图', 'Views');
  G('digest', 'filters', '筛选', 'Filters');
  G('digest', 'actions', '操作', 'Actions');
  G('events', 'ev', '事件', 'Events');
  G('journal', 'stats', '顶部统计', 'Headline numbers');
  G('journal', 'tabs', '日记的标签页', 'Journal tabs');
  G('paper', 'pt', '模拟盘', 'Paper Trade');
  G('paper', 'cols', '表格的列', 'Table columns');
  G('ta', 'run', '扫描', 'Scan');
  G('ta', 'filt', '筛选和视图', 'Filters and views');
  G('ta', 'cols', '表格的列', 'Table columns');
  G('ta', 'det', '详情里的各块', 'Inside a ticker’s detail');
  G('checkup', 'gates', '四道关', 'The four gates');
  G('checkup', 'use', '怎么用', 'Using it');
  G('review', 'rv', '复盘页', 'Review page');

  // ================================================================== top bar
  E('top', 'bar', 'top.tabs', '#topNavTabs',
    ['页面标签', '在六个页面之间切换：Positions 持仓、🎯 选股、📋 Digest 新闻、📅 Events 事件、Trade Journal 交易日记、🧪 模拟盘。窗口窄时标签左右滚动。', '', '每个页面上的按钮和数字，都可以在这份说明里查到。'],
    ['Page tabs', 'Switch between the six pages: Positions, 🎯 Picker, 📋 Digest (news), 📅 Events, Trade Journal and 🧪 Paper Trade. In a narrow window the tabs scroll sideways.', '', 'Every button and number on these pages is explained in this guide.']);
  E('top', 'bar', 'top.help', '#zsHelpBtn',
    ['? 说明', '打开这份说明：当前页面上每个按钮、每个数字是什么意思，怎么算，怎么用。表头、分数和卡片旁边的小 ? 直接打开那一项。', '', '快捷键：不在输入框里时按 ? 键。列表顶上可以搜索，也可以关掉页面上的小 ? 图标。'],
    ['? Guide', 'Opens this guide: what each button and number on the current page means, how it is calculated and how to use it. The small ? beside a column, a score or a card opens that one item.', '', 'Shortcut: press ? anywhere outside a text box. The search box at the top finds an item; the switch beside it hides the small ? icons.']);
  E('top', 'bar', 'top.lang', '#zsLangSel',
    ['界面语言', '中文、双语、English 三种界面语言，随时切换。双语保留中文，在按钮和表头旁边加上英文。', '', '只改界面上的字，新闻保持原文。English 界面下，Digest 里的「EN + 中文」按钮能在每条英文标题下面显示中文翻译。'],
    ['Interface language', 'Chinese, bilingual or English, switch any time. Bilingual keeps the Chinese labels and adds English beside the buttons and column headers.', '', 'Only the interface changes; news keeps its own language. In English, the “EN + 中文” button in the Digest shows a Chinese translation under each headline.']);
  E('top', 'bar', 'top.gear', '#zsGearBtn',
    ['⚙ 设置', '所有设置都在这里：tastytrade 账号、AI 提供方、新闻源、手动上传交易、AI 智能体（MCP）。只存在这台电脑上。', '', '鼠标停在 ⚙ 上能看到现在的模式：● tastytrade 实时，或 ◐ 手动上传和已导入的交易笔数。'],
    ['⚙ Settings', 'Every setting lives here: the tastytrade account, the AI provider, news sources, manual trade uploads and AI agents (MCP). They are stored only on this computer.', '', 'Hover ⚙ to see the current mode: ● tastytrade live, or ◐ manual upload with the number of trades imported.']);
  E('top', 'bar', 'top.ai', '#aiPauseBtn',
    ['AI 暂停', '暂停或恢复定时跑的 AI 任务（每天的 AI 选股、新闻叙事等）。', '', '想省 AI 额度时暂停，再点一次恢复。'],
    ['AI pause', 'Pauses or resumes the scheduled AI jobs (the daily AI picks, the news narratives and so on).', '', 'Pause it to save AI credits; click again to resume.']);
  E('top', 'bar', 'top.mute', '#muteBtn',
    ['🔔 静音', '一键静音所有通知：止盈提醒、异动、风险、AI 持仓点评。', '', '开会或录屏时打开，再点一次恢复。'],
    ['🔔 Mute', 'Mutes every notification at once: take-profit, movers, risk and the AI position review.', '', 'Turn it on for a meeting or a recording; click again to undo.']);
  E('top', 'bar', 'top.tr', '#trStatusBtn',
    ['🌐 翻译状态', '新闻标题用哪家翻译（按钮上写着名字），其他几家为什么用不了，还可以手动测一句。', '', '新闻没有翻译时，先点它看原因。'],
    ['🌐 Translation status', 'Which engine translates the headlines (its name is on the button), why the others don’t work, and a box to test one sentence.', '', 'When headlines come without a translation, open it to see why.']);
  E('top', 'bar', 'top.privacy', '#privacyBtn',
    ['👁 隐私模式', '把页面上的金额模糊掉。', '', '录屏、截图或共享屏幕前打开。'],
    ['👁 Privacy mode', 'Blurs the dollar amounts on the page.', '', 'Turn it on before recording, taking screenshots or sharing your screen.']);

  // ================================================================== positions
  E('positions', 'acct', 'pos.tickers', '#dashSummary .dash-stat|Tickers',
    ['Tickers 标的数', '持仓里有多少个不同的标的。同一只股票上的几个价差只算一个。', '数持仓里不同的正股代码。到 15 个就变黄，加 ⚠。', '标的太多，盯不过来，风险也难管。变黄时考虑收缩。'],
    ['Tickers', 'How many different underlyings you hold. Several spreads on one stock count once.', 'Counts the distinct underlying symbols. It turns yellow with ⚠ at 15.', 'Too many names are hard to watch and to manage. When it turns yellow, consider trimming.'], true);
  E('positions', 'acct', 'pos.bp', '#dashSummary .dash-stat|BP Used',
    ['BP Used 已用购买力', '持仓占用的购买力（保证金），后面是占账户净值 NLV 的百分比。', '连着 tastytrade 时 = NLV − 剩下的衍生品购买力，和 tastytrade 的「已用 BP」一致。手动模式下券商不给这个数，改成所有限定风险价差的最大亏损相加。占 NLV 50% 以上变黄，75% 以上变红。', '变红说明仓位太重，一次急跌就可能伤筋动骨。先减仓，或者等旧仓位到期再开新的。'],
    ['BP Used', 'The buying power (margin) your positions tie up, and its share of the account’s NLV.', 'With tastytrade: NLV minus the remaining derivative buying power, the same as tastytrade’s “BP used”. In manual mode the broker doesn’t give it, so it is the sum of the max losses of the defined-risk spreads. Yellow from 50% of NLV, red from 75%.', 'Red means the book is heavy and one sharp drop could hurt badly. Trim, or let positions expire before opening new ones.'], true);
  E('positions', 'acct', 'pos.cash', '#dashSummary .dash-stat|Cash',
    ['Cash 现金', '账户里的现金，后面是占 NLV 的百分比。', '连着 tastytrade 时是券商给的现金余额；手动模式下 = 设置里填的起始现金 + 导入的交易带来的现金流。低于 NLV 的 50% 变黄，低于 30% 变红。', '留一半现金，急跌时才有钱加仓或对冲。变黄时先别再开新仓。'],
    ['Cash', 'The cash in the account and its share of NLV.', 'With tastytrade: the broker’s cash balance. In manual mode: the starting cash set in Settings plus the cash flow of the trades you imported. Yellow below 50% of NLV, red below 30%.', 'Keeping half in cash leaves room to add or hedge in a sharp drop. When it turns yellow, hold off on new positions.'], true);
  E('positions', 'acct', 'pos.nlv', '#dashSummary .dash-stat|NLV',
    ['NLV 净清算值', '账户总值：现金，加上把所有持仓按现价平掉能拿回的钱。', '', '上面几个百分比（已用 BP、现金、最大盈亏的占比）都以它做分母。'],
    ['NLV (net liquidating value)', 'The account’s total value: the cash plus what all positions would fetch if closed at current prices.', '', 'The percentages next to BP Used, Cash and the max profit and loss columns are all shares of it.'], true);
  E('positions', 'exit', 'pos.exit', '[onclick="setExitFilter(\'\')"]',
    ['出场状态 🔴🔵🟡🟢', '按每个价差离止盈还有多远，把持仓分成四类。点一类只看那一类，再点一次或点「全部」取消。快捷键 1–4，0 = 全部。', '进度 = 浮动盈亏 ÷ 最大盈利。止盈线跟着大盘走：牛市（SPY 在 200 日均线上方、VIX 低于 22）70%，震荡或偏弱 50%，取不到大盘数据时 65%；三个数都能在 🔔 告警设置里改。<br>🔵 止盈：进度到了止盈线；🟡 接近：进度到了止盈线 − 10%；🔴 深亏：亏损达 50% 或更多（且没到接近）；🟢 持有：其余。进度到止盈线 + 10%（剩不到 20 天时只要到止盈线）算强制止盈，体检里会提示马上平。', '🔵 该落袋；🟡 别提前跑；🔴 是借方价差，亏损有上限，这套规则不建议砍，拿到止盈区或到期。'],
    ['Exit status 🔴🔵🟡🟢', 'Sorts the positions into four groups by how close each spread is to its take-profit. Click one to see only that group; click it again or “All” to clear. Shortcuts 1–4, 0 = all.', 'Progress = unrealized P/L ÷ max profit. The take-profit level follows the market: 70% in a bull market (SPY above its 200-day average and VIX under 22), 50% when it is choppy or weak, 65% when the market data is missing; all three can be changed in 🔔 Alerts.<br>🔵 Take profit: progress has reached the level. 🟡 Near: within 10 points of it. 🔴 Deep loss: down 50% or more (and not near). 🟢 Hold: everything else. Ten points past the level (or the level itself with under 20 days left) is a forced take-profit, and the checkup says to close it now.', '🔵: bank it. 🟡: don’t exit early. 🔴: a debit spread’s loss is capped; these rules say hold to the take-profit zone or expiration rather than cut.'], 'after');
  E('positions', 'tools', 'pos.ai', '#posAiBtn',
    ['🤖 AI 新闻分析', '让 AI 逐个持仓读相关新闻，判断有没有利多、利空催化，以及要不要采取行动。', '', '需要先在 ⚙ 设置 → AI 选一个提供方。结果只是参考，不会替你下单。'],
    ['🤖 AI news review', 'Has the AI read the news for each position, judge the bullish and bearish catalysts, and say whether to act.', '', 'Needs an AI provider in ⚙ Settings → AI. It is advice only and never places an order.']);
  E('positions', 'tools', 'pos.view', '#hmPlBtn',
    ['🔥/💲 换视图', '同一批持仓的三种画法，点一下换一种：表格 → 🔥 热图 → 💵 盈亏条形图。', '', '想一眼看出哪些在赚、哪些在亏时用热图；比金额大小时用条形图。'],
    ['🔥/💲 Switch view', 'Three ways to draw the same positions; each click moves to the next: table → 🔥 heatmap → 💵 P/L bars.', '', 'The heatmap shows at a glance which positions make or lose money; the bars compare the amounts.']);
  E('positions', 'tools', 'pos.risk', '#riskBtn',
    ['⚠ 风险分析', '整个持仓的压力测试：1σ 和 2σ 涨跌下的盈亏、95% VaR 和 CVaR、到期阶梯和 Theta、情景网格。', '每个价差用 Black-Scholes 按各腿的隐含波动率重新定价。1σ = 现价 × IV × √(1/365)，就是一天的典型波动；算明天涨、跌 1σ 和 2σ 时的盈亏，取较差的一边。VaR 95% = 不利方向 1.645σ 时一天的亏损；CVaR = 1.645σ 到 3.5σ 之间更坏情况的平均亏损。', '回答「明天大跌一下我会亏多少」。CVaR 比 VaR 更能反映极端情况。'],
    ['⚠ Risk', 'Stress tests for the whole book: P/L for a 1σ and a 2σ move, 95% VaR and CVaR, the expiration ladder and theta, and a scenario grid.', 'Every spread is repriced with Black-Scholes at each leg’s implied volatility. 1σ = price × IV × √(1/365), a typical one-day move; it takes the P/L for tomorrow’s 1σ and 2σ moves up and down and keeps the worse side. VaR 95% = the one-day loss for a 1.645σ move the wrong way; CVaR = the average loss over the worse moves from 1.645σ to 3.5σ.', 'It answers “how much do I lose if the market drops tomorrow”. CVaR shows the extremes better than VaR.']);
  E('positions', 'tools', 'pos.checkup', '#checkupBtn',
    ['🩺 体检', '三项检查在一个弹窗里：开单前体检（四道关）、持仓的止盈止损建议、流动性（买卖价差会不会太宽）。', '', '开新单前先跑开单体检。持仓那一栏按「出场状态」的规则给每个仓位一句建议。'],
    ['🩺 Checkup', 'Three checks in one pop-up: the pre-trade checkup (four gates), take-profit and stop advice for each position, and liquidity (whether bid-ask spreads are too wide).', '', 'Run the pre-trade check before opening a trade. The positions part gives each position one line of advice using the exit-status rules.']);
  E('positions', 'tools', 'pos.refresh', '#refreshBtn',
    ['↻ 刷新', '马上重新取一遍持仓和报价。', '', '页面平时会自动更新；数字看着不对时点一下。'],
    ['↻ Refresh', 'Fetches the positions and quotes again right away.', '', 'The page updates by itself; click it when a number looks off.']);
  E('positions', 'tools', 'pos.alerts', '#alertBtn',
    ['🔔 告警设置', '止盈提醒的开关和参数：牛市、震荡、中性三种大盘下的止盈线，提醒多久一次。', '', '这里的止盈线同时用于出场状态、体检和提醒，三处一致。'],
    ['🔔 Alerts', 'Turns take-profit alerts on or off and sets them: the take-profit level for a bull, a choppy and a neutral market, and how often to alert.', '', 'These levels drive the exit status, the checkup and the alerts alike.']);
  E('positions', 'tools', 'pos.macro', '#macroEventBanner',
    ['📅 宏观事件提示', '下一个重要宏观数据（如 CPI、PCE、FOMC），以及会在它之后才到期的持仓。', '', '这些仓位要先扛过那次数据公布，波动可能变大。点横幅跳到 📅 Events 看完整日历。'],
    ['📅 Macro event banner', 'The next major macro release (CPI, PCE, FOMC and the like) and the positions that expire after it.', '', 'Those positions have to sit through that release, when moves can get bigger. Click the banner for the full calendar in 📅 Events.']);
  E('positions', 'cols', 'pos.c.name', '#dashTable th[data-sort="name"]',
    ['Name 名称', '价差的名称：标的加策略。名字旁边的 📣 表示到期前有财报，点它看这只票以往财报后的涨跌。', '', ''],
    ['Name', 'The spread’s name: the underlying and the strategy. A 📣 beside it means earnings come before expiration; click it for how the stock moved after past reports.', '', '']);
  E('positions', 'cols', 'pos.c.under', '#dashTable th[data-sort="under"]',
    ['Underlying 标的价格', '正股现价和今天的涨跌幅。', '', ''],
    ['Underlying', 'The stock’s current price and today’s change.', '', ''], true);
  E('positions', 'cols', 'pos.c.total', '#dashTable th[data-sort="total"]',
    ['Total Return 总收益', '这个价差从开仓到现在的浮动盈亏。点 $▸% 在美元和百分比之间切换。', '盈亏 = Σ（各腿现价 − 开仓价）× 数量 × 乘数，按现价盯市，和 tastytrade 的 P/L Open 一致，不含手续费。百分比 = 盈亏 ÷ |开仓净支出|。', ''],
    ['Total Return', 'The spread’s unrealized P/L since it was opened. Click $▸% to switch between dollars and percent.', 'P/L = Σ (leg’s current price − entry price) × quantity × multiplier, marked to market like tastytrade’s P/L Open, without fees. Percent = P/L ÷ |net debit paid|.', ''], true);
  E('positions', 'cols', 'pos.c.today', '#dashTable th[data-sort="today"]',
    ['Today’s Return 今日收益', '这个价差今天赚了或亏了多少。', 'Σ（各腿现价 − 昨收）× 数量 × 乘数。今天才开的仓位没有昨收，就等于总收益。百分比 = 今日盈亏 ÷ |开仓净支出|。', ''],
    ['Today’s Return', 'How much the spread made or lost today.', 'Σ (leg’s current price − previous close) × quantity × multiplier. A position opened today has no previous close, so this equals its total return. Percent = today’s P/L ÷ |net debit paid|.', ''], true);
  E('positions', 'cols', 'pos.c.be', '#dashTable th[data-sort="be"]',
    ['B/E % 离盈亏平衡多远', '现价离最近的到期盈亏平衡价还有多远。', '(最近的盈亏平衡价 − 现价) ÷ 现价 × 100。盈亏平衡价 = 到期时整个价差刚好不赚不亏的股价。在 ±2% 以内标黄。', '看涨价差：负数 = 平衡价在现价下方，股价不大跌就能赚钱；正数 = 还要涨这么多才回本。标黄说明就在平衡点附近，一点小波动就决定盈亏。'],
    ['B/E % (distance to breakeven)', 'How far the price is from the nearest breakeven at expiration.', '(nearest breakeven − price) ÷ price × 100. The breakeven is the stock price at which the whole spread neither makes nor loses money at expiration. Within ±2% it turns yellow.', 'For a bullish spread: negative = the breakeven is below the price, so it makes money unless the stock falls that far; positive = the stock still has to rise this much. Yellow means it sits near the breakeven, where small moves decide the result.'], true);
  E('positions', 'cols', 'pos.c.maxp', '#dashTable th[data-sort="maxprofit"]',
    ['Max Profit 最大盈利', '持有到期最多能赚多少，下面是占 NLV 的百分比。', '在股价 0、每个行权价和一张覆盖所有行权价的价格网格上算到期盈亏，取最大值。日历、对角价差在最近一腿到期时，用 Black-Scholes 给远月腿估值。看涨期权净买入时上涨没有上限，显示 ∞。', '和进度、止盈线一起看：赚到最大盈利的五到八成，通常就该考虑落袋了。'],
    ['Max Profit', 'The most the spread can make if held to expiration, with its share of NLV below.', 'The expiration P/L is computed at a price of 0, at every strike and on a grid covering all strikes; the highest value is the max profit. For calendars and diagonals the far leg is valued with Black-Scholes when the near leg expires. When you are net long calls the upside has no limit, shown as ∞.', 'Read it with Progress and the take-profit level: once 50–80% of it is made, it is usually time to think about banking it.'], true);
  E('positions', 'cols', 'pos.c.maxl', '#dashTable th[data-sort="maxloss"]',
    ['Max Loss 最大亏损', '持有到期最多会亏多少，下面是占 NLV 的百分比。', '算法同最大盈利，取最小值。看涨期权净卖出时上涨的亏损没有上限，显示 ∞。占 NLV 5% 以上标红，2% 以上标黄。', '颜色就是提醒：单笔最大亏损太大时，一次失败就伤到整个账户。'],
    ['Max Loss', 'The most the spread can lose if held to expiration, with its share of NLV below.', 'Computed the same way as the max profit, taking the lowest value. When you are net short calls the loss on a rally has no limit, shown as ∞. Red from 5% of NLV, yellow from 2%.', 'The colour is the warning: a single trade that can lose this much of the account hurts the whole book when it fails.'], true);
  E('positions', 'cols', 'pos.c.cop', '#dashTable th[data-sort="cop"]',
    ['CoP 获利概率', 'Chance of Profit：到期时这笔交易赚钱的概率。几个到期日的价差按最近一腿的到期日算。', '假设股价服从对数正态分布：波动率用隐含波动率 IV，漂移用无风险利率，不带任何看涨看跌的判断。把到期盈亏大于 0 的那些价格的概率加起来。50% 以上显示绿色，否则红色。', '这是期权价格里隐含的概率，不是预测。获利概率高的价差通常赚得少，低的赚得多，要和最大盈亏一起看。'],
    ['CoP (chance of profit)', 'The probability that the trade makes money at expiration. Spreads with several expirations use the nearest leg’s.', 'The stock price is taken as lognormal, with the implied volatility as volatility and the risk-free rate as drift, so there is no bullish or bearish view in it. It adds up the probability of every price at which the expiration P/L is above zero. Green at 50% or more, red below.', 'It is the probability implied by option prices, not a forecast. Spreads with a high chance of profit usually make less, and the reverse, so read it with the max profit and loss.'], true);
  E('positions', 'cols', 'pos.c.dte', '#dashTable th[data-sort="dte"]',
    ['DTE 剩余天数', '离到期还有多少个日历日。', '', '剩不到 20 天时，到了止盈线就算强制止盈：时间少、gamma 大，利润容易吐回去。'],
    ['DTE (days to expiration)', 'Calendar days left until expiration.', '', 'With under 20 days left, reaching the take-profit level counts as a forced take-profit: little time and high gamma make profits easy to give back.'], true);
  E('positions', 'cols', 'pos.c.prog', '#dashTable th[data-sort="progress"]',
    ['Progress 进度', '这笔交易离最大盈利（或最大亏损）走了多远。', '赚钱时 = 浮动盈亏 ÷ 最大盈利；亏钱时 = 浮动亏损 ÷ 最大亏损，显示为负数。最大盈亏是 ∞ 时算不出来，显示 -。', '出场状态的 🔵、🟡 就是看这个百分比有没有到止盈线。'],
    ['Progress', 'How far the trade has gone toward its max profit (or its max loss).', 'When in profit: unrealized P/L ÷ max profit. When in loss: unrealized loss ÷ max loss, shown as negative. With an unlimited (∞) side it can’t be computed and shows -.', 'The 🔵 and 🟡 exit statuses check this percentage against the take-profit level.'], true);

  // ================================================================== picker (all views)
  E(PICKER, 'views', 'pk.views', '#hmSubNav',
    ['选股的三个视图', '📈 K 线图墙：每只票一张日线小图；🟩 热力图：按板块排的涨跌方格；🧭 大盘判断：AI 的市场判断，加上行业轮动和期权情绪。', '', ''],
    ['The Picker’s three views', '📈 K-line chart wall: a small daily chart per ticker. 🟩 Heatmap: tiles of today’s moves by sector. 🧭 Market Brief: the AI’s read of the market, plus sector rotation and options sentiment.', '', '']);
  E(['picker:kline', 'picker:heatmap'], 'tools', 'pk.collapse', '#hmCollapseBtn',
    ['▴ 收起', '收起或展开这一排设置，给图腾出地方。', '', ''],
    ['▴ Collapse', 'Hides or shows this row of settings to give the charts more room.', '', '']);
  E(['picker:kline', 'picker:heatmap'], 'tools', 'pk.auto', '#hmAutoSel',
    ['自动刷新', '页面多久重新取一次行情快照（5 秒、15 秒……）。', '', '服务器那边一直在收报价，这里只决定页面多久刷新一次，选 5 秒也很省。'],
    ['Auto refresh', 'How often the page fetches a new quote snapshot (5 s, 15 s, …).', '', 'The server receives quotes all the time; this only sets how often the page refreshes, and even 5 s is cheap.']);
  E(['picker:kline', 'picker:heatmap'], 'tools', 'pk.earn', '#hmTaEarn',
    ['财报筛选', '全部 / 排除临近财报 / 只看临近财报。右边的数字是多少天内算「临近」，默认 28 天。', '', '借方价差跨财报风险大，开新单时常用「排除临财报」。'],
    ['Earnings filter', 'All / exclude upcoming earnings / only upcoming earnings. The number beside it sets how many days count as upcoming (28 by default).', '', 'A debit spread held through earnings is risky, so “exclude” is the usual choice when looking for new trades.']);
  E(['picker:kline', 'picker:heatmap'], 'tools', 'pk.refresh', '#hmRefreshBtn',
    ['Refresh 刷新', '马上重新取行情和评分。', '', ''],
    ['Refresh', 'Fetches the quotes and scores again right away.', '', '']);
  E('picker:kline', 'tools', 'pk.days', '#hmGraphDays',
    ['周期', '每张小图显示多长的日线，例如 6 个月。', '', ''],
    ['Period', 'How much daily history each chart shows, for example 6 months.', '', '']);
  E('picker:kline', 'tools', 'pk.sort', '#hmGraphSort',
    ['排序', '图墙怎么排：按市值、今日涨跌，或 TA 评分（和 TA 扫描页同一个 0–100 分，高的在前）。', '', '想找今天技术面最好的票时选「TA 评分」。评分怎么算，见「TA 评分」那一条。'],
    ['Sort', 'How the wall is ordered: by market cap, today’s change, or TA score (the same 0–100 score as the TA scanner, highest first).', '', 'Pick “TA score” to find today’s best technical setups. How the score is built is in the “TA score” item.']);
  E('picker:kline', 'tools', 'pk.cols', '#hmGraphCols',
    ['每行', '每行放几张图；自动 = 按窗口宽度来。', '', ''],
    ['Per row', 'How many charts per row; auto fits the window width.', '', '']);
  E('picker:kline', 'tools', 'pk.aifilter', '#hmGraphAiFilter',
    ['AI 分类筛选', '只看 AI 选股分到某一类的票：▲ 低点抄底、▲ 趋势追涨、— 不交易、▼ 高点阻力、▼ 颓势追跌。', 'AI 每天读每只票的价格、RSI、均线、支撑阻力、TA 评分的方向、期权结构、基本面、新闻和财报日期，把它分进五类之一：<br>▲ 低点抄底：上升或健康的结构里回踩到支撑或均线，RSI 偏低，适合开 call 价差抄底；<br>▲ 趋势追涨：站上 EMA20/50，动能向上，接近新高，顺势开 call 价差；<br>▼ 高点阻力：冲到阻力被打回，RSI 偏高，用 put 价差做回落；<br>▼ 颓势追跌：跌破均线，动能向下，下面没有支撑，顺势开 put 价差；<br>— 不交易：信号互相打架、没有方向、离关键位太远，或三天内有财报。', '卡片上的 AI 徽章点开能看理由。AI 的分类只是参考，开单前还是要自己看图、跑体检。'],
    ['AI category filter', 'Shows only the tickers the AI picks put in one category: ▲ buy the dip, ▲ ride the trend, — no trade, ▼ fade at resistance, ▼ ride the downtrend.', 'Every day the AI reads each ticker’s price, RSI, moving averages, support and resistance, TA score direction, options structure, fundamentals, news and earnings date, and files it under one of five categories:<br>▲ Buy the dip: a pullback to support or a moving average in a rising or healthy structure, RSI on the low side; a call spread to buy the dip.<br>▲ Ride the trend: above EMA20/50, momentum up, near highs; a call spread with the trend.<br>▼ Fade at resistance: rejected at resistance, RSI on the high side; a put spread for the pullback.<br>▼ Ride the downtrend: below the moving averages, momentum down, no support below; a put spread with the trend.<br>— No trade: signals disagree, no direction, too far from key levels, or earnings within three days.', 'Click the AI badge on a card for its reasons. The categories are a starting point; still read the chart and run the checkup before a trade.'], 'after');
  E('picker:kline', 'tools', 'pk.aipicks', '#hmAiPicksBtn',
    ['🚀 AI 选股', '打开今天的 AI 选股报告：整个票池分成五类，每只票附技术面、基本面和新闻的理由。弹窗里可以「重新分析」，整批重跑。', '', '需要在 ⚙ 设置 → AI 选好提供方。分类也以徽章挂在每张卡片上。'],
    ['🚀 AI picks', 'Opens today’s AI picks report: the whole pool sorted into five categories, each ticker with technical, fundamental and news reasons. “Re-analyze” in the pop-up reruns the whole batch.', '', 'Needs an AI provider in ⚙ Settings → AI. The category also shows as a badge on each card.']);
  E('picker:kline', 'tools', 'pk.expand', '#hmExpandAllBtn',
    ['⊕ 全展开', '一次展开或收起所有卡片上的 AI 理由和多空要点。', '', ''],
    ['⊕ Expand all', 'Opens or closes the AI reasons and bull/bear notes on every card at once.', '', '']);
  E('picker:kline', 'tools', 'pk.bc', '#hmBcWrap',
    ['📊 Barchart 榜单', '在你 Barchart 账号里保存的选股榜单中勾几个，取交集或并集，图墙只显示这些票。要先在设置里登录 Barchart。', '', ''],
    ['📊 Barchart lists', 'Tick some of the screeners saved in your Barchart account, combine them by intersection or union, and the wall shows only those tickers. Needs a Barchart login in Settings.', '', '']);
  E('picker:kline', 'cards', 'pk.legend', '#hmGraphView > div:first-child > span:last-child',
    ['卡片上的标记', '每张卡片是一只票的日线图：<ul><li><b>⭐</b> = 强支撑或阻力：日线、周线、月线三个周期都在这个价位出现过拐点；</li><li><b>绿线</b> = 现价下方的支撑，<b>红线</b> = 上方的阻力；</li><li><b>橙线</b> = EMA50，<b>蓝线</b> = EMA200；</li><li>右边的黄色标签 = 最新价；</li><li>顶上 📅 = 下次财报的日期和剩余天数；</li><li>AI 徽章 = AI 选股的分类，点开看理由；</li><li>🔎 深度 = 这只票的 TA、公司简介、财报、新闻和 SEC 文件。</li></ul>', '', ''],
    ['Marks on each card', 'Each card is one ticker’s daily chart:<ul><li><b>⭐</b> = strong support or resistance: the daily, weekly and monthly charts all turned at this price;</li><li><b>green lines</b> = support below the price, <b>red lines</b> = resistance above;</li><li><b>orange</b> = EMA50, <b>blue</b> = EMA200;</li><li>the yellow tag on the right = the latest price;</li><li>📅 at the top = the next earnings date and the days left;</li><li>the AI badge = the AI picks category; click it for the reasons;</li><li>🔎 Deep = the ticker’s TA, company profile, financials, news and SEC filings.</li></ul>', '', ''], true);
  E('picker:kline', 'cards', 'pk.deep', '.hm-card-deep',
    ['🔎 深度', '打开这只票的完整资料：TA 详情、公司简介、财报、新闻和 SEC 文件。', '', ''],
    ['🔎 Deep', 'Opens the ticker’s full file: TA detail, company profile, financials, news and SEC filings.', '', '']);

  // ================================================================== TA score (Picker and TA scanner)
  E(['picker:kline', 'ta'], 'score', 'ta.score', '#hdr th[onclick="headSort(\'score\')"], #hmGraphSort',
    ['TA 评分（0–100）', '把今天对上涨有利的价格信号加起来的分数：分越高，今天同时指向上涨的信号越多。它是排序工具，不是胜率。', '四块相加，封顶 100：<ul><li><b>支撑（最多 40）</b>：今天最低价打进一条支撑带得 16；这条带被日、周、月三个周期一起确认 +10（两个周期 +5）；带里每多一个历史拐点 +3（最多 +9）；这只票以往「测试支撑后反弹」回测达标再 +5。</li><li><b>上穿 EMA100/200（最多 20）</b>：今天收盘上穿 100 日或 200 日指数均线得 12，这个信号在这只票上回测达标再 +8。</li><li><b>趋势（最多 25）</b>：长期（200 日均线）向上 +12，中期（50 日）+8，短期（20 日）+5。「向上」= 价格在均线上方，并且均线在上升。</li><li><b>MACD（15）</b>：MACD 线在信号线上方 +15。</li></ul>「回测达标」= 这个信号在这只票自己的历史里出现够多次（支撑 12 次，均线信号 20 次），胜率至少 60%，并且比「每天随便买入」高出至少 10 个百分点。期权未平仓量、P/C、GEX、异动期权流和统计 edge 只展示，不计分：回测里它们对方向基本是噪音。', '分高 = 好几个看涨信号今天同时出现；40 分以上算「高把握」，50 分以上显示绿色。先按评分找候选，再点开看支撑阻力、历史胜率和推荐的价差；开单前跑一遍体检。<br>注意：现在评分只算看涨一边。代码本来打算在熊市时也算看跌分，但判断熊市的条件永远不会成立，所以不会出现看跌方向。'],
    ['TA score (0–100)', 'A sum of today’s price signals that favour a rise: the higher it is, the more signals point up at once. It ranks candidates; it is not a win rate.', 'Four parts, capped at 100:<ul><li><b>Support (up to 40)</b>: today’s low reached a support zone, 16; the zone is confirmed on the daily, weekly and monthly charts +10 (two of them +5); +3 for each extra past turning point in the zone (up to +9); +5 when this stock’s past bounces from support pass the backtest.</li><li><b>Crossing EMA100/200 (up to 20)</b>: today’s close crossed above the 100- or 200-day exponential average, 12; +8 when that signal passes the backtest on this stock.</li><li><b>Trend (up to 25)</b>: long-term (200-day average) up +12, medium (50-day) +8, short (20-day) +5. “Up” means the price is above the average and the average is rising.</li><li><b>MACD (15)</b>: the MACD line above its signal line, +15.</li></ul>“Passes the backtest” means the signal occurred often enough in this stock’s own history (12 times for support, 20 for the average crossings), won at least 60% of the time, and beat buying on any random day by at least 10 points. Open interest, put/call, GEX, unusual option flow and the statistical edge are shown but not scored: in backtests they were noise for direction.', 'A high score means several bullish signals line up today; 40 or more counts as “high confidence” and 50 or more shows green. Use it to find candidates, then open the ticker for its support and resistance, past win rates and the suggested spread, and run the checkup before trading.<br>Note: the score currently counts only the bullish side. The code meant to add bearish points in a bear market, but its bear-market test can never be true, so no bearish direction appears.'], true);

  // ================================================================== picker: heatmap, brief, rotation, options sentiment
  E('picker:heatmap', 'map', 'pk.heat', '#hmCanvas',
    ['热力图', '票池里的股票按板块、行业排成方格：面积 = 市值，颜色 = 今天的涨跌（绿涨红跌，颜色越深幅度越大）。', '', '点板块标题放大到这个板块，每只票都能看清；点方格看单票详情；面包屑里的返回键回到全图。'],
    ['Heatmap', 'The pool’s stocks as tiles grouped by sector and industry: area = market cap, colour = today’s change (green up, red down, darker = bigger).', '', 'Click a sector title to zoom into it so every ticker is readable; click a tile for the ticker’s details; the back button in the breadcrumb returns to the whole map.']);
  E('picker:brief', 'brief', 'mb.what', '#mbHeader strong',
    ['🧭 市场判断', 'AI 写的当天大盘判断：坏消息有多坏，价格已经反映了多少。不贴「看多 / 看空」标签，只给概率，以及「出现什么情况说明判断错了」。', '每天早上（开着定时 AI 时）或点 🔄 重新分析时生成，约 3 分钟。AI 读的是各板块的数据、当天的分析文章、行业轮动和期权情绪。大跌概率要对照历史基准：5% 的回撤每年约 3 次，10% 每年约 1 次，20% 以上每 4–5 年 1 次；熊市概率高于 22% 时必须给出系统性的证据。写完还会自动审一遍质量，不达标重写一次。', '重点看三块：「这轮下跌会走多远」的概率，「三层支撑」，以及各板块「坏消息有多坏 vs 价格反映了多少」。坏消息已经被价格大幅反映时，追空往往是接盘。'],
    ['🧭 Market brief', 'The AI’s read of the market today: how bad the news is and how much of it prices already reflect. No bullish or bearish label, just probabilities and what would prove the view wrong.', 'Written every morning (when scheduled AI jobs are on) or when you press 🔄 Re-analyze, in about 3 minutes. The AI reads sector data, the day’s analysis articles, sector rotation and options sentiment. Crash odds must be anchored to the historical base rates: a 5% pullback about 3 times a year, 10% about once a year, 20% or more once every 4–5 years, and a bear-market probability above 22% needs systematic evidence. An automatic quality check reruns it once when it falls short.', 'Three parts matter most: the odds of how far a decline runs, the three layers of support, and per sector how bad the news is versus how much prices reflect. When bad news is already priced in, shorting it usually means buying someone else’s exit.'], true);
  E('picker:brief', 'brief', 'mb.run', '#mbRunBtn',
    ['🔄 重新分析', '马上重新生成今天的市场判断，约 3 分钟，期间可以切走做别的。', '', '需要先在 ⚙ 设置 → AI 选一个提供方，否则会提示「AI 未配置」。'],
    ['🔄 Re-analyze', 'Writes today’s market brief again now, in about 3 minutes; you can switch to other pages meanwhile.', '', 'Needs an AI provider in ⚙ Settings → AI, otherwise it says the AI is not configured.']);
  E(['picker:brief', 'picker:rot', 'picker:opt'], 'views', 'mb.subtabs', '#mbSubNav',
    ['三个子标签', '🧭 市场判断：AI 的文字判断；🔄 行业轮动：80 个细分行业谁强谁弱；🎲 期权情绪：各板块的期权贵不贵、避险需求大不大。', '', ''],
    ['Three sub-tabs', '🧭 Market brief: the AI’s written view. 🔄 Sector rotation: which of 80 industry groups are strong or weak. 🎲 Options sentiment: how expensive each sector’s options are and how much protection people are buying.', '', '']);
  E('picker:rot', 'rot', 'rot.chart', '#igChart',
    ['行业强弱图', '每根柱子是一个细分行业组：柱高 = 所选周期的涨跌幅，颜色 = 趋势状态（见「状态」）。只标两端各约 12 组的名字，鼠标停在柱子上看成员股和各自的收益。', '组的涨跌幅取组内成员的中位数，不是平均数：一只暴涨的小票拉不动整组。', '左边最强，右边最弱。颜色比高度更要紧：同样是正收益，绿色「趋势向上」和蓝色「超跌反弹」意思完全不同。'],
    ['Industry strength chart', 'Each bar is one industry group: height = the change over the chosen period, colour = the trend state (see “State”). Only about 12 groups at each end are labelled; hover a bar for its members and their returns.', 'A group’s change is the median of its members, not the mean, so one small stock that doubled can’t lift the whole group.', 'Strongest on the left, weakest on the right. The colour matters more than the height: a green uptrend and a blue oversold bounce can show the same gain and mean very different things.']);
  E('picker:rot', 'rot', 'rot.horizon', '#igHorizon',
    ['排序周期', '按哪个周期的涨跌给行业排序：短期 1 个月、中期 3 个月等。', '', ''],
    ['Sorting period', 'Which period’s change orders the groups: short term 1 month, medium 3 months and so on.', '', '']);
  E('picker:rot', 'rot', 'rot.reload', '#igRefreshBtn',
    ['🔄 重拉数据', '重新下载成员股的日线，重算所有组。', '', '图或表格是空的时候点它。'],
    ['🔄 Reload data', 'Downloads the members’ daily prices again and recomputes every group.', '', 'Use it when the chart or the table is empty.']);
  E('picker:rot', 'rot', 'rot.ma50', '#mbRotPane th:nth-child(7)',
    ['组内>MA50', '组里有多少比例的成员股价在自己的 50 日均线上方，也叫广度。', '数成员里收盘价 ≥ 50 日均线的比例。60% 以上绿，30–60% 黄，30% 以下红。', '收益是结果，广度是结构：「组涨 5%，只有 30% 成员在均线上」是少数几只撑着；「涨 5%，80% 在均线上」才是整个行业在走强。'],
    ['Members above MA50', 'The share of the group’s stocks trading above their own 50-day average, also called breadth.', 'The share of members whose close is at or above their 50-day average. Green from 60%, yellow 30–60%, red below 30%.', 'Return is the result, breadth is the structure: “up 5% with 30% above the average” is a few stocks carrying the group; “up 5% with 80% above” is the whole industry strengthening.'], true);
  E('picker:rot', 'rot', 'rot.state', '#mbRotPane th:nth-child(8)',
    ['状态', '用 1 个月和 3 个月的组收益，把每个组分成四种状态。', '🟢 趋势向上：1 个月 ≥ 0 且 3 个月 ≥ 0；🟡 高位回调：3 个月 ≥ 0 但 1 个月 < 0（中期仍强，短期回落）；🔵 超跌反弹（未确认）：3 个月 < 0 但 1 个月 ≥ 0（中期仍弱，短期在修复）；🔴 趋势向下：两个都 < 0。强弱看两者中较大的绝对值：10% 以上强，4% 以上中，其余弱。', '顺势做「趋势向上」；「高位回调」是强行业回踩的机会；「超跌反弹」还没确认转势，别当成趋势向上。'],
    ['State', 'Each group is put in one of four states from its 1-month and 3-month returns.', '🟢 Uptrend: 1 month ≥ 0 and 3 months ≥ 0. 🟡 Pullback from highs: 3 months ≥ 0 but 1 month < 0 (strong medium term, falling short term). 🔵 Oversold bounce (unconfirmed): 3 months < 0 but 1 month ≥ 0 (weak medium term, recovering short term). 🔴 Downtrend: both below 0. Strength uses the larger of the two in absolute value: strong from 10%, medium from 4%, weak below.', 'Trade with an uptrend; a pullback from highs is a dip in a strong industry; an oversold bounce has not turned the trend yet, so don’t treat it as an uptrend.'], true);
  E('picker:rot', 'rot', 'rot.ret', '#mbRotPane th:nth-child(4)',
    ['5日 / 1月 / 3月 / 6月 / 1年', '组在各个周期的涨跌幅，都是成员的中位数。旁边带 ⚠ 表示组内分化：3 个月收益的平均数和中位数相差 5 个百分点以上，说明涨的只是少数几只。', '', '有 ⚠ 的组别只看组收益，点开看是哪几只在动。'],
    ['5d / 1m / 3m / 6m / 1y', 'The group’s change over each period, each the median of its members. A ⚠ means the group is split: the 3-month mean and median differ by 5 points or more, so only a few stocks are moving.', '', 'For a group with ⚠, look at which members are moving rather than at the group number.']);
  E('picker:rot', 'rot', 'rot.heat', '',
    ['📈 升温 / 📉 降温', '短期强于中期趋势的 5 个组（在升温，可能刚启动），和短期弱于中期的 5 个组（在退潮，可能见顶）。', '升温度 = 1 个月收益 − 3 个月收益 ÷ 3（把 3 个月折成每个月来比）。', '升温的组是资金正在流入的地方。和「状态」一起看：「超跌反弹」又在升温的，值得盯，但还没确认。'],
    ['📈 Heating / 📉 Cooling', 'The 5 groups whose short term beats their medium-term trend (heating up, maybe just starting) and the 5 doing worse (cooling, maybe topping).', 'Heat = 1-month return − 3-month return ÷ 3 (the 3 months scaled to one month).', 'Heating groups are where money is flowing in. Read it with the state: an oversold bounce that is also heating is worth watching, but not confirmed.']);
  E('picker:rot', 'rot', 'rot.filters', '',
    ['隐藏 ETF 组 / 图只看显著', '「隐藏 ETF 组」不显示由 ETF 组成的组，只看个股组成的行业；「图只看显著（≥3%）」图里只画涨跌 3% 以上的组，表格仍显示全部。', '', ''],
    ['Hide ETF groups / significant only', '“Hide ETF groups” leaves out the groups made of ETFs, keeping the industries made of stocks. “Chart: significant only (≥3%)” draws only groups that moved 3% or more; the table still lists all of them.', '', '']);
  E('picker:opt', 'opt', 'opt.what', '#opBody',
    ['🎲 期权情绪', '每个板块的期权市场今天在定价什么：期权贵不贵，避险需求大不大，put 和 call 的量在哪边。不给方向标签。', '用收盘后的期权链，只看 20–60 天到期的合约（避开 0DTE 的赌博噪音和远期 LEAPS）。每只票算平值 IV、25Δ 偏斜、P/C 成交量比和 P/C 持仓比，再在这只票自己的历史里排百分位；板块的百分位取成员百分位的中位数。', 'IV 百分位 = 恐慌已经定价了多少。坏消息 + IV 低百分位 = 市场还没定价，风险在你这边；坏消息 + IV 高百分位 = 已经定价，这时追空往往是接盘。put 量大可能是看空，也可能是买保险或卖 put 收权利金，光看量分不出方向。'],
    ['🎲 Options sentiment', 'What each sector’s options market is pricing today: how expensive options are, how much protection is being bought, and which side the put and call volume is on. No direction labels.', 'From after-close option chains, using only contracts 20–60 days out (leaving out 0DTE gambling noise and far LEAPS). For each stock it computes the at-the-money IV, the 25-delta skew and the put/call volume and open-interest ratios, then ranks each against the stock’s own history; a sector’s percentile is the median of its members’.', 'The IV percentile says how much fear is already priced. Bad news with a low IV percentile = not priced yet, the risk is on your side; bad news with a high percentile = already priced, and shorting it usually means buying someone else’s exit. Heavy put volume can be bearish bets, insurance or put selling; volume alone can’t tell.']);
  E('picker:opt', 'opt', 'opt.sort', '#opSortKey',
    ['排序', '按哪个百分位给板块排：IV 百分位（恐慌已定价多少）、25Δ 偏斜百分位（避险需求）、P/C 成交量百分位、P/C 持仓百分位。', '', ''],
    ['Sort by', 'Which percentile orders the sectors: IV percentile (how much fear is priced), 25-delta skew percentile (demand for protection), put/call volume percentile, put/call open-interest percentile.', '', '']);
  E('picker:opt', 'opt', 'opt.cov', '#opBody th:nth-child(2)',
    ['覆盖', '这个板块有多少成员在期权数据池里。', '有数据的成员不到 3 只，或不到全部成员的一半，就算低覆盖：照样显示，但不参加排名；勾「隐藏低覆盖组」时不显示。', '低覆盖的板块可能被一两只小票代表，别把它的读数当成整个行业的。'],
    ['Coverage', 'How many of the sector’s members are in the options data.', 'Fewer than 3 members with data, or under half of all members, counts as low coverage: still shown, but left out of the rankings, and hidden when “Hide low-coverage groups” is ticked.', 'A low-coverage sector may be standing in for one or two small stocks, so don’t read it as the whole industry.'], true);
  E('picker:opt', 'opt', 'opt.iv', '#opBody th:nth-child(3)',
    ['IV 百分位', '这个板块的平值隐含波动率，在成员各自的历史里排第几（0–100）。', '每只成员票：今天 20–60 天到期的平值 IV 在它自己历史值里的百分位；板块的值 = 成员百分位的中位数。', '高 = 期权贵，市场已经在为大波动付钱；低 = 期权便宜。'],
    ['IV percentile', 'Where the sector’s at-the-money implied volatility stands within its members’ own history (0–100).', 'For each member: the percentile of today’s 20–60-day at-the-money IV among its own past values; the sector’s value is the median of the members’.', 'High = options are expensive and the market is already paying for a big move; low = options are cheap.'], true);
  E('picker:opt', 'opt', 'opt.skew', '#opBody th:nth-child(4)',
    ['偏斜百分位', '25Δ 的 put 比 25Δ 的 call 贵多少（偏斜），在自身历史里排第几。', '偏斜 = 25Δ 附近 put 的 IV − 25Δ 附近 call 的 IV，各取 15Δ–35Δ 之间最接近 25Δ 的 3 个合约的中位数；再排百分位，取成员的中位数。', '高 = 大家在花钱买下跌保护，避险情绪重。'],
    ['Skew percentile', 'How much more 25-delta puts cost than 25-delta calls (the skew), ranked against its own history.', 'Skew = the IV of puts near 25 delta − the IV of calls near 25 delta, each the median of the 3 contracts closest to 25 delta between 15 and 35 delta; then ranked, and the members’ median taken.', 'High = people are paying up for downside protection.'], true);
  E('picker:opt', 'opt', 'opt.pc', '#opBody th:nth-child(5)',
    ['P/C 量', 'put 成交量 ÷ call 成交量，以及它在自身历史里的百分位。', '', '量大不等于看空：可能是买保险，也可能是卖 put。只当「仓位在哪边」的线索。'],
    ['P/C volume', 'Put volume ÷ call volume, and its percentile in its own history.', '', 'High put volume isn’t necessarily bearish: it can be insurance or put selling. Treat it as a hint of where positions are.'], true);
  E('picker:opt', 'opt', 'opt.refresh', '#opRefreshBtn',
    ['🔄 汇总新交易日', '用最新的收盘后期权链，把新的交易日算进历史。', '', '刚开始用时要先攒几周收盘后的数据，百分位才有意义。'],
    ['🔄 Add the new trading day', 'Adds the latest trading day to the history from the newest after-close option chains.', '', 'It takes a few weeks of after-close data before the percentiles mean much.']);
  E('picker:opt', 'opt', 'opt.hidelow', '#opHideLow',
    ['隐藏低覆盖组', '不显示成员大多不在期权数据池里的板块（见「覆盖」）。', '', ''],
    ['Hide low-coverage groups', 'Leaves out the sectors whose members are mostly missing from the options data (see “Coverage”).', '', '']);

  // ================================================================== digest
  E('digest', 'modes', 'dg.today', '[onclick="digestMode(\'today\')"]',
    ['📡 今天', '今天各个来源的新闻流，新消息自动推进来。', '', ''],
    ['📡 Today', 'Today’s news from every source; new items stream in by themselves.', '', '']);
  E('digest', 'modes', 'dg.cal', '[onclick="digestMode(\'cal\')"]',
    ['📅 日历/历史', '按日期回看以前的新闻。', '', ''],
    ['📅 Calendar / history', 'Past news by date.', '', '']);
  E('digest', 'modes', 'dg.pre', '[onclick="digestMode(\'premarket\')"]',
    ['🌅 Pre-market', 'Benzinga 的盘前要闻和以往的盘前报道。', '', ''],
    ['🌅 Pre-market', 'Benzinga’s pre-market briefing and past ones.', '', '']);
  E('digest', 'modes', 'dg.like', '[onclick="digestMode(\'like\')"]',
    ['❤️ 喜欢', '你点了 🤍 收藏的新闻。', '', ''],
    ['❤️ Liked', 'The stories you saved with 🤍.', '', '']);
  E('digest', 'modes', 'dg.chart', '[onclick="digestMode(\'chart\')"]',
    ['📈 大盘图', 'SPY、QQQ 从 2011 年至今的日线蜡烛图：拖动或滚轮平移缩放，点某一根看那天的 AI 新闻总结（那天生成过的话）。', '', '大跌或大涨的那天，点开看当时市场在担心什么。'],
    ['📈 Market chart', 'Daily candles for SPY and QQQ since 2011: drag or scroll to pan and zoom, click a candle for that day’s AI news summary (when one was made).', '', 'On a day of a big drop or rally, click it to see what the market was worried about then.']);
  E('digest', 'filters', 'dg.src', '#tab-digest button|Finviz',
    ['新闻源', 'Benzinga、Finviz、WSJ、MarketWatch：点一家只看这一家。没有 Benzinga 账号时只用后面三个免费源。', '', ''],
    ['News sources', 'Benzinga, Finviz, WSJ, MarketWatch: click one to see only that source. Without a Benzinga account only the three free ones are used.', '', '']);
  E('digest', 'filters', 'dg.must', '[onclick="digestMustRead()"]',
    ['⭐ 必读', '只看必读：清单里的股票有强催化的新闻，和大的宏观新闻。', '', ''],
    ['⭐ Must-read', 'Only the must-reads: strong catalysts for the stocks on your list and big macro news.', '', '']);
  E('digest', 'filters', 'dg.fvai', '[onclick="digestFvai()"]',
    ['✦ AI', '只看 Finviz 的 AI「为什么异动」解读；配合日历能看历史。', '', ''],
    ['✦ AI', 'Only Finviz’s AI notes on why a stock moved; with the calendar you can see past ones.', '', '']);
  E('digest', 'filters', 'dg.chan', '#dgChanSel',
    ['🗂 分类', '按 Benzinga 的新闻分类筛选。', '', ''],
    ['🗂 Category', 'Filters by Benzinga’s news category.', '', '']);
  E('digest', 'filters', 'dg.sent', '[onclick="digestSent(\'pos\')"]',
    ['▲ 多 / ▼ 空 / ● 中 / 有情绪', '按新闻的情绪标签筛选：利多、利空、中性，或任何带情绪标签的。', '', '情绪标签只说明标题的倾向，不是对股价的预测。'],
    ['▲ Bull / ▼ Bear / ● Neutral / Has sentiment', 'Filters by the story’s sentiment tag: bullish, bearish, neutral, or any tagged story.', '', 'A sentiment tag describes the headline, not where the stock will go.']);
  E('digest', 'filters', 'dg.ticker', '[onclick="digestInTicker()"]',
    ['筛 / 清除', '按代码筛选（输入后按回车也行）；「清除」清掉分类、情绪和代码筛选。', '', ''],
    ['Filter / Clear', 'Filters by ticker (Enter works too); “Clear” resets the category, sentiment and ticker filters.', '', '']);
  E('digest', 'filters', 'dg.watch', '[onclick="digestToggleWatchlist()"]',
    ['🔖 只看清单', '只看清单里股票的新闻，默认打开；今天、日历和历史共用这个开关。', '', ''],
    ['🔖 List only', 'Shows only news about the stocks on your list (on by default); Today, Calendar and History share this switch.', '', '']);
  E('digest', 'filters', 'dg.denoise', '[onclick="digestToggleDenoise()"]',
    ['✨ 去噪', '过滤掉标题党，例如「3 只该买的股票」「发生了什么」这类；可以和「只看清单」一起开。', '', ''],
    ['✨ Denoise', 'Hides clickbait such as “3 stocks to buy” or “what’s going on”; it works together with “List only”.', '', '']);
  E('digest', 'actions', 'dg.read', '#dgReadHideBtn',
    ['✓ 标已读', '把现在看得到的新闻全部标成已读并隐藏。只在今天有效，过夜自动重置；之后新进来的照常显示。', '', '读完一屏点一下，剩下的就都是新消息。'],
    ['✓ Mark read', 'Marks every story you can see as read and hides it. It lasts for today and resets overnight; new stories still appear.', '', 'Click it after reading a screen so that only new stories remain.']);
  E('digest', 'actions', 'dg.expand', '[onclick="digestExpandAll(\'digestTodayList\',true)"]',
    ['⇊ 展开 / ⇈ 收起', '一次展开或收起所有新闻卡片。', '', ''],
    ['⇊ Expand / ⇈ Collapse', 'Opens or closes every story card at once.', '', '']);
  E('digest', 'actions', 'dg.ai', '[onclick="digestAiSummary()"]',
    ['🤖 AI 总结', '让 AI 把现在显示的标题里相似的合并，写一段中文速读。', '', '需要在 ⚙ 设置 → AI 选好提供方，用的是便宜的快速模型。'],
    ['🤖 AI summary', 'Has the AI merge similar headlines on screen and write a short summary in Chinese.', '', 'Needs an AI provider in ⚙ Settings → AI; it uses the cheap, fast model.']);
  E('digest', 'actions', 'dg.tk', '#dgTkToggleBtn',
    ['📊 Tickers', '按新闻条数给股票排名；点一只看这段时间它的全部新闻，可以切 1、3、7、30 天。', '', '新闻突然变多的股票往往有事。'],
    ['📊 Tickers', 'Ranks stocks by number of stories; click one for all of its news in the window (1, 3, 7 or 30 days).', '', 'A sudden jump in a stock’s news count often means something is happening.']);
  E('digest', 'actions', 'dg.pos', '#dgPosToggleBtn',
    ['💼 持仓 news', '只看你现在持仓的股票的新闻；持仓变了，新闻也跟着变。', '', ''],
    ['💼 Position news', 'Only news about the stocks you currently hold; it follows your positions as they change.', '', '']);
  E('digest', 'actions', 'dg.refresh', '[onclick="digestTodayLoad(true)"]',
    ['🔄 刷新', '马上重新取一遍新闻。', '', ''],
    ['🔄 Refresh', 'Fetches the news again right away.', '', '']);
  E('digest', 'actions', 'dg.cn', '#zsCnBtn',
    ['EN + 中文', 'English 界面下，在每条英文标题下面显示中文翻译。', '', ''],
    ['EN + 中文', 'In the English interface, shows the Chinese translation under each English headline.', '', '']);
  E('digest', 'actions', 'dg.likebtn', '',
    ['🤍 收藏', '每条新闻上的 🤍 把它收进 ❤️ 喜欢。', '', ''],
    ['🤍 Save', 'The 🤍 on a story saves it to ❤️ Liked.', '', '']);

  // ================================================================== events
  E('events', 'ev', 'ev.cal', '#evSubNav [data-ev="cal"]',
    ['📅 财报日历', '自选股和持仓接下来的财报日，以及 FOMC、CPI、非农等宏观事件。', '', '借方价差最好别跨财报。开单前看一眼到期前有没有财报。'],
    ['📅 Earnings calendar', 'Upcoming earnings for your watchlist and positions, plus macro events such as FOMC, CPI and the jobs report.', '', 'A debit spread is best kept clear of earnings; check for a report before expiration when opening one.']);
  E('events', 'ev', 'ev.corp', '#evSubNav [data-ev="me_corp"]',
    ['📋 企业事件', 'Benzinga 的公司事件：并购、IPO、增发和拆股，可以按代码筛选、选时间范围（默认 31 天）。需要 Benzinga。', '', '增发常常压股价，持仓的票有增发时留意。'],
    ['📋 Corporate events', 'Benzinga’s company events: mergers, IPOs, offerings and splits, filterable by ticker and time range (31 days by default). Needs Benzinga.', '', 'An offering often weighs on the price; watch for one in a stock you hold.']);
  E('events', 'ev', 'ev.sec', '#evSubNav [data-ev="sec"]',
    ['📄 SEC 文件', '读 SEC 文件（10-K、10-Q、8-K 等）和财报电话会的文字稿。', '', ''],
    ['📄 SEC filings', 'Read SEC filings (10-K, 10-Q, 8-K and so on) and earnings call transcripts.', '', '']);
  E('events', 'ev', 'ev.radar', '[onclick="openRiskRadar()"]',
    ['📡 风险雷达', '三件事：今天新闻里风险话题有多热（0–100 分），未来 30 天的事件密度，以及雷区日历（历史上容易大跌的日子）。', '数今天新闻里每个风险话题（美债收益率、关税、美联储、通胀、就业、银行信用等）出现了多少次，跟过去约 250 个交易日的基线比：超过 P90 算偏高，超过 P95 且占比高算饱和；再按历史研究里每个话题和大跌的关联倍数（lift）加权，得出 0–100 分。日历上的事件也贴着历史 lift 徽章。', '这是「高危窗口」的提示，不是预测。分数到 60 时，开单体检的第 4 关不通过。'],
    ['📡 Risk radar', 'Three things: how hot today’s risk topics are in the news (0–100), how crowded the next 30 days are with events, and a landmine calendar of days that often brought big drops.', 'It counts how often each risk topic (Treasury yields, tariffs, the Fed, inflation, jobs, bank credit and others) appears in today’s news and compares it with a baseline from about the last 250 trading days: above P90 is elevated, above P95 with a large share is saturated. Each topic is weighted by how much more likely big drops were around it in historical research (its lift), giving a 0–100 score. Calendar events carry their historical lift as a badge.', 'It flags high-risk windows; it is not a forecast. At 60 or more, gate 4 of the pre-trade checkup fails.']);
  E('events', 'ev', 'ev.refresh', '#events-refresh-btn',
    ['🔄 Refresh', '重新取事件数据。', '', ''],
    ['🔄 Refresh', 'Fetches the event data again.', '', '']);

  // ================================================================== trade journal (anchors live in the journal page)
  E('journal', 'stats', 'jr.trades', '.hstat:has(#h-total) .hstat-label',
    ['Trades 交易数', '交易总笔数，包括已平仓和还没平仓的。', '', ''],
    ['Trades', 'The number of trades, closed and still open.', '', ''], true);
  E('journal', 'stats', 'jr.win', '.hstat:has(#h-winpct) .hstat-label',
    ['Win % 胜率', '已平仓的交易里赚钱的比例。', '赚钱 = 平仓的毛盈亏 > $0.01，亏钱 = < −$0.01；胜率 = 赚钱笔数 ÷（赚钱 + 亏钱笔数）。打平的和没平仓的不算。50% 以上绿，40% 以下红。', '胜率要和盈利因子一起看：胜率不高，但赚的时候赚得多，照样能赚钱。'],
    ['Win %', 'The share of closed trades that made money.', 'A win = gross P/L above $0.01 when closed, a loss = below −$0.01; win % = wins ÷ (wins + losses). Breakeven and open trades don’t count. Green above 50%, red below 40%.', 'Read it with the profit factor: a modest win rate still makes money when the wins are bigger than the losses.'], true);
  E('journal', 'stats', 'jr.pf', '.hstat:has(#h-pf) .hstat-label',
    ['Profit Factor 盈利因子', '总赚 ÷ 总亏。', '所有赚钱交易的毛盈利之和 ÷ 所有亏钱交易的毛亏损之和，都不含手续费。1 以上显示绿色。', '大于 1 说明整体在赚钱，小于 1 说明在亏。'],
    ['Profit Factor', 'Total won ÷ total lost.', 'The gross profit of all winning trades ÷ the gross loss of all losing trades, both before fees. Green at 1 or more.', 'Above 1 the trading makes money overall; below 1 it loses.'], true);
  E('journal', 'stats', 'jr.avg', '.hstat:has(#h-avg) .hstat-label',
    ['Avg Trade 平均每笔', '平均每笔已平仓交易赚了多少。', '已平仓交易的毛盈亏之和 ÷ 已平仓笔数。', ''],
    ['Avg Trade', 'What a closed trade made on average.', 'The gross P/L of closed trades ÷ the number of closed trades.', ''], true);
  E('journal', 'stats', 'jr.closed', '.hstat:has(#h-closed) .hstat-label',
    ['Closed PnL 已平仓盈亏', '所有已平仓交易的盈亏之和，是毛盈亏，没扣手续费。', '', ''],
    ['Closed PnL', 'The P/L of all closed trades added up, gross before fees.', '', ''], true);
  E('journal', 'stats', 'jr.open', '.hstat:has(#h-open) .hstat-label',
    ['Unrealized 浮动盈亏', '还没平仓的持仓现在的盈亏：收盘价对平均开仓价。', '', ''],
    ['Unrealized', 'The P/L of positions still open: closing price against the average opening price.', '', ''], true);
  E('journal', 'stats', 'jr.mdd', '.hstat:has(#h-mdd) .hstat-label',
    ['Max DD 最大回撤', '累计盈亏从最高点往下跌得最深的那一次。', '把已平仓交易按平仓日期排好，累加净盈亏（扣了手续费）得到资金曲线；回撤 = 之前的最高点 − 当前值，取最大的一次。', '衡量最难熬的时候亏掉了多少。回撤越大，越该控制单笔风险。'],
    ['Max DD (maximum drawdown)', 'The deepest fall of the running P/L from a previous high.', 'Closed trades are ordered by closing date and their net P/L (after fees) is added up into an equity curve; a drawdown is the previous high minus the current value, and this is the largest one.', 'It measures how much was lost at the worst stretch. The bigger it is, the more each trade’s risk needs a cap.'], true);
  E('journal', 'tabs', 'jr.tabs', '.nav-tab',
    ['日记的标签页', 'Overview 总览（资金曲线和汇总）、Trades Table 交易明细、Calendar 日历（每天的盈亏）、Analysis 分析（按策略、标的、持仓天数等拆开看）、Review 复盘、Journal 交易笔记、Weekly 周报。', '', ''],
    ['Journal tabs', 'Overview (equity curve and totals), Trades Table (every trade), Calendar (P/L by day), Analysis (split by strategy, symbol, holding time and more), Review, Journal (your notes) and Weekly reports.', '', '']);

  // ================================================================== paper trade
  E('paper', 'pt', 'pt.what', '',
    ['🧪 模拟盘', '不花真钱试交易：建期权腿（买卖 call、put）和股票腿，看实时盈亏，和 OptionStrat 类似。', '按实时报价盯市：Σ（现价 − 开仓价）× 数量 × 乘数。最大盈亏、盈亏平衡和获利概率用的是持仓页的同一套算法。', '想验证一个价差，或者 TA 扫描页推荐的单子时，先放在这里看几天。'],
    ['🧪 Paper Trade', 'Practice trades without real money: build option legs (buy or sell calls and puts) and stock legs and watch the live P/L, much like OptionStrat.', 'Marked to market with live quotes: Σ (current price − entry price) × quantity × multiplier. The max profit and loss, breakevens and chance of profit use the same math as the Positions page.', 'Try a spread here for a few days first, including the ones the TA scanner suggests.']);
  E('paper', 'pt', 'pt.refresh', '[onclick="PT.refresh()"]',
    ['↻ Refresh P/L', '重新取报价，重算所有未平仓模拟交易的盈亏。', '', ''],
    ['↻ Refresh P/L', 'Fetches quotes again and recomputes the P/L of all open paper trades.', '', '']);
  E('paper', 'pt', 'pt.new', '[onclick="PT.newTrade()"]',
    ['+ New Trade', '新建一笔模拟交易：选标的、到期日和行权价，加期权腿或股票腿。', '', ''],
    ['+ New Trade', 'Starts a paper trade: pick the underlying, expiration and strikes and add option or stock legs.', '', '']);
  E('paper', 'pt', 'pt.group', '[onclick="PT.addGrp()"]',
    ['分组', '左边的下拉只看某个分组，+ 新建分组。', '', '用分组把不同的想法或策略分开。'],
    ['Groups', 'The menu beside it shows one group; + adds a group.', '', 'Use groups to keep different ideas or strategies apart.']);
  E('paper', 'pt', 'pt.csv', '[onclick="PT.csv()"]',
    ['Export CSV ↓', '把模拟交易导出成 CSV 表格。', '', ''],
    ['Export CSV ↓', 'Exports the paper trades as a CSV file.', '', '']);
  E('paper', 'pt', 'pt.gear', '',
    ['⚙ / 🗑', '每一行的 ⚙ 打开这笔交易的选项菜单，🗑 删除它。', '', ''],
    ['⚙ / 🗑', 'On each row, ⚙ opens the trade’s options menu and 🗑 deletes it.', '', '']);
  E('paper', 'cols', 'pt.c.total', 'th[onclick="PT.tgSort(\'pnl\')"]',
    ['Total Return 总收益', '这笔模拟交易从开仓到现在的盈亏。', '按实时报价盯市：Σ（各腿现价 − 开仓价）× 数量 × 乘数。', ''],
    ['Total Return', 'The paper trade’s P/L since it was opened.', 'Marked to market with live quotes: Σ (leg’s current price − entry price) × quantity × multiplier.', ''], true);
  E('paper', 'cols', 'pt.c.dte', 'th[onclick="PT.tgSort(\'dte\')"]',
    ['Days Until Exp', '离到期还有几天。', '', ''],
    ['Days Until Exp', 'Days left until expiration.', '', '']);

  // ================================================================== TA scanner
  E('ta', 'run', 'ta.run', '#runBtn',
    ['▶ 跑扫描', '对 TA 票池重新跑一遍技术分析：支撑阻力、趋势、信号回测、评分和期权推荐，约 2–4 分钟。', '', '每天收盘后跑一次就够了。第一次打开页面要先跑一次才有数据。'],
    ['▶ Run scan', 'Reruns the technical analysis for the TA pool: support and resistance, trends, signal backtests, scores and option suggestions, in 2–4 minutes.', '', 'Once a day after the close is enough. On first use, run it once to get any data.']);
  E('ta', 'run', 'ta.eod', '#eodCrawlBtn',
    ['⚡ 现在盘后爬取', '马上抓一次收盘后的期权链，期权结构和期权情绪用的就是它。', '', '平时收盘后会自动抓，漏了时手动点。'],
    ['⚡ Crawl after-close data now', 'Fetches the after-close option chains now; the options structure and options sentiment are built from them.', '', 'It normally runs by itself after the close; use it when a day was missed.']);
  E('ta', 'filt', 'ta.views', '#vbTable',
    ['📋 表格 / 📊 图 / 🔥 Heatmap', '同一批扫描结果的三种看法：表格，每只票一张图，按行业排的方格（大小 = 评分，颜色 = 今日涨跌）。', '', ''],
    ['📋 Table / 📊 Charts / 🔥 Heatmap', 'Three ways to view the same scan: a table, one chart per ticker, and tiles by industry (size = score, colour = today’s change).', '', '']);
  E('ta', 'filt', 'ta.cond', '#taFilterDD',
    ['TA 条件和 Barchart 榜单', '两个多选下拉：按 TA 条件筛选（多选时要同时满足），或按 Barchart 榜单筛选；可以存成预设，或设为默认。', '🏅 高把握 = 有方向且评分 ≥ 40；🎯 抄底@支撑 = 长期向上 + 短期回调 + 今天打到支撑；📉 今天大跌；触及 S/R；🟩 测试支撑；🟥 测试阻力；📈 看多 / 📉 看空 按长期趋势（200 日均线）分。', ''],
    ['TA conditions and Barchart lists', 'Two multi-select menus: filter by TA conditions (several must all hold) or by Barchart lists; a selection can be saved as a preset or as the default.', '🏅 High confidence = has a direction and a score of 40 or more; 🎯 Dip at support = long-term uptrend + short-term pullback + touched support today; 📉 Big drop today; Touching S/R; 🟩 Testing support; 🟥 Testing resistance; 📈 Bull / 📉 Bear by the long-term trend (200-day average).', '']);
  E('ta', 'filt', 'ta.sec', '#secSel',
    ['行业 / 财报', '按行业筛选；财报下拉可以排除或只看 14 天内有财报的票。', '', ''],
    ['Sector / earnings', 'Filter by sector; the earnings menu leaves out, or keeps only, tickers reporting within 14 days.', '', '']);
  E('ta', 'cols', 'ta.c.trend', '#hdr th[onclick="headSort(\'_trend\')"]',
    ['趋势 短·中·长', '20 日、50 日、200 日简单均线三个周期的趋势：▲ 向上、▼ 向下、— 中性。', '▲ = 价格在均线上方，并且均线在上升（20 日线看 8 天，50 日看 15 天，200 日看 20 天，涨幅至少 0.3%）；▼ = 在均线下方且均线在下降；其余是中性。', '三个都是 ▲ 的票，顺势最强；评分的趋势部分就是按这三个打分。'],
    ['Trend short · medium · long', 'The trend over three periods, by the 20-, 50- and 200-day simple averages: ▲ up, ▼ down, — neutral.', '▲ = the price is above the average and the average is rising (the 20-day over 8 days, the 50-day over 15, the 200-day over 20, by at least 0.3%); ▼ = below a falling average; anything else is neutral.', 'Three ▲ is the strongest trend; the score’s trend part counts exactly these.'], true);
  E('ta', 'cols', 'ta.c.touch', '#hdr th[onclick="headSort(\'_touch\')"]',
    ['S/R 状态', '今天的 K 线有没有打进某条支撑或阻力带：测试支撑、测试阻力、在带内，或没碰到。', '', ''],
    ['S/R status', 'Whether today’s bar reached a support or resistance zone: testing support, testing resistance, inside a zone, or none.', '', ''], true);
  E('ta', 'cols', 'ta.c.dist', '#hdr th[onclick="headSort(\'_dn\')"]',
    ['距下撑 / 距上阻', '现价到最近的下方支撑带、上方阻力带的距离（%）。', '', '离支撑近、离阻力远，向上的空间相对大。'],
    ['To support / to resistance', 'The distance (%) from the price to the nearest support zone below and resistance zone above.', '', 'Close to support and far from resistance leaves more room to rise.'], true);
  E('ta', 'cols', 'ta.c.conf', '#hdr th[onclick="headSort(\'max_conf\')"]',
    ['Conf 共振', '这只票的支撑阻力带最多被几个周期（日、周、月）一起确认：x2 = 两个周期，⭐x3 = 三个周期都有，最硬。', '', ''],
    ['Conf (confluence)', 'The most timeframes (daily, weekly, monthly) that confirm one of this ticker’s support or resistance zones: x2 = two, ⭐x3 = all three, the strongest.', '', ''], true);
  E('ta', 'cols', 'ta.c.sig', '#hdr th[onclick="headSort(\'_ssp\')"]',
    ['信号胜率', '几个经典信号（均线信号、MACD、RSI、放量）今天有没有触发，以及它们在这只票历史上的胜率。', '', '详情里有每个信号的样本数，以及是否回测达标。'],
    ['Signal win rates', 'Whether classic signals (moving-average signals, MACD, RSI, volume) fired today, and their win rates in this stock’s history.', '', 'The detail shows each signal’s sample size and whether it passes the backtest.'], true);
  E('ta', 'cols', 'ta.c.opt', '#hdr th[onclick="headSort(\'_opt\')"]',
    ['期权结构（仅展示）', '期权未平仓量和成交量的分布、P/C、GEX 等，只展示，不计入评分。', '', ''],
    ['Options structure (display only)', 'Where open interest and volume sit, put/call and GEX; shown, not scored.', '', ''], true);
  E('ta', 'cols', 'ta.c.rec', '#hdr th[onclick="headSort(\'_rec\')"]',
    ['期权推荐（debit）', '按方向自动挑的借记价差候选：看涨 = 牛市看涨价差（BCS），35–45 天到期。', '', '点开看每个候选的最大盈亏、R/R 和 POP；⚡ 一键放进模拟盘。'],
    ['Option suggestion (debit)', 'Debit spread candidates picked for the direction: bullish = a bull call spread (BCS), 35–45 days out.', '', 'Open it for each candidate’s max profit and loss, R/R and POP; ⚡ loads it into Paper Trade.'], true);
  // the TA scanner’s own ? icons (beside each block of a ticker’s detail) open these
  E('ta', 'det', 'ta.srzones', '',
    ['S/R 支撑阻力区', '把日线、周线、月线上的拐点（pivot 高低点）按价格聚成一条条价格带。很多拐点扎堆的价位，就是市场反复看的支撑或阻力。<ul><li><b>Conf</b>：这条带被几个周期一起确认；x2 = 两个周期，⭐x3 = 日、周、月三个都有，最硬。</li><li><b>中枢</b>：带里所有拐点的平均价；<b>区间</b>：带的下沿到上沿，价格进了这个区间就算触及。</li><li><b>周期</b>：拐点来自哪些周期，D = 日线，W = 周线，M = 月线。</li><li><b>pivots</b>：带里聚了几个拐点，越多说明这个价位被测试的次数越多。</li><li><b>距离</b>：收盘价到带中枢的百分比，负数 = 在现价下方（支撑），正数 = 上方（阻力）。</li></ul>', '', '图里金色带 = ⭐x3，蓝色带 = x2，白线 = 收盘价。'],
    ['Support and resistance zones', 'Turning points (pivot highs and lows) on the daily, weekly and monthly charts are clustered by price into zones. A price where many of them pile up is a level the market keeps testing.<ul><li><b>Conf</b>: how many timeframes confirm the zone; x2 = two, ⭐x3 = daily, weekly and monthly, the strongest.</li><li><b>Center</b>: the average price of the zone’s pivots; <b>range</b>: from its lowest to its highest pivot; a price inside it counts as touching.</li><li><b>Timeframes</b>: where the pivots come from, D = daily, W = weekly, M = monthly.</li><li><b>Pivots</b>: how many turning points are in the zone; more means more tests.</li><li><b>Distance</b>: from the close to the zone’s center; negative = below the price (support), positive = above (resistance).</li></ul>', '', 'In the chart, gold zones are ⭐x3, blue ones x2, and the white line is the close.']);
  E('ta', 'det', 'ta.edge', '',
    ['概率 edge（只展示）', '在这只票大约一年的历史里，给每一天贴上「市场状态 + 强弱档」两个标签，统计「以前处在同样状态时，5 个交易日后收高的比例」。是频率统计，不是预测，也不计入评分。', '<ul><li><b>牛市 / 熊市</b>：收盘价在 200 日均线上方 = 牛市，下方 = 熊市。</li><li><b>高波 / 低波</b>：ATR（真实波幅）在自己过去一年里偏高 = 高波，偏低 = 低波。高波只说明波动大，不代表看涨。</li><li><b>Q1–Q5</b>：把动量、RSI、离均线的距离合成一个强弱分，按过去一年排名切 5 档，Q1 最弱，Q5 最强。</li><li><b>n</b> = 出现过几次；<b>t</b> = 显著度，|t| > 2 才算可信。</li><li><b>支撑→4周+5%</b>：以前每次测试支撑后，4 周内涨过 5% 的比例；<b>阻力→1周−1%</b>：测试阻力后当周跌过 1% 的比例；<b>lift</b> = 比「每天随便买入」的基线高出几个百分点，至少 10 才算真本事。</li><li><b>52 周位置</b>：现价在 52 周最低到最高之间的位置，0% = 一年最低，100% = 一年最高。</li></ul>', '没有显著的方向优势时，与其赌涨跌，不如做中性策略（铁鹰、勒式）。'],
    ['Probability edge (display only)', 'Each day of the stock’s last year or so gets two labels, market state and strength band, and it counts how often the stock closed higher 5 trading days later when it was in the same state. A frequency, not a forecast, and not part of the score.', '<ul><li><b>Bull / bear</b>: close above the 200-day average = bull, below = bear.</li><li><b>High / low volatility</b>: the ATR (average true range) high or low within its own past year. High volatility only means bigger swings, not a rise.</li><li><b>Q1–Q5</b>: momentum, RSI and distance from the average combined into a strength score and ranked against the past year in five bands, Q1 weakest, Q5 strongest.</li><li><b>n</b> = how many times it happened; <b>t</b> = significance, believable when |t| > 2.</li><li><b>Support → 4 weeks +5%</b>: after past support tests, how often the stock rose 5% within 4 weeks; <b>resistance → 1 week −1%</b>: how often it fell 1% within the week; <b>lift</b> = points above buying on any random day, 10 or more counts.</li><li><b>52-week position</b>: where the price sits between the 52-week low (0%) and high (100%).</li></ul>', 'With no significant directional edge, a neutral strategy (iron condor, strangle) beats betting on direction.']);
  E('ta', 'det', 'ta.dip', '',
    ['抄底设置', '「是否适合抄底」的清单，三条都满足就标 🎯 抄底：', '<ul><li><b>长期多头</b>：收盘价在 200 日均线上方，并且 200 日均线在上升（跌破则标 🔻 长期偏空）。</li><li><b>跌到支撑</b>：今天的最低价打进了一条支撑带；带「历史达标」的，说明这只票以往测试支撑后反弹的回测达标。</li><li><b>回调</b>：今天跌了 2.5% 以上，或近 10 天跌了 5% 以上。</li></ul>', '三条都满足 = 长期向上 + 短期回调 + 跌到支撑，是看涨价差抄底的典型位置。'],
    ['Dip setup', 'A checklist for buying a dip; when all three hold, the ticker is marked 🎯:', '<ul><li><b>Long-term uptrend</b>: the close is above the 200-day average and that average is rising (below it, the ticker is marked 🔻 long-term weak).</li><li><b>At support</b>: today’s low reached a support zone; “passed” means this stock’s past bounces from support pass the backtest.</li><li><b>Pullback</b>: down 2.5% or more today, or 5% or more over 10 days.</li></ul>', 'All three together, a long-term uptrend with a short-term pullback into support, is the classic spot for a bullish spread on a dip.']);
  E('ta', 'det', 'ta.tasig', '',
    ['TA 信号实测', '经典技术信号逐票回测：每个信号都在这只票自己的历史上算胜率。', '<ul><li><b>MACD 金叉</b>（快线上穿慢线）看涨，<b>死叉</b>看跌；</li><li><b>RSI 超卖反转</b>（RSI 上穿 30）看涨，<b>超买回落</b>（下穿 70）看跌；</li><li><b>放量上攻</b>：成交量 ≥ 20 日均量的 2 倍，且收阳涨 1% 以上，看涨；</li><li><b>缩量</b>：近 10 天均量 < 前 10 天的 0.7 倍，看跌。</li></ul>表格：今日 = 今天有没有触发；N = 历史样本数；胜率；状态 = 达标或未达标。达标 = N ≥ 15，胜率 ≥ 60%，并且比基线高出至少 10 个百分点。下方还有当前的 RSI（≥ 75 超买、≤ 25 超卖标红）、MACD 多空和量比。', '这些信号只展示；评分里只有 MACD 的多空状态（MACD 线是否在信号线上方）算分。'],
    ['TA signals tested', 'Classic signals backtested ticker by ticker, each scored on this stock’s own history.', '<ul><li><b>MACD cross up</b> (fast line over slow) bullish, <b>cross down</b> bearish;</li><li><b>RSI oversold turn</b> (RSI back above 30) bullish, <b>overbought turn</b> (back below 70) bearish;</li><li><b>Volume surge</b>: volume at least 2× its 20-day average on an up day of 1% or more, bullish;</li><li><b>Drying volume</b>: the 10-day average volume under 0.7× the 10 days before, bearish.</li></ul>Table: Today = fired today; N = past samples; win rate; status = passes or not. Passing = N ≥ 15, a win rate of 60% or more, and at least 10 points above the baseline. Below: the current RSI (red at ≥ 75 overbought or ≤ 25 oversold), the MACD state and the volume ratio.', 'These signals are display only; of them, the score uses just the MACD state (whether the MACD line is above its signal line).']);
  E('ta', 'det', 'ta.ssp', '',
    ['均线信号胜率（SSP）', '一套均线信号逐票回测，按周统计胜负。看涨 5 个、看跌 2 个：', '<ul><li><b>STK</b>：EMA20 > 50 > 100 > 200 多头排列，且 RSI < 70；</li><li><b>X20 / X50 / X100 / X200</b>：收盘上穿 EMA20 / 50 / 100 / 200；</li><li><b>V1</b>：跌破 EMA20 且空头排列；<b>V2</b>：反抽 EMA20 失败的阴线。</li></ul>赢的定义：看涨 = 信号后 4 周内任何一周的最高价 ≥ 入场价 + 5%；看跌 = 信号那周的最低价 ≤ 入场价 − 1%。达标 = 样本 ≥ 20（看跌 15），胜率 ≥ 60%，并且比基线高出至少 10 个百分点。', '评分只用其中的 X100 / X200：今天上穿算 12 分，达标再加 8 分。'],
    ['Moving-average signals (SSP)', 'A set of moving-average signals backtested ticker by ticker and scored week by week. Five bullish, two bearish:', '<ul><li><b>STK</b>: EMA20 > 50 > 100 > 200 stacked and RSI under 70;</li><li><b>X20 / X50 / X100 / X200</b>: the close crosses above EMA20 / 50 / 100 / 200;</li><li><b>V1</b>: a break below EMA20 with the averages stacked down; <b>V2</b>: a red bar after a failed retest of EMA20.</li></ul>A win: bullish = any week’s high within 4 weeks at least 5% above the entry; bearish = the signal week’s low at least 1% below the entry. Passing = 20 samples (15 for bearish), a win rate of 60% or more, and at least 10 points above the baseline.', 'The score uses only X100 and X200: 12 points for a cross today, 8 more when it passes.']);
  E('ta', 'det', 'ta.opt', '',
    ['期权结构（只展示）', '用这只票最近 2 个月度期权链算的结构，<b>只展示，不计入评分</b>（回测里对方向基本是噪音）：', '<ul><li><b>OI / Vol 方向结构</b>：call 的未平仓量和成交量堆在现价上方 = 市场在赌往上；put 堆在下方 = 赌往下。离现价越远的行权价权重越低。</li><li><b>PCR（OI）</b>：put 未平仓量 ÷ call 未平仓量，< 1 = call 多，> 1 = put 多。</li><li><b>GEX</b>：gamma 敞口；现价在 gamma 翻转位上方 = 正 gamma 区（做市商压低波动，较稳），下方 = 负 gamma 区（下跌容易自我加速）。只用 60 天内到期的链。</li><li>MaxPain、OI 重心也只作参考。</li></ul>', '当作背景信息看，别拿它判断方向。'],
    ['Options structure (display only)', 'Built from the ticker’s two nearest monthly option chains, <b>shown but not scored</b> (in backtests it was noise for direction):', '<ul><li><b>OI / volume structure</b>: call open interest and volume piled above the price = bets on a rise; puts piled below = bets on a fall. Strikes further from the price weigh less.</li><li><b>PCR (OI)</b>: put open interest ÷ call open interest, under 1 = more calls, over 1 = more puts.</li><li><b>GEX</b>: gamma exposure; the price above the gamma flip = positive-gamma zone (dealers damp moves, steadier), below = negative-gamma zone (drops can feed on themselves). Uses chains within 60 days.</li><li>Max pain and the OI center are for reference only.</li></ul>', 'Treat it as background, not as a direction call.']);
  E('ta', 'det', 'ta.oaflow', '',
    ['异动期权流（只展示）', 'Benzinga 标好情绪的异常大单期权流，统计近 5 天看涨、看跌各花了多少权利金。', '<ul><li><b>看涨 / 看跌权利金</b>：各方向大单花的钱（$M = 百万美元）和笔数。</li><li><b>净多空</b> =（看涨权利金 − 看跌权利金）÷ 总额，> +5% 偏多，< −5% 偏空。</li></ul>只展示，不计入评分。需要 Benzinga。', '噪音大，只当旁证。'],
    ['Unusual option flow (display only)', 'Benzinga’s large, sentiment-tagged option orders: how much premium went into bullish and bearish bets over the last 5 days.', '<ul><li><b>Bullish / bearish premium</b>: the money spent on each side ($M = million) and the number of orders.</li><li><b>Net</b> = (bullish − bearish premium) ÷ total; above +5% leans bullish, below −5% bearish.</li></ul>Shown, not scored. Needs Benzinga.', 'Noisy; use it as a side note only.']);
  E('ta', 'det', 'ta.rec', '',
    ['期权推荐：借记价差', '按方向自动挑的借记价差候选。看涨开牛市看涨价差 BCS（买低行权价 call，卖高行权价 call）：35–45 天到期（目标 40 天，优先月度期权），长腿平值，短腿高 10%（默认）或 5%。看跌只提示方向，不推荐开单。', '<ul><li><b>借记</b>：净付出的权利金（每股）；<b>最大亏</b> = 借记 × 100；<b>最大盈</b> =（价差宽度 − 借记）× 100；</li><li><b>R/R</b> = 最大盈 ÷ 最大亏，越高越好；</li><li><b>POP</b> = 到期价越过盈亏平衡点的概率，用长腿的隐含波动率按对数正态估算，50% 以上绿；</li><li><b>BE vs 今价</b>：盈亏平衡价相对现价的百分比，绿 = 平衡价在现价下方，更稳；</li><li><b>✓推荐</b>：盈亏平衡价不高于现价的候选里 R/R 最高的那个。</li></ul>', 'R/R 高的 POP 通常低，两者要自己权衡。⚡ 把价差放进模拟盘（快捷键 B），📋 复制订单，⚠️财报 = 到期前有财报。'],
    ['Option suggestion: debit spreads', 'Debit spread candidates picked for the direction. Bullish = a bull call spread (buy a lower-strike call, sell a higher one): 35–45 days out (40 targeted, monthlies preferred), long leg at the money, short leg 10% higher (default) or 5%. Bearish only names the direction; no trade is suggested.', '<ul><li><b>Debit</b>: the net premium paid per share; <b>max loss</b> = debit × 100; <b>max profit</b> = (spread width − debit) × 100;</li><li><b>R/R</b> = max profit ÷ max loss, higher is better;</li><li><b>POP</b> = the probability the price ends past the breakeven, from the long leg’s implied volatility on a lognormal model; green at 50% or more;</li><li><b>BE vs price</b>: the breakeven relative to the price; green = below the price, safer;</li><li><b>✓ Suggested</b>: among the candidates with a breakeven at or below the price, the one with the best R/R.</li></ul>', 'A high R/R usually comes with a low POP; weigh the two. ⚡ loads the spread into Paper Trade (shortcut B), 📋 copies the order, ⚠️ earnings = a report before expiration.']);
  E('ta', 'det', 'ta.finviz', '',
    ['Finviz 技术 / 表现 / 基本面', '来自 Finviz 的补充数据：', '<ul><li><b>技术</b>：RSI(14)、ATR(14)（日均真实波幅）、SMA20/50/200（显示的是现价离这条均线的百分比，绿 = 在均线上方）、波动率。</li><li><b>表现</b>：周、月、季、半年、年、年初至今的涨跌幅。</li><li><b>基本面</b>：P/E 市盈率、Fwd P/E 预期市盈率、PEG、市值、P/S 市销率、P/B 市净率、Beta（相对大盘的波动）、股息率。</li><li><b>分析师评级（近 5 条）</b>：日期、动作（上调、维持、下调）、机构、评级和目标价。</li></ul>', ''],
    ['Finviz technicals, performance and fundamentals', 'Extra data from Finviz:', '<ul><li><b>Technicals</b>: RSI(14), ATR(14) (average daily true range), SMA20/50/200 (shown as the price’s distance from each average, green = above it) and volatility.</li><li><b>Performance</b>: the change over a week, month, quarter, half year, year and year to date.</li><li><b>Fundamentals</b>: P/E, forward P/E, PEG, market cap, P/S, P/B, beta (swings relative to the market) and dividend yield.</li><li><b>Analyst ratings (last 5)</b>: date, action (upgrade, maintain, downgrade), firm, rating and price target.</li></ul>', '']);

  // ================================================================== pre-trade checkup page
  E('checkup', 'gates', 'ck.gates', '',
    ['开单前体检：四道关', '开新的价差前，把四条开单纪律逐条检查；任何一关不过，结论就是「别开」，不模棱两可。', '<ul><li><b>第 1 关 长期趋势</b>：这只票和 SPY 都要站在 200 日均线的同一边，并且和你的方向一致（看涨要都在上方）。</li><li><b>第 2 关 回踩均线</b>：今天要回踩到 EMA20、50、100、200 之一。看涨 = 最低价在均线下方 1.5% 到均线之间；看跌 = 最高价在均线到上方 1.5% 之间。多条均线同时满足时，慢线优先。</li><li><b>第 3 关 IV 和财报</b>：IV Rank（一年百分位）不能高于 50，期权别太贵；财报日不能落在到期日加 3 天之内。</li><li><b>第 4 关 宏观</b>：VIX 高于 20 不过（15–20 提醒）；看 SPY 短期有没有急跌；风险雷达的新闻热度到 60 不过；美联储、通胀、就业、关税、银行信用这几个话题当天热到历史 P95 且 lift ≥ 2 时，也不过。</li></ul>', '先填代码、方向（bull 或 bear）和 DTE，再点 🩺 跑体检。'],
    ['Pre-trade checkup: four gates', 'Before opening a new spread, four trading rules are checked one by one; if any gate fails, the answer is “don’t open it”, with no hedging.', '<ul><li><b>Gate 1, long-term trend</b>: the stock and SPY must both be on the same side of their 200-day averages, matching your direction (both above for a bullish trade).</li><li><b>Gate 2, back to an average</b>: today must touch EMA20, 50, 100 or 200. Bullish = the low between 1.5% below the average and the average; bearish = the high between the average and 1.5% above. When several qualify, the slower one wins.</li><li><b>Gate 3, IV and earnings</b>: IV Rank (the one-year percentile) must not exceed 50, so options aren’t too expensive; earnings must not fall within the expiration plus 3 days.</li><li><b>Gate 4, macro</b>: VIX above 20 fails (15–20 warns); a sharp short-term fall in SPY is checked; a risk-radar news score of 60 fails; so does any of the Fed, inflation, jobs, tariffs and bank-credit topics reaching its historical P95 with a lift of 2 or more.</li></ul>', 'Enter the ticker, the direction (bull or bear) and the DTE, then press 🩺 to run it.']);
  E('checkup', 'use', 'ck.override', '',
    ['不同意结论时', '可以写理由强行通过（override），理由至少 20 个字，会记进日志。', '', '复盘时回头看这些「我觉得这次不一样」的单子到底怎么样，这是这一步的意义。'],
    ['When you disagree', 'You can override the verdict with a written reason of at least 20 characters; it goes into a log.', '', 'The point is the review later: see how the “this time is different” trades actually turned out.']);
  E('checkup', 'use', 'ck.run', '#runBtn',
    ['🩺 跑体检', '按上面填的代码、方向和天数跑四道关。🔄 重新跑一次。', '', ''],
    ['🩺 Run the checkup', 'Runs the four gates for the ticker, direction and days entered. 🔄 runs it again.', '', '']);

  // ================================================================== journal review page
  E('review', 'rv', 'rv.what', '',
    ['日记复盘（开盘前 5 分钟）', '每天开盘前花 5 分钟看的一页：顶上是关键数字和一句提示，下面是 📉 昨天平掉的单子、📊 昨天开的单子、👀 今天要重点盯的持仓、🚨 本周违反了哪些开单规则，最后是快速链接（跑今日体检、看今日选股、全时段砍单复盘）。', '只读本地的日记和缓存，不发新的行情请求。', '先看「本周规则违反」：同一条规则反复违反，说明纪律要收紧。'],
    ['Journal review (5 minutes before the open)', 'A page for the five minutes before each open: key numbers and one insight at the top, then 📉 trades closed yesterday, 📊 trades opened yesterday, 👀 positions to watch today, 🚨 the trading rules broken this week, and quick links (run today’s checkup, see today’s picks, the all-time cut review).', 'It reads only the local journal and caches and sends no new quote requests.', 'Start with the rules broken this week: one rule broken again and again means the discipline needs tightening.']);

  // ================================================================== engine
  function lang() {
    try { if (window.zeusLang) return window.zeusLang(); } catch (e) {}
    try { var s = localStorage.getItem('zeusLang'); if (s === 'zh' || s === 'bi' || s === 'en') return s; } catch (e) {}
    return (navigator.language || '').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en';
  }
  var UI = {
    what: ['是什么', 'What it is'], calc: ['怎么算', 'How it is calculated'], use: ['怎么用', 'How to use it'],
    close: ['✕ 关闭', '✕ Close'], back: ['← 返回列表', '← All items'], all: ['☰ 本页全部说明', '☰ Everything on this page'],
    show: ['👉 在页面上指给我看', '👉 Show me on the page'], guide: ['说明', 'Guide'],
    search: ['搜索按钮或指标…', 'Search a button or number…'], icons: ['页面上显示 ? 图标', 'Show ? icons on the page'],
    manual: ['完整使用手册 ↗', 'Full manual ↗'], none: ['没有找到', 'Nothing found'],
    q: ['这是什么？怎么算？', 'What is this? How is it calculated?'],
    btn: ['说明：这个页面上每个按钮和数字的意思（快捷键 ?）', 'Guide: what every button and number on this page means (shortcut ?)']
  };
  function T(pair) { var l = lang(); return l === 'en' ? pair[1] : (l === 'bi' ? pair[0] + ' · ' + pair[1] : pair[0]); }
  function title(e) { var l = lang(); return l === 'en' ? e.en[0] : (l === 'bi' ? e.zh[0] + ' · ' + e.en[0] : e.zh[0]); }
  function part(e, i) {
    var l = lang(), z = e.zh[i] || '', n = e.en[i] || '';
    if (l === 'en') return n || z;
    if (l === 'bi') return z + (n ? '<div class="zs-help-en">' + n + '</div>' : '');
    return z || n;
  }
  function plain(html) { return String(html || '').replace(/<li>/g, ' · ').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim(); }
  function summary(e) {
    var l = lang(), s = plain(l === 'en' ? e.en[1] : e.zh[1]);
    var cut = l === 'en' ? s.search(/\.(\s|$)/) : s.indexOf('。');     // the first sentence
    if (cut > 0) s = s.slice(0, cut + 1);
    s = s.replace(/[:：]$/, '');
    return s.length > 110 ? s.slice(0, 108) + '…' : s;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function manualUrl() {
    return 'https://github.com/jackson-cmd/zeus-options-terminal/blob/main/docs/' + (lang() === 'en' ? 'USER_GUIDE.md' : 'USER_GUIDE_zh.md');
  }

  // ---- which page is on screen
  function visible(el) { return !!(el && el.getClientRects && el.getClientRects().length); }
  function pagesNow() {
    var p = location.pathname;
    if (p.indexOf('/ta') === 0) return ['ta'];
    if (p.indexOf('/pretrade-checkup') === 0) return ['checkup'];
    if (p.indexOf('/journal-review') === 0) return ['review'];
    if (p.indexOf('/journal') === 0) return ['journal'];
    var tab = document.querySelector('.top-nav-tab.active');
    var t = tab ? tab.getAttribute('data-tab') : 'positions';
    var out;
    if (t === 'heatmap') {
      var host = document.getElementById('hmSubMbriefHost');
      if (visible(host)) out = visible(document.getElementById('mbRotPane')) ? 'picker:rot'
        : (visible(document.getElementById('mbOptPane')) ? 'picker:opt' : 'picker:brief');
      else out = (window.hmState && window.hmState.view === 'treemap') ? 'picker:heatmap' : 'picker:kline';
    } else out = t || 'positions';
    return [out, 'top'];
  }

  // ---- the element an item explains (the journal's live in its frame on the main page)
  function docFor(e) {
    if (e.pages.indexOf('journal') >= 0 && location.pathname.indexOf('/journal') !== 0) {
      var f = document.getElementById('journalFrame');
      try { return (f && f.contentDocument) || null; } catch (err) { return null; }
    }
    return document;
  }
  function find(e, doc) {
    if (!e.at) return [];
    doc = doc || docFor(e);
    if (!doc) return [];
    var bar = e.at.indexOf('|'), sel = bar < 0 ? e.at : e.at.slice(0, bar), has = bar < 0 ? '' : e.at.slice(bar + 1);
    var out;
    try { out = [].slice.call(doc.querySelectorAll(sel)); } catch (err) { return []; }
    if (has) out = out.filter(function (el) { return (el.textContent || '').indexOf(has) >= 0; });
    return out;
  }
  function target(e) {
    var all = find(e);
    for (var i = 0; i < all.length; i++) if (visible(all[i])) return all[i];
    return null;
  }

  // ---- the small ? icons
  function iconsOn() { try { return localStorage.getItem('zeusHelpIcons') !== '0'; } catch (e) { return true; } }
  function setIcons(on) {
    try { localStorage.setItem('zeusHelpIcons', on ? '1' : '0'); } catch (e) {}
    document.documentElement.classList.toggle('zs-noq', !on);
  }
  function qIcon(id) {
    var s = document.createElement('span');
    s.className = 'zs-q'; s.setAttribute('data-help', id); s.setAttribute('role', 'button'); s.tabIndex = 0;
    s.setAttribute('data-no-i18n', ''); s.title = T(UI.q); s.setAttribute('aria-label', T(UI.q));
    return s;
  }
  function decorate() {
    scheduled = false;
    ensureButton();
    for (var i = 0; i < LIST.length; i++) {
      var e = LIST[i];
      if (!e.q) continue;
      var els = find(e, document);
      for (var j = 0; j < els.length; j++) {
        var el = els[j];
        if (e.q === 'after' || /^(SELECT|INPUT|BUTTON|TEXTAREA)$/.test(el.tagName)) {
          var n = el.nextElementSibling;
          if (!(n && n.classList.contains('zs-q') && n.getAttribute('data-help') === e.id)) el.insertAdjacentElement('afterend', qIcon(e.id));
        } else if (!(el.lastElementChild && el.lastElementChild.classList.contains('zs-q'))) {
          var old = el.querySelector('.zs-q[data-help="' + e.id + '"]');
          if (old) old.remove();
          el.appendChild(qIcon(e.id));
        }
      }
    }
  }
  var scheduled = false;
  function schedule() { if (!scheduled) { scheduled = true; setTimeout(decorate, 250); } }

  // ---- the ? button: in the top bar, or round at the bottom right where there is no top bar
  function ensureButton() {
    if (document.getElementById('zsHelpBtn') || document.getElementById('zsHelpFab')) return;
    var right = document.querySelector('#topNav .top-nav-right');
    if (right) {
      var gear = document.getElementById('zsGearBtn');
      if (!gear || gear.parentNode !== right) return;    // zeus.js puts ⚙ in first; next pass
      var b = document.createElement('button');
      b.id = 'zsHelpBtn'; b.type = 'button'; b.className = 'privacy-btn'; b.textContent = '?';
      b.setAttribute('data-no-i18n', ''); b.title = T(UI.btn); b.setAttribute('aria-label', T(UI.btn));
      b.onclick = function () { guide(); };
      right.insertBefore(b, gear);
      return;
    }
    if (/[?&]embedded=1/.test(location.search) || !document.body) return;   // the journal inside the main page
    var f = document.createElement('button');
    f.id = 'zsHelpFab'; f.type = 'button'; f.textContent = '?';
    f.setAttribute('data-no-i18n', ''); f.title = T(UI.btn); f.setAttribute('aria-label', T(UI.btn));
    f.onclick = function () { guide(); };
    document.body.appendChild(f);
  }

  // ---- the card / guide overlay
  function overlay() {
    var ov = document.getElementById('zsHelpOverlay');
    if (ov) return ov;
    ov = document.createElement('div');
    ov.id = 'zsHelpOverlay'; ov.setAttribute('data-no-i18n', '');
    ov.innerHTML = '<div class="zs-help-card" role="dialog" aria-modal="true"><div class="zs-help-head">'
      + '<b class="zs-help-title"></b><button type="button" class="zs-help-back"></button>'
      + '<button type="button" class="zs-help-x"></button></div><div class="zs-help-body"></div></div>';
    ov.addEventListener('click', function (ev) { if (ev.target === ov) close(); });
    ov.querySelector('.zs-help-x').onclick = close;
    ov.querySelector('.zs-help-back').onclick = function () { guide(); };
    document.body.appendChild(ov);
    return ov;
  }
  function show(ov) {
    ov.querySelector('.zs-help-x').textContent = T(UI.close);
    ov.classList.add('on');
    ov.querySelector('.zs-help-card').scrollTop = 0;
  }
  function close() {
    var ov = document.getElementById('zsHelpOverlay');
    if (ov) ov.classList.remove('on');
  }
  function flash(el) {
    try { el.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
    el.classList.add('zs-help-flash');
    setTimeout(function () { el.classList.remove('zs-help-flash'); }, 2400);
  }
  function open(id, fromGuide) {
    var e = REG[id];
    if (!e) return false;
    var ov = overlay(), body = ov.querySelector('.zs-help-body');
    ov.querySelector('.zs-help-title').textContent = title(e);
    var h = '';
    [['what', 1], ['calc', 2], ['use', 3]].forEach(function (p) {
      if (!(e.zh[p[1]] || e.en[p[1]])) return;
      h += '<div class="zs-help-sec"><div class="zs-help-k">' + T(UI[p[0]]) + '</div><div>' + part(e, p[1]) + '</div></div>';
    });
    var el = target(e);
    h += '<div class="zs-help-foot">' + (el ? '<button type="button" class="zs-help-show">' + T(UI.show) + '</button>' : '')
      + '<a class="zs-help-man" target="_blank" rel="noopener" href="' + manualUrl() + '">' + T(UI.manual) + '</a></div>';
    body.innerHTML = h;
    var sb = body.querySelector('.zs-help-show');
    if (sb) sb.onclick = function () { close(); flash(el); };
    var back = ov.querySelector('.zs-help-back');
    back.textContent = T(fromGuide ? UI.back : UI.all);
    show(ov);
    return true;
  }
  function guide() {
    var ov = overlay(), body = ov.querySelector('.zs-help-body');
    var pages = pagesNow(), l = lang();
    var name = PAGE_NAME[pages[0]] || ['', ''];
    ov.querySelector('.zs-help-title').textContent = T(UI.guide) + ' · ' + (l === 'en' ? name[1] : (l === 'bi' ? name[0] + ' · ' + name[1] : name[0]));
    ov.querySelector('.zs-help-back').textContent = '';
    body.innerHTML = '<div class="zs-help-tools"><input class="zs-help-filter" type="search" placeholder="' + esc(T(UI.search)) + '">'
      + '<label class="zs-help-icons"><input type="checkbox"> ' + T(UI.icons) + '</label></div>'
      + '<div class="zs-help-list"></div><div class="zs-help-foot"><a class="zs-help-man" target="_blank" rel="noopener" href="'
      + manualUrl() + '">' + T(UI.manual) + '</a></div>';
    var input = body.querySelector('.zs-help-filter'), box = body.querySelector('.zs-help-icons input');
    var list = body.querySelector('.zs-help-list');
    var paint = function () { list.innerHTML = rows(pages, input.value); };
    input.oninput = paint;
    box.checked = iconsOn();
    box.onchange = function () { setIcons(box.checked); };
    list.addEventListener('click', function (ev) {
      var r = ev.target.closest && ev.target.closest('.zs-help-row');
      if (r) open(r.getAttribute('data-id'), true);
    });
    paint();
    show(ov);
    setTimeout(function () { try { input.focus(); } catch (e) {} }, 30);
  }
  function rows(pages, q) {
    q = String(q || '').trim().toLowerCase();
    var l = lang(), h = '', n = 0;
    pages.forEach(function (pg) {
      var lastGroup = null;
      LIST.forEach(function (e) {
        if (e.pages.indexOf(pg) < 0) return;
        if (pg === 'top' && pages[0] !== 'top' && e.pages.length > 1 && e.pages.indexOf(pages[0]) >= 0) return;
        if (q && (e.zh.join(' ') + ' ' + e.en.join(' ')).toLowerCase().indexOf(q) < 0) return;
        var gk = GROUPS[pg + '/' + e.group] || GROUPS['*/' + e.group] || ['', ''];
        if (e.group + pg !== lastGroup) {
          lastGroup = e.group + pg;
          h += '<div class="zs-help-grp">' + esc(l === 'en' ? gk[1] : (l === 'bi' ? gk[0] + ' · ' + gk[1] : gk[0])) + '</div>';
        }
        h += '<button type="button" class="zs-help-row" data-id="' + e.id + '"><span class="t">' + esc(title(e))
          + '</span><span class="s">' + esc(summary(e)) + '</span></button>';
        n++;
      });
    });
    return n ? h : '<div class="zs-help-none">' + T(UI.none) + '</div>';
  }

  // ---- events
  document.addEventListener('click', function (ev) {
    var q = ev.target && ev.target.closest && ev.target.closest('.zs-q');
    if (!q) return;
    ev.preventDefault(); ev.stopPropagation();      // a ? in a sortable header must not sort it
    open(q.getAttribute('data-help'));
  }, true);
  window.addEventListener('keydown', function (ev) {
    var ov = document.getElementById('zsHelpOverlay'), on = !!(ov && ov.classList.contains('on'));
    if (ev.key === 'Escape' && on) {       // window capture: before zeus.js's Escape (which goes back a view)
      ev.preventDefault(); ev.stopPropagation(); close(); return;
    }
    var t = ev.target;
    var typing = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
    if ((ev.key === 'Enter' || ev.key === ' ') && t && t.classList && t.classList.contains('zs-q')) {
      ev.preventDefault(); open(t.getAttribute('data-help')); return;
    }
    if (ev.key === '?' && !typing && !on && !ev.ctrlKey && !ev.metaKey && !ev.altKey) { ev.preventDefault(); guide(); }
  }, true);

  window.zeusHelp = {
    open: open, guide: guide, close: close,
    has: function (id) { return !!REG[id]; },
    entries: function () { return LIST.map(function (e) { return { id: e.id, pages: e.pages, group: e.group, at: e.at, q: e.q, zh: e.zh, en: e.en }; }); },
    groups: function () { return GROUPS; }
  };

  function boot() {
    if (!document.body) return;
    setIcons(iconsOn());
    decorate();
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
