/* Run log page: every run today with its four steps, plus the order lifecycle. Read-only. */
(function () {
  'use strict';
  const PB = window.PB;
  const $ = (id) => document.getElementById(id);
  const esc = PB.esc;

  const openRuns = new Set();
  const openOrders = new Set();
  let filter = 'all';
  let query = '';
  let items = [];
  let first = true;

  const FILTERS = [
    ['all', 'All'], ['activity', 'With activity'], ['orders', 'Orders'], ['alerts', 'Alerts'], ['problems', 'Problems'],
  ];
  const STATUS_RANK = { fail: 3, warn: 2, pass: 1, info: 1, skip: 0 };

  /* ------------------------------------------------------------ model */

  function worst(validations) {
    let w = 'pass';
    (validations || []).forEach((v) => { if ((STATUS_RANK[v.status] || 0) > STATUS_RANK[w]) w = v.status; });
    return w;
  }

  function buildItems(d) {
    const groups = new Map();
    (d.runs || []).forEach((r) => {
      const key = r.source === 'cycle.py' ? r.run_id : `${r.run_id}#${r.pass_in_run}`;
      if (!groups.has(key)) groups.set(key, { key, type: 'run', passes: [] });
      groups.get(key).passes.push(r);
    });
    const runs = [...groups.values()].map((g) => {
      const first = g.passes[0];
      const last = g.passes[g.passes.length - 1];
      const symbols = new Set();
      let evaluated = 0, signals = 0, orders = 0, requests = 0, alerts = 0, w = 'pass';
      g.passes.forEach((p) => {
        evaluated += (p.scan && p.scan.evaluated) || 0;
        signals += (p.signals || []).length;
        orders += (p.order_events || []).length;
        requests += p.requests || 0;
        const a = (p.validations || []).find((v) => v.key === 'alerts');
        alerts += (a && a.value) || 0;
        const pw = worst(p.validations);
        if (STATUS_RANK[pw] > STATUS_RANK[w]) w = pw;
        (p.evaluations || []).forEach((e) => symbols.add(e.symbol));
        (p.order_events || []).forEach((e) => e.symbol && symbols.add(String(e.symbol).toUpperCase()));
        (p.positions || []).forEach((x) => symbols.add(x.symbol));
      });
      return {
        ...g, time: first.started || first.ended, end: last.ended, phase: first.phase, source: first.source,
        mode: first.mode, evaluated, signals, orders, requests, alerts, worst: w, symbols: [...symbols],
        duration: first.started ? PB.ms(last.ended) - PB.ms(first.started) : null,
      };
    });
    const extras = (d.timeline_extras || []).map((x) => ({ ...x, key: `x-${x.kind}-${x.time}`, type: x.kind, symbols: [] }));
    const all = runs.concat(extras).sort((a, b) => PB.ms(a.time) - PB.ms(b.time));
    let n = 0;
    all.forEach((it) => { if (it.type === 'run') { n += 1; it.number = n; } });
    return all.reverse();
  }

  function visible(it) {
    const q = query.trim().toUpperCase();
    if (q && !(it.symbols.some((s) => s.includes(q)) || String(it.text || '').toUpperCase().includes(q))) return false;
    if (filter === 'all') return true;
    if (it.type !== 'run') return filter !== 'orders';
    if (filter === 'activity') return it.requests > 0 || it.signals > 0 || it.evaluated > 0 || it.orders > 0;
    if (filter === 'orders') return it.orders > 0;
    if (filter === 'alerts') return it.alerts > 0;
    if (filter === 'problems') return it.worst === 'fail' || it.worst === 'warn';
    return true;
  }

  /* ------------------------------------------------------------ summary */

  function renderSummary(d) {
    const r = d.kpis.runs;
    const runsWithOrders = items.filter((i) => i.type === 'run' && i.orders > 0).length;
    const evaluated = items.reduce((s, i) => s + (i.evaluated || 0), 0);
    const tile = (label, value, tone = 'text-white', sub = '') => `<div class="rounded-xl border border-ink-700/80 bg-ink-900/80 px-4 py-3">
      <p class="text-[10px] font-semibold uppercase tracking-[.12em] text-ink-400">${label}</p>
      <p class="mt-1 text-2xl font-semibold tabular ${tone}">${value}</p>${sub ? `<p class="text-[11px] text-ink-400">${sub}</p>` : ''}</div>`;
    $('summary').innerHTML = tile('Runs executed', r.executed, 'text-white', `${r.passes} passes recorded`)
      + tile('Skipped', r.skipped, r.skipped ? 'text-amber-300' : 'text-white', 'STOP file / already running')
      + tile('Failed', r.failed, r.failed ? 'text-rose-300' : 'text-white', 'cycle_errors.log')
      + tile('Evaluations', evaluated, 'text-white', 'symbols run through the filters')
      + tile('Runs with orders', runsWithOrders, runsWithOrders ? 'text-sky-300' : 'text-white')
      + tile('Orders today', (d.orders || []).length, 'text-white', `${(d.intents || []).length} dry-run/blocked`);
    if (d.runs_truncated) {
      $('summary').insertAdjacentHTML('beforeend', `<p class="col-span-full text-xs text-amber-300">${d.runs_truncated} older passes are counted but not shown (dashboard keeps the latest ${d.runs.length}).</p>`);
    }
  }

  function renderFilters() {
    $('filters').innerHTML = FILTERS.map(([k, label]) => `<button type="button" data-filter="${k}" aria-pressed="${filter === k}"
      class="rounded-lg px-2.5 py-1 text-xs ${filter === k ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800 hover:text-white'}">${label}</button>`).join('');
  }

  /* ------------------------------------------------------------ timeline rows */

  function badge(it) {
    if (it.type === 'skipped') return PB.pill('Skipped', 'amber');
    if (it.type === 'failed') return PB.pill('Failed start', 'red');
    if (it.type === 'error') return PB.pill('Error', 'red');
    if (it.type === 'missed') return PB.pill('Missed', 'red');
    if (it.passes.length > 1) return PB.pill(`Run · ${it.passes.length} passes`, 'violet');
    return PB.pill(it.source === 'bot.py' ? 'Loop' : 'Run', 'green');
  }

  function stat(label, value, cls = '') {
    return `<span class="whitespace-nowrap"><span class="text-ink-400">${label}</span> <span class="tabular ${cls}">${value}</span></span>`;
  }

  function rowHeader(it) {
    const open = openRuns.has(it.key);
    if (it.type !== 'run') {
      return `<div class="flex flex-wrap items-start gap-x-4 gap-y-1 px-4 py-3 sm:px-5">
        <span class="w-20 flex-none font-mono text-sm tabular text-slate-300">${PB.et(it.time)}</span>${badge(it)}
        <p class="min-w-0 flex-1 break-words text-xs text-slate-300">${esc(it.text)}</p></div>`;
    }
    const tone = { fail: 'bg-rose-500', warn: 'bg-amber-400', pass: 'bg-emerald-400/80', info: 'bg-sky-400', skip: 'bg-ink-600' }[it.worst];
    return `<button type="button" data-run="${esc(it.key)}" aria-expanded="${open}" class="flex w-full flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-3 text-left hover:bg-ink-850 sm:px-5 ${open ? 'pb-open bg-ink-850' : ''}">
      <span class="pb-chev w-3 text-ink-400">›</span>
      <span class="w-20 font-mono text-sm tabular text-white">${PB.et(it.time)}</span>
      <span class="w-12 text-xs tabular text-ink-400">#${it.number}</span>
      ${badge(it)} ${PB.pill(PB.PHASES[it.phase] ? PB.PHASES[it.phase].label : it.phase, PB.PHASES[it.phase] ? PB.PHASES[it.phase].tone : 'slate')}
      <span class="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        ${stat('evaluated', it.evaluated)}${stat('signals', it.signals, it.signals ? 'text-emerald-300' : '')}
        ${stat('orders', it.orders, it.orders ? 'text-sky-300' : '')}${stat('alerts', it.alerts, it.alerts ? 'text-rose-300' : '')}
        ${stat('took', it.duration != null ? PB.dur(it.duration) : '—')}
        <span class="inline-block h-2.5 w-2.5 rounded-sm ${tone}" title="Worst validation: ${esc(it.worst)}"></span>
      </span></button>`;
  }

  function step(n, title, body, right = '') {
    return `<section class="rounded-xl border border-ink-700/70 bg-ink-950/60">
      <header class="flex flex-wrap items-center gap-2 border-b border-ink-700/60 px-3.5 py-2.5">
        <span class="grid h-5 w-5 place-items-center rounded-md bg-ink-700 text-[11px] font-semibold text-white">${n}</span>
        <h4 class="text-xs font-semibold uppercase tracking-[.08em] text-slate-200">${title}</h4><span class="ml-auto">${right}</span></header>
      <div class="p-3.5 text-sm">${body}</div></section>`;
  }

  const table = (head, rows, min = 560) => `<div class="overflow-x-auto"><table class="w-full text-left text-xs" style="min-width:${min}px">
    <thead><tr class="text-[10px] uppercase tracking-wide text-ink-400">${head.map((h) => `<th class="pb-2 pr-3 font-medium">${h}</th>`).join('')}</tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  const td = (v, cls = '') => `<td class="border-t border-ink-700/50 py-1.5 pr-3 align-top ${cls}">${v}</td>`;

  /** Why the bot's entry scan returned without evaluating anything, when the telemetry shows it. */
  function skipReason(p) {
    const v = (key) => (p.validations || []).find((x) => x.key === key) || {};
    if (v('net_liq').status === 'fail') return `Scan skipped by the bot: ${v('net_liq').detail || 'no USD net liquidation'}.`;
    if (v('concurrency').status === 'warn') return 'Scan skipped by the bot: maximum concurrent positions reached.';
    return null;
  }

  function scanStep(p) {
    const s = p.scan || {};
    const a = p.account || {};
    const acct = `<dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
      <div><dt class="text-ink-400">Net liquidation</dt><dd class="tabular text-slate-200">${PB.acctValue(a, 'net_liquidation')}</dd></div>
      <div><dt class="text-ink-400">Buying power</dt><dd class="tabular text-slate-200">${PB.acctValue(a, 'buying_power')}</dd></div>
      <div><dt class="text-ink-400">Daily P&amp;L</dt><dd class="tabular ${PB.signClass(a.daily_pnl)}">${PB.money(a.daily_pnl, true)}</dd></div>
      <div><dt class="text-ink-400">Positions / working orders</dt><dd class="tabular text-slate-200">${(p.positions || []).length} / ${(p.orders || []).length}</dd></div></dl>`;
    let body;
    if (s.in_window) {
      const chips = (p.evaluations || []).map((e) => `<span class="rounded px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-inset ${e.pass ? 'bg-emerald-400/10 text-emerald-300 ring-emerald-400/30' : e.static ? 'bg-ink-800 text-ink-400 ring-ink-700' : 'bg-amber-400/5 text-amber-200 ring-amber-400/20'}">${esc(e.symbol)}</span>`).join(' ');
      body = `<p class="text-slate-300">Scanned <b class="text-white">${s.evaluated}</b> of ${s.size} symbols from
        <b class="text-white">${s.source === 'watchlist' ? 'today’s watchlist' : 'universe.txt'}</b>${s.fresh ? '' : ' <span class="text-amber-300">(watchlist not generated today)</span>'}.
        ${s.passed ? `<span class="text-emerald-300">${s.passed} passed every filter.</span>` : ''}</p>
        ${s.paused ? `<p class="mt-1 text-xs text-amber-300">${esc(s.paused)}</p>` : ''}
        ${chips ? `<div class="mt-2 flex flex-wrap gap-1">${chips}</div>` : `<p class="mt-1 text-xs ${skipReason(p) ? 'text-rose-300' : 'text-ink-400'}">${esc(skipReason(p) || 'Nothing evaluated: the list was already scanned this 5-minute bar, or the position limit is reached.')}</p>`}`;
    } else {
      body = `<p class="text-slate-300">No entry scan in this phase (${esc(PB.PHASES[p.phase] ? PB.PHASES[p.phase].label : p.phase)}).
        Data fetched for exit management only${(p.positions || []).length ? `: today’s 5-minute bars for ${(p.positions || []).length} held position(s)` : ''}.</p>`;
    }
    return step(1, 'Market scan fetch', body + acct, `<span class="text-[11px] text-ink-400">${p.started ? `started ${PB.et(p.started)}` : ''} · ended ${PB.et(p.ended)} ET</span>`);
  }

  function validationStep(p) {
    const rows = (p.validations || []).map((v) => `<tr>${td(esc(v.label), 'text-slate-200')}
      ${td(PB.pill(PB.STATUS_LABEL[v.status], PB.STATUS_TONE[v.status]))}
      ${td(v.value == null ? '—' : PB.isNum(v.value) ? PB.num(v.value, Number.isInteger(v.value) ? 0 : 2) : esc(v.value), 'tabular text-slate-200')}
      ${td(v.limit == null ? '—' : PB.isNum(v.limit) ? PB.num(v.limit, Number.isInteger(v.limit) ? 0 : 2) : esc(v.limit), 'tabular text-ink-300')}
      ${td(v.enforced ? '<span class="text-sky-300">enforced</span>' : '<span class="text-ink-400">monitor</span>')}
      ${td(esc(v.detail || ''), 'text-ink-300')}</tr>`).join('');
    const counts = {};
    (p.validations || []).forEach((v) => { counts[v.status] = (counts[v.status] || 0) + 1; });
    return step(2, 'Validations', table(['Check', 'Result', 'Value', 'Limit', 'Type', 'Detail'], rows, 720),
      `<span class="text-[11px] text-ink-400">${Object.entries(counts).map(([k, n]) => `${n} ${PB.STATUS_LABEL[k].toLowerCase()}`).join(' · ')}</span>`);
  }

  function signalStep(p) {
    const evals = p.evaluations || [];
    if (!evals.length && !(p.signals || []).length) {
      return step(3, 'Strategy signal evaluation', '<p class="text-xs text-ink-400">No symbols evaluated in this pass.</p>');
    }
    const rows = evals.map((e) => {
      const result = e.pass ? PB.pill('Signal', 'green') : e.static ? PB.pill('Blocked today', 'slate') : PB.pill('No entry', 'amber');
      const stopped = e.pass ? `all ${e.total_steps} filters passed · stop ${PB.num(e.stop)}` : `${esc(e.check_label || e.check)} <span class="text-ink-400">(${e.passed_steps}/${e.total_steps} passed)</span>`;
      const dist = !e.pass && !e.static && PB.isNum(e.distance_pct) ? `<span class="${e.distance_pct <= 0.5 ? 'text-amber-300' : 'text-ink-300'}">${PB.pct(e.distance_pct)}</span>` : '—';
      return `<tr>${td(`<b class="font-mono text-white">${esc(e.symbol)}</b>`)}${td(result)}
        ${td(PB.num(e.price), 'tabular text-slate-200')}${td(PB.isNum(e.gap) ? PB.pct(e.gap, 1) : '—', 'tabular')}
        ${td(PB.isNum(e.rvol) ? e.rvol.toFixed(2) : '—', 'tabular')}${td(stopped, 'text-slate-300')}${td(dist, 'tabular')}
        ${td(`<span class="text-ink-400">${esc(e.fail || '')}</span>`)}</tr>`;
    }).join('');
    const sig = (p.signals || []).map((s) => `<li class="text-emerald-300">▲ ${esc(s)}</li>`).join('');
    return step(3, 'Strategy signal evaluation',
      (sig ? `<ul class="mb-3 space-y-1 text-xs">${sig}</ul>` : '') + table(['Symbol', 'Result', 'Price', 'Gap', 'RVOL', 'Stopped at', 'To pass', 'Reason'], rows, 820),
      `<span class="text-[11px] text-ink-400">prices = last completed 5m close</span>`);
  }

  const EVENT_TONE = { sent: 'sky', stop: 'sky', status: 'slate', fill: 'green', cancel: 'amber', blocked: 'red', dry_run: 'violet' };
  const EVENT_LABEL = { sent: 'Placed', stop: 'Stop set', status: 'Status', fill: 'Fill', cancel: 'Cancel', blocked: 'Blocked', dry_run: 'Dry run' };

  function orderStep(p) {
    const ev = p.order_events || [];
    const rows = ev.map((e) => `<tr>${td(PB.et(e.time), 'font-mono tabular text-slate-300')}
      ${td(PB.pill(EVENT_LABEL[e.event] || e.event, EVENT_TONE[e.event] || 'slate'))}
      ${td(esc((e.order_ids || []).join(', ') || '—'), 'font-mono text-ink-300')}${td(`<b class="text-white">${esc(e.symbol || '—')}</b>`)}
      ${td(esc(e.role || ''), 'text-ink-300')}${td(esc(e.detail), 'text-slate-300')}</tr>`).join('');
    const working = (p.orders || []).map((o) => `<tr>${td(esc(o.order_id), 'font-mono text-ink-300')}${td(`<b class="text-white">${esc(o.symbol)}</b>`)}
      ${td(esc(o.role))}${td(`${esc(o.action)} ${esc(o.type)}`)}${td(`${PB.num(o.filled, 0)}/${PB.num(o.qty, 0)}`, 'tabular')}
      ${td(o.limit != null ? `lmt ${PB.num(o.limit)}` : o.aux != null ? `stp ${PB.num(o.aux)}` : '—', 'tabular')}${td(esc(o.tif))}${td(esc(o.status))}</tr>`).join('');
    const body = (rows ? table(['Time ET', 'Event', 'Order', 'Symbol', 'Role', 'Detail'], rows, 640) : '<p class="text-xs text-ink-400">No order events in this pass.</p>')
      + (working ? `<p class="mb-2 mt-4 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Working paperbot orders after this pass</p>${table(['Order', 'Symbol', 'Role', 'Type', 'Filled', 'Price', 'TIF', 'Status'], working, 640)}` : '');
    return step(4, 'Order events', body, `<span class="text-[11px] text-ink-400">${p.requests} request${p.requests === 1 ? '' : 's'} sent to the risk guard</span>`);
  }

  function logBlock(p) {
    const lines = p.log || [];
    if (!lines.length) return '';
    return `<details class="rounded-xl border border-ink-700/70 bg-ink-950/60">
      <summary class="px-3.5 py-2.5 text-xs text-ink-300"><span class="pb-chev mr-2 inline-block">›</span>Log lines (${lines.length}, key INFO + all warnings)</summary>
      <pre class="max-h-72 overflow-auto border-t border-ink-700/60 px-3.5 py-3 font-mono text-[11px] leading-relaxed text-slate-300">${lines.map((l) =>
    `<span class="text-ink-400">${PB.et(l.time)}</span> <span class="${l.level === 'ERROR' ? 'text-rose-300' : l.level === 'WARNING' ? 'text-amber-300' : 'text-sky-300'}">${esc(l.level.padEnd(7))}</span> ${esc(l.msg)}`).join('\n')}</pre></details>`;
  }

  function runBody(it) {
    return `<div class="space-y-4 bg-ink-850/60 px-4 pb-5 pt-1 sm:px-5">${it.passes.map((p, i) => `
      ${it.passes.length > 1 ? `<p class="pt-3 text-[11px] font-semibold uppercase tracking-[.12em] text-violet-300">Pass ${i + 1} of ${it.passes.length} · ended ${PB.et(p.ended)} ET</p>` : ''}
      <div class="space-y-3 pt-3">${scanStep(p)}${validationStep(p)}</div>
      ${signalStep(p)}${orderStep(p)}${logBlock(p)}`).join('')}</div>`;
  }

  function renderTimeline() {
    const shown = items.filter(visible);
    $('timeline').innerHTML = shown.length
      ? shown.map((it) => `<article>${rowHeader(it)}${it.type === 'run' && openRuns.has(it.key) ? runBody(it) : ''}</article>`).join('')
      : `<div class="p-5">${PB.empty(items.length ? 'No runs match this filter.' : 'No runs recorded today yet. Telemetry starts with the first pass after this dashboard was installed.')}</div>`;
  }

  /* ------------------------------------------------------------ order lifecycle */

  const STATE_TONE = { Filled: 'green', Working: 'amber', 'Partially filled': 'amber', 'Partially filled, rest cancelled': 'amber', Cancelled: 'slate', Inactive: 'red', Sent: 'sky' };
  const KIND_TONE = { Entry: 'sky', 'Protective stop': 'slate', 'Stop-out': 'red', 'EOD liquidation': 'violet', 'Partial profit': 'green', Exit: 'green', Order: 'slate' };

  function renderOrders(d) {
    const list = d.orders || [];
    const rows = list.map((o) => {
      const open = openOrders.has(String(o.order_id));
      const price = o.avg_fill ? `avg ${PB.num(o.avg_fill)}` : o.limit != null ? `lmt ${PB.num(Number(o.limit))}` : o.stop != null ? `stp ${PB.num(Number(o.stop))}` : '—';
      const events = open ? `<tr><td colspan="8" class="bg-ink-850/60 px-4 py-3">
        <ol class="relative ml-2 space-y-2 border-l border-ink-600 pl-4">${o.events.map((e) => `<li class="relative text-xs">
          <span class="absolute -left-[21px] top-1 h-2 w-2 rounded-full ${{ placed: 'bg-sky-400', modified: 'bg-sky-300', status: 'bg-ink-400', fill: 'bg-emerald-400', cancel: 'bg-amber-400' }[e.kind] || 'bg-ink-400'}"></span>
          <span class="font-mono tabular text-ink-300">${PB.et(e.time)}</span> <span class="font-semibold uppercase tracking-wide text-[10px] text-slate-200">${esc(e.kind)}</span>
          <span class="text-slate-300">${esc(e.text)}</span></li>`).join('')}</ol>
        ${o.reason ? `<p class="mt-2 text-xs text-ink-400">Reason: ${esc(o.reason)}</p>` : ''}</td></tr>` : '';
      return `<tr class="cursor-pointer hover:bg-ink-850 ${open ? 'pb-open bg-ink-850' : ''}" data-order="${esc(o.order_id)}" aria-expanded="${open}">
        ${td(`<span class="pb-chev mr-2 inline-block text-ink-400">›</span><span class="font-mono tabular">${o.first_time ? PB.et(o.first_time) : '—'}</span>`)}
        ${td(`<b class="text-white">${esc(o.symbol || '?')}</b>`)}${td(PB.pill(o.kind, KIND_TONE[o.kind] || 'slate'))}
        ${td(esc(o.order_id), 'font-mono text-ink-300')}${td(esc(o.action || '—'))}
        ${td(`${PB.num(o.filled, 0)}/${o.qty != null ? PB.num(Number(o.qty), 0) : '?'}${o.partial ? ' <span class="text-amber-300">partial</span>' : ''}`, 'tabular')}
        ${td(price, 'tabular')}${td(PB.pill(o.state, STATE_TONE[o.state] || 'slate'))}</tr>${events}`;
    }).join('');
    $('orders').innerHTML = PB.panel('Order lifecycle · today',
      rows ? table(['Placed ET', 'Symbol', 'Kind', 'Order', 'Side', 'Filled', 'Price', 'State'], rows, 760)
        + '<p class="mt-3 text-xs text-ink-400">Fills come from IBKR’s execution report (trades.csv), so stops that filled between scheduled runs appear with their exact time. Click a row for its full event history.</p>'
        : PB.empty('No orders placed today.'),
      { right: `<span class="text-xs text-ink-400">${list.length} orders · ${list.filter((o) => o.liquidation).length} liquidations · ${list.filter((o) => o.partial).length} partial</span>` });
  }

  function renderIntents(d) {
    const list = d.intents || [];
    if (!list.length) { $('intents').innerHTML = ''; return; }
    const rows = list.map((i) => `<tr>${td(PB.et(i.time), 'font-mono tabular text-slate-300')}${td(PB.pill(i.kind === 'blocked' ? 'Blocked' : 'Dry run', i.kind === 'blocked' ? 'red' : 'violet'))}
      ${td(`<b class="text-white">${esc(i.symbol || '—')}</b>`)}${td(esc(i.detail), 'text-slate-300')}</tr>`).join('');
    $('intents').innerHTML = PB.panel('Not sent · dry-run and blocked requests', table(['Time ET', 'Kind', 'Symbol', 'Detail'], rows, 560),
      { right: `<span class="text-xs text-ink-400">${list.length} today</span>` });
  }

  /* ------------------------------------------------------------ wiring */

  document.addEventListener('click', (ev) => {
    const run = ev.target.closest('[data-run]');
    if (run) {
      const k = run.getAttribute('data-run');
      if (openRuns.has(k)) openRuns.delete(k); else openRuns.add(k);
      renderTimeline();
      return;
    }
    const order = ev.target.closest('[data-order]');
    if (order) {
      const k = order.getAttribute('data-order');
      if (openOrders.has(k)) openOrders.delete(k); else openOrders.add(k);
      renderOrders(PB.data);
      return;
    }
    const f = ev.target.closest('[data-filter]');
    if (f) { filter = f.getAttribute('data-filter'); renderFilters(); renderTimeline(); }
  });
  $('search').addEventListener('input', (ev) => { query = ev.target.value; renderTimeline(); });
  $('expand').addEventListener('click', () => { items.filter(visible).forEach((i) => i.type === 'run' && openRuns.add(i.key)); renderTimeline(); });
  $('collapse').addEventListener('click', () => { openRuns.clear(); renderTimeline(); });

  PB.onData((d) => {
    items = buildItems(d);
    if (first && items.length && items[0].type === 'run') openRuns.add(items[0].key);
    renderSummary(d);
    renderFilters();
    renderTimeline();
    renderOrders(d);
    renderIntents(d);
    $('footer').innerHTML = `Generated ${PB.et(d.generated_at)} ET · one telemetry line per pass (logs/telemetry.jsonl) ·
      skipped and failed runs come from bot.log and cycle_errors.log · read-only`;
    if (first && location.hash === '#orders') $('orders').scrollIntoView();
    first = false;
  });
  PB.start('logs.html');
})();
