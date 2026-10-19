/* Paperbot dashboard core (read-only).
 *
 * Loads data/telemetry.js (window.PAPERBOT, written by paperbot.reporting.dashboard)
 * with a <script> tag so the pages work straight from disk, reloads it every 15 s,
 * and provides the shared time model, formatting and page chrome.
 *
 * The live state (phase, next run, countdowns) is recomputed every second from the
 * session calendar in the data, so it stays correct between data refreshes.
 */
(function () {
  'use strict';

  const PB = (window.PB = window.PB || {});
  const DATA_URL = 'data/telemetry.js';
  const POLL_MS = 15000;
  const ET = 'America/New_York';
  const dataListeners = [];
  const tickListeners = [];
  PB.data = null;
  PB.loadError = null;

  /* ------------------------------------------------------------ formatting */

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  PB.esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
  PB.ms = (t) => (t == null ? null : typeof t === 'number' ? t : Date.parse(t));
  PB.isNum = (v) => typeof v === 'number' && isFinite(v);

  const fmtCache = {};
  function fmt(tz, opts) {
    const key = (tz || 'local') + JSON.stringify(opts);
    if (!fmtCache[key]) {
      fmtCache[key] = new Intl.DateTimeFormat('en-US', tz ? { timeZone: tz, ...opts } : opts);
    }
    return fmtCache[key];
  }
  const HMS = { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' };
  const HM = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
  PB.et = (t, sec = true) => (t == null ? '—' : fmt(ET, sec ? HMS : HM).format(new Date(PB.ms(t))));
  PB.local = (t, sec = false) => (t == null ? '—' : fmt(null, sec ? HMS : HM).format(new Date(PB.ms(t))));
  PB.etDay = (t) => {
    const p = {};
    fmt(ET, { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short' })
      .formatToParts(new Date(PB.ms(t))).forEach((x) => { p[x.type] = x.value; });
    return { iso: `${p.year}-${p.month}-${p.day}`, weekday: p.weekday };
  };
  PB.localZone = (() => {
    try {
      const part = fmt(null, { timeZoneName: 'short' }).formatToParts(new Date())
        .find((x) => x.type === 'timeZoneName');
      return part ? part.value : 'local';
    } catch (e) { return 'local'; }
  })();

  PB.dur = (ms) => {
    if (ms == null) return '—';
    let s = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
    if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
    return `${s}s`;
  };
  PB.countdown = (ms) => {
    let s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
  };
  PB.ago = (t) => (t == null ? '—' : `${PB.dur(Date.now() - PB.ms(t))} ago`);
  PB.num = (v, d = 2) => (PB.isNum(v) ? v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');
  PB.money = (v, signed = false, d = 2) => {
    if (!PB.isNum(v)) return '—';
    const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    return `${v < 0 ? '−' : signed && v > 0 ? '+' : ''}$${s}`;
  };
  PB.pct = (v, d = 2, signed = true) => (PB.isNum(v) ? `${signed && v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%` : '—');
  /** Account value in USD, else in the account's base currency (flagged). */
  PB.acctValue = (a, key) => {
    a = a || {};
    if (PB.isNum(a[key])) return PB.money(a[key]);
    const base = a[`${key}_base`];
    return PB.isNum(base) ? `${PB.esc(a.base_currency || '')} ${PB.num(base)} <span class="text-rose-300">(no USD)</span>` : '—';
  };
  PB.signClass = (v) => (!PB.isNum(v) || v === 0 ? 'text-ink-300' : v > 0 ? 'text-emerald-300' : 'text-rose-300');

  /* ------------------------------------------------------------ tones & pills */

  PB.TONES = {
    green: 'text-emerald-300 bg-emerald-400/10 ring-emerald-400/25',
    amber: 'text-amber-300 bg-amber-400/10 ring-amber-400/25',
    red: 'text-rose-300 bg-rose-500/10 ring-rose-400/30',
    sky: 'text-sky-300 bg-sky-400/10 ring-sky-400/25',
    violet: 'text-violet-300 bg-violet-400/10 ring-violet-400/25',
    slate: 'text-ink-300 bg-ink-700/40 ring-ink-600',
  };
  PB.TEXT = { green: 'text-emerald-300', amber: 'text-amber-300', red: 'text-rose-300',
    sky: 'text-sky-300', violet: 'text-violet-300', slate: 'text-ink-300' };
  PB.pill = (text, tone = 'slate', opts = {}) =>
    `<span class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset ${PB.TONES[tone] || PB.TONES.slate}">` +
    `${opts.dot ? `<span class="pb-dot ${opts.live ? 'live' : ''}"></span>` : ''}${PB.esc(text)}</span>`;
  PB.STATUS_TONE = { pass: 'green', warn: 'amber', fail: 'red', skip: 'slate', info: 'sky' };
  PB.STATUS_LABEL = { pass: 'Pass', warn: 'Warn', fail: 'Fail', skip: 'n/a', info: 'Info' };
  PB.SCHED_TONE = { ACTIVE: 'green', IDLE: 'slate', PAUSED: 'amber', STALE: 'amber',
    DEGRADED: 'red', CRASHED: 'red', 'NOT SCHEDULED': 'slate' };

  /* ------------------------------------------------------------ session model */

  PB.PHASES = {
    closed: { label: 'Market closed', tone: 'slate' },
    pre_open: { label: 'Pre-open', tone: 'slate' },
    opening: { label: 'Opening · manage only', tone: 'sky' },
    entry_window: { label: 'Entry window', tone: 'green' },
    manage_only: { label: 'Exits only', tone: 'sky' },
    force_close: { label: 'Force close', tone: 'violet' },
    post_close: { label: 'Post-close', tone: 'slate' },
  };

  PB.sessionOn = (t, d) => (d.model.sessions || []).find((s) => s.date === PB.etDay(t).iso) || null;

  PB.phaseAt = (t, d) => {
    const s = PB.sessionOn(t, d);
    if (!s) return 'closed';
    if (t < s.open) return 'pre_open';
    if (t < s.entry_start) return 'opening';
    if (t <= s.entry_end) return 'entry_window';
    if (t < s.force_close) return 'manage_only';
    if (t < s.close) return 'force_close';
    if (t < s.run_until) return 'post_close';
    return 'closed';
  };

  PB.nextSession = (t, d) => (d.model.sessions || []).find((s) => t < s.run_until) || null;

  /** The next pass the bot will actually run: {at, mode, phase} or null. */
  PB.nextRun = (t, d) => {
    const sched = d.scheduler || {};
    if (sched.status === 'PAUSED' || sched.status === 'NOT SCHEDULED') return null;
    if (sched.mode === 'continuous' && d.heartbeat && d.heartbeat.time) {
      const at = Math.max(t, PB.ms(d.heartbeat.time) + (d.model.loop_seconds || 30) * 1000);
      return { at, mode: 'continuous', phase: PB.phaseAt(at, d) };
    }
    const step = (d.model.cycle_interval_min || 5) * 60000;
    let at = Math.ceil((t + 1) / step) * step;
    for (const s of d.model.sessions || []) {
      if (at < s.open) at = s.open;
      if (at < s.run_until) {
        // a run starts a few seconds after its slot: 15:30:00 is already past the entry window
        return { at, mode: 'scheduled', phase: PB.phaseAt(at + 3000, d) };
      }
    }
    return null;
  };

  /** Fixed milestones after t, soonest first. */
  PB.milestones = (t, d) => {
    const out = [];
    const tm = d.model.times || {};
    for (const s of d.model.sessions || []) {
      const day = s.weekday;
      out.push({ at: s.logrotate, label: 'Log rotation', sub: 'HT_LogRotate', tone: 'slate', day });
      out.push({ at: s.open, label: 'Market open · first HT_Cycle run', sub: 'protect & manage exits', tone: 'sky', day });
      (s.prefilter || []).forEach((p, i) => out.push({ at: p, label: `Prefilter #${i + 1}`, sub: 'rebuilds today’s watchlist', tone: 'amber', day }));
      out.push({ at: s.entry_start, label: 'Entry window opens', sub: `${tm.entry_start} ET · scans begin`, tone: 'green', day });
      out.push({ at: s.entry_end, label: 'Entry window closes', sub: `${tm.entry_end} ET · no new entries`, tone: 'amber', day });
      out.push({ at: s.force_close, label: 'Force close begins', sub: d.scheduler.mode === 'continuous' ? 'flatten every position' : 'acts at the next 5-min run', tone: 'violet', day });
      out.push({ at: s.alert, label: 'Still-open alert', sub: '15:58 ET check', tone: 'red', day });
      out.push({ at: s.close, label: 'Market close', sub: 'DAY orders expire', tone: 'slate', day });
      out.push({ at: s.summary, label: 'Daily summary', sub: 'HT_Dashboard push', tone: 'slate', day });
    }
    return out.filter((m) => m.at > t).sort((a, b) => a.at - b.at);
  };

  /** Plain-language description of what a pass in `phase` will do. */
  PB.runPlan = (phase, d) => {
    const risk = d.risk || {};
    const lim = (d.strategy && d.strategy.limits) || {};
    const rk = (d.strategy && d.strategy.risk) || {};
    const npos = (d.positions || []).length;
    const manage = npos ? `manage ${npos} position${npos > 1 ? 's' : ''} (stops, breakeven, trail, partial)` : 'check protective stops (no positions)';
    if (phase === 'opening') return `Fetch fills, ${manage}. No entries before ${d.model.times.entry_start} ET.`;
    if (phase === 'entry_window') {
      const failed = (key) => ((d.last_run && d.last_run.validations) || []).some((v) => v.key === key && v.status === 'fail');
      const blocked = failed('net_liq') ? 'the bot has no USD net liquidation (see System health)'
        : failed('pnl_available') ? 'IBKR daily P&L unavailable, so the guard blocks entries'
        : risk.halted ? 'entries halted (daily loss limit)'
        : risk.orders_today >= (lim.max_orders_per_day || Infinity) ? 'entry cap reached for today'
          : npos >= (rk.max_concurrent_positions || Infinity) ? 'max concurrent positions reached' : null;
      const scan = `scan ${d.scanner.size} ${d.scanner.source === 'watchlist' ? 'watchlist' : 'universe.txt'} symbols for entries`;
      return `Fetch fills, ${manage}, then ${blocked ? `skip the scan: ${blocked}` : scan}.`;
    }
    if (phase === 'manage_only') return `Fetch fills, ${manage}. Entry window is closed.`;
    if (phase === 'force_close') return npos
      ? `Force close: cancel stops, sell all ${npos} position${npos > 1 ? 's' : ''} at marketable limits; repeat every 30 s until flat.`
      : 'Force-close pass: nothing to flatten (no positions).';
    if (phase === 'post_close') return 'Post-close pass: restore any missing GTC stop, raise overnight alerts.';
    return 'Off-hours: the run exits in under a second.';
  };

  /** Scheduler status re-evaluated against the clock (data may be minutes old). */
  PB.liveStatus = (t, d) => {
    const s = d.scheduler || {};
    if (['PAUSED', 'CRASHED', 'NOT SCHEDULED'].includes(s.status)) return { status: s.status, detail: s.detail };
    const phase = PB.phaseAt(t, d);
    if (phase === 'closed' || phase === 'pre_open') {
      return { status: 'IDLE', detail: 'Market closed: scheduled runs exit in under a second until 09:30 ET' };
    }
    const last = d.heartbeat && d.heartbeat.time ? PB.ms(d.heartbeat.time) : null;
    const staleMs = (d.model.stale_minutes || 10) * 60000;
    if (last == null || t - last > staleMs) {
      const gw = (d.health || []).find((c) => c.key === 'gateway' && c.status === 'fail');
      return { status: gw ? 'DEGRADED' : 'STALE', detail: gw ? gw.detail : `No completed pass for ${PB.dur(t - (last || t))}` };
    }
    return { status: s.status === 'IDLE' ? 'ACTIVE' : s.status, detail: s.detail };
  };

  /* ------------------------------------------------------------ chrome */

  const NAV = [
    ['index.html', 'Overview'],
    ['logs.html', 'Run log'],
    ['strategy.html', 'Strategy & scanner'],
    ['performance.html', 'Performance'],
  ];

  PB.renderHeader = (active) => {
    const host = document.getElementById('pb-header');
    if (!host) return;
    host.innerHTML = `
      <div class="pb-offline-note text-sm">Styles could not load (offline?). All data is still shown.</div>
      <div class="pb-backdrop border-b border-ink-700/70">
        <div class="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3 sm:px-6">
          <a href="index.html" class="flex items-center gap-2.5 text-white no-underline">
            <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" class="flex-none">
              <rect x="0.5" y="0.5" width="25" height="25" rx="7" fill="#0d131d" stroke="#253144"/>
              <path d="M4 15h4l2.2-5 3.4 9 2.6-6H22" fill="none" stroke="#34d399" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span class="leading-tight">
              <span class="block text-[15px] font-semibold tracking-tight">Paperbot</span>
              <span class="block text-[11px] text-ink-300" id="pb-sub">Monitoring · IBKR paper</span>
            </span>
          </a>
          <nav class="order-3 flex w-full gap-1 overflow-x-auto sm:order-none sm:w-auto" aria-label="Pages">
            ${NAV.map(([href, label]) => `<a href="${href}" class="whitespace-nowrap rounded-lg px-3 py-1.5 text-sm no-underline transition ${href === active ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800 hover:text-white'}">${label}</a>`).join('')}
          </nav>
          <div class="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-300">
            <span id="pb-sched-pill"></span>
            <span class="tabular"><span class="text-ink-400">ET</span> <span id="pb-clock-et" class="mono text-white">--:--:--</span></span>
            <span class="tabular"><span class="text-ink-400" id="pb-local-zone">${PB.esc(PB.localZone)}</span> <span id="pb-clock-local" class="mono text-slate-200">--:--</span></span>
            <span class="tabular" id="pb-age" title="Age of dashboard/data/telemetry.js"></span>
          </div>
        </div>
      </div>
      <div id="pb-banner"></div>`;
  };

  function updateChrome() {
    const now = Date.now();
    const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
    set('pb-clock-et', PB.et(now));
    set('pb-clock-local', PB.local(now));
    const d = PB.data;
    if (!d) {
      set('pb-age', PB.loadError ? '<span class="text-rose-300">no data</span>' : 'loading…');
      return;
    }
    const age = now - d.generated_at_ms;
    const live = PB.liveStatus(now, d);
    set('pb-sched-pill', PB.pill(live.status, PB.SCHED_TONE[live.status], { dot: true, live: live.status === 'ACTIVE' }));
    const stale = age > (d.model.stale_minutes || 10) * 60000 && !['closed', 'pre_open'].includes(PB.phaseAt(now, d));
    set('pb-age', `<span class="${stale ? 'text-amber-300' : ''}">data ${PB.dur(age)} old</span>`);
    const mode = d.heartbeat && d.heartbeat.mode;
    set('pb-sub', `Monitoring · IBKR paper${mode ? ` · ${PB.esc(mode)}` : ''}`);
    set('pb-banner', stale
      ? `<div class="border-b border-amber-400/20 bg-amber-400/10"><div class="mx-auto max-w-7xl px-4 py-2 text-sm text-amber-200 sm:px-6">
          Dashboard data is ${PB.dur(age)} old during market hours: no pass has completed since ${PB.et(d.generated_at_ms)} ET.
          Check IB Gateway and the HT_Cycle task (System health below).</div></div>`
      : '');
  }

  /* ------------------------------------------------------------ shared widgets */

  /** Session ribbon: phases between 09:25 and 16:10 ET with milestones and a now-marker. */
  PB.timeline = (d, t) => {
    const s = PB.sessionOn(t, d) || PB.nextSession(t, d);
    if (!s) return '';
    const from = s.logrotate, to = s.run_until + 5 * 60000;
    const pos = (x) => `${(((x - from) / (to - from)) * 100).toFixed(3)}%`;
    const seg = (a, b, cls, label) => `<div class="absolute inset-y-0 ${cls}" style="left:${pos(a)};width:calc(${pos(b)} - ${pos(a)})" title="${PB.esc(label)}"></div>`;
    const ticks = [
      [s.open, '09:30'], [s.entry_start, d.model.times.entry_start], [s.entry_end, d.model.times.entry_end],
      [s.force_close, d.model.times.force_close],
    ];
    const nowIn = t >= from && t <= to && PB.etDay(t).iso === s.date;
    return `
      <div class="relative h-9 overflow-hidden rounded-lg bg-ink-850 ring-1 ring-inset ring-ink-700">
        ${seg(s.open, s.entry_start, 'bg-sky-400/15', 'Opening: protect & manage exits')}
        ${seg(s.entry_start, s.entry_end, 'bg-emerald-400/20', 'Entry window')}
        ${seg(s.entry_end, s.force_close, 'bg-sky-400/10', 'Exits only')}
        ${seg(s.force_close, s.close, 'bg-violet-400/25', 'Force close')}
        ${seg(s.close, s.run_until, 'bg-ink-600/40', 'Post-close')}
        ${(s.prefilter || []).map((p) => `<div class="absolute top-0 h-2 w-px bg-amber-300/80" style="left:${pos(p)}" title="Prefilter ${PB.et(p, false)} ET"></div>`).join('')}
        ${nowIn ? `<div class="absolute inset-y-0 w-0.5 bg-white shadow-[0_0_8px_rgba(255,255,255,.7)]" style="left:${pos(t)}" title="Now"></div>` : ''}
      </div>
      <div class="relative mt-1 h-7 text-[10px] text-ink-400 tabular">
        ${ticks.map(([x, l], i) => `<span class="absolute -translate-x-1/2 ${i % 2 ? 'top-3' : 'top-0'}" style="left:${pos(x)}">${PB.esc(l)}</span>`).join('')}
      </div>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-300">
        <span class="inline-flex items-center gap-1.5"><i class="inline-block h-2 w-3 rounded-sm bg-sky-400/40"></i>Manage exits</span>
        <span class="inline-flex items-center gap-1.5"><i class="inline-block h-2 w-3 rounded-sm bg-emerald-400/50"></i>Entry window</span>
        <span class="inline-flex items-center gap-1.5"><i class="inline-block h-2 w-3 rounded-sm bg-violet-400/60"></i>Force close</span>
        <span class="inline-flex items-center gap-1.5"><i class="inline-block h-2 w-px bg-amber-300"></i>Prefilter</span>
        <span class="text-ink-400">${PB.esc(s.weekday)} ${PB.esc(s.date)} · US holidays not modelled</span>
      </div>`;
  };

  PB.panel = (title, body, opts = {}) => `
    <section class="rounded-2xl border border-ink-700/80 bg-ink-900/80 shadow-[0_1px_0_rgba(255,255,255,.03)_inset] ${opts.cls || ''}">
      ${title ? `<header class="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700/70 px-4 py-3 sm:px-5">
        <h2 class="text-[13px] font-semibold uppercase tracking-[.08em] text-ink-300">${title}</h2>${opts.right || ''}</header>` : ''}
      <div class="${opts.pad === false ? '' : 'p-4 sm:p-5'}">${body}</div>
    </section>`;

  PB.empty = (text) => `<div class="rounded-xl border border-dashed border-ink-700 px-4 py-6 text-center text-sm text-ink-400">${PB.esc(text)}</div>`;

  /* ------------------------------------------------------------ loading */

  function safe(fn, arg) {
    try { fn(arg); } catch (e) { console.error('[paperbot dashboard]', e); }
  }

  function load() {
    const script = document.createElement('script');
    script.src = `${DATA_URL}?t=${Date.now()}`;
    script.onload = () => {
      script.remove();
      if (window.PAPERBOT && (!PB.data || window.PAPERBOT.generated_at_ms !== PB.data.generated_at_ms)) {
        PB.data = window.PAPERBOT;
        PB.loadError = null;
        dataListeners.forEach((fn) => safe(fn, PB.data));
        updateChrome();
      }
    };
    script.onerror = () => {
      script.remove();
      PB.loadError = 'data/telemetry.js is missing. Build it with: python -m paperbot.reporting.dashboard';
      const host = document.getElementById('pb-main');
      if (host && !PB.data) host.innerHTML = PB.panel('No data yet', PB.empty(PB.loadError));
      updateChrome();
    };
    window.PAPERBOT = PB.data; // so a failed reload does not wipe the current data
    document.head.appendChild(script);
  }

  PB.onData = (fn) => { dataListeners.push(fn); if (PB.data) safe(fn, PB.data); };
  PB.onTick = (fn) => { tickListeners.push(fn); };

  PB.start = (activePage) => {
    PB.renderHeader(activePage);
    if (!window.tailwind) document.documentElement.classList.add('no-tw');
    updateChrome();
    load();
    setInterval(load, POLL_MS);
    setInterval(() => {
      updateChrome();
      if (PB.data) tickListeners.forEach((fn) => safe(fn, PB.data));
    }, 1000);
  };
})();
