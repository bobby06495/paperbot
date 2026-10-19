/* Strategy & scanner page: parameters, filter funnel, exit ladder, limits, sizing what-if,
 * and the market-scanning live view. Read-only: nothing here changes the bot. */
(function () {
  'use strict';
  const PB = window.PB;
  const $ = (id) => document.getElementById(id);
  const esc = PB.esc;

  let scanFilter = 'all';
  let calc = null; // sizing inputs, kept across data reloads

  const STATUS = {
    SIGNAL: { tone: 'green', rank: 0, hint: 'passed every filter on its last evaluation' },
    NEAR: { tone: 'amber', rank: 1, hint: `price within the near threshold of its next trigger` },
    WAITING: { tone: 'sky', rank: 2, hint: 'failing a check that can still pass today' },
    'IN POSITION': { tone: 'violet', rank: 3, hint: 'already held' },
    'ENTERED TODAY': { tone: 'violet', rank: 4, hint: 'one entry per symbol per day' },
    'NOT EVALUATED': { tone: 'slate', rank: 5, hint: 'no evaluation recorded today' },
    SKIPPED: { tone: 'slate', rank: 6, hint: 'skipped before the filters' },
    'BLOCKED TODAY': { tone: 'slate', rank: 7, hint: 'failed a check that cannot change today' },
  };
  const SCAN_FILTERS = [['all', 'All'], ['actionable', 'Signals & pending'], ['blocked', 'Blocked today'], ['none', 'Not evaluated']];

  /* ------------------------------------------------------------ helpers */

  function stepParams(rules) {
    const uf = rules.universe_filters || {}, df = rules.daily_filters || {}, inf = rules.intraday_filters || {};
    const on = (flag, text) => (flag === false ? 'disabled' : text);
    return {
      contract: 'US stock, SMART routing, qualified at IBKR',
      daily_history: '1 year of daily bars (cached per day)',
      prior_price: `prior close ≥ $${uf.min_price_usd}`,
      D2: on(df.D2_prior_close_above_sma200, 'prior close > 200-day SMA of closes'),
      bars: '≥ 2 completed regular-hours 5-minute bars',
      price: `last 5m close ≥ $${uf.min_price_usd}`,
      D3: `09:30 open ≥ ${df.D3_min_gap_pct_from_prior_close}% above prior close`,
      D1: on(df.D1_above_prior_day_high, 'last 5m close > prior-day high'),
      I1: on(inf.I1_above_premarket_high, 'last 5m close > premarket high'),
      I2: on(inf.I2_above_today_hod, "last 5m close > today's high of earlier bars"),
      I3: `cumulative volume ≥ ${inf.I3_rvol_min}× the ${inf.I3_rvol_lookback_days}-day average at this time`,
    };
  }
  const STATIC_STEPS = new Set(['contract', 'daily_history', 'prior_price', 'D2', 'D3']);

  function stopPct(rules) {
    const m = /lod_minus_([\d.]+)pct/.exec(((rules.exit || {}).initial_stop_rule) || '');
    return m ? parseFloat(m[1]) : 1;
  }
  const round2 = (x) => Math.round(x * 100) / 100;

  /* ------------------------------------------------------------ identity */

  function renderIdentity(d) {
    const r = d.strategy.rules || {};
    const uf = r.universe_filters || {};
    const tf = r.time_filter || {};
    const fact = (label, value) => `<div class="min-w-0"><dt class="text-[10px] font-semibold uppercase tracking-[.12em] text-ink-400">${label}</dt>
      <dd class="mt-0.5 truncate text-sm text-slate-100">${value}</dd></div>`;
    $('identity').innerHTML = `<section class="pb-backdrop rounded-2xl border border-ink-700/80 bg-ink-900/80 p-4 sm:p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div><p class="text-[11px] font-semibold uppercase tracking-[.12em] text-ink-400">Active strategy</p>
          <h1 class="mt-1 text-2xl font-semibold tracking-tight text-white">${esc(r.strategy_name || '—')}</h1>
          <p class="mt-1 text-sm text-ink-300">Joins strong gap-up trends on a fresh intraday high, with a broker-held stop below the day's low.</p></div>
        <div class="flex flex-wrap gap-2">${PB.pill((r.direction || '').replace('_', ' '), 'sky')}${PB.pill(`${r.trade_timeframe || ''} bars`, 'slate')}${PB.pill('read-only view', 'slate')}</div>
      </div>
      <dl class="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        ${fact('Universe', `${esc(uf.index || '—')}, top gappers`)}
        ${fact('Min price', `$${esc(uf.min_price_usd)}`)}
        ${fact('Entry window', `${esc(tf.earliest_entry_et)}–${esc(tf.latest_entry_et)} ET`)}
        ${fact('Force close', `${esc(tf.force_close_et)} ET`)}
        ${fact('Cadence', d.scheduler.mode === 'continuous' ? `every ${esc(d.model.loop_seconds)} s` : 'every 5 min (HT_Cycle)')}
        ${fact('Scan budget', `${esc(d.strategy.scan_budget_s)} s per pass`)}
      </dl>
      <p class="mt-4 text-xs text-ink-400">Values come from config/rules.json and config/config.json as of ${PB.et(d.generated_at)} ET. The bot reads them on its first pass, so edits apply after a restart.</p>
    </section>`;
  }

  /* ------------------------------------------------------------ scanner */

  function nextTrigger(row, d) {
    const e = row.eval;
    if (row.status === 'IN POSITION') return 'Held: managed by the exit rules';
    if (row.status === 'ENTERED TODAY') return 'Already entered today; not re-entered';
    if (!e) {
      const phase = PB.phaseAt(Date.now(), d);
      return phase === 'entry_window' ? 'Queued for the next scan' : `First scan at ${d.model.times.entry_start} ET`;
    }
    if (e.pass) return `Passed all ${e.total_steps} filters at ${PB.et(e.time, false)} ET. Entry limit ≈ ${PB.num(round2((e.price || 0) * 1.002))}, stop ${PB.num(e.stop)}`;
    const thr = PB.num(e.threshold);
    switch (e.check) {
      case 'D1': return `5m close above prior-day high ${thr}`;
      case 'I1': return `5m close above premarket high ${thr}`;
      case 'I2': return `5m close above today's high ${thr}`;
      case 'I3': return `RVOL ≥ ${PB.num(e.threshold, 1)} (now ${PB.isNum(e.current) ? e.current.toFixed(2) : 'n/a'})`;
      case 'price': return `Price ≥ $${thr}`;
      case 'bars': return 'Needs 2 completed 5m bars (from 09:40 ET)';
      case 'precheck': return e.check_label;
      default: return `${e.check_label}: cannot pass today`;
    }
  }

  function levels(e) {
    if (!e || !e.levels) return '—';
    const l = e.levels;
    const parts = [['PDH', l.prior_high], ['PMH', l.premarket_high], ['HOD', l.today_high], ['SMA200', l.sma200]]
      .filter(([, v]) => PB.isNum(v)).map(([k, v]) => `<span class="text-ink-400">${k}</span> ${PB.num(v)}`);
    if (!e.pass && PB.isNum(e.threshold) && ['D1', 'I1', 'I2'].includes(e.check)) {
      parts.push(`<span class="text-amber-300">${{ D1: 'PDH', I1: 'PMH', I2: 'HOD' }[e.check]}</span> ${PB.num(e.threshold)}`);
    }
    return parts.join(' · ') || '—';
  }

  function scanVisible(row) {
    if (scanFilter === 'all') return true;
    if (scanFilter === 'blocked') return row.status === 'BLOCKED TODAY' || row.status === 'SKIPPED';
    if (scanFilter === 'none') return row.status === 'NOT EVALUATED';
    return ['SIGNAL', 'NEAR', 'WAITING', 'IN POSITION'].includes(row.status);
  }

  function renderScanner(d) {
    const sc = d.scanner || { rows: [] };
    const rows = (sc.rows || []).slice().sort((a, b) => {
      const ra = (STATUS[a.status] || {}).rank ?? 9, rb = (STATUS[b.status] || {}).rank ?? 9;
      if (ra !== rb) return ra - rb;
      const da = a.eval && PB.isNum(a.eval.distance_pct) ? a.eval.distance_pct : 1e9;
      const db = b.eval && PB.isNum(b.eval.distance_pct) ? b.eval.distance_pct : 1e9;
      return da - db || a.symbol.localeCompare(b.symbol);
    });
    const counts = {};
    rows.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
    const shown = rows.filter(scanVisible);
    const near = d.model.near_pct;
    const body = shown.map((r) => {
      const e = r.eval;
      const st = STATUS[r.status] || STATUS['NOT EVALUATED'];
      const progress = e ? e.passed_steps / e.total_steps : 0;
      const dist = e && !e.pass && !e.static && PB.isNum(e.distance_pct) ? e.distance_pct : null;
      return `<tr class="border-t border-ink-700/60 ${r.status === 'BLOCKED TODAY' ? 'opacity-60' : ''}">
        <td class="py-2.5 pr-3"><span class="font-mono text-sm font-semibold text-white">${esc(r.symbol)}</span>${r.in_list ? '' : '<span class="ml-1 text-[10px] text-ink-400">not in list</span>'}</td>
        <td class="py-2.5 pr-3 text-right tabular ${PB.signClass(r.watch && r.watch.gap_pct)}">${r.watch ? PB.pct(r.watch.gap_pct) : e && PB.isNum(e.gap) ? PB.pct(e.gap) : '—'}</td>
        <td class="py-2.5 pr-3 text-right"><span class="tabular text-white">${e ? PB.num(e.price) : '—'}</span>
          <span class="block text-[10px] tabular text-ink-400">${e ? PB.et(e.time, false) + ' ET' : ''}</span></td>
        <td class="py-2.5 pr-3" title="${esc(st.hint)}">${PB.pill(r.status, st.tone, { dot: r.status === 'SIGNAL' || r.status === 'NEAR', live: r.status === 'SIGNAL' })}</td>
        <td class="py-2.5 pr-3"><div class="flex items-center gap-2"><div class="h-1.5 w-20 rounded-full bg-ink-800"><div class="h-1.5 rounded-full ${e && e.pass ? 'bg-emerald-400' : e && e.static ? 'bg-ink-500' : 'bg-amber-400/80'}" style="width:${(progress * 100).toFixed(0)}%"></div></div>
          <span class="text-[10px] tabular text-ink-400">${e ? `${e.passed_steps}/${e.total_steps}` : ''}</span></div></td>
        <td class="py-2.5 pr-3 text-xs text-slate-300">${esc(nextTrigger(r, d))}</td>
        <td class="py-2.5 pr-3 text-right text-xs tabular ${dist == null ? 'text-ink-400' : dist <= near ? 'text-amber-300' : 'text-ink-300'}">${dist == null ? '—' : PB.pct(dist)}</td>
        <td class="py-2.5 pr-3 text-right text-xs tabular">${e && PB.isNum(e.rvol) ? e.rvol.toFixed(2) : '—'}</td>
        <td class="py-2.5 text-[11px] tabular text-slate-300">${levels(e)}</td></tr>`;
    }).join('');
    const filters = SCAN_FILTERS.map(([k, label]) => `<button type="button" data-scan="${k}" aria-pressed="${scanFilter === k}"
      class="rounded-lg px-2.5 py-1 text-xs ${scanFilter === k ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800 hover:text-white'}">${label}</button>`).join('');
    const summary = Object.keys(STATUS).filter((k) => counts[k]).map((k) => `<span class="inline-flex items-center gap-1.5">${PB.pill(k, STATUS[k].tone)}<span class="tabular text-ink-300">${counts[k]}</span></span>`).join('');
    const source = sc.source === 'watchlist'
      ? `today’s watchlist · generated ${esc(sc.generated || '?')} ET`
      : '<span class="text-amber-300">universe.txt fallback (no watchlist generated today)</span>';
    $('scanner').innerHTML = PB.panel('Market scanning · live view', `
      <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div class="flex flex-wrap gap-1" role="group" aria-label="Filter symbols">${filters}</div>
        <div class="ml-auto flex flex-wrap gap-3 text-xs">${summary}</div>
      </div>
      <div class="-mx-1 mt-3 overflow-x-auto">${shown.length ? `<table class="w-full min-w-[980px] text-left text-sm">
        <thead><tr class="text-[11px] uppercase tracking-wide text-ink-400">
          <th class="pb-2 pr-3 font-medium">Symbol</th><th class="pb-2 pr-3 text-right font-medium">Gap</th><th class="pb-2 pr-3 text-right font-medium">Last price</th>
          <th class="pb-2 pr-3 font-medium">Status</th><th class="pb-2 pr-3 font-medium">Filters</th><th class="pb-2 pr-3 font-medium">Next trigger</th>
          <th class="pb-2 pr-3 text-right font-medium">Distance</th><th class="pb-2 pr-3 text-right font-medium">RVOL</th><th class="pb-2 font-medium">Known levels</th></tr></thead>
        <tbody>${body}</tbody></table>` : PB.empty(rows.length ? 'No symbols in this filter.' : 'No symbols to scan yet.')}</div>
      <p class="mt-3 text-xs text-ink-400">Last price is the close of the last completed 5-minute bar when the symbol was last evaluated; Paperbot does not stream quotes.
        Distance is how far price (or RVOL) must still rise to pass the check that stopped it; “near” is within ${near}%.
        Checks run in order and stop at the first failure, so later checks are unknown until earlier ones pass.</p>`,
    { right: `<span class="text-xs text-ink-400">${sc.size} symbols from ${source}</span>` });
  }

  /* ------------------------------------------------------------ pipeline */

  function renderPipeline(d) {
    const params = stepParams(d.strategy.rules || {});
    const fun = d.strategy.funnel || [];
    const evaluated = fun.reduce((s, f) => s + f.stopped, 0);
    const peak = Math.max(1, ...fun.map((f) => f.stopped));
    const items = fun.map((f, i) => {
      const isPass = f.key === 'pass';
      return `<li class="flex items-start gap-3 py-2">
        <span class="mt-0.5 grid h-6 w-6 flex-none place-items-center rounded-md text-[11px] font-semibold ${isPass ? 'bg-emerald-400/15 text-emerald-300' : 'bg-ink-700 text-slate-200'}">${isPass ? '✓' : i + 1}</span>
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-baseline gap-x-2"><p class="text-sm font-medium text-slate-100">${esc(f.label)}</p>
            ${!isPass ? `<span class="text-[10px] uppercase tracking-wide ${STATIC_STEPS.has(f.key) ? 'text-ink-400' : 'text-sky-300'}">${STATIC_STEPS.has(f.key) ? 'fixed for the day' : 'can change intraday'}</span>` : ''}</div>
          <p class="text-xs text-ink-300">${esc(isPass ? 'Signal → sized entry bracket (BUY limit + GTC stop) through the risk guard' : params[f.key] || '')}</p>
        </div>
        <div class="w-28 flex-none text-right"><div class="ml-auto h-1.5 rounded-full bg-ink-800"><div class="ml-auto h-1.5 rounded-full ${isPass ? 'bg-emerald-400' : 'bg-amber-400/70'}" style="width:${(f.stopped / peak) * 100}%"></div></div>
          <span class="text-[10px] tabular text-ink-400">${f.stopped} ${isPass ? 'passed' : 'stopped here'}</span></div></li>`;
    }).join('');
    $('pipeline').innerHTML = PB.panel('Entry filter pipeline', `<ol class="divide-y divide-ink-700/50">${items}</ol>
      <p class="mt-3 text-xs text-ink-400">Counts use each symbol's latest evaluation today (${evaluated} symbols). Evaluation is cheapest-first and stops at the first failure.</p>`,
    { right: '<span class="text-xs text-ink-400">strategy.evaluate()</span>' });
  }

  /* ------------------------------------------------------------ exits */

  function renderExits(d) {
    const r = d.strategy.rules || {};
    const ex = r.exit || {};
    const tf = r.time_filter || {};
    const lo = -1.35, hi = 2.1;
    const x = (v) => `${(((v - lo) / (hi - lo)) * 100).toFixed(2)}%`;
    const marks = [
      { r: -1, label: 'Initial stop', sub: `day low − ${stopPct(r)}%`, tone: 'bg-rose-400', text: 'text-rose-300' },
      { r: 0, label: 'Entry', sub: 'avg cost', tone: 'bg-slate-200', text: 'text-slate-200' },
      { r: ex.partial_profit_trigger_R, label: `+${ex.partial_profit_trigger_R}R`, sub: `sell ${Math.round((ex.partial_profit_fraction || 0) * 100)}%`, tone: 'bg-amber-400', text: 'text-amber-300' },
      { r: ex.breakeven_trigger_R, label: `+${ex.breakeven_trigger_R}R`, sub: 'stop → entry', tone: 'bg-emerald-400', text: 'text-emerald-300' },
    ];
    const swing = /swing_low_5m_(\d+)_(\d+)/.exec(ex.post_breakeven_trail || '') || [];
    $('exits').innerHTML = PB.panel('Exit ladder', `
      <div class="relative mb-4 mt-2 h-28">
        <div class="absolute inset-x-0 top-9 h-1 rounded-full bg-gradient-to-r from-rose-500/50 via-ink-600 to-emerald-400/60"></div>
        <div class="absolute top-9 h-1 rounded-full bg-sky-400/40" style="left:${x(ex.breakeven_trigger_R)};right:0" title="Trailing zone"></div>
        ${marks.map((m, i) => `<div class="absolute -translate-x-1/2 text-center" style="left:${x(m.r)};top:${i % 2 ? '3.1rem' : '0'}">
          ${i % 2 ? `<div class="mx-auto h-3 w-3 -translate-y-[0.95rem] rounded-full ring-4 ring-ink-900 ${m.tone}"></div>` : ''}
          <p class="whitespace-nowrap text-[11px] font-semibold ${m.text}">${esc(m.label)}</p><p class="whitespace-nowrap text-[10px] text-ink-400">${esc(m.sub)}</p>
          ${i % 2 ? '' : `<div class="mx-auto mt-0.5 h-3 w-3 rounded-full ring-4 ring-ink-900 ${m.tone}"></div>`}</div>`).join('')}
        <span class="absolute right-0 top-[3.3rem] text-[10px] text-sky-300">trail →</span>
      </div>
      <ul class="space-y-1.5 text-xs text-slate-300">
        <li><b class="text-white">R</b> = average cost − initial stop: the dollars risked per share.</li>
        <li><b class="text-rose-300">Stop</b>: SELL STP, GTC, sent with the entry as a bracket and held by IBKR. Only ever moves up.</li>
        <li><b class="text-amber-300">Partial</b>: at +${esc(ex.partial_profit_trigger_R)}R sell ${Math.round((ex.partial_profit_fraction || 0) * 100)}% (stop shrunk first); one attempt, cancelled after 60 s if unfilled. Skipped for 1-share positions.</li>
        <li><b class="text-emerald-300">Breakeven</b>: at +${esc(ex.breakeven_trigger_R)}R the stop moves to the entry price.</li>
        <li><b class="text-sky-300">Trail</b>: then the stop follows the latest 5m swing low (${esc(swing[1] || 2)} bars before, ${esc(swing[2] || 2)} after).</li>
        <li><b class="text-violet-300">Force close</b> from ${esc(tf.force_close_et)} ET: stop cancelled, marketable limit SELL re-priced about every 60 s, 0.2% wider per attempt (max 1%).</li>
      </ul>`);
  }

  function renderSession(d) {
    const tm = d.model.times || {};
    const row = (t, label, cls = 'text-slate-300') => `<li class="flex gap-3"><span class="w-12 flex-none font-mono tabular text-ink-300">${esc(t)}</span><span class="${cls}">${esc(label)}</span></li>`;
    $('session').innerHTML = PB.panel('Trading day', `${PB.timeline(d, Date.now())}
      <ul class="mt-4 space-y-1 text-xs">
        ${row('09:25', 'Log rotation')}${row(tm.open, 'First run: protect positions, manage exits')}
        ${row('09:55', 'Prefilter builds the watchlist (re-run every 30 min until 12:55)', 'text-amber-200')}
        ${row(tm.entry_start, 'Entry scans begin', 'text-emerald-300')}${row(tm.entry_end, 'Last entry scan (inclusive to the second)', 'text-amber-200')}
        ${row(tm.force_close, 'Force close starts', 'text-violet-300')}${row(tm.alert, 'ALERT for anything still open', 'text-rose-300')}
        ${row(tm.close, 'Close: DAY orders expire; stops are restored for anything left')}${row('16:05', 'Daily summary push')}
      </ul>`);
  }

  /* ------------------------------------------------------------ limits & sizing */

  function renderLimits(d) {
    const lim = d.strategy.limits || {};
    const risk = d.strategy.risk || {};
    const nlv = d.account && d.account.net_liquidation;
    const pctOf = (p) => (PB.isNum(nlv) ? ` = ${PB.money(nlv * p / 100, false, 0)}` : '');
    const rows = [
      ['Risk per trade', `${risk.max_risk_per_trade_pct}% of net liquidation${pctOf(risk.max_risk_per_trade_pct)}`, 'rules.json', 'Entry sizing + guard'],
      ['Position size', `${risk.max_position_size_pct_of_portfolio}% of net liquidation${pctOf(risk.max_position_size_pct_of_portfolio)}`, 'rules.json', 'Entry sizing + guard'],
      ['Concurrent positions', risk.max_concurrent_positions, 'rules.json', 'Scan + guard (manual positions count)'],
      ['Shares per order', lim.max_quantity_per_order, 'config.json', 'Entry sizing + guard'],
      ['Order value', PB.money(lim.max_order_value, false, 0), 'config.json', 'Entry sizing + guard'],
      ['Entries per day', lim.max_orders_per_day, 'config.json', 'Guard'],
      ['Working entry/exit orders', lim.max_open_orders, 'config.json', 'Guard (stops not counted)'],
      ['Daily loss halt', PB.money(-Math.abs(lim.max_daily_loss || 0), false, 0), 'config.json', 'Halts entries; cancels working entries/exits'],
      ['Order types', lim.limit_orders_only ? 'limit orders only (stops are STP)' : 'market allowed', 'config.json', 'Guard'],
      ['Time in force', `${d.strategy.time_in_force} entries/exits · GTC stops`, 'config.json', 'Orders'],
      ['Allowed symbols', (lim.allowed_symbols || []).join(', ') === '*' ? 'any' : (lim.allowed_symbols || []).join(', '), 'config.json', 'Guard'],
    ].map(([k, v, src, eff]) => `<tr class="border-t border-ink-700/60"><td class="py-2 pr-3 text-slate-200">${esc(k)}</td>
      <td class="py-2 pr-3 tabular text-white">${esc(v)}</td><td class="py-2 pr-3 font-mono text-[11px] text-ink-400">${esc(src)}</td>
      <td class="py-2 text-xs text-ink-300">${esc(eff)}</td></tr>`).join('');
    $('limits').innerHTML = PB.panel('Risk limits', `<div class="overflow-x-auto"><table class="w-full min-w-[560px] text-left text-sm">
      <thead><tr class="text-[11px] uppercase tracking-wide text-ink-400"><th class="pb-2 pr-3 font-medium">Limit</th><th class="pb-2 pr-3 font-medium">Value</th>
        <th class="pb-2 pr-3 font-medium">Source</th><th class="pb-2 font-medium">Applies to</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${d.account && !PB.isNum(d.account.net_liquidation) && PB.isNum(d.account.net_liquidation_base)
    ? `<p class="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-200 ring-1 ring-inset ring-rose-400/30">The account reports net liquidation in ${esc(d.account.base_currency)} only.
        The bot reads the USD value, so the percentage limits have nothing to work from: it skips the entry scan and the guard blocks entries.</p>` : ''}
      <p class="mt-3 text-xs text-ink-400">Entry limits never block exits or stops: those only have to be valid and no larger than the shares held, so the bot can always get out and never goes short.
      Not enforced by the strategy: multi-day drawdown, buying power, wash-sale rules, bid/ask spread.</p>`);
  }

  function sizing(d, inp) {
    const r = d.strategy.rules || {};
    const lim = d.strategy.limits || {};
    const risk = r.risk || {};
    const stop = round2(inp.lod * (1 - stopPct(r) / 100));
    const limit = round2(inp.price * 1.002);
    const perShare = limit - stop;
    const cons = [
      ['1% risk', perShare > 0 ? (inp.nlv * risk.max_risk_per_trade_pct / 100) / perShare : 0],
      [`${risk.max_position_size_pct_of_portfolio}% size`, (inp.nlv * risk.max_position_size_pct_of_portfolio / 100) / limit],
      ['Shares cap', lim.max_quantity_per_order],
      ['Order value cap', lim.max_order_value / limit],
    ];
    const qty = perShare > 0 ? Math.floor(Math.min(...cons.map((c) => c[1]))) : 0;
    const binding = cons.reduce((a, b) => (b[1] < a[1] ? b : a));
    return { stop, limit, perShare, cons, qty, binding };
  }

  function renderSizing(d) {
    if (!calc) {
      const sig = (d.scanner.rows || []).find((r) => r.eval && r.eval.price);
      const price = sig ? sig.eval.price : 100;
      const a = d.account || {};
      calc = { price, lod: round2(price * 0.98), nlv: a.net_liquidation || a.net_liquidation_base || 1000000 };
    }
    const s = sizing(d, calc);
    const input = (key, label, step) => `<label class="block"><span class="text-[11px] uppercase tracking-wide text-ink-400">${label}</span>
      <input data-calc="${key}" type="number" step="${step}" min="0" value="${calc[key]}" class="mt-1 w-full rounded-lg border border-ink-700 bg-ink-850 px-3 py-1.5 text-sm tabular text-white focus:border-sky-400/60 focus:outline-none"></label>`;
    $('sizing').innerHTML = PB.panel('Sizing what-if', `
      <div class="grid grid-cols-3 gap-3">${input('price', 'Last 5m close', '0.01')}${input('lod', 'Day low', '0.01')}${input('nlv', 'Net liq', '1000')}</div>
      <dl class="mt-4 grid grid-cols-3 gap-3 text-xs">
        <div><dt class="text-ink-400">Entry limit</dt><dd class="tabular text-white">${PB.num(s.limit)}</dd></div>
        <div><dt class="text-ink-400">Stop</dt><dd class="tabular text-rose-300">${PB.num(s.stop)}</dd></div>
        <div><dt class="text-ink-400">Risk / share</dt><dd class="tabular text-white">${PB.num(s.perShare)}</dd></div></dl>
      <ul class="mt-4 space-y-1.5 text-xs">${s.cons.map((c) => `<li class="flex justify-between gap-3 ${c === s.binding ? 'text-amber-300' : 'text-slate-300'}">
        <span>${esc(c[0])}${c === s.binding ? ' · binding' : ''}</span><span class="tabular">${PB.num(c[1], 1)} sh</span></li>`).join('')}</ul>
      <div class="mt-4 flex items-baseline justify-between border-t border-ink-700/70 pt-3">
        <span class="text-xs text-ink-300">Shares the bot would buy</span>
        <span class="font-mono text-2xl font-semibold tabular ${s.qty ? 'text-emerald-300' : 'text-rose-300'}">${s.qty}</span></div>
      <p class="mt-1 text-right text-[11px] tabular text-ink-400">${s.qty ? `${PB.money(s.qty * s.limit)} position · ${PB.money(s.qty * s.perShare)} at risk` : 'no trade: under 1 share or invalid stop'}</p>
      <p class="mt-3 text-[11px] text-ink-400">Same formula as the bot's sizing step, computed in your browser only. Rounding can differ by a cent.</p>`,
    { right: '<span class="text-xs text-ink-400">does not affect the bot</span>' });
  }

  /* ------------------------------------------------------------ wiring */

  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-scan]');
    if (b) { scanFilter = b.getAttribute('data-scan'); renderScanner(PB.data); }
  });
  document.addEventListener('change', (ev) => {
    const el = ev.target.closest('[data-calc]');
    if (!el || !calc) return;
    const v = parseFloat(el.value);
    if (isFinite(v) && v > 0) calc[el.getAttribute('data-calc')] = v;
    renderSizing(PB.data);
  });

  PB.onData((d) => {
    renderIdentity(d);
    renderScanner(d);
    renderPipeline(d);
    renderExits(d);
    renderSession(d);
    renderLimits(d);
    renderSizing(d);
    $('footer').innerHTML = `Generated ${PB.et(d.generated_at)} ET · scanner rows use each symbol's latest evaluation in logs/telemetry.jsonl · read-only`;
  });
  PB.start('strategy.html');
})();
