/* Overview page: KPIs, live state monitor, positions, health, orders, alerts. Read-only. */
(function () {
  'use strict';
  const PB = window.PB;
  const $ = (id) => document.getElementById(id);
  const esc = PB.esc;

  /* ------------------------------------------------------------ KPI cards */

  function card(label, value, sub, extra, accent) {
    return `
      <article class="relative overflow-hidden rounded-2xl border border-ink-700/80 bg-ink-900/80 p-4 sm:p-5">
        <div class="absolute inset-x-0 top-0 h-px ${accent || 'bg-ink-600'}"></div>
        <p class="text-[11px] font-semibold uppercase tracking-[.12em] text-ink-300">${label}</p>
        <div class="mt-2 flex items-baseline gap-2">${value}</div>
        <p class="mt-1 text-xs text-ink-300">${sub}</p>
        ${extra || ''}
      </article>`;
  }

  function runsCard(d) {
    const r = d.kpis.runs;
    const hours = ['09', '10', '11', '12', '13', '14', '15', '16'];
    const peak = Math.max(1, ...hours.map((h) => r.by_hour[h] || 0));
    const bars = hours.map((h) => {
      const n = r.by_hour[h] || 0;
      return `<div class="flex flex-1 flex-col items-center gap-1" title="${n} run(s) in the ${h}:00 ET hour">
        <div class="flex h-10 w-full items-end rounded-sm bg-ink-800"><div class="w-full rounded-sm ${n ? 'bg-emerald-400/70' : ''}" style="height:${(n / peak) * 100}%"></div></div>
        <span class="text-[9px] text-ink-400">${h}</span></div>`;
    }).join('');
    const sub = `${r.passes} passes recorded · ${r.skipped} skipped · <span class="${r.failed ? 'text-rose-300' : ''}">${r.failed} failed</span>`;
    return card('Total runs today', `<span class="text-3xl font-semibold tabular text-white">${r.executed}</span>
      <span class="text-xs text-ink-300">${r.bot_loops ? `${r.bot_loops} bot loops` : 'scheduled runs'}</span>`,
    sub, `<div class="mt-3 flex gap-1">${bars}</div>`, 'bg-emerald-400/60');
  }

  function pnlCard(d) {
    const p = d.kpis.pnl;
    const daily = p.daily;
    const used = PB.isNum(daily) && daily < 0 && p.loss_limit ? Math.min(100, (daily / p.loss_limit) * 100) : 0;
    const value = PB.isNum(daily)
      ? `<span class="text-3xl font-semibold tabular ${PB.signClass(daily)}">${PB.money(daily, true)}</span><span class="text-xs text-ink-300">IBKR daily</span>`
      : `<span class="text-3xl font-semibold text-ink-400">—</span><span class="text-xs text-ink-300">no P&amp;L yet today</span>`;
    const sub = `Unrealized ${PB.money(p.unrealized, true)} · realized ${PB.money(p.realized, true)} · ${p.closed_trades} closed (${PB.money(p.ledger_closed, true)})`;
    const meter = `<div class="mt-3">
      <div class="flex justify-between text-[10px] text-ink-400"><span>Loss limit used</span><span class="tabular">${used.toFixed(0)}% of ${PB.money(Math.abs(p.loss_limit || 0), false, 0)}</span></div>
      <div class="mt-1 h-1.5 rounded-full bg-ink-800"><div class="h-1.5 rounded-full ${used >= 50 ? 'bg-amber-400' : 'bg-emerald-400/70'} ${used >= 100 ? 'bg-rose-500' : ''}" style="width:${used}%"></div></div>
      ${p.as_of ? `<p class="mt-1 text-[10px] text-ink-400">as of ${PB.et(p.as_of)} ET</p>` : ''}</div>`;
    return card('Current P&amp;L', value, sub, meter, PB.isNum(daily) && daily < 0 ? 'bg-rose-400/60' : 'bg-emerald-400/60');
  }

  function positionsCard(d) {
    const k = d.kpis.positions;
    const chips = (d.positions || []).map((p) => `<span class="inline-flex items-center gap-1 rounded-md bg-ink-800 px-2 py-0.5 text-xs ring-1 ring-inset ring-ink-700">
      <b class="font-semibold text-white">${esc(p.symbol)}</b><span class="tabular ${PB.signClass(p.r_now)}">${PB.isNum(p.r_now) ? `${p.r_now > 0 ? '+' : ''}${p.r_now.toFixed(2)}R` : ''}</span>${p.managed ? '' : '<span class="text-ink-400">manual</span>'}</span>`).join(' ');
    return card('Active positions', `<span class="text-3xl font-semibold tabular text-white">${k.count}</span>
      <span class="text-xs text-ink-300">of ${esc(k.max ?? '—')} max concurrent</span>`,
    `${k.managed} managed by paperbot · ${d.risk.orders_today || 0} entr${(d.risk.orders_today || 0) === 1 ? 'y' : 'ies'} sent today`,
    `<div class="mt-3 flex min-h-[2.5rem] flex-wrap content-start gap-1.5">${chips || '<span class="text-xs text-ink-400">Flat: no open positions</span>'}</div>`,
    'bg-sky-400/60');
  }

  function healthCard(d) {
    const h = d.kpis.health;
    const label = { pass: 'Healthy', warn: 'Needs attention', fail: 'Critical' }[h.status] || '—';
    const tone = PB.STATUS_TONE[h.status] || 'slate';
    const bad = (d.health || []).filter((c) => c.status === 'fail' || c.status === 'warn');
    return card('System health', `<span class="text-3xl font-semibold ${PB.TEXT[tone]}">${label}</span>
      <span class="text-xs tabular text-ink-300">${h.score != null ? `${h.score}% checks passing` : ''}</span>`,
    bad.length ? `${bad.length} check${bad.length > 1 ? 's' : ''} need${bad.length > 1 ? '' : 's'} a look` : 'All checks passing',
    `<ul class="mt-3 space-y-1 text-xs">${bad.slice(0, 3).map((c) => `<li class="flex gap-2"><span class="${PB.TEXT[PB.STATUS_TONE[c.status]]}">●</span><span><b class="font-medium text-slate-200">${esc(c.label)}</b> <span class="text-ink-300">${esc(c.detail)}</span></span></li>`).join('')}</ul>`,
    { pass: 'bg-emerald-400/60', warn: 'bg-amber-400/70', fail: 'bg-rose-500/70' }[h.status]);
  }

  /* ------------------------------------------------------------ live state */

  function lastSummary(r) {
    if (!r) return 'No pass recorded today yet.';
    const parts = [];
    if (r.scan && r.scan.evaluated) parts.push(`${r.scan.evaluated} evaluated`);
    if (r.signals && r.signals.length) parts.push(`${r.signals.length} signal${r.signals.length > 1 ? 's' : ''}`);
    parts.push(`${r.requests} order request${r.requests === 1 ? '' : 's'}`);
    const alerts = (r.validations || []).find((v) => v.key === 'alerts');
    if (alerts && alerts.value) parts.push(`${alerts.value} alert${alerts.value > 1 ? 's' : ''}`);
    return parts.join(' · ');
  }

  function activity(now, d) {
    const live = PB.liveStatus(now, d);
    const phase = PB.phaseAt(now, d);
    const last = d.last_run;
    const lastEnd = last ? PB.ms(last.ended) : null;
    const next = PB.nextSession(now, d);
    if (live.status === 'PAUSED') return { tone: 'amber', title: 'Paused', text: live.detail };
    if (live.status === 'CRASHED') return { tone: 'red', title: 'Crashed', text: live.detail };
    if (live.status === 'NOT SCHEDULED') return { tone: 'slate', title: 'Not scheduled', text: live.detail };
    if (phase === 'closed' || phase === 'pre_open') {
      return { tone: 'slate', title: phase === 'pre_open' ? 'Waiting for the open' : 'Market closed · idle',
        text: next ? `Next session ${next.weekday} ${PB.et(next.open, false)} ET (${PB.local(next.open)} ${PB.localZone}). Scheduled runs exit in under a second until then.` : 'No session ahead in the calendar.' };
    }
    if (live.status === 'DEGRADED' || live.status === 'STALE') {
      return { tone: live.status === 'DEGRADED' ? 'red' : 'amber', title: live.status === 'DEGRADED' ? 'Runs are failing' : 'No recent pass', text: live.detail };
    }
    if (d.scheduler.mode === 'continuous') {
      return { tone: 'green', live: true, title: 'Continuous loop running',
        text: `${PB.PHASES[phase].label}. Last pass ${PB.et(lastEnd)} ET (${PB.ago(lastEnd)}): ${lastSummary(last)}` };
    }
    if (lastEnd && now - lastEnd < 15000) {
      return { tone: 'green', live: true, title: 'Run just completed', text: `${PB.PHASES[phase].label}. ${lastSummary(last)}` };
    }
    if (phase === 'force_close' && lastEnd && now - lastEnd < 45000 && (d.positions || []).length) {
      return { tone: 'violet', live: true, title: 'Force-close loop', text: `Re-pricing the closing SELL every 30 s until flat. ${(d.positions || []).length} position(s) open.` };
    }
    return { tone: 'green', live: true, title: 'Idle between scheduled runs',
      text: `${PB.PHASES[phase].label}. Last pass ${lastEnd ? `${PB.et(lastEnd)} ET (${PB.ago(lastEnd)})` : '—'}: ${lastSummary(last)}` };
  }

  function conditions(d) {
    const out = [];
    (d.positions || []).forEach((p) => {
      const price = p.market_price;
      const dist = (lvl) => (PB.isNum(price) && PB.isNum(lvl) ? ((lvl / price) - 1) * 100 : null);
      if (PB.isNum(p.stop)) out.push({ sym: p.symbol, tone: 'red', text: `Stop fills at IBKR if price ≤ ${PB.num(p.stop)}`, dist: dist(p.stop) });
      if (p.triggers && PB.isNum(p.triggers.partial)) out.push({ sym: p.symbol, tone: 'amber', text: `Partial profit (⅓) when a 5m close ≥ ${PB.num(p.triggers.partial)}`, dist: dist(p.triggers.partial) });
      if (p.triggers && PB.isNum(p.triggers.breakeven)) out.push({ sym: p.symbol, tone: 'amber', text: `Stop to breakeven when a 5m close ≥ ${PB.num(p.triggers.breakeven)}`, dist: dist(p.triggers.breakeven) });
      if (p.breakeven) out.push({ sym: p.symbol, tone: 'sky', text: 'Trailing: stop follows each new 5m swing low', dist: null });
    });
    const scan = (d.scanner.rows || []).filter((r) => r.status === 'NEAR' || r.status === 'SIGNAL').slice(0, 3);
    scan.forEach((r) => out.push({ sym: r.symbol, tone: r.status === 'SIGNAL' ? 'green' : 'amber',
      text: r.status === 'SIGNAL' ? 'Passed every filter on its last evaluation' : `${r.eval.check}: price must exceed ${PB.num(r.eval.threshold)}`,
      dist: r.status === 'NEAR' ? r.eval.distance_pct : null }));
    return out;
  }

  function renderLive(d) {
    const now = Date.now();
    const a = activity(now, d);
    const phase = PB.phaseAt(now, d);
    const live = PB.liveStatus(now, d);
    const sched = d.scheduler || {};
    const cyc = sched.cycle || {};
    const interp = sched.interpreter;
    const last = d.last_run;
    const next = PB.nextRun(now, d);
    const ms = PB.milestones(now, d);
    const conds = conditions(d);

    const nowCol = `
      <div class="flex items-center gap-2">
        <span class="${PB.TEXT[a.tone]}"><span class="pb-dot ${a.live ? 'live' : ''}"></span></span>
        <h3 class="text-lg font-semibold text-white">${esc(a.title)}</h3>
      </div>
      <p class="mt-2 text-sm leading-relaxed text-slate-300">${esc(a.text)}</p>
      <div class="mt-3 flex flex-wrap gap-2">${PB.pill(PB.PHASES[phase].label, PB.PHASES[phase].tone)}
        ${d.heartbeat && d.heartbeat.mode ? PB.pill(d.heartbeat.mode, d.heartbeat.mode === 'SUBMIT' ? 'sky' : 'slate') : ''}</div>
      <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <dt class="text-ink-400">Last pass</dt><dd class="tabular text-slate-200">${last ? `${PB.et(last.ended)} ET` : '—'}</dd>
        <dt class="text-ink-400">Source</dt><dd class="text-slate-200">${last ? `${esc(last.source)} · pass ${last.pass_in_run}` : '—'}</dd>
        <dt class="text-ink-400">Duration</dt><dd class="tabular text-slate-200">${last && PB.isNum(last.duration_s) ? `${last.duration_s.toFixed(1)} s` : '—'}</dd>
        <dt class="text-ink-400">Validations</dt><dd>${last ? validationDots(last.validations) : '—'}</dd>
      </dl>`;

    const resultTone = cyc.registered ? (cyc.result_ok ? 'green' : 'red') : 'slate';
    const schedCol = `
      <div class="flex flex-wrap items-center gap-2">${PB.pill(live.status, PB.SCHED_TONE[live.status], { dot: true, live: live.status === 'ACTIVE' })}
        <span class="text-xs text-ink-300">${esc(sched.mode === 'continuous' ? (sched.builtin ? 'paper-bot · built-in scheduler' : 'continuous bot.py') : 'Task Scheduler · HT_Cycle every 5 min')}</span></div>
      <p class="mt-2 text-sm leading-relaxed text-slate-300">${esc(live.detail)}</p>
      <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <dt class="text-ink-400">HT_Cycle</dt><dd class="text-slate-200">${sched.available === false ? 'n/a on this OS' : cyc.registered ? (cyc.enabled ? 'enabled' : 'disabled') : 'not registered'}</dd>
        <dt class="text-ink-400">Last run</dt><dd class="tabular text-slate-200">${cyc.last_run ? `${PB.et(cyc.last_run)} ET` : '—'}</dd>
        <dt class="text-ink-400">Last result</dt><dd class="${PB.TEXT[resultTone]}">${esc(cyc.last_result_text || '—')}</dd>
        <dt class="text-ink-400">Task's next run</dt><dd class="tabular text-slate-200">${cyc.next_run ? `${PB.et(cyc.next_run)} ET` : '—'}</dd>
        <dt class="text-ink-400">Python env</dt><dd class="${interp ? (interp.ok ? 'text-emerald-300' : 'text-rose-300') : 'text-ink-300'}">${interp ? (interp.ok ? 'OK' : interp.exists ? `missing ${esc(interp.missing.join(', '))}` : 'python.exe not found') : '—'}</dd>
        <dt class="text-ink-400">STOP file</dt><dd class="${sched.stop_file ? 'text-amber-300' : 'text-slate-200'}">${sched.stop_file ? 'present' : 'absent'}</dd>
      </dl>`;

    const nextCol = next ? `
      <p class="text-xs text-ink-300">${next.mode === 'continuous' ? 'Next loop pass' : 'Next HT_Cycle run'} at <span class="tabular text-slate-200">${PB.et(next.at)} ET</span> · ${PB.local(next.at)} ${esc(PB.localZone)}</p>
      <p class="mt-1 font-mono text-4xl font-semibold tabular tracking-tight ${PB.TEXT[PB.PHASES[next.phase].tone === 'slate' ? 'sky' : PB.PHASES[next.phase].tone]}">${PB.countdown(next.at - now)}</p>
      <p class="mt-2 text-sm leading-relaxed text-slate-300">${esc(PB.runPlan(next.phase, d))}</p>
      ${conds.length ? `<ul class="mt-3 space-y-1.5 text-xs">${conds.map((c) => `<li class="flex items-start gap-2">
          <span class="mt-0.5 ${PB.TEXT[c.tone]}">◆</span>
          <span class="flex-1"><b class="font-semibold text-white">${esc(c.sym)}</b> <span class="text-slate-300">${esc(c.text)}</span></span>
          ${PB.isNum(c.dist) ? `<span class="tabular ${c.dist >= 0 ? 'text-amber-300' : 'text-rose-300'}">${PB.pct(c.dist)}</span>` : ''}</li>`).join('')}</ul>` : ''}
      ${ms.length ? `<div class="mt-4 border-t border-ink-700/70 pt-3 text-xs">
          <p class="text-ink-400">Then</p>
          ${ms.slice(0, 3).map((m) => `<div class="mt-1.5 flex items-center justify-between gap-3">
            <span><span class="${PB.TEXT[m.tone]}">●</span> <span class="text-slate-200">${esc(m.label)}</span> <span class="text-ink-400">${esc(m.sub)}</span></span>
            <span class="whitespace-nowrap tabular text-ink-300">${PB.et(m.at, false)} · in ${PB.countdown(m.at - now)}</span></div>`).join('')}
        </div>` : ''}`
      : `<p class="text-sm text-slate-300">No scheduled pass: ${esc(live.detail)}</p>`;

    const col = (title, body) => `<div class="min-w-0 p-4 sm:p-5"><p class="mb-3 text-[11px] font-semibold uppercase tracking-[.12em] text-ink-400">${title}</p>${body}</div>`;
    $('live').innerHTML = PB.panel('Live state monitor', `
      <div class="grid grid-cols-1 divide-y divide-ink-700/70 lg:grid-cols-3 lg:divide-x lg:divide-y-0">
        ${col('Paperbot is now', nowCol)}${col('Scheduler', schedCol)}${col('Next predicted action', nextCol)}
      </div>
      <div class="border-t border-ink-700/70 p-4 sm:p-5">${PB.timeline(d, now)}</div>`,
    { pad: false, right: `<span class="text-xs text-ink-400">updates every second · data reloads every 15 s</span>` });
  }

  function validationDots(list) {
    return `<span class="inline-flex flex-wrap gap-1">${(list || []).map((v) =>
      `<span title="${esc(v.label)}: ${esc(PB.STATUS_LABEL[v.status])}${v.detail ? ` (${esc(v.detail)})` : ''}" class="inline-block h-2.5 w-2.5 rounded-sm ${{ pass: 'bg-emerald-400/80', warn: 'bg-amber-400', fail: 'bg-rose-500', skip: 'bg-ink-600', info: 'bg-sky-400' }[v.status]}"></span>`).join('')}</span>`;
  }

  /* ------------------------------------------------------------ tables */

  function renderPositions(d) {
    const rows = (d.positions || []).map((p) => {
      const t = p.triggers || {};
      const nextT = PB.isNum(t.partial) ? ['Partial', t.partial] : PB.isNum(t.breakeven) ? ['Breakeven', t.breakeven] : null;
      const dist = nextT && PB.isNum(p.market_price) ? ((nextT[1] / p.market_price) - 1) * 100 : null;
      return `<tr class="border-t border-ink-700/60">
        <td class="py-2.5 pr-3"><b class="font-semibold text-white">${esc(p.symbol)}</b>${p.managed ? '' : ' <span class="text-xs text-ink-400">manual</span>'}</td>
        <td class="py-2.5 pr-3 text-right tabular">${PB.num(p.qty, 0)}</td>
        <td class="py-2.5 pr-3 text-right tabular">${PB.num(p.avg_cost)}</td>
        <td class="py-2.5 pr-3 text-right tabular text-white">${PB.num(p.market_price)}</td>
        <td class="py-2.5 pr-3 text-right tabular ${PB.signClass(p.unrealized_pnl)}">${PB.money(p.unrealized_pnl, true)}</td>
        <td class="py-2.5 pr-3 text-right tabular ${PB.signClass(p.r_now)}">${PB.isNum(p.r_now) ? `${p.r_now > 0 ? '+' : ''}${p.r_now.toFixed(2)}R` : '—'}</td>
        <td class="py-2.5 pr-3 text-right tabular text-rose-200">${PB.num(p.stop)}${p.breakeven ? ' <span class="text-[10px] text-sky-300">BE</span>' : ''}</td>
        <td class="py-2.5 text-right tabular">${nextT ? `<span class="text-ink-300">${nextT[0]}</span> ${PB.num(nextT[1])} <span class="text-amber-300">${PB.pct(dist)}</span>` : p.breakeven ? '<span class="text-sky-300">trailing</span>' : '—'}</td>
      </tr>`;
    }).join('');
    $('positions').innerHTML = PB.panel('Open positions', rows ? `<div class="-mx-1 overflow-x-auto"><table class="w-full min-w-[640px] text-sm">
        <thead><tr class="text-left text-[11px] uppercase tracking-wide text-ink-400">
          <th class="pb-2 pr-3 font-medium">Symbol</th><th class="pb-2 pr-3 text-right font-medium">Qty</th><th class="pb-2 pr-3 text-right font-medium">Avg cost</th>
          <th class="pb-2 pr-3 text-right font-medium">Last</th><th class="pb-2 pr-3 text-right font-medium">Unrl P&amp;L</th><th class="pb-2 pr-3 text-right font-medium">R now</th>
          <th class="pb-2 pr-3 text-right font-medium">Stop</th><th class="pb-2 text-right font-medium">Next trigger</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
        <p class="mt-3 text-xs text-ink-400">Last price is IBKR's portfolio mark at the last pass. Exit rules act on completed 5-minute closes; stops are GTC orders held at IBKR.</p>`
      : PB.empty('No open positions. Paperbot is flat.'),
    { right: `<span class="text-xs text-ink-400">${(d.positions || []).length} open</span>` });
  }

  function renderHealth(d) {
    const rows = (d.health || []).map((c) => `<li class="flex items-start gap-3 border-t border-ink-700/60 py-2.5 first:border-0 first:pt-0">
        ${PB.pill(PB.STATUS_LABEL[c.status], PB.STATUS_TONE[c.status])}
        <div class="min-w-0"><p class="text-sm font-medium text-slate-100">${esc(c.label)}</p><p class="break-words text-xs text-ink-300">${esc(c.detail)}</p></div></li>`).join('');
    $('health').innerHTML = PB.panel('System health', `<ul>${rows}</ul>`,
      { right: d.kpis.health.score != null ? `<span class="text-xs tabular text-ink-300">${d.kpis.health.score}% passing</span>` : '' });
  }

  const STATE_TONE = { Filled: 'green', Working: 'amber', 'Partially filled': 'amber', 'Partially filled, rest cancelled': 'amber', Cancelled: 'slate', Inactive: 'red' };

  function renderOrders(d) {
    const list = (d.orders || []).slice(0, 8);
    const rows = list.map((o) => `<li class="flex items-center justify-between gap-3 border-t border-ink-700/60 py-2.5 first:border-0 first:pt-0">
        <div class="min-w-0">
          <p class="text-sm"><b class="font-semibold text-white">${esc(o.symbol || '?')}</b> <span class="text-ink-300">${esc(o.kind)}</span>
            <span class="text-xs text-ink-400">#${esc(o.order_id)}</span></p>
          <p class="text-xs tabular text-ink-300">${esc(o.action || '')} ${PB.num(o.filled, 0)}/${PB.isNum(Number(o.qty)) ? PB.num(Number(o.qty), 0) : '?'}
            ${o.avg_fill ? `@ ${PB.num(o.avg_fill)}` : o.limit ? `lmt ${PB.num(Number(o.limit))}` : o.stop ? `stp ${PB.num(Number(o.stop))}` : ''}
            · placed ${o.first_time ? PB.et(o.first_time) : '—'} · ${o.events.length} event${o.events.length === 1 ? '' : 's'}</p>
        </div>${PB.pill(o.state, STATE_TONE[o.state] || 'slate')}</li>`).join('');
    $('orders').innerHTML = PB.panel('Order lifecycle · today', rows ? `<ul>${rows}</ul>` : PB.empty('No orders today.'),
      { right: '<a class="text-xs text-sky-300 hover:text-sky-200" href="logs.html#orders">Full lifecycle →</a>' });
  }

  function renderAlerts(d) {
    const list = (d.alerts || []).slice(-10).reverse();
    const rows = list.map((a) => `<li class="flex items-start gap-3 border-t border-ink-700/60 py-2.5 first:border-0 first:pt-0">
        <span class="w-16 flex-none pt-0.5 text-xs tabular text-ink-400">${PB.et(a.time)}</span>
        ${PB.pill(a.msg.includes('ALERT') ? 'Alert' : a.level === 'WARNING' ? 'Warn' : 'Error', a.level === 'WARNING' && !a.msg.includes('ALERT') ? 'amber' : 'red')}
        <p class="min-w-0 break-words text-xs text-slate-300">${esc(a.msg)}</p></li>`).join('');
    $('alerts').innerHTML = PB.panel('Alerts &amp; warnings · today', rows ? `<ul>${rows}</ul>` : PB.empty('No alerts or warnings today.'),
      { right: `<span class="text-xs text-ink-400">${(d.alerts || []).length} today</span>` });
  }

  function renderTasks(d) {
    const s = d.scheduler || {};
    if (!s.available) { $('tasks').innerHTML = ''; return; }
    const prev = $('tasks').querySelector('details');
    const open = prev && prev.open ? ' open' : '';
    const rows = (s.tasks || []).map((t) => `<tr class="border-t border-ink-700/60">
        <td class="py-2 pr-3 font-mono text-xs text-slate-200">${esc(t.name)}</td>
        <td class="py-2 pr-3 text-xs">${t.registered ? (t.enabled ? '<span class="text-emerald-300">enabled</span>' : '<span class="text-amber-300">disabled</span>') : '<span class="text-ink-400">not registered</span>'}</td>
        <td class="py-2 pr-3 text-xs tabular text-ink-300">${t.last_run ? `${PB.et(t.last_run)} ET` : '—'}</td>
        <td class="py-2 pr-3 text-xs ${t.registered ? (t.result_ok ? 'text-emerald-300' : 'text-rose-300') : 'text-ink-400'}">${esc(t.last_result_text || '—')}</td>
        <td class="py-2 text-xs tabular text-ink-300">${t.next_run ? `${PB.et(t.next_run)} ET <span class="text-ink-400">(${PB.local(t.next_run)} ${esc(PB.localZone)})</span>` : '—'}</td></tr>`).join('');
    $('tasks').innerHTML = `<details class="group rounded-2xl border border-ink-700/80 bg-ink-900/80"${open}>
      <summary class="flex items-center justify-between px-4 py-3 sm:px-5">
        <span class="text-[13px] font-semibold uppercase tracking-[.08em] text-ink-300"><span class="pb-chev mr-2 inline-block">›</span>Scheduled tasks</span>
        <span class="text-xs text-ink-400">${(s.tasks || []).filter((t) => t.registered).length}/${(s.tasks || []).length} registered · other tasks refresh every 10 min</span></summary>
      <div class="overflow-x-auto border-t border-ink-700/70 px-4 pb-4 sm:px-5"><table class="mt-2 w-full min-w-[640px] text-left">
        <thead><tr class="text-[11px] uppercase tracking-wide text-ink-400"><th class="py-2 pr-3 font-medium">Task</th><th class="py-2 pr-3 font-medium">State</th>
          <th class="py-2 pr-3 font-medium">Last run</th><th class="py-2 pr-3 font-medium">Last result</th><th class="py-2 font-medium">Next run</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
  }

  function renderFooter(d) {
    $('footer').innerHTML = `Generated ${PB.et(d.generated_at)} ET from logs/telemetry.jsonl, bot.log, orders.jsonl, trades.csv and Task Scheduler ·
      read-only: this dashboard never sends orders or changes the scheduler · telemetry schema v${esc(d.telemetry_schema)}`;
  }

  PB.onData((d) => {
    $('kpis').innerHTML = runsCard(d) + pnlCard(d) + positionsCard(d) + healthCard(d);
    renderLive(d);
    renderPositions(d);
    renderHealth(d);
    renderOrders(d);
    renderAlerts(d);
    renderTasks(d);
    renderFooter(d);
  });
  PB.onTick(renderLive);
  PB.start('index.html');
})();
