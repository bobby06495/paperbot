/* Paperbot BACKTESTING pages and standalone reports (read-only).
 *
 * Dashboard pages load data/backtest.js (window.PAPERBOT_BT, written by
 * paperbot.backtesting.report) and call PBT.page(name). Standalone report.html
 * files embed one payload and call PBT.standalone(view, payload). Both use the
 * same renderers, so a report shows exactly what the dashboard shows.
 *
 * Every number comes from the run's own files; nothing is computed for display
 * except formatting, cumulative sums and chart scaling. Unavailable values are
 * shown as "N/A" with the reason, never as zero.
 */
(function () {
  'use strict';
  const PBT = (window.PBT = window.PBT || {});

  /* ------------------------------------------------------------ formatting */
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
  const isNum = (v) => typeof v === 'number' && isFinite(v);
  const NA = 'N/A — insufficient data';
  const na = (reason) => `<span class="text-ink-400" title="${esc(reason || NA)}">N/A</span>`;
  const fixed = (v, d) => Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v, o = {}) => {
    if (!isNum(v)) return na(o.reason);
    const s = `${v < 0 ? '−' : o.signed && v > 0 ? '+' : ''}$${fixed(v, o.d ?? 2)}`;
    return o.plain ? s : `<span class="${o.color ? signClass(v) : ''} tabular">${s}</span>`;
  };
  const usd = (v, o = {}) => (isNum(v) ? `${money(v, o)} <span class="text-[10px] text-ink-400">USD</span>` : na(o.reason));
  const pct = (v, o = {}) => {
    if (!isNum(v)) return na(o.reason);
    const s = `${v < 0 ? '−' : o.signed && v > 0 ? '+' : ''}${fixed(v, o.d ?? 2)}%`;
    return o.plain ? s : `<span class="${o.color ? signClass(v) : ''} tabular">${s}</span>`;
  };
  const num = (v, d = 2, reason) => (isNum(v) ? `<span class="tabular">${fixed(v, d)}</span>` : na(reason));
  const int = (v) => (isNum(v) ? `<span class="tabular">${Math.round(v).toLocaleString('en-US')}</span>` : na());
  const signClass = (v) => (!isNum(v) || v === 0 ? 'text-ink-300' : v > 0 ? 'text-emerald-300' : 'text-rose-300');
  const et = (iso) => (iso ? `${String(iso).slice(0, 16).replace('T', ' ')} <span class="text-ink-400">ET</span>` : '—');
  const etPlain = (iso) => (iso ? `${String(iso).slice(0, 16).replace('T', ' ')} ET` : '—');
  const day = (iso) => (iso ? String(iso).slice(0, 10) : '—');
  const utc = (iso) => (iso ? `${String(iso).slice(0, 16).replace('T', ' ')} UTC` : '—');
  const ms = (iso) => Date.parse(iso);
  Object.assign(PBT, { esc, money, usd, pct, num, et });

  /* ------------------------------------------------------------ widgets */
  const TONES = {
    green: 'text-emerald-300 bg-emerald-400/10 ring-emerald-400/25',
    amber: 'text-amber-300 bg-amber-400/10 ring-amber-400/25',
    red: 'text-rose-300 bg-rose-500/10 ring-rose-400/30',
    sky: 'text-sky-300 bg-sky-400/10 ring-sky-400/25',
    violet: 'text-violet-300 bg-violet-400/10 ring-violet-400/25',
    slate: 'text-ink-300 bg-ink-700/40 ring-ink-600',
  };
  const pill = (text, tone = 'slate') =>
    `<span class="inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset ${TONES[tone] || TONES.slate}">${esc(text)}</span>`;
  const STATUS_TONE = { completed: 'green', completed_with_warnings: 'amber', failed: 'red', running: 'sky',
    pass: 'green', warn: 'amber', fail: 'red', ok: 'green', concern: 'amber', info: 'sky', 'insufficient data': 'slate',
    proposed: 'violet', none: 'slate' };
  const statusPill = (s) => pill(String(s || 'unknown').replace(/_/g, ' '), STATUS_TONE[s] || 'slate');
  const tip = (text) => (text ? ` <span class="cursor-help text-ink-400" title="${esc(text)}" aria-label="${esc(text)}">ⓘ</span>` : '');
  const panel = (title, body, o = {}) => `
    <section class="rounded-2xl border border-ink-700/80 bg-ink-900/80 ${o.cls || ''}" ${o.id ? `id="${o.id}"` : ''}>
      ${title ? `<header class="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700/70 px-4 py-3 sm:px-5">
        <h2 class="text-[13px] font-semibold uppercase tracking-[.08em] text-ink-300">${title}</h2>${o.right || ''}</header>` : ''}
      <div class="${o.pad === false ? '' : 'p-4 sm:p-5'}">${body}</div></section>`;
  const empty = (text) => `<div class="rounded-xl border border-dashed border-ink-700 px-4 py-6 text-center text-sm text-ink-400">${esc(text)}</div>`;
  const kpi = (label, value, sub = '', help = '') => `
    <div class="rounded-xl border border-ink-700/70 bg-ink-850 px-4 py-3">
      <div class="text-[11px] font-semibold uppercase tracking-wider text-ink-400">${esc(label)}${tip(help)}</div>
      <div class="mt-1 text-lg font-semibold text-white">${value}</div>
      ${sub ? `<div class="mt-0.5 text-xs text-ink-300">${sub}</div>` : ''}</div>`;
  const grid = (items, cols = 'sm:grid-cols-2 xl:grid-cols-4') => `<div class="grid grid-cols-1 gap-3 ${cols}">${items.join('')}</div>`;
  const notice = (text, tone = 'amber') => `<div class="rounded-xl px-4 py-3 text-sm ring-1 ring-inset ${TONES[tone]}">${text}</div>`;
  const FUTURE = 'Historical simulation results do not guarantee future performance.';
  const list = (items) => (items && items.length ? `<ul class="list-disc space-y-1 pl-5 text-sm text-slate-200">${items.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : empty('None.'));
  const details = (summary, body, open = false) => `<details ${open ? 'open' : ''} class="rounded-xl border border-ink-700/70 bg-ink-850">
      <summary class="flex items-center gap-2 px-4 py-2 text-sm text-slate-200"><span class="pb-chev text-ink-400">▸</span>${summary}</summary>
      <div class="border-t border-ink-700/60 p-4">${body}</div></details>`;

  /* ------------------------------------------------------------ tables (sortable, filterable) */
  const TABLES = {};
  function table(id, columns, rows, o = {}) {
    TABLES[id] = { columns, rows, o, sort: o.sort || null, dir: o.dir || 1, filter: '' };
    const filter = o.filter === false ? '' : `<input type="search" placeholder="Filter rows…" aria-label="Filter rows"
      class="mb-3 w-full rounded-lg border border-ink-700 bg-ink-850 px-3 py-1.5 text-sm text-slate-100 placeholder:text-ink-400 sm:w-72"
      oninput="PBT._filter('${id}', this.value)">`;
    return `${filter}<div class="overflow-x-auto" id="${id}">${tableBody(id)}</div>`;
  }
  function tableBody(id) {
    const t = TABLES[id];
    let rows = t.rows;
    if (t.filter) {
      const f = t.filter.toLowerCase();
      rows = rows.filter((r) => t.columns.some((c) => String(r[c.key] ?? '').toLowerCase().includes(f)));
    }
    if (t.sort) {
      const col = t.columns.find((c) => c.key === t.sort) || {};
      const val = col.sortValue || ((r) => r[t.sort]);
      rows = [...rows].sort((a, b) => {
        const x = val(a), y = val(b);
        if (x === y) return 0;
        if (x === '' || x == null) return 1;
        if (y === '' || y == null) return -1;
        return (x > y ? 1 : -1) * t.dir;
      });
    }
    if (!rows.length) return empty(t.o.empty || 'No rows.');
    const limit = t.o.limit || 2000;
    const head = t.columns.map((c) => `<th scope="col" class="cursor-pointer select-none whitespace-nowrap px-2 py-2 font-medium ${c.align === 'right' ? 'text-right' : 'text-left'}"
        onclick="PBT._sort('${id}','${c.key}')" title="${esc(c.tip || 'Sort')}">${esc(c.label)}${t.sort === c.key ? (t.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('');
    const body = rows.slice(0, limit).map((r, i) => `<tr class="border-t border-ink-700/50 ${t.o.onRow ? 'cursor-pointer hover:bg-ink-800' : ''}"
        ${t.o.onRow ? `onclick="PBT._row('${id}', ${t.rows.indexOf(r)})"` : ''}>${t.columns.map((c) =>
      `<td class="whitespace-nowrap px-2 py-1.5 ${c.align === 'right' ? 'text-right' : ''}">${c.fmt ? c.fmt(r[c.key], r) : esc(r[c.key])}</td>`).join('')}</tr>`).join('');
    const more = rows.length > limit ? `<p class="mt-2 text-xs text-ink-400">Showing ${limit} of ${rows.length} rows; filter to narrow down. The full list is in the run's CSV.</p>` : '';
    return `<table class="w-full text-xs text-slate-200"><thead class="text-ink-300">${head ? `<tr>${head}</tr>` : ''}</thead><tbody>${body}</tbody></table>${more}`;
  }
  PBT._filter = (id, v) => { TABLES[id].filter = v; document.getElementById(id).innerHTML = tableBody(id); };
  PBT._sort = (id, key) => {
    const t = TABLES[id];
    t.dir = t.sort === key ? -t.dir : 1;
    t.sort = key;
    document.getElementById(id).innerHTML = tableBody(id);
  };
  PBT._row = (id, i) => { const t = TABLES[id]; if (t.o.onRow) t.o.onRow(t.rows[i]); };

  /* ------------------------------------------------------------ charts (inline SVG) */
  const W = 820, H = 260, PAD = { l: 70, r: 16, t: 12, b: 42 };
  const COLORS = ['#34d399', '#7dd3fc', '#c4b5fd', '#fbbf24', '#f472b6', '#a3e635'];
  function niceTicks(lo, hi, n = 5) {
    if (!isFinite(lo) || !isFinite(hi)) return [];
    if (lo === hi) { lo -= 1; hi += 1; }
    const span = hi - lo, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  const shortNum = (v, unit) => {
    const a = Math.abs(v);
    const s = a >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : a >= 1e4 ? `${(v / 1e3).toFixed(0)}k` : a >= 1000 ? `${(v / 1e3).toFixed(1)}k` : `${+v.toFixed(2)}`;
    return unit === '$' ? `$${s}` : unit === '%' ? `${s}%` : s;
  };
  function axes(xmin, xmax, ymin, ymax, o) {
    const sx = (x) => PAD.l + ((x - xmin) / (xmax - xmin || 1)) * (W - PAD.l - PAD.r);
    const sy = (y) => H - PAD.b - ((y - ymin) / (ymax - ymin || 1)) * (H - PAD.t - PAD.b);
    let g = '';
    niceTicks(ymin, ymax).forEach((v) => {
      g += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#1a2433"/>`;
      g += `<text x="${PAD.l - 6}" y="${sy(v) + 4}" text-anchor="end" font-size="10" fill="#8b98aa">${esc(shortNum(v, o.unit))}</text>`;
    });
    const xt = o.xTicks || [];
    xt.forEach(([x, label]) => {
      g += `<text x="${sx(x)}" y="${H - PAD.b + 14}" text-anchor="middle" font-size="10" fill="#8b98aa">${esc(label)}</text>`;
    });
    g += `<text x="${(PAD.l + W - PAD.r) / 2}" y="${H - 6}" text-anchor="middle" font-size="11" fill="#8b98aa">${esc(o.xLabel || '')}</text>`;
    g += `<text transform="translate(14 ${(H - PAD.b + PAD.t) / 2}) rotate(-90)" text-anchor="middle" font-size="11" fill="#8b98aa">${esc(o.yLabel || '')}</text>`;
    if (ymin < 0 && ymax > 0) g += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${sy(0)}" y2="${sy(0)}" stroke="#5d6b80" stroke-dasharray="3 3"/>`;
    return { sx, sy, g };
  }
  function timeTicks(xmin, xmax) {
    const n = 6, out = [];
    for (let i = 0; i <= n; i++) {
      const x = xmin + ((xmax - xmin) * i) / n;
      out.push([x, new Date(x).toISOString().slice(0, 10)]);
    }
    return out;
  }
  function svgWrap(inner, label) {
    return `<svg viewBox="0 0 ${W} ${H}" class="h-auto w-full" style="max-height:340px" role="img" aria-label="${esc(label)}">${inner}</svg>`;
  }
  /** series: [{name, points: [[ms, y], ...], color, area}] */
  function lineChart(series, o) {
    const pts = series.flatMap((s) => s.points).filter((p) => isNum(p[0]) && isNum(p[1]));
    if (pts.length < 2) return empty('Not enough data points for this chart.');
    let xmin = Math.min(...pts.map((p) => p[0])), xmax = Math.max(...pts.map((p) => p[0]));
    let ymin = Math.min(...pts.map((p) => p[1])), ymax = Math.max(...pts.map((p) => p[1]));
    if (o.zero) { ymin = Math.min(0, ymin); ymax = Math.max(0, ymax); }
    const padY = (ymax - ymin) * 0.06 || 1; ymin -= o.zero && ymin === 0 ? 0 : padY; ymax += o.zero && ymax === 0 ? 0 : padY;
    const a = axes(xmin, xmax, ymin, ymax, { ...o, xTicks: o.xTicks || timeTicks(xmin, xmax) });
    let g = a.g;
    series.forEach((s, i) => {
      const color = s.color || COLORS[i % COLORS.length];
      const p = s.points.filter((q) => isNum(q[1]));
      if (!p.length) return;
      const d = p.map((q, j) => `${j ? 'L' : 'M'}${a.sx(q[0]).toFixed(1)},${a.sy(q[1]).toFixed(1)}`).join('');
      if (s.area) g += `<path d="${d}L${a.sx(p[p.length - 1][0]).toFixed(1)},${a.sy(Math.max(ymin, Math.min(0, ymax)))}L${a.sx(p[0][0]).toFixed(1)},${a.sy(Math.max(ymin, Math.min(0, ymax)))}Z" fill="${color}" fill-opacity=".15"/>`;
      g += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" ${s.dash ? 'stroke-dasharray="5 4"' : ''}/>`;
    });
    const legend = series.length > 1 ? `<div class="mt-1 flex flex-wrap gap-4 text-[11px] text-ink-300">${series.map((s, i) =>
      `<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4" style="background:${s.color || COLORS[i % COLORS.length]}"></i>${esc(s.name)}</span>`).join('')}</div>` : '';
    return svgWrap(g, o.title || '') + legend;
  }
  /** bars: [{label, value}] */
  function barChart(bars, o) {
    const vals = bars.map((b) => b.value).filter(isNum);
    if (!vals.length) return empty('No data for this chart.');
    const ymin = Math.min(0, ...vals), ymax = Math.max(0, ...vals);
    const n = bars.length;
    const a = axes(0, n, ymin, ymax === ymin ? ymin + 1 : ymax, {
      ...o, xTicks: bars.map((b, i) => [i + 0.5, n > 16 && i % Math.ceil(n / 12) ? '' : b.label]) });
    let g = a.g;
    const bw = Math.max(2, ((W - PAD.l - PAD.r) / n) * 0.7);
    bars.forEach((b, i) => {
      if (!isNum(b.value)) return;
      const x = a.sx(i + 0.5) - bw / 2, y0 = a.sy(0), y1 = a.sy(b.value);
      const color = b.color || (b.value >= 0 ? '#34d399' : '#fb7185');
      g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(y1 - y0)).toFixed(1)}" fill="${color}" fill-opacity=".8"><title>${esc(b.label)}: ${esc(b.title || shortNum(b.value, o.unit))}</title></rect>`;
    });
    return svgWrap(g, o.title || '');
  }
  function histogram(values, o) {
    const v = values.filter(isNum);
    if (v.length < 2) return empty('Fewer than 2 completed trades: no distribution to show.');
    const lo = Math.min(...v), hi = Math.max(...v), k = Math.min(20, Math.max(5, Math.round(Math.sqrt(v.length) * 2)));
    const w = (hi - lo) / k || 1;
    const bins = Array.from({ length: k }, (_, i) => ({ from: lo + i * w, to: lo + (i + 1) * w, n: 0 }));
    v.forEach((x) => { bins[Math.min(k - 1, Math.floor((x - lo) / w))].n += 1; });
    return barChart(bins.map((b) => ({ label: shortNum((b.from + b.to) / 2, o.unit), value: b.n, title: `${b.n} trades between ${shortNum(b.from, o.unit)} and ${shortNum(b.to, o.unit)}`,
      color: (b.from + b.to) / 2 >= 0 ? '#34d399' : '#fb7185' })), { ...o, unit: '' });
  }
  function scatter(points, o) {
    const p = points.filter((q) => isNum(q[0]) && isNum(q[1]));
    if (!p.length) return empty('No completed trades to plot.');
    let xmin = Math.min(...p.map((q) => q[0])), xmax = Math.max(...p.map((q) => q[0]));
    if (xmin === xmax) { xmin -= 864e5; xmax += 864e5; }
    const ymin = Math.min(0, ...p.map((q) => q[1])), ymax = Math.max(0, ...p.map((q) => q[1]));
    const a = axes(xmin, xmax, ymin, ymax === ymin ? 1 : ymax, { ...o, xTicks: timeTicks(xmin, xmax) });
    let g = a.g;
    p.forEach((q) => { g += `<circle cx="${a.sx(q[0]).toFixed(1)}" cy="${a.sy(q[1]).toFixed(1)}" r="3.5" fill="${q[1] >= 0 ? '#34d399' : '#fb7185'}" fill-opacity=".85"><title>${esc(q[2] || '')}</title></circle>`; });
    return svgWrap(g, o.title || '');
  }
  /** OHLC bars of one day with entry/exit markers and stop levels. */
  function candles(rows, marks, levels) {
    if (!rows || !rows.length) return empty('No market data stored for this trade.');
    const lo = Math.min(...rows.map((r) => r[3]), ...levels.map((l) => l.y)), hi = Math.max(...rows.map((r) => r[2]));
    const step = rows.length > 1 ? rows[1][0] - rows[0][0] : 300000;
    const xmin = rows[0][0], xmax = rows[rows.length - 1][0] + step;
    const a = axes(xmin, xmax, lo * 0.998, hi * 1.002, { unit: '$', yLabel: 'Price (USD)', xLabel: 'Time (ET)',
      xTicks: rows.filter((_, i) => i % Math.max(1, Math.round(rows.length / 8)) === 0).map((r) => [r[0], (xmax - xmin > 2 * 864e5)
        ? new Date(r[0]).toISOString().slice(5, 10)
        : new Date(r[0]).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })]) });
    let g = a.g;
    const bw = Math.max(1.5, ((W - PAD.l - PAD.r) / rows.length) * 0.6);
    rows.forEach((r) => {
      const x = a.sx(r[0] + step / 2), up = r[4] >= r[1], c = up ? '#34d399' : '#fb7185';
      g += `<line x1="${x}" x2="${x}" y1="${a.sy(r[2])}" y2="${a.sy(r[3])}" stroke="${c}" stroke-opacity=".7"/>`;
      g += `<rect x="${x - bw / 2}" y="${a.sy(Math.max(r[1], r[4]))}" width="${bw}" height="${Math.max(1, Math.abs(a.sy(r[1]) - a.sy(r[4])))}" fill="${c}" fill-opacity=".7"/>`;
    });
    levels.forEach((l) => { g += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${a.sy(l.y)}" y2="${a.sy(l.y)}" stroke="${l.color}" stroke-dasharray="4 3"><title>${esc(l.label)}</title></line>`; });
    marks.forEach((m) => {
      const x = a.sx(m.t), y = a.sy(m.price);
      g += `<path d="M${x},${y + (m.buy ? 10 : -10)}l-5,${m.buy ? 7 : -7}h10z" fill="${m.buy ? '#7dd3fc' : '#fbbf24'}"><title>${esc(m.label)}</title></path>`;
    });
    return svgWrap(g, 'Price bars of the trade day') + `<div class="mt-1 flex flex-wrap gap-4 text-[11px] text-ink-300">
      <span>▲ blue: buy fill</span><span>▼ amber: sell fill</span>${levels.map((l) => `<span style="color:${l.color}">– – ${esc(l.label)}</span>`).join('')}</div>`;
  }
  const chart = (title, svg, explanation) => `<figure class="space-y-2">
      <figcaption class="text-sm font-semibold text-slate-100">${esc(title)}</figcaption>${svg}
      <p class="text-xs leading-relaxed text-ink-300">${explanation}</p></figure>`;

  /* ------------------------------------------------------------ run helpers */
  const D = () => window.PAPERBOT_BT || null;
  const param = (k) => new URLSearchParams(location.search).get(k);
  function pickRun(d) {
    const want = param('run');
    if (want && d.details[want]) return d.details[want];
    const sid = param('strategy');
    const s = d.strategies.find((x) => x.strategy_id === (sid || (d.strategies[0] || {}).strategy_id)) || {};
    if (s.latest_run && d.details[s.latest_run]) return d.details[s.latest_run];
    if (sid) return null;
    const any = d.runs.find((r) => d.details[r.run_id]);
    return any ? d.details[any.run_id] : null;
  }
  function strategyTabs(d, page, currentId) {
    return `<div class="flex flex-wrap gap-1">${d.strategies.map((s) => `<a href="${page}?strategy=${encodeURIComponent(s.strategy_id)}"
      class="rounded-lg px-2.5 py-1 text-xs no-underline ${s.strategy_id === currentId ? 'bg-emerald-400/15 text-emerald-200 ring-1 ring-inset ring-emerald-400/30' : 'text-ink-300 hover:bg-ink-800'}"
      title="${esc(s.description)}">${esc(s.name)} <span class="text-ink-400">${esc(s.timeframe || '')}</span></a>`).join('')}</div>`;
  }
  PBT.strategyTabs = strategyTabs;
  function runSelect(d, current, page) {
    const sid = current ? current.metadata.strategy_id : null;
    const tabs = strategyTabs(d, page, sid);
    const opts = d.runs.filter((r) => d.details[r.run_id] && (!sid || r.strategy_id === sid)).map((r) =>
      `<option value="${esc(r.run_id)}" ${current && r.run_id === current.metadata.run_id ? 'selected' : ''}>${esc(r.run_id)} · ${esc(r.kind)}${r.experiment_id ? ' ' + esc(r.experiment_id) : ''} · ${esc(r.status)}${r.synthetic_data ? ' · SYNTHETIC' : ''}</option>`).join('');
    return `<div class="space-y-2">${tabs}<label class="text-xs text-ink-300">Run <select class="ml-1 rounded-lg border border-ink-700 bg-ink-850 px-2 py-1 text-xs text-slate-100"
      onchange="location.href='${page}?run='+encodeURIComponent(this.value)">${opts}</select></label></div>`;
  }
  const M = (p) => p.metrics || {};
  const naReason = (p, key) => (M(p).na_reasons || {})[key];
  const defn = (p, key) => (M(p).definitions || {})[key] || '';
  const synthBanner = (meta) => (meta.synthetic_data ? notice('<b>SYNTHETIC DATA.</b> This run used generated prices for demonstration and testing. Its results say nothing about the real strategy.', 'red') : '');

  /* ------------------------------------------------------------ section A-G (strategy report) */
  function sectionA(p) {
    const meta = p.metadata, m = M(p);
    const lines = (m.summary || []).map((l) => `<li>${esc(l)}</li>`).join('');
    const exp = meta.experiment ? notice(`<b>Experiment ${esc(meta.experiment_id)}</b>: ${esc(meta.experiment.hypothesis || '')} Changed: ${esc(JSON.stringify(meta.experiment.changes))}. This is NOT the production configuration.`, 'violet') : '';
    return panel('A · Executive summary', `<div class="space-y-3">
      ${notice(`<b>${FUTURE}</b>`, 'amber')}${synthBanner(meta)}${exp}
      <ul class="list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-slate-200">${lines}</ul>
      ${grid([
        kpi('Run', `<span class="mono text-sm">${esc(meta.run_id)}</span>`, `${statusPill(meta.status)} ${esc(meta.kind)}`),
        kpi('Data provider', esc(meta.provider), `dataset <span class="mono">${esc(meta.dataset_id || '—')}</span>`),
        kpi('Code version', `<span class="mono text-sm">${esc(((meta.code || {}).commit || 'unknown').slice(0, 10))}</span>`, `${esc((meta.code || {}).branch || '')}${(meta.code || {}).dirty ? ' · uncommitted changes' : ''}`),
        kpi('Rules version', `<span class="mono text-sm">${esc(meta.strategy_version)}</span>`, 'hash of the rules used'),
      ])}</div>`, { id: 'summary' });
  }
  function sectionB(p) {
    const rows = (M(p).rules_explained || []).map((r) => ({ ...r, value: typeof r.value === 'object' ? JSON.stringify(r.value) : r.value }));
    const ex = (p.config && p.config.backtest && p.config.backtest.execution) || p.metadata.execution_assumptions || {};
    const exRows = Object.entries(ex).map(([k, v]) => ({ key: k, value: String(v) }));
    return panel('B · Strategy configuration', `<p class="mb-3 text-sm text-ink-300">The values below are the settings this run actually used (config snapshot), with what each one means.</p>
      ${table('rules-' + p.metadata.run_id, [
        { key: 'label', label: 'Setting', fmt: (v, r) => `<span class="text-slate-100">${esc(v)}</span><div class="mono text-[10px] text-ink-400">${esc(r.key)}</div>` },
        { key: 'value', label: 'Value', fmt: (v) => `<span class="mono">${esc(v)}</span>` },
        { key: 'explanation', label: 'What it means', fmt: (v) => `<span class="whitespace-normal text-ink-300">${esc(v)}</span>` },
      ], rows, { filter: false })}
      <div class="mt-4">${details('Execution and cost assumptions', table('exec-' + p.metadata.run_id, [{ key: 'key', label: 'Assumption' }, { key: 'value', label: 'Value' }], exRows, { filter: false }) +
        `<p class="mt-2 text-xs text-ink-300">Commission: ${esc((p.metadata.cost_assumptions || {}).commission || '')}</p>`)}</div>
      <div class="mt-3">${details('Full configuration snapshot (JSON)', `<pre class="mono max-h-96 overflow-auto whitespace-pre-wrap text-[11px] text-ink-300">${esc(JSON.stringify(p.config, null, 1))}</pre>`)}</div>`, { id: 'config' });
  }
  const METRIC_ROWS = [
    ['starting_capital', 'Starting capital', 'usd'], ['ending_equity', 'Ending equity', 'usd'], ['net_pnl', 'Net P&L after costs', 'usdc'],
    ['total_return_pct', 'Total return (vs starting capital)', 'pctc'], ['realized_pnl', 'Realized P&L', 'usdc'], ['unrealized_pnl', 'Unrealized P&L', 'usdc'],
    ['gross_profits', 'Gross profits', 'usdc'], ['gross_losses', 'Gross losses', 'usdc'], ['completed_trades', 'Completed trades', 'int'],
    ['open_positions_end', 'Open positions at end', 'int'], ['winning_trades', 'Winning trades', 'int'], ['losing_trades', 'Losing trades', 'int'],
    ['breakeven_trades', 'Breakeven trades', 'int'], ['win_rate_pct', 'Win rate (of completed trades)', 'pct'], ['avg_win', 'Average winning trade', 'usdc'],
    ['avg_loss', 'Average losing trade', 'usdc'], ['largest_win', 'Largest winning trade', 'usdc'], ['largest_loss', 'Largest losing trade', 'usdc'],
    ['profit_factor', 'Profit factor', 'num'], ['expectancy', 'Average trade (expectancy)', 'usdc'], ['avg_r', 'Average R per trade', 'num'],
    ['max_drawdown_pct', 'Maximum drawdown (vs prior peak)', 'pct'], ['max_drawdown_usd', 'Maximum drawdown', 'usd'], ['avg_holding_minutes', 'Average holding period (minutes)', 'num1'],
    ['total_commission', 'Total commissions', 'usd'], ['total_slippage', 'Simulated spread + slippage', 'usd'], ['cost_share_of_gross_pct', 'Costs as % of gross P&L', 'pct'],
    ['avg_exposure_pct', 'Average exposure (% of equity)', 'pct'], ['max_exposure_pct', 'Maximum exposure (% of equity)', 'pct'], ['time_in_market_pct', 'Time in the market', 'pct'],
    ['max_concurrent_positions', 'Most positions at once', 'int'], ['max_consecutive_losses', 'Longest losing streak', 'int'], ['top3_share_of_net_pct', 'Top-3 trades share of net P&L', 'pct'],
    ['daily_volatility_pct', 'Daily volatility', 'pct'], ['sharpe_like', 'Risk-adjusted ratio (Sharpe-like)', 'num'], ['benchmark_return_pct', 'Benchmark buy-and-hold return', 'pctc'],
    ['mean_trade_ci95', 'Average trade, 95% bootstrap range', 'ci'], ['days_with_trades', 'Days with completed trades', 'int'], ['days_without_trades', 'Days without trades', 'int'],
  ];
  function metricValue(p, key, kind) {
    const v = M(p)[key], reason = naReason(p, key);
    if (kind === 'ci') return Array.isArray(v) ? `${money(v[0])} to ${money(v[1])}` : na(reason);
    if (kind === 'usd') return usd(v, { reason });
    if (kind === 'usdc') return usd(v, { reason, color: true, signed: true });
    if (kind === 'pct') return pct(v, { reason });
    if (kind === 'pctc') return pct(v, { reason, color: true, signed: true });
    if (kind === 'int') return int(v);
    if (kind === 'num1') return num(v, 1, reason);
    return num(v, 2, reason);
  }
  function sectionC(p) {
    const cells = METRIC_ROWS.map(([k, label, kind]) => `<div class="flex items-baseline justify-between gap-3 border-b border-ink-700/40 py-1.5">
      <span class="text-ink-300">${esc(label)}${tip(defn(p, k))}</span><span class="text-right">${metricValue(p, k, kind)}</span></div>`).join('');
    const m = M(p);
    return panel('C · Key performance metrics', `<div class="grid grid-cols-1 gap-x-8 text-sm md:grid-cols-2 xl:grid-cols-3">${cells}</div>
      <p class="mt-3 text-xs text-ink-300">All money in USD. Trade results are net of commissions; spread and slippage are already in the fill prices.
      A position sold in pieces counts as one trade. Open positions at the end are valued at their last 5-minute close and appear only in unrealized P&L and ending equity.
      ${m.benchmark_symbol ? `Benchmark: ${esc(m.benchmark_symbol)} buy-and-hold over the same days.` : ''} Hover ⓘ for definitions.</p>`, { id: 'metrics' });
  }
  function equitySeries(p) {
    return (p.equity || []).map((r) => [ms(r.time_et), r.equity]);
  }
  function benchmarkSeries(p) {
    const b = M(p).benchmark_series || [];
    if (b.length < 2) return null;
    const cap = M(p).starting_capital, base = b[0][1];
    return b.map(([d, c], i) => [ms(`${d}T${i === 0 ? '09:30' : '16:00'}:00-05:00`), (cap * c) / base]);
  }
  function drawdownSeries(p) {
    let peak = -Infinity;
    return (p.equity || []).map((r) => { peak = Math.max(peak, r.equity); return [ms(r.time_et), peak > 0 ? -100 * (peak - r.equity) / peak : 0]; });
  }
  function cumRealized(p) {
    let c = 0;
    return (p.trades || []).filter((t) => t.status === 'closed' && t.exit_time_et).sort((a, b) => ms(a.exit_time_et) - ms(b.exit_time_et))
      .map((t) => { c += t.net_pnl || 0; return [ms(t.exit_time_et), c]; });
  }
  function sectionD(p, o = {}) {
    const bench = benchmarkSeries(p);
    const eq = [{ name: 'Strategy equity', points: equitySeries(p), area: false }];
    if (bench) eq.push({ name: `${M(p).benchmark_symbol} buy-and-hold (same starting capital)`, points: bench, dash: true, color: '#8b98aa' });
    const closed = (p.trades || []).filter((t) => t.status === 'closed');
    const charts = [
      chart('Portfolio equity curve', lineChart(eq, { unit: '$', yLabel: 'Equity (USD)', xLabel: 'Date (ET)' }),
        'Cash plus open positions, every 5 minutes, after costs. The grey dashed line is what the same money would have done in the benchmark ETF. A rising line in the past does not mean it will keep rising.'),
      chart('Drawdown', lineChart([{ name: 'Drawdown', points: drawdownSeries(p), area: true, color: '#fb7185' }], { unit: '%', yLabel: 'Below previous peak (%)', xLabel: 'Date (ET)', zero: true }),
        'How far equity was below its highest earlier value at each moment. The deepest point is the maximum drawdown.'),
      chart('Cumulative realized P&L', lineChart([{ name: 'Realized', points: cumRealized(p), color: '#7dd3fc' }], { unit: '$', yLabel: 'Realized P&L (USD)', xLabel: 'Exit date (ET)', zero: true }),
        'Running total of completed trades\' net results, in the order they closed. Unrealized gains on open positions are not included.'),
      chart('Monthly performance', barChart((p.monthly || []).map((r) => ({ label: r.month, value: r.pnl })), { unit: '$', yLabel: 'P&L (USD)', xLabel: 'Month' }),
        'Change in equity per calendar month (realized and unrealized), after costs.'),
      chart('Distribution of trade results', histogram(closed.map((t) => t.net_pnl), { unit: '$', yLabel: 'Number of trades', xLabel: 'Net result per trade (USD)' }),
        'How many completed trades fell into each result range. A few large winners with many small losers looks very different from the opposite.'),
      chart('Trade outcomes over time', scatter(closed.map((t) => [ms(t.exit_time_et), t.net_pnl, `#${t.trade_id} ${t.symbol} ${money(t.net_pnl, { plain: true })}`]), { unit: '$', yLabel: 'Net result (USD)', xLabel: 'Exit date (ET)' }),
        'Each dot is one completed trade: green made money, red lost money. Clusters of red show losing periods.'),
    ];
    return panel(o.title || 'D · Performance charts', `<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">${charts.join('')}</div>`, { id: 'charts' });
  }
  /* trade detail modal */
  function tradeDetail(p, t) {
    const orders = (p.orders || []).filter((o) => String(o.trade_id) === String(t.trade_id));
    const fills = (p.fills || []).filter((f) => String(f.trade_id) === String(t.trade_id));
    let stops = [];
    try { stops = JSON.parse(t.stop_history || '[]'); } catch (e) { stops = []; }
    const ctx = (p.trade_context || {})[String(t.trade_id)] || [];
    const marks = fills.map((f) => ({ t: ms(f.time_et), price: f.price, buy: f.side === 'BUY', label: `${f.side} ${f.quantity} @ ${f.price} (${f.kind})` }));
    const levels = [{ y: t.initial_stop, color: '#fb7185', label: `initial stop ${t.initial_stop}` }];
    stops.forEach((s) => levels.push({ y: s.new, color: '#fbbf24', label: `${s.event} stop ${s.new}` }));
    const body = `
      <div class="space-y-4 text-sm">
        ${grid([kpi('Net result', usd(t.net_pnl, { color: true, signed: true }), `${pct(t.return_on_position_pct, { signed: true })} of position value · ${num(t.r_multiple)} R`),
          kpi('Entry', `${money(t.entry_price)} × ${int(t.quantity)}`, et(t.entry_time_et)),
          kpi('Exit', money(t.exit_price), `${et(t.exit_time_et)} · ${esc(t.exit_reason)}`),
          kpi('Costs', `${money(t.commission)} fees`, `${money(t.slippage_cost)} spread+slippage`)])}
        ${chart(p.metadata.holding === 'multi_day' ? 'Daily bars around the holding period' : `Price bars on the trade day (${esc(p.metadata.tape || '5m')}, ET)`,
          candles(ctx, marks, levels.filter((l) => isNum(l.y))),
          'Market data the simulator saw for this symbol, with the simulated fills and stop levels. The strategy only ever used bars that had completed at decision time.')}
        <div><h3 class="mb-1 font-semibold text-slate-100">Strategy conditions at entry</h3>${list(String(t.entry_conditions || '').split(' | ').filter(Boolean))}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Stop history</h3>${stops.length ? list(stops.map((s) => `${etPlain(s.time)}: ${s.event} ${s.old} → ${s.new}`)) : '<p class="text-ink-400">Stop never moved.</p>'}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Orders</h3>${table('ord-' + t.trade_id, [
          { key: 'order_id', label: 'ID' }, { key: 'kind', label: 'Kind' }, { key: 'side', label: 'Side' }, { key: 'quantity', label: 'Qty', align: 'right' },
          { key: 'limit_price', label: 'Limit', align: 'right' }, { key: 'stop_price', label: 'Stop', align: 'right' }, { key: 'placed_at_et', label: 'Placed', fmt: et },
          { key: 'status', label: 'Status' }, { key: 'filled', label: 'Filled', align: 'right' }, { key: 'avg_fill_price', label: 'Avg fill', align: 'right' },
          { key: 'commission', label: 'Fee', align: 'right' }, { key: 'reason', label: 'Reason' }], orders, { filter: false })}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Fills</h3>${table('fill-' + t.trade_id, [
          { key: 'fill_id', label: 'ID' }, { key: 'time_et', label: 'Time', fmt: et }, { key: 'side', label: 'Side' }, { key: 'kind', label: 'Kind' },
          { key: 'quantity', label: 'Qty', align: 'right' }, { key: 'price', label: 'Price', align: 'right' }, { key: 'reference_price', label: 'Reference', align: 'right', tip: 'Price before spread/slippage' },
          { key: 'commission', label: 'Fee', align: 'right' }, { key: 'slippage_cost', label: 'Slippage $', align: 'right' }, { key: 'realized_pnl', label: 'Realized', align: 'right' },
          { key: 'note', label: 'How it filled' }], fills, { filter: false })}</div>
        ${t.warnings ? `<div><h3 class="mb-1 font-semibold text-slate-100">Execution warnings</h3>${list(String(t.warnings).split(';').map(warningText))}</div>` : ''}
      </div>`;
    modal(`Trade #${t.trade_id} · ${t.symbol} · ${t.status}`, body);
  }
  const WARNINGS = {
    ambiguous_bar: 'Ambiguous bar: a stop and a profit target could both have been reached in one 5-minute bar; the stop was assumed first (conservative).',
    gap_through_stop: 'The price opened below the stop, so the stop filled at the (worse) opening price.',
    entry_filled_below_stop: 'The entry filled at an opening price already below the stop; the position was stopped out immediately.',
    force_close_price_estimated: 'Force-close price estimated from the 15:50 bar (lower of open and close): the 15:51 price is not in 5-minute data.',
    partial_entry_fill: 'The entry filled over several bars because of the volume participation cap.',
    volume_capped_no_fill: 'A bar had too little volume for any fill under the participation cap.',
    missing_bar: 'A 5-minute bar was missing while the position was open.',
    held_overnight: 'Held overnight because the force close had no data.',
    open_at_end_of_test: 'Still open when the test ended; valued at the last price.',
    force_close_no_data: 'No bar at force-close time.',
  };
  const warningText = (w) => WARNINGS[w] || w;
  function modal(title, body) {
    let el = document.getElementById('pbt-modal');
    if (!el) { el = document.createElement('div'); el.id = 'pbt-modal'; document.body.appendChild(el); }
    el.innerHTML = `<div class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-2 sm:p-6" onclick="if(event.target===this)PBT.closeModal()">
      <div class="w-full max-w-5xl rounded-2xl border border-ink-600 bg-ink-900 shadow-2xl" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <header class="flex items-center justify-between border-b border-ink-700 px-5 py-3"><h2 class="font-semibold text-white">${esc(title)}</h2>
          <button class="rounded-lg px-3 py-1 text-sm text-ink-300 hover:bg-ink-800" onclick="PBT.closeModal()">Close ✕</button></header>
        <div class="p-5">${body}</div></div></div>`;
    document.addEventListener('keydown', escClose);
  }
  function escClose(e) { if (e.key === 'Escape') PBT.closeModal(); }
  PBT.closeModal = () => { const el = document.getElementById('pbt-modal'); if (el) el.innerHTML = ''; document.removeEventListener('keydown', escClose); };

  const TRADE_COLS = (p) => [
    { key: 'trade_id', label: 'ID', align: 'right' }, { key: 'symbol', label: 'Symbol', fmt: (v) => `<b>${esc(v)}</b>` },
    { key: 'entry_time_et', label: 'Entry (ET)', fmt: et }, { key: 'exit_time_et', label: 'Exit (ET)', fmt: et },
    { key: 'entry_price', label: 'Entry $', align: 'right', fmt: (v) => money(v) }, { key: 'exit_price', label: 'Exit $', align: 'right', fmt: (v) => money(v) },
    { key: 'quantity', label: 'Qty', align: 'right' }, { key: 'direction', label: 'Dir' },
    { key: 'initial_stop', label: 'Initial stop', align: 'right', fmt: (v) => money(v) }, { key: 'exit_reason', label: 'Exit reason' },
    { key: 'gross_pnl', label: 'Gross P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
    { key: 'commission', label: 'Fees', align: 'right', fmt: (v) => money(v) }, { key: 'slippage_cost', label: 'Slippage', align: 'right', fmt: (v) => money(v) },
    { key: 'net_pnl', label: 'Net P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
    { key: 'return_on_position_pct', label: 'Return', align: 'right', fmt: (v) => pct(v, { color: true, signed: true }), tip: 'Net result / position value at entry' },
    { key: 'holding_minutes', label: 'Held (min)', align: 'right' }, { key: 'r_multiple', label: 'R', align: 'right', fmt: (v) => num(v) },
    { key: 'warnings', label: 'Warnings', fmt: (v) => (v ? `<span class="text-amber-300" title="${esc(String(v).split(';').map(warningText).join('\n'))}">⚠ ${String(v).split(';').length}</span>` : '') },
    { key: 'status', label: 'Status' },
  ];
  function sectionE(p, o = {}) {
    const closed = (p.trades || []).filter((t) => t.status === 'closed');
    const open = (p.trades || []).filter((t) => t.status === 'open');
    const id = 'trades-' + p.metadata.run_id;
    const onRow = (t) => tradeDetail(p, t);
    const openBlock = open.length ? `<h3 class="mb-2 mt-6 text-sm font-semibold text-slate-100">Open at the end of the test (${open.length})</h3>
      <p class="mb-2 text-xs text-ink-300">These positions were still held when the data ended. Their unrealized P&L = shares × (last 5-minute close − average entry price); their net P&L also subtracts the fees already paid. They are not counted as completed trades.</p>
      ${table(id + '-open', TRADE_COLS(p).concat([{ key: 'unrealized_pnl', label: 'Unrealized', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) }]), open, { onRow, filter: false })}` : '';
    return panel(o.title || `E · Trade-by-trade ledger (${closed.length} completed)`, `<p class="mb-2 text-xs text-ink-300">Every simulated trade from the run's trades.csv. Click a row for its orders, fills, stop history and the price bars around it. Click a column title to sort. Times are US Eastern (ET).</p>
      ${table(id, TRADE_COLS(p), closed, { onRow, empty: 'No completed trades in this run.', sort: 'entry_time_et' })}${openBlock}`, { id: 'ledger' });
  }
  function sectionF(p) {
    const m = M(p);
    const monthly = table('monthly-' + p.metadata.run_id, [
      { key: 'month', label: 'Month' }, { key: 'pnl', label: 'P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
      { key: 'return_pct', label: 'Return', align: 'right', fmt: (v) => pct(v, { color: true, signed: true }), tip: 'vs equity at the start of the month' },
      { key: 'trades', label: 'Trades', align: 'right' }, { key: 'wins', label: 'Won', align: 'right' }, { key: 'losses', label: 'Lost', align: 'right' },
      { key: 'commission', label: 'Fees', align: 'right', fmt: (v) => money(v) }, { key: 'max_drawdown_pct', label: 'Drawdown', align: 'right', fmt: (v) => pct(v), tip: 'Deepest fall of daily closing equity within the month' },
      { key: 'trading_days', label: 'Days', align: 'right' }, { key: 'days_without_trades', label: 'No-trade days', align: 'right' }], p.monthly || [], { filter: false });
    let peak = -Infinity;
    const daily = (p.daily || []).map((r) => { peak = Math.max(peak, r.end_equity); return { ...r, dd: peak > 0 ? 100 * (peak - r.end_equity) / peak : 0 }; });
    const dailyT = table('daily-' + p.metadata.run_id, [
      { key: 'date', label: 'Date' }, { key: 'pnl', label: 'P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }), tip: 'Change in equity over the day (realized + unrealized)' },
      { key: 'realized_pnl', label: 'Realized', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
      { key: 'trades_closed', label: 'Trades', align: 'right' }, { key: 'wins', label: 'Won', align: 'right' }, { key: 'losses', label: 'Lost', align: 'right' },
      { key: 'commission', label: 'Fees', align: 'right', fmt: (v) => money(v) }, { key: 'dd', label: 'Drawdown', align: 'right', fmt: (v) => pct(v), tip: 'Daily close below the previous highest daily close' },
      { key: 'watchlist_size', label: 'Watchlist', align: 'right', tip: 'Gappers the prefilter would have listed' },
      { key: 'halted', label: 'Halted', fmt: (v) => (v === true || v === 'True' ? pill('halted', 'red') : '') }], daily, { sort: 'date' });
    return panel('F · Daily and monthly breakdown', `<div class="space-y-3 text-sm">
      <p class="text-ink-300"><b class="text-slate-100">Realized</b> P&L is locked in by selling. <b class="text-slate-100">Unrealized</b> P&L is the paper gain or loss on shares still held, valued at the latest price. Daily P&L below is the change in total equity, so it includes both.
      Only days with market data are listed: a day without data is missing, not a zero-return day. ${int(m.days_without_trades)} of ${int(m.days)} trading days had no completed trade.</p>
      <h3 class="font-semibold text-slate-100">Monthly</h3>${monthly}
      <h3 class="font-semibold text-slate-100">Daily</h3>${dailyT}</div>`, { id: 'breakdown' });
  }
  function questionsTable(qs, id) {
    return table(id, [
      { key: 'question', label: 'Research question', fmt: (v) => `<span class="whitespace-normal text-slate-100">${esc(v)}</span>` },
      { key: 'status', label: 'Status', fmt: statusPill },
      { key: 'evidence', label: 'Computed evidence', fmt: (v) => `<span class="whitespace-normal text-ink-300">${esc(v)}</span>` },
      { key: 'next_step', label: 'Next step', fmt: (v) => `<span class="whitespace-normal text-ink-300">${esc(v)}</span>` }], qs || [], { filter: false });
  }
  function sectionG(p) {
    const a = M(p).assessment || {};
    return panel('G · Plain-English assessment', `<div class="space-y-4 text-sm">
      <div><h3 class="mb-1 font-semibold text-emerald-200">Computed findings (from the run's numbers)</h3>${list(a.computed_findings)}</div>
      ${notice(esc(a.label || ''), 'sky')}
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div><h3 class="mb-1 font-semibold text-slate-100">What the results do NOT establish</h3>${list(a.not_established)}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Assumptions that materially affect the outcome</h3>${list(a.assumptions)}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Is the dataset sufficient?</h3><p class="text-slate-200">${esc(a.data_sufficiency || '')}</p></div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Do costs and execution assumptions change the result?</h3><p class="text-slate-200">${esc(a.costs_and_execution || '')}</p></div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Concerns raised</h3>${list(a.concerns)}</div>
        <div><h3 class="mb-1 font-semibold text-slate-100">Additional tests required</h3>${list(a.further_tests)}</div>
      </div>
      <h3 class="font-semibold text-slate-100">Research questions</h3>${questionsTable(M(p).research_questions, 'rq-' + p.metadata.run_id)}
    </div>`, { id: 'assessment' });
  }
  function segmentsTable(p) {
    const per = M(p).periods || {}, segs = per.segments || {};
    const rows = ['in_sample', 'validation', 'out_of_sample'].map((k) => ({ name: k.replace(/_/g, ' '), ...(segs[k] || {}) }))
      .concat((per.folds || []).map((f) => ({ name: f.segment.replace('_', ' '), ...f })));
    return `${table('seg-' + p.metadata.run_id, [
      { key: 'name', label: 'Period' }, { key: 'start', label: 'From' }, { key: 'end', label: 'To' }, { key: 'days', label: 'Days', align: 'right' },
      { key: 'trades', label: 'Trades', align: 'right' }, { key: 'net_pnl', label: 'Net P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
      { key: 'return_pct', label: 'Return', align: 'right', fmt: (v) => pct(v, { color: true, signed: true }), tip: 'vs equity at the start of the period' },
      { key: 'win_rate_pct', label: 'Win rate', align: 'right', fmt: (v) => pct(v) }, { key: 'profit_factor', label: 'PF', align: 'right', fmt: (v) => num(v) },
      { key: 'max_drawdown_pct', label: 'Max DD', align: 'right', fmt: (v) => pct(v) }], rows, { filter: false })}
      <p class="mt-2 text-xs text-ink-300">${esc(per.method || '')}</p>`;
  }

  /* ------------------------------------------------------------ pages */
  const SUBNAV = [
    ['backtest.html', 'Overview'], ['bt-strategy.html', 'Strategy reports'], ['bt-portfolio.html', 'Portfolio summary'],
    ['bt-trades.html', 'Trade history'], ['bt-performance.html', 'Performance analysis'], ['bt-risk.html', 'Risk analysis'],
    ['bt-experiments.html', 'Rule experiments'], ['bt-data.html', 'Data quality & validation'], ['bt-history.html', 'Run comparison'],
    ['bt-analysis.html', 'Limitations & analysis'],
  ];
  PBT.SUBNAV = SUBNAV;
  function subnav(active) {
    return `<nav aria-label="Backtesting sections" class="flex flex-wrap gap-1 rounded-2xl border border-ink-700/80 bg-ink-900/80 p-2">
      <span class="px-2 py-1.5 text-xs font-bold uppercase tracking-[.12em] text-emerald-300">Backtesting</span>
      ${SUBNAV.map(([h, l]) => `<a href="${h}" class="whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs no-underline ${h === active ? 'bg-ink-700 text-white' : 'text-ink-300 hover:bg-ink-800 hover:text-white'}">${esc(l)}</a>`).join('')}</nav>`;
  }
  const noData = () => panel('No backtests yet', `${empty('dashboard/data/backtest.js is missing: no backtest has been run or published yet.')}
    <p class="mt-3 text-sm text-ink-300">Run one with <code class="mono">paperbot-backtest run</code> (IB Gateway, paper) or <code class="mono">paperbot-backtest run --provider yfinance</code>, then reload this page.</p>`);
  const noRun = (d, page) => (d && page ? strategyTabs(d, page, param('strategy')) + '<div class="mt-3"></div>' : '') +
    empty('No completed run with full details is available for this strategy. Run: paperbot-backtest run --strategy ' + (param('strategy') || '<key>'));

  const PAGES = {};
  PAGES['backtest.html'] = (d) => {
    const ok = d.runs.filter((r) => String(r.status).startsWith('completed'));
    const last = ok[0], attempted = d.runs[0];
    const failed = d.strategies.filter((s) => !s.latest_run || String(s.last_attempt_status) === 'failed');
    const covered = ok.length ? `${day(ok.map((r) => (r.period || {}).actual_start).sort()[0])} → ${day(ok.map((r) => (r.period || {}).actual_end).sort().slice(-1)[0])}` : na();
    const kpis = grid([
      kpi('Last successful backtest', last ? utc(last.finished_at) : na('none yet'), last ? `<a href="bt-strategy.html?run=${esc(last.run_id)}">${esc(last.run_id)}</a>` : ''),
      kpi('Last attempted backtest', attempted ? utc(attempted.started_at) : na('none yet'), attempted ? statusPill(attempted.status) : ''),
      kpi('Strategies evaluated', int(d.strategies.length), `${d.strategies.filter((s) => s.latest_run).length} with a completed test · ${failed.length} failed or not run`, 'Strategies registered in paperbot.backtesting.strategies'),
      kpi('Historical period covered', covered, 'across completed runs'),
      kpi('Dataset freshness', last ? `data to ${day((last.period || {}).actual_end)}` : na(), last ? `provider ${esc(last.provider)}${last.synthetic_data ? ' · SYNTHETIC' : ''}` : ''),
      kpi('Validation status', last ? statusPill(last.data_status) : na(), 'data quality of the latest run', 'pass = no data problems found; warn = see Data quality'),
      kpi('Code / config version', last ? `<span class="mono text-sm">${esc(((last.code || {}).commit || '?').slice(0, 10))}</span>` : na(), last ? `rules <span class="mono">${esc(last.strategy_version)}</span>` : ''),
      kpi('Runs recorded', int(d.runs.length), `${d.runs.filter((r) => r.kind === 'experiment').length} experiment runs`),
    ]);
    const rows = d.strategies.map((s) => {
      const r = d.runs.find((x) => x.run_id === s.latest_run) || d.runs.find((x) => x.run_id === s.last_attempt) || {};
      const h = r.headline || {};
      return { name: s.name, sid: s.strategy_id, tf: `${s.timeframe || ''} · ${(s.holding || '').replace('_', '-')}`, status: s.latest_run ? r.status : s.last_attempt_status, period: r.period ? `${day(r.period.actual_start)} → ${day(r.period.actual_end)}` : '',
        trades: h.completed_trades, net: h.net_pnl, ret: h.total_return_pct, win: h.win_rate_pct, dd: h.max_drawdown_pct, pf: h.profit_factor,
        costs: isNum(h.total_commission) ? h.total_commission + (h.total_slippage || 0) : null, dq: r.data_status, oos: r.out_of_sample, run: r.run_id, report: r.report_path, synthetic: r.synthetic_data };
    });
    const table1 = table('strategies', [
      { key: 'name', label: 'Strategy', fmt: (v, r) => `<b class="text-white">${esc(v)}</b>${r.synthetic ? ' ' + pill('synthetic', 'red') : ''}` },
      { key: 'tf', label: 'Timeframe' }, { key: 'status', label: 'Status', fmt: statusPill }, { key: 'period', label: 'Historical period' },
      { key: 'trades', label: 'Trades', align: 'right', fmt: int, tip: 'Completed simulated trades' },
      { key: 'net', label: 'Net P&L (USD)', align: 'right', fmt: (v) => money(v, { color: true, signed: true }), tip: 'After configured costs' },
      { key: 'ret', label: 'Return', align: 'right', fmt: (v) => pct(v, { color: true, signed: true }), tip: 'Net P&L / starting capital' },
      { key: 'win', label: 'Win rate', align: 'right', fmt: (v) => pct(v), tip: 'Profitable completed trades / completed trades' },
      { key: 'dd', label: 'Max drawdown', align: 'right', fmt: (v) => pct(v), tip: 'Largest fall from a previous equity peak' },
      { key: 'pf', label: 'Profit factor', align: 'right', fmt: (v) => num(v, 2, 'N/A — no losing trades or no trades'), tip: 'Gross profits / |gross losses|' },
      { key: 'costs', label: 'Costs (USD)', align: 'right', fmt: (v) => money(v), tip: 'Commissions + simulated spread/slippage' },
      { key: 'dq', label: 'Data quality', fmt: (v) => (v ? statusPill(v) : na()) },
      { key: 'oos', label: 'Out-of-sample', fmt: (v) => (v ? pill('evaluated', 'green') : pill('not evaluated', 'slate')), tip: 'Whether a separate, later period was held out and reported' },
      { key: 'run', label: 'Report', fmt: (v, r) => (v ? `<a href="bt-strategy.html?run=${esc(v)}">Open</a> · <a href="${esc(r.report)}">HTML</a>` : '—') }], rows, { filter: false });
    const links = SUBNAV.slice(1).map(([h, l]) => `<a href="${h}" class="block rounded-xl border border-ink-700/70 bg-ink-850 px-4 py-3 text-sm text-slate-100 no-underline hover:border-emerald-400/50">${esc(l)} →</a>`).join('');
    return [notice(`<b>${FUTURE}</b> Figures are simulations of the paperbot rules on historical data, after simulated costs. Currency: USD. Times: US Eastern (ET) unless marked UTC.`),
      panel('Backtesting overview', kpis), panel('Strategies', table1), panel('Sections', `<div class="grid grid-cols-1 gap-2 sm:grid-cols-3">${links}</div>`),
      panel('Commands', `<div class="grid grid-cols-1 gap-1 text-xs md:grid-cols-2">${Object.entries(d.commands || {}).map(([k, v]) => `<div><span class="text-ink-400">${esc(k)}:</span> <code class="mono text-emerald-200">${esc(v)}</code></div>`).join('')}</div>`)];
  };
  PAGES['bt-strategy.html'] = (d) => {
    const p = pickRun(d);
    if (!p) return [panel('', noRun(d, 'bt-strategy.html'))];
    return [panel(`${esc(p.metadata.strategy_name)} · strategy report`, `<div class="flex flex-wrap items-center justify-between gap-3">${runSelect(d, p, 'bt-strategy.html')}
      <span class="text-xs"><a href="${esc(p.report_path)}">Standalone HTML report</a> · folder <span class="mono">backtesting/results/${esc(p.metadata.strategy_id)}/${esc(p.metadata.run_id)}/</span></span></div>`),
      ...strategyReport(p)];
  };
  function strategyReport(p) {
    return [sectionA(p), sectionB(p), sectionC(p), sectionD(p), sectionE(p), sectionF(p), sectionG(p)];
  }
  PAGES['bt-trades.html'] = (d) => {
    const p = pickRun(d);
    if (!p) return [panel('', noRun(d, 'bt-trades.html'))];
    return [panel('Trade history', runSelect(d, p, 'bt-trades.html')), synthBanner(p.metadata), sectionE(p, { title: `Trades of run ${esc(p.metadata.run_id)}` }),
      panel('Results by exit reason', table('byexit', breakdownCols(), M(p).by_exit_reason || [], { filter: false })),
      panel('Results by symbol', table('bysym', breakdownCols(), M(p).by_symbol || []))];
  };
  const breakdownCols = () => [{ key: 'key', label: 'Group' }, { key: 'trades', label: 'Trades', align: 'right' },
    { key: 'net_pnl', label: 'Net P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) }, { key: 'win_rate_pct', label: 'Win rate', align: 'right', fmt: (v) => pct(v) }];
  PAGES['bt-performance.html'] = (d) => {
    const p = pickRun(d);
    if (!p) return [panel('', noRun(d, 'bt-performance.html'))];
    return [panel('Performance analysis', runSelect(d, p, 'bt-performance.html')), synthBanner(p.metadata), sectionC(p), sectionD(p, { title: 'Charts' }),
      panel('In-sample, validation and out-of-sample periods', segmentsTable(p)), sectionF(p)];
  };
  PAGES['bt-risk.html'] = (d) => {
    const p = pickRun(d);
    if (!p) return [panel('', noRun(d, 'bt-risk.html'))];
    const m = M(p), rules = (p.config || {}).rules || {}, rk = rules.risk || {};
    const exposure = (p.equity || []).map((r) => [ms(r.time_et), r.exposure_pct]);
    const concurrent = (p.equity || []).map((r) => [ms(r.time_et), r.open_positions]);
    const worst = [...(p.daily || [])].sort((a, b) => a.pnl - b.pnl).slice(0, 10);
    const st = m.stress || {};
    const halts = (p.daily || []).filter((r) => r.halted === true || r.halted === 'True');
    const rs = (p.validation || {}).risk_state || {};
    const bySym = (m.by_symbol || []);
    const totalAbs = bySym.reduce((s, r) => s + Math.abs(r.net_pnl), 0);
    return [panel('Risk analysis', runSelect(d, p, 'bt-risk.html')), synthBanner(p.metadata),
      panel('Risk summary', grid([
        kpi('Maximum drawdown', pct(m.max_drawdown_pct, { reason: naReason(p, 'max_drawdown_pct') }), `${money(m.max_drawdown_usd)} · ${esc(day(m.max_drawdown_peak_time))} → ${esc(day(m.max_drawdown_trough_time))}`, defn(p, 'max_drawdown_pct')),
        kpi('Longest losing streak', int(m.max_consecutive_losses), 'completed trades in a row', defn(p, 'max_consecutive_losses')),
        kpi('Largest losing trade', usd(m.largest_loss, { color: true, reason: naReason(p, 'largest_loss') }), '', defn(p, 'largest_loss')),
        kpi('Daily volatility', pct(m.daily_volatility_pct, { reason: naReason(p, 'daily_volatility_pct') }), `Sharpe-like ${isNum(m.sharpe_like) ? m.sharpe_like : 'N/A'}`, defn(p, 'sharpe_like')),
        kpi('Average / max exposure', `${pct(m.avg_exposure_pct)} / ${pct(m.max_exposure_pct)}`, 'of equity in open positions', defn(p, 'avg_exposure_pct')),
        kpi('Most positions at once', int(m.max_concurrent_positions), `limit ${esc(rk.max_concurrent_positions ?? '—')}`),
        kpi('Daily-loss halts', int(halts.length), `limit $${esc(rk.max_daily_loss ?? '—')} per day`),
        kpi('Drawdown breaker', rs.drawdown_breaker_tripped ? pill('tripped', 'red') : pill('not tripped', 'green'), esc(rs.drawdown_breaker_reason || `limit $${rk.max_drawdown_usd ?? '—'}`)),
      ])),
      panel('Drawdown and exposure', `<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
        ${chart('Drawdown', lineChart([{ name: 'Drawdown', points: drawdownSeries(p), area: true, color: '#fb7185' }], { unit: '%', yLabel: 'Below previous peak (%)', xLabel: 'Date (ET)', zero: true }), 'Distance below the highest earlier equity, every 5 minutes.')}
        ${chart('Capital in use (exposure)', lineChart([{ name: 'Exposure', points: exposure, area: true, color: '#7dd3fc' }], { unit: '%', yLabel: 'Positions / equity (%)', xLabel: 'Date (ET)', zero: true }), 'Value of open positions as a percentage of equity. 0% = all cash.')}
        ${chart('Concurrent positions', lineChart([{ name: 'Open positions', points: concurrent, color: '#c4b5fd' }], { unit: '', yLabel: 'Positions', xLabel: 'Date (ET)', zero: true }), 'Number of positions held at each 5-minute mark.')}
        ${chart('Worst days', barChart(worst.map((r) => ({ label: r.date.slice(5), value: r.pnl })), { unit: '$', yLabel: 'P&L (USD)', xLabel: 'Date' }), 'The ten worst days by change in equity.')}
      </div>`),
      panel('Execution stress test', st.net_pnl !== undefined ? `<p class="mb-3 text-sm text-ink-300">The same rules and data with ${esc(st.multiplier)}× the spread and slippage assumptions (half-spread ${esc(st.half_spread_bps)} bps, slippage ${esc(st.slippage_bps)} bps). Commissions unchanged.</p>
        ${table('stress', [{ key: 'k', label: 'Metric' }, { key: 'base', label: 'Default assumptions', align: 'right', fmt: (v) => v }, { key: 'hard', label: `${st.multiplier}× costs`, align: 'right', fmt: (v) => v }], [
          { k: 'Net P&L', base: money(m.net_pnl, { color: true, signed: true }), hard: money(st.net_pnl, { color: true, signed: true }) },
          { k: 'Return', base: pct(m.total_return_pct, { signed: true }), hard: pct(st.total_return_pct, { signed: true }) },
          { k: 'Completed trades', base: int(m.completed_trades), hard: int(st.completed_trades) },
          { k: 'Max drawdown', base: pct(m.max_drawdown_pct), hard: pct(st.max_drawdown_pct) },
          { k: 'Spread + slippage', base: money(m.total_slippage), hard: money(st.total_slippage) }], { filter: false })}` : empty('No stress run for this run (experiment runs skip it).')),
      panel('Concentration by symbol', `${bySym.length ? barChart(bySym.slice(0, 20).map((r) => ({ label: r.key, value: r.net_pnl })), { unit: '$', yLabel: 'Net P&L (USD)', xLabel: 'Symbol' }) : empty('No trades.')}
        <p class="mt-2 text-xs text-ink-300">Net result per symbol (top 20 by size). ${bySym.length && totalAbs ? `The largest symbol accounts for ${(100 * Math.abs(bySym[0].net_pnl) / totalAbs).toFixed(1)}% of the absolute P&L across symbols.` : ''}</p>`),
      panel('Configured risk limits (from the rules used)', table('limits', [{ key: 'k', label: 'Limit', fmt: (v) => `<span class="mono">${esc(v)}</span>` }, { key: 'v', label: 'Value' }],
        Object.entries(rk).map(([k, v]) => ({ k, v: v === null ? 'not enforced' : v })), { filter: false }))];
  };
  PAGES['bt-portfolio.html'] = (d) => portfolioView(d);
  function portfolioView(d) {
    const pf = d.portfolio || {};
    if (pf.status === 'none' || !pf.status) return [panel('Portfolio summary', empty(pf.note || 'No completed baseline run yet.'))];
    const rowsTable = table('pfrows', [
      { key: 'name', label: 'Strategy', fmt: (v, r) => `<a href="bt-strategy.html?run=${esc(r.run_id)}"><b>${esc(v)}</b></a>${r.synthetic ? ' ' + pill('synthetic', 'red') : ''}` },
      { key: 'period', label: 'Period' }, { key: 'capital', label: 'Capital (USD)', align: 'right', fmt: (v) => money(v, { d: 0 }) },
      { key: 'net_pnl', label: 'Net P&L (USD)', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
      { key: 'return_pct', label: 'Return', align: 'right', fmt: (v) => pct(v, { color: true, signed: true }), tip: 'vs that strategy\'s own starting capital' },
      { key: 'share_pct', label: 'Share of |P&L|', align: 'right', fmt: (v) => pct(v), tip: 'Absolute net P&L of this strategy as a share of the absolute net P&L of all strategies' },
      { key: 'trades', label: 'Trades', align: 'right' }, { key: 'max_drawdown_pct', label: 'Max DD', align: 'right', fmt: (v) => pct(v) },
      { key: 'avg_exposure_pct', label: 'Avg exposure', align: 'right', fmt: (v) => pct(v), tip: 'Average capital in use' },
      { key: 'costs', label: 'Costs (USD)', align: 'right', fmt: (v) => money(v) }], pf.strategies || [], { filter: false });
    const conc = table('pfconc', breakdownCols(), pf.concentration || [], { empty: 'No trades.' });
    if (pf.status === 'indicative') {
      const combined = (pf.combined_cumulative_pnl || []).map(([t, v]) => [ms(t + 'T16:00:00-05:00'), v]);
      const sim = table('simloss', [{ key: 'date', label: 'Date' }, { key: 'strategies_losing', label: 'Strategies with a losing day' },
        { key: 'net_pnl', label: 'Combined P&L that day', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) }],
        pf.simultaneous_losses || [], { filter: false, empty: 'No day on which two or more strategies lost money.' });
      return [notice(`<b>${FUTURE}</b> Portfolio basis: <b>${esc(pf.basis)}</b>. Currency: USD.`),
        panel('How these figures are built', list(pf.assumptions)),
        panel('Combined (indicative)', grid([
          kpi('Strategies', int((pf.strategies || []).length), 'latest completed baseline of each'),
          kpi('Sum of capitals', usd(pf.combined_capital), 'each strategy simulated on its own capital'),
          kpi('Summed net P&L', usd(pf.combined_net_pnl, { color: true, signed: true }), 'INDICATIVE: not a simulated portfolio'),
          kpi('Summed return', pct(pf.combined_capital ? 100 * pf.combined_net_pnl / pf.combined_capital : null, { color: true, signed: true }), 'vs the sum of capitals'),
          kpi('Days with ≥2 strategies losing', int((pf.simultaneous_losses || []).length)),
          kpi('Overlapping exposure', 'not simulated', 'strategies did not share cash or limits')])),
        panel('Strategy contribution', rowsTable),
        panel('Combined cumulative P&L (indicative)', chart('Summed daily P&L of all strategies', lineChart([{ name: 'Combined', points: combined, area: true, color: '#7dd3fc' }],
          { unit: '$', yLabel: 'Cumulative P&L (USD)', xLabel: 'Date (ET)', zero: true }),
          'The running total of every strategy\'s daily P&L added together. Indicative only: strategies were simulated separately, so shared buying power and account-wide limits are not reflected.')),
        panel('Concentration by symbol (all strategies)', conc), panel('Periods of simultaneous losses', sim)];
    }
    const p = d.details[pf.run_id];
    if (!p) return [panel('Portfolio summary', empty('The run behind the portfolio view is not in the embedded data.'))];
    const m = M(p);
    const sim = table('simloss', [{ key: 'date', label: 'Date' }, { key: 'losing_trades', label: 'Losing trades that day', align: 'right' },
      { key: 'net_pnl', label: 'Net of all trades', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) }], pf.simultaneous_losses || [], { filter: false, empty: 'No day had two or more losing trades.' });
    const daily = (p.daily || []).map((r) => ({ label: r.date.slice(5), value: r.start_equity ? 100 * r.pnl / r.start_equity : null }));
    return [notice(`<b>${FUTURE}</b> Portfolio basis: <b>${esc(pf.basis)}</b>. Currency: USD.`), synthBanner(p.metadata),
      panel('How the portfolio is built', list(pf.assumptions)),
      panel('Portfolio results', grid([
        kpi('Initial capital', usd(m.starting_capital)), kpi('Ending equity', usd(m.ending_equity)),
        kpi('Total net P&L', usd(m.net_pnl, { color: true, signed: true })), kpi('Total return', pct(m.total_return_pct, { color: true, signed: true }), 'of initial capital'),
        kpi('Completed trades', int(m.completed_trades)), kpi('Transaction costs', usd((m.total_commission || 0) + (m.total_slippage || 0)), `${money(m.total_commission)} fees + ${money(m.total_slippage)} spread/slippage`),
        kpi('Maximum drawdown', pct(m.max_drawdown_pct), money(m.max_drawdown_usd)), kpi('Benchmark', pct(m.benchmark_return_pct, { signed: true, reason: naReason(p, 'benchmark_return_pct') }), `${esc(m.benchmark_symbol || '')} buy-and-hold`),
        kpi('Capital utilisation', `${pct(m.avg_exposure_pct)} avg`, `${pct(m.max_exposure_pct)} max of equity`, defn(p, 'avg_exposure_pct')),
        kpi('Concurrent positions', `${int(m.max_concurrent_positions)} max`, `time in market ${pct(m.time_in_market_pct)}`),
        kpi('Overlapping strategy exposure', 'N/A', 'one strategy only'), kpi('Days with ≥2 losing trades', int((pf.simultaneous_losses || []).length)),
      ])),
      sectionD(p, { title: 'Portfolio charts' }),
      panel('Daily returns', barChart(daily, { unit: '%', yLabel: 'Return (% of day-start equity)', xLabel: 'Date' }) + '<p class="mt-2 text-xs text-ink-300">Change in equity per day as a percentage of that day\'s starting equity.</p>'),
      panel('Strategy contribution', rowsTable), panel('Concentration by symbol', conc),
      panel('Periods of simultaneous losses', sim)];
  }
  PAGES['bt-data.html'] = (d) => {
    const p = pickRun(d);
    if (!p) return [panel('', noRun(d, 'bt-data.html'))];
    const v = p.validation || {}, q = v.data_quality || {};
    const usage = v.dataset_usage || {};
    return [panel('Data quality and validation', runSelect(d, p, 'bt-data.html')), synthBanner(p.metadata),
      panel('Data used', grid([
        kpi('Status', statusPill(q.status), 'pass / warn / fail'), kpi('Provider', esc(q.provider), q.synthetic ? pill('synthetic', 'red') : 'market data'),
        kpi('Requested period', `${esc(q.requested_start)} → ${esc(q.requested_end)}`), kpi('Actual period', `${esc(q.actual_start)} → ${esc(q.actual_end)}`, `${int(q.trading_days)} trading days`),
        kpi('Symbols', `${int((q.symbols_with_data || []).length)} of ${int((q.symbols_requested || []).length)}`, 'with daily data'),
        kpi('5-minute symbol-days', int(q.intraday_days_loaded), `${int(q.intraday_days_missing_total)} requested but missing`),
        kpi('Invalid / duplicate bars', `${int(q.invalid_bars)} / ${int(q.duplicate_bars)}`, `${int(q.unordered_series)} unordered series · ${int(q.off_grid_bars)} off-grid`),
        kpi('Incomplete sessions', int(q.incomplete_sessions_total), 'fewer than 78 regular-hours bars'),
        kpi('Dataset id', `<span class="mono text-xs">${esc(v.dataset_id)}</span>`, `${int((usage.variants || []).length)} variant(s), ${int((usage.runs || []).length)} run(s) on it`),
      ])),
      panel('Validation checks', table('checks', [{ key: 'label', label: 'Check' }, { key: 'status', label: 'Result', fmt: statusPill }, { key: 'detail', label: 'Detail', fmt: (x) => `<span class="whitespace-normal">${esc(x)}</span>` }], v.checks || [], { filter: false })),
      panel('Warnings', list(q.warnings)), panel('Data limitations', list(q.limitations)), panel('Errors', list(q.errors)),
      panel('Symbols without data', list(q.symbols_without_data)),
      panel('Missing 5-minute data (symbol, day)', table('missing', [{ key: 0, label: 'Symbol' }, { key: 1, label: 'Day' }], (q.intraday_days_missing || []).map((x) => ({ 0: x[0], 1: x[1] })), { empty: 'None missing.' })),
      panel('Incomplete sessions (symbol, day, regular-hours bars)', table('incomplete', [{ key: 0, label: 'Symbol' }, { key: 1, label: 'Day' }, { key: 2, label: 'Bars', align: 'right' }], (q.incomplete_sessions || []).map((x) => ({ 0: x[0], 1: x[1], 2: x[2] })), { empty: 'All sessions complete.' })),
      panel('Validation method', `<p class="text-sm text-slate-200">${esc(v.periods_method || '')}</p><div class="mt-3">${segmentsTable(p)}</div>`)];
  };
  PAGES['bt-history.html'] = (d) => {
    const rows = d.runs.map((r) => ({ ...r, net: (r.headline || {}).net_pnl, trades: (r.headline || {}).completed_trades, start: r.started_at, s: (r.period || {}).actual_start, e: (r.period || {}).actual_end }));
    const runsTable = table('runs', [
      { key: 'run_id', label: 'Run', fmt: (v, r) => `<span class="mono">${esc(v)}</span>${r.synthetic_data ? ' ' + pill('synthetic', 'red') : ''}` },
      { key: 'strategy_id', label: 'Strategy' }, { key: 'kind', label: 'Kind' }, { key: 'experiment_id', label: 'Experiment' },
      { key: 'status', label: 'Status', fmt: statusPill }, { key: 'start', label: 'Started', fmt: utc }, { key: 's', label: 'From' }, { key: 'e', label: 'To' },
      { key: 'trades', label: 'Trades', align: 'right' }, { key: 'net', label: 'Net P&L', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
      { key: 'warnings_count', label: 'Warnings', align: 'right' }, { key: 'dataset_id', label: 'Dataset', fmt: (v) => `<span class="mono text-[10px]">${esc(v)}</span>` },
      { key: 'report_path', label: 'Open', fmt: (v, r) => `${d.details[r.run_id] ? `<a href="bt-strategy.html?run=${esc(r.run_id)}">dashboard</a> · ` : ''}<a href="${esc(v)}">HTML</a>` }], rows, { sort: 'start', dir: -1 });
    const opts = (sel) => d.runs.map((r) => `<option value="${esc(r.run_id)}" ${r.run_id === sel ? 'selected' : ''}>${esc(r.run_id)} · ${esc(r.kind)} ${esc(r.experiment_id || '')}</option>`).join('');
    const a = param('a') || (d.runs[1] || d.runs[0] || {}).run_id, b = param('b') || (d.runs[0] || {}).run_id;
    return [panel('Report history', `<p class="mb-2 text-xs text-ink-300">Every recorded run, newest first. Use the filter box for strategy, date (YYYY-MM-DD) or status. Failed runs are kept with their error.</p>${runsTable}`),
      panel('Compare two runs', `<form class="mb-4 flex flex-wrap items-center gap-2 text-xs" onsubmit="event.preventDefault();location.href='bt-history.html?a='+encodeURIComponent(this.a.value)+'&b='+encodeURIComponent(this.b.value)+'#compare'">
        <label>Run A <select name="a" class="ml-1 rounded-lg border border-ink-700 bg-ink-850 px-2 py-1 text-slate-100">${opts(a)}</select></label>
        <label>Run B <select name="b" class="ml-1 rounded-lg border border-ink-700 bg-ink-850 px-2 py-1 text-slate-100">${opts(b)}</select></label>
        <button class="rounded-lg bg-ink-700 px-3 py-1 text-white">Compare</button></form><div id="compare">${compareRuns(d, a, b)}</div>`)];
  };
  function flat(obj, prefix = '', out = {}) {
    Object.entries(obj || {}).forEach(([k, v]) => { if (v && typeof v === 'object' && !Array.isArray(v)) flat(v, `${prefix}${k}.`, out); else out[`${prefix}${k}`] = v; });
    return out;
  }
  function compareRuns(d, ida, idb) {
    const A = d.runs.find((r) => r.run_id === ida), B = d.runs.find((r) => r.run_id === idb);
    if (!A || !B) return empty('Pick two runs.');
    const keys = [['net_pnl', 'Net P&L', 'usd'], ['total_return_pct', 'Return', 'pct'], ['completed_trades', 'Completed trades', 'int'], ['win_rate_pct', 'Win rate', 'pct'],
      ['max_drawdown_pct', 'Max drawdown', 'pct'], ['profit_factor', 'Profit factor', 'num'], ['total_commission', 'Commissions', 'usd'], ['total_slippage', 'Spread + slippage', 'usd'], ['ending_equity', 'Ending equity', 'usd']];
    const f = (v, k) => (k === 'usd' ? money(v, { signed: true }) : k === 'pct' ? pct(v) : k === 'int' ? int(v) : num(v));
    const rows = keys.map(([key, label, kind]) => {
      const x = (A.headline || {})[key], y = (B.headline || {})[key];
      return `<tr class="border-t border-ink-700/50"><td class="px-2 py-1.5">${label}</td><td class="px-2 text-right">${f(x, kind)}</td><td class="px-2 text-right">${f(y, kind)}</td>
        <td class="px-2 text-right">${isNum(x) && isNum(y) ? `<span class="${signClass(y - x)}">${kind === 'usd' ? money(y - x, { signed: true, plain: true }) : (y - x > 0 ? '+' : '') + (+(y - x).toFixed(3))}</span>` : na('not available for both')}</td></tr>`;
    }).join('');
    const ra = flat({ rules: A.rules, execution: A.execution }), rb = flat({ rules: B.rules, execution: B.execution });
    const diffs = [...new Set([...Object.keys(ra), ...Object.keys(rb)])].filter((k) => JSON.stringify(ra[k]) !== JSON.stringify(rb[k]))
      .map((k) => `<tr class="border-t border-ink-700/50"><td class="mono px-2 py-1">${esc(k)}</td><td class="mono px-2">${esc(JSON.stringify(ra[k]))}</td><td class="mono px-2">${esc(JSON.stringify(rb[k]))}</td></tr>`).join('');
    const same = A.dataset_id && A.dataset_id === B.dataset_id;
    return `<div class="space-y-4">
      ${notice(same ? 'Both runs used the <b>same dataset</b>, so differences come from rules, assumptions or code.' : 'The runs used <b>different datasets</b> (period, symbols or data corrections differ): differences may come from the data itself.', same ? 'sky' : 'amber')}
      <div class="overflow-x-auto"><table class="w-full text-xs"><thead class="text-ink-300"><tr><th class="px-2 text-left">Metric</th><th class="px-2 text-right">A ${esc(A.run_id)}</th><th class="px-2 text-right">B ${esc(B.run_id)}</th><th class="px-2 text-right">B − A</th></tr></thead><tbody>${rows}</tbody></table></div>
      <h3 class="text-sm font-semibold text-slate-100">Configuration differences</h3>
      ${diffs ? `<div class="overflow-x-auto"><table class="w-full text-xs"><thead class="text-ink-300"><tr><th class="px-2 text-left">Setting</th><th class="px-2 text-left">A</th><th class="px-2 text-left">B</th></tr></thead><tbody>${diffs}</tbody></table></div>` : '<p class="text-sm text-ink-300">Identical rules and execution settings.</p>'}
      <p class="text-xs text-ink-300">Code: A ${esc(((A.code || {}).commit || '?').slice(0, 10))}, B ${esc(((B.code || {}).commit || '?').slice(0, 10))}. Data status: A ${esc(A.data_status)}, B ${esc(B.data_status)}.</p>
      ${chart('Daily closing equity', lineChart([{ name: `A ${A.run_id}`, points: (A.equity_daily || []).map(([t, v]) => [ms(t + 'T16:00:00-05:00'), v]) },
        { name: `B ${B.run_id}`, points: (B.equity_daily || []).map(([t, v]) => [ms(t + 'T16:00:00-05:00'), v]), color: '#fbbf24' }], { unit: '$', yLabel: 'Equity (USD)', xLabel: 'Date (ET)' }), 'Equity at each day\'s close for both runs.')}</div>`;
  }
  PAGES['bt-experiments.html'] = (d) => {
    const sid = param('strategy') || ((d.strategies.find((s) => s.latest_run) || d.strategies[0] || {}).strategy_id);
    const strat = d.strategies.find((s) => s.strategy_id === sid) || {};
    const base = strat.latest_run && d.details[strat.latest_run];
    const cmp = (d.comparisons || []).find((c) => c.strategy_id === sid);
    const exps = (d.experiments || []).filter((e) => e.invalid || e.strategy_id === sid);
    const ran = new Set(d.runs.filter((r) => r.kind === 'experiment').map((r) => r.experiment_id));
    const cards = exps.map((e) => e.invalid ? `<div class="rounded-xl border border-rose-400/40 p-4 text-sm">${esc(e.id)}: invalid (${esc(e.invalid)})</div>` : `
      <div class="space-y-2 rounded-xl border border-ink-700/70 bg-ink-850 p-4 text-sm">
        <div class="flex flex-wrap items-center gap-2"><b class="mono text-white">${esc(e.id)}</b>${statusPill(e.status)}${ran.has(e.id) ? pill('has run', 'green') : pill('not run yet', 'slate')}</div>
        <div><span class="text-ink-400">Changes:</span> ${Object.entries(e.changes).map(([k, v]) => `<span class="mono">${esc(k)}</span>: <span class="mono text-ink-300">${esc(JSON.stringify((e.baseline_values || {})[k]))}</span> → <span class="mono text-emerald-200">${esc(JSON.stringify(v))}</span>`).join('<br>')}</div>
        <div><span class="text-ink-400">Hypothesis:</span> ${esc(e.hypothesis)}</div>
        <div><span class="text-ink-400">Why test it:</span> ${esc(e.reason)}</div>
        ${e.evidence ? `<div class="text-xs text-ink-300">${esc(e.evidence)}</div>` : ''}
        <div class="text-xs text-ink-400">${esc(e.data_periods || '')}</div>
        <div class="text-xs text-amber-200">${esc(e.approval || 'Not applied to production.')}</div>
        <code class="mono text-[11px] text-emerald-200">paperbot-backtest experiment ${esc(e.id)}</code></div>`).join('');
    let comparison = empty(`No comparison yet for this strategy. Run: paperbot-backtest all --strategy ${sid} --with-experiments`);
    if (cmp) {
      const fmtv = (metric, v) => (/pnl|commission|slippage|expectancy/.test(metric) ? money(v, { color: true, signed: true }) : /pct/.test(metric) ? pct(v) : isNum(v) ? num(v, metric === 'trades' || metric === 'completed_trades' ? 0 : 2) : na());
      const rows = cmp.rows.map((r) => `<tr class="border-t border-ink-700/50 ${r.segment === 'out_of_sample' ? 'bg-ink-850' : ''}"><td class="px-2 py-1.5 text-ink-300">${esc(r.segment.replace(/_/g, ' '))}</td><td class="px-2">${esc(r.metric.replace(/_/g, ' '))}</td>
        ${r.values.map((v) => `<td class="px-2 text-right">${fmtv(r.metric, v)}</td>`).join('')}</tr>`).join('');
      const find = table('findings', [{ key: 'experiment_id', label: 'Experiment', fmt: (v) => `<b class="mono">${esc(v)}</b>` },
        { key: 'changes', label: 'What changed', fmt: (v) => `<span class="mono whitespace-normal">${esc(JSON.stringify(v))}</span>` },
        { key: 'validation_net_pnl_delta', label: 'Δ net P&L (validation)', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
        { key: 'mean_trade_diff_ci95', label: 'Δ avg trade, 95% range', fmt: (v) => (Array.isArray(v) ? `${money(v[0])} to ${money(v[1])}` : na('fewer than 5 trades in a segment')) },
        { key: 'verdict', label: 'Assessment', fmt: (v) => `<span class="whitespace-normal">${esc(v)}</span>` },
        { key: 'out_of_sample_net_pnl_delta', label: 'Δ out-of-sample (check only)', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
        { key: 'holds_out_of_sample', label: 'Same direction on untouched data?', fmt: (v) => (v === true ? pill('yes', 'green') : v === false ? pill('no', 'amber') : na()) }], cmp.findings, { filter: false });
      const wf = cmp.walk_forward ? table('wf', [{ key: 'fold', label: 'Fold' }, { key: 'chosen', label: 'Chosen (best in previous fold)' },
        { key: 'chosen_net_pnl', label: 'Chosen result', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) },
        { key: 'baseline_net_pnl', label: 'Baseline result', align: 'right', fmt: (v) => money(v, { color: true, signed: true }) }], cmp.walk_forward.steps, { filter: false }) +
        `<p class="mt-2 text-xs text-ink-300">Picking the variant that did best in the previous period would have made ${money(cmp.walk_forward.chosen_total, { signed: true })} vs ${money(cmp.walk_forward.baseline_total, { signed: true })} for the baseline over the same folds.</p>` : empty('Not enough folds for a walk-forward check.');
      comparison = `<div class="space-y-4">
        ${notice(`${cmp.same_dataset ? 'All columns used the same dataset' : '<b>Columns used different datasets</b>; differences may be data-driven'} (<span class="mono">${esc(cmp.dataset_id)}</span>). ${esc(cmp.variants_evaluated_on_dataset)} variant(s) have been evaluated on this dataset: the more variants are tried, the more the best one may look good by chance (selection bias).`, cmp.same_dataset ? 'sky' : 'amber')}
        <div class="overflow-x-auto"><table class="w-full text-xs"><thead class="text-ink-300"><tr><th class="px-2 text-left">Period</th><th class="px-2 text-left">Metric</th>${cmp.labels.map((l) => `<th class="px-2 text-right">${esc(l)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>
        <p class="text-xs text-ink-300">${esc(cmp.selection_rule)} Only experiments that have actually run are shown; N/A means the value could not be computed.</p>
        <h3 class="text-sm font-semibold text-slate-100">Findings</h3>${find}
        <h3 class="text-sm font-semibold text-slate-100">Walk-forward check</h3>${wf}
        ${notice(`<b>${esc(cmp.approval)}</b> A higher historical return alone is not evidence of an improvement.`, 'amber')}</div>`;
    }
    return [panel('Rule analysis &amp; experiments', `<div class="space-y-3 text-sm text-slate-200">
        ${strategyTabs(d, 'bt-experiments.html', sid)}
        <p>This section analyses the production rules of <b>${esc(strat.name || sid)}</b> (<span class="mono">config/rules.json → strategies.${esc(sid)}</span>), proposes testable variants and compares them with the baseline on the same data. Production rules are never changed automatically; experiment files live in <span class="mono">backtesting/experiments/</span>.</p>
        ${base ? `<p>Baseline: run <a href="bt-strategy.html?run=${esc(base.metadata.run_id)}" class="mono">${esc(base.metadata.run_id)}</a>, rules version <span class="mono">${esc(base.metadata.strategy_version)}</span>, snapshot stored in its <span class="mono">config_snapshot.json</span> (immutable).</p>` : '<p>No baseline run yet for this strategy.</p>'}</div>`),
      panel('Step B · Research questions (from the baseline)', base ? questionsTable(M(base).research_questions, 'rq-exp') : empty('No baseline run yet.')),
      panel(`Step C · Experimental configurations (${exps.length})`, exps.length ? `<div class="grid grid-cols-1 gap-3 lg:grid-cols-2">${cards}</div>` : empty(`No experiment files for this strategy. Create proposals with: paperbot-backtest propose --strategy ${sid}`)),
      panel('Step D/E · Baseline vs experiments', comparison)];
  };
  PAGES['bt-analysis.html'] = (d) => analysisView(d);
  function analysisView(d) {
    const failed = d.runs.filter((r) => r.status === 'failed');
    const blocks = d.strategies.map((s) => {
      const base = s.latest_run && d.details[s.latest_run];
      if (!base) return panel(`${esc(s.name)} · areas requiring investigation`, empty('No completed baseline run yet.'));
      const qs = (M(base).research_questions || []).filter((q) => q.status === 'concern' || q.status === 'insufficient data');
      const dq = (base.validation || {}).data_quality || {};
      return panel(`${esc(s.name)} · areas requiring investigation`, `${questionsTable(qs, 'concerns-' + s.strategy_id)}
        <div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div><h3 class="mb-1 text-sm font-semibold text-slate-100">Data limitations</h3>${list(dq.limitations)}</div>
          <div><h3 class="mb-1 text-sm font-semibold text-slate-100">Assumptions that affect results</h3>${list((M(base).assessment || {}).assumptions)}</div>
        </div>`);
    });
    return [notice(`<b>${FUTURE}</b> This page lists what the backtests cannot show, weaknesses found in the latest results of every strategy, and what to investigate next.`),
      ...blocks,
      panel('Known limitations of the simulation', `<div class="space-y-3">${(d.limitations || []).map((l) => `<div class="text-sm"><b class="text-slate-100">${esc(l.title)}.</b> <span class="text-slate-300">${esc(l.text)}</span></div>`).join('')}</div>`),
      panel('Suggested improvements', list(d.improvements)),
      panel(`Failed or incomplete runs (${failed.length})`, table('failed', [{ key: 'run_id', label: 'Run', fmt: (v) => `<span class="mono">${esc(v)}</span>` }, { key: 'strategy_id', label: 'Strategy' }, { key: 'kind', label: 'Kind' }, { key: 'experiment_id', label: 'Experiment' },
        { key: 'started_at', label: 'Started', fmt: utc }, { key: 'errors', label: 'Error', fmt: (v) => `<span class="whitespace-normal text-rose-200">${esc((v || []).join('; '))}</span>` }], failed, { filter: false, empty: 'No failed runs.' }))];
  }

  /* ------------------------------------------------------------ boot */
  function mount(nodes) {
    const host = document.getElementById('bt-app');
    host.innerHTML = nodes.filter(Boolean).join('');
  }
  PBT.page = (name) => {
    const host = document.getElementById('bt-app');
    try {
      const d = D();
      const nav = subnav(name);
      if (!d) { host.innerHTML = nav + noData(); return; }
      mount([nav, ...PAGES[name](d), footer(d)]);
    } catch (e) {
      console.error('[paperbot backtest]', e);
      host.innerHTML = subnav(name) + panel('Page error', empty(`This page could not be drawn: ${e.message}`));
    }
  };
  const footer = (d) => `<p class="text-xs text-ink-400">Backtest data generated ${esc(utc(d.generated_at))}. Source files: backtesting/results/. ${esc(FUTURE)}</p>`;
  PBT.standalone = (view, payload) => {
    try {
      const head = `<header class="flex flex-wrap items-center justify-between gap-2"><div><h1 class="text-xl font-semibold text-white">Paperbot backtest report</h1>
        <p class="text-xs text-ink-300">Self-contained report generated by paperbot.backtesting · all times US Eastern (ET) · currency USD</p></div></header>`;
      if (view === 'run') mount([head, ...strategyReport(payload), panel('Validation periods', segmentsTable(payload))]);
      else if (view === 'portfolio') mount([head, ...portfolioView(payload)]);
      else if (view === 'analysis') mount([head, ...analysisView(payload)]);
      else if (view === 'periodic') {
        const pr = payload.period_report || {};
        mount([head, panel(`${esc(pr.kind)} executive report · ${esc(pr.since)} to ${esc(pr.until)}`, `<p class="mb-2 text-sm text-ink-300">${int((pr.runs || []).length)} run(s) in this period.</p>` +
          table('periodruns', [{ key: 'run_id', label: 'Run' }, { key: 'kind', label: 'Kind' }, { key: 'experiment_id', label: 'Experiment' }, { key: 'status', label: 'Status', fmt: statusPill },
            { key: 'headline', label: 'Net P&L', align: 'right', fmt: (v) => money((v || {}).net_pnl, { color: true, signed: true }) },
            { key: 'headline', label: 'Trades', align: 'right', fmt: (v) => int((v || {}).completed_trades) }], pr.runs || [], { filter: false })), ...portfolioView(payload), ...analysisView(payload)]);
      }
    } catch (e) {
      console.error('[paperbot backtest]', e);
      document.getElementById('bt-app').innerHTML = `<p>Report could not be drawn: ${esc(e.message)}</p>`;
    }
  };
})();
