/* Token page (/t/SYMBOL). Same data sources as the homepage:
 *   /api/token/{symbol}   group data (mark, three tapes, cheapest/richest rail) for RWA names
 *   /api/prices           shared price cache (polled every second)
 *   /api/balances/{addr}  wallet holdings
 *   /api/chart/{symbol}   price history for the chart
 * Buy / Sell always show and open swap.js (stock picker inside the sheet).
 * Position card always shows X / Ondo / B rows (0 when empty or disconnected).
 *
 * Group pages (data-kind="group", e.g. /t/NVDA or the legacy /t/NVDAx): the traded
 * SYMBOL is the cheapest rail (auto-picked, or the wrapper the URL/legacy link named
 * if you want a specific one focused). #rotate scrolls to the cross-wrapper card;
 * #swap opens Buy on the focused wrapper once a wallet is connected.
 */
(function () {
  'use strict';
  const page = document.getElementById('token-page');
  if (!page) return;

  const IS_GROUP = page.dataset.kind === 'group';
  const UNDERLYING = page.dataset.underlying || '';
  const FOCUS = page.dataset.focus || '';
  let SYMBOL = page.dataset.symbol; // traded wrapper once resolved (group) or the asset itself
  const PRICE_MS = 1000;
  const BALANCE_MS = 6000;
  const CHART_MS = 30000;
  const UP = '#4ade80';
  const DOWN = '#fb7185';
  const PLAT_LABEL = { xstocks: 'x', ondo: 'Ondo', bstocks: 'b' };

  const $ = (id) => document.getElementById(id);
  const els = {
    stats: $('tk-stats'), mark: $('tk-mark'), prem: $('tk-prem'), fair: $('tk-fair'), premFair: $('tk-prem-fair'),
    plot: $('tk-plot'), axis: $('tk-axis'), xaxis: $('tk-xaxis'), cursor: $('tk-cursor'), tip: $('tk-tip'),
    noHist: $('tk-nohist'), ranges: $('tk-ranges'),
    posList: $('tk-pos-list'), posTotal: $('tk-pos-total'),
    bar: $('tk-bar'), sell: $('tk-sell'), buy: $('tk-buy'),
    back: $('tk-back'), copy: $('tk-copy'), copyMenu: $('tk-copy-menu'),
    about: $('tk-about'), aboutText: $('tk-about-text'), more: $('tk-readmore'),
    logo: $('tk-logo'), symText: $('tk-sym-text'), aboutName: $('tk-about-name'), urlLink: $('tk-url-link'),
    tapeRow: $('tk-tape-row'),
    newsList: $('tk-news-list'), newsEmpty: $('tk-news-empty'),
  };

  const state = {
    address: null,
    holdings: null, // { SYMBOL: amount } once loaded
    prices: {},
    assets: {},
    range: '1D',
    points: [],       // [[ms, price], ...] history for the selected range (single-series)
    multiChart: null, // { mark: [[ms,px]], wrappers: { SYM: [[ms,px]] } } for group pages
    group: null,      // /api/token/{underlying} result once loaded (group pages only)
    session: null,    // { cashOpen, ... } from /api/prices
    tapeStale: false,
    arb: null,        // latest cross-wrapper hit (winner = cheapSymbol, rejected = richSymbol)
  };

  // ---- Formatting ---------------------------------------------------------
  const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
  const fmtPrice = (v) => (v >= 1 ? usd.format(v) : '$' + v.toFixed(4));
  const fmtUsd = (v) => (v > 0 && v < 0.005 ? '<$0.01' : usd.format(v));

  function fmtCompact(v) {
    const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (const [div, suffix] of units) {
      if (v >= div) return '$' + (v / div).toFixed(2) + suffix;
    }
    return '$' + Math.round(v).toLocaleString('en-US');
  }

  function fmtAmount(v) {
    const max = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
    return v.toLocaleString('en-US', { maximumFractionDigits: max });
  }

  function fmtDelta(v) {
    const abs = Math.abs(v);
    const digits = abs >= 1 ? 2 : abs >= 0.01 ? 3 : 5;
    return (v < 0 ? '-' : v > 0 ? '+' : '') + '$' + abs.toFixed(digits);
  }

  // Returns "X tokens × M ≈ Y shares" when multiplier is set, else null.
  function sharesNote(tokens, multiplier) {
    if (!(multiplier > 0) || !(tokens > 0)) return null;
    const shares = tokens * multiplier;
    return `${fmtAmount(tokens)} tokens \u00d7 ${multiplier} \u2248 ${fmtAmount(shares)} shares`;
  }

  function pct(p, digits) {
    if (p === null || p === undefined || !isFinite(p)) return null;
    const r = Number(p.toFixed(digits === undefined ? 1 : digits));
    return { text: (r > 0 ? '+' : '') + r.toString() + '%', cls: r > 0 ? 'pos' : r < 0 ? 'neg' : 'flat' };
  }

  function setTone(el, base, cls) {
    el.className = base + (cls ? ' ' + cls : '');
  }

  // ---- Render -------------------------------------------------------------
  function open24h(p) {
    return p.change24h === null || p.change24h === undefined ? p.price : p.price / (1 + p.change24h / 100);
  }

  // Server keys /api/prices by UPPERCASE symbol (TSLAon -> TSLAON); SYMBOL keeps wrapper case.
  const livePrice = (sym) => state.prices[String(sym || '').toUpperCase()] || state.prices[sym];

  const DASH = '\u2013';
  function setVal(el, text, cls) {
    el.textContent = text;
    el.className = cls || '';
  }
  function premVal(el, v) {
    if (v === null || v === undefined || !isFinite(v)) return setVal(el, DASH);
    const r = Number((v * 100).toFixed(1));
    setVal(el, (r > 0 ? '+' : '') + r + '%', r > 0 ? 'pos' : r < 0 ? 'neg' : 'flat');
  }

  // Wrappers shown in Position / copy menu: always X, Ondo, B on group pages.
  function railList() {
    if (!IS_GROUP) return [{ plat: null, label: SYMBOL, symbol: SYMBOL, mint: els.copy.dataset.mint || '' }];
    const ws = (state.group && state.group.wrappers) || [];
    return ['xstocks', 'ondo', 'bstocks'].map((plat) => {
      const w = ws.find((x) => x.platform === plat) || {};
      return { plat, label: PLAT_LABEL[plat], symbol: w.symbol || '', mint: w.mint || w.address || '', tokenPrice: w.tokenPrice };
    });
  }

  function renderPosition() {
    const rows = railList();
    let total = 0;
    els.posList.textContent = '';
    rows.forEach((r) => {
      const key = r.symbol ? keyFor(r.symbol) : '';
      const amount = state.address && state.holdings && key ? state.holdings[key] || 0 : 0;
      const px = (state.prices[key] && state.prices[key].price) || r.tokenPrice || 0;
      const val = amount * px;
      total += val;
      const row = document.createElement('div');
      row.className = 'tk-pos-row';
      const left = document.createElement('div');
      const lbl = document.createElement('div'); lbl.className = 'tk-pos-lbl'; lbl.textContent = r.label;
      const amt = document.createElement('div'); amt.className = 'tk-pos-amt'; amt.textContent = `${fmtAmount(amount)} ${r.symbol || ''}`.trim();
      left.appendChild(lbl); left.appendChild(amt);
      const v = document.createElement('div'); v.className = 'tk-pos-val'; v.textContent = fmtUsd(val);
      row.appendChild(left); row.appendChild(v);
      els.posList.appendChild(row);
    });
    els.posTotal.textContent = fmtUsd(total);
  }

  function render() {
    const p = livePrice(SYMBOL);

    // Four stats, always rendered; dash when a value isn't available.
    if (!p || p.noYahoo) {
      setVal(els.mark, DASH); setVal(els.prem, DASH); setVal(els.fair, DASH); setVal(els.premFair, DASH);
    } else {
      setVal(els.mark, p.mark ? fmtPrice(p.mark) : DASH);
      premVal(els.prem, p.premium);
      const showFair = !!p.fairPrice && state.session && !state.session.cashOpen;
      setVal(els.fair, showFair ? fmtPrice(p.fairPrice) : DASH);
      if (showFair) premVal(els.premFair, p.premiumToFair); else setVal(els.premFair, DASH);
    }

    renderPosition();
    drawChart();
  }

  // ---- Chart --------------------------------------------------------------
  const NS = 'http://www.w3.org/2000/svg';
  const W = 360;
  const H = 230;
  const PAD_T = 14;
  const PAD_B = 14;
  const BUCKETS = 96;

  function svgEl(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    Object.keys(attrs).forEach((k) => n.setAttribute(k, attrs[k]));
    return n;
  }

  const SERIES_COLORS = {
    mark:    '#94a3b8', // slate: cash/reference
    xstocks: '#60a5fa', // blue
    ondo:    '#a78bfa', // purple
    bstocks: '#34d399', // green
  };

  function _wrapperPlatform(sym) {
    if (!state.group) return null;
    const w = (state.group.wrappers || []).find((x) => x.symbol === sym);
    return w ? w.platform : null;
  }

  // Resample onto a shared grid (last value per bucket, carried forward).
  function resample(pts, t0, t1) {
    const span = t1 - t0 || 1;
    const out = new Array(BUCKETS).fill(null);
    const sorted = pts.slice().sort((a, b) => a[0] - b[0]);
    sorted.forEach(([t, v]) => {
      const i = Math.min(BUCKETS - 1, Math.max(0, Math.round(((t - t0) / span) * (BUCKETS - 1))));
      out[i] = v;
    });
    let last = null;
    for (let i = 0; i < BUCKETS; i++) {
      if (out[i] === null) out[i] = last; else last = out[i];
    }
    return out;
  }

  // Monotone cubic (Fritsch-Carlson): smooth, never overshoots the data.
  function smoothPath(xs, ys) {
    const n = xs.length;
    if (n < 2) return '';
    const dx = [], m = [], t = new Array(n);
    for (let i = 0; i < n - 1; i++) { dx.push(xs[i + 1] - xs[i]); m.push((ys[i + 1] - ys[i]) / dx[i]); }
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
      if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
      const a = t[i] / m[i], b = t[i + 1] / m[i], h = Math.hypot(a, b);
      if (h > 3) { t[i] = (3 * a / h) * m[i]; t[i + 1] = (3 * b / h) * m[i]; }
    }
    let d = `M${xs[0].toFixed(2)},${ys[0].toFixed(2)}`;
    for (let i = 0; i < n - 1; i++) {
      const c1x = xs[i] + dx[i] / 3, c1y = ys[i] + (t[i] * dx[i]) / 3;
      const c2x = xs[i + 1] - dx[i] / 3, c2y = ys[i + 1] - (t[i + 1] * dx[i]) / 3;
      d += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${xs[i + 1].toFixed(2)},${ys[i + 1].toFixed(2)}`;
    }
    return d;
  }

  function fmtTime(ms, range) {
    const d = new Date(ms);
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    return range === '1W' ? d.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm : hm;
  }

  let chartModel = null;

  function clearChart() {
    els.plot.textContent = '';
    els.axis.textContent = '';
    els.cursor.hidden = true;
    els.tip.hidden = true;
    els.plot.parentElement.querySelectorAll('.tk-end').forEach((n) => n.remove());
    chartModel = null;
  }

  function drawChart() {
    clearChart();
    let series = [];
    let empty = 'No price history yet';

    if (IS_GROUP && state.multiChart) {
      const mc = state.multiChart;
      empty = 'Tape history starts after first refresh.';
      Object.entries(mc.wrappers || {}).forEach(([sym, pts]) => {
        if (pts.length < 2) return;
        const plat = _wrapperPlatform(sym) || 'xstocks';
        series.push({ label: sym, color: SERIES_COLORS[plat] || UP, pts, w: 1.8 });
      });
      if ((mc.mark || []).length >= 2) series.push({ label: 'Cash', color: SERIES_COLORS.mark, pts: mc.mark, w: 1.3, dash: '4 4', last: true });
    } else if (!IS_GROUP) {
      const pts = state.points.slice();
      const live = livePrice(SYMBOL);
      if (live && pts.length) pts.push([Date.now(), live.price]);
      if (pts.length >= 2) {
        const color = pts[pts.length - 1][1] >= pts[0][1] ? UP : DOWN;
        series.push({ label: SYMBOL, color, pts, w: 2, fill: true });
      }
    }

    const hasAny = series.length > 0;
    els.noHist.hidden = hasAny;
    els.noHist.textContent = empty;
    els.axis.hidden = !hasAny;
    els.xaxis.hidden = !hasAny;
    if (!hasAny) return;

    const all = series.flatMap((s) => s.pts);
    const t0 = Math.min(...all.map((q) => q[0]));
    const t1 = Math.max(...all.map((q) => q[0]));
    series.forEach((s) => { s.vals = resample(s.pts, t0, t1); });

    const flat = series.flatMap((s) => s.vals.filter((v) => v !== null));
    let min = Math.min(...flat);
    let max = Math.max(...flat);
    const base = Math.abs(max) || 1;
    if (max - min < base * 0.0005) { min -= base * 0.0005; max += base * 0.0005; }
    const padV = (max - min) * 0.12;
    min -= padV; max += padV;
    const span = max - min;
    const x = (i) => (i / (BUCKETS - 1)) * W;
    const y = (v) => PAD_T + (H - PAD_T - PAD_B) * (1 - (v - min) / span);

    // Grid + price labels (4 lines)
    [0, 1 / 3, 2 / 3, 1].forEach((f) => {
      const gy = PAD_T + (H - PAD_T - PAD_B) * f;
      els.plot.appendChild(svgEl('line', { class: 'tk-grid', x1: 0, x2: W, y1: gy, y2: gy }));
      const lab = document.createElement('span');
      lab.style.top = ((gy / H) * 100).toFixed(2) + '%';
      lab.textContent = fmtPrice(max - (max - min) * f);
      els.axis.appendChild(lab);
    });

    // Time labels
    const xl = els.xaxis.children;
    xl[0].textContent = fmtTime(t0, state.range);
    xl[1].textContent = fmtTime((t0 + t1) / 2, state.range);
    xl[2].textContent = fmtTime(t1, state.range);

    // Lines (cash last-drawn on top only if it has no wrappers beneath)
    series.forEach((s, si) => {
      const idx = [];
      s.vals.forEach((v, i) => { if (v !== null) idx.push(i); });
      const xs = idx.map(x);
      const ys = idx.map((i) => y(s.vals[i]));
      const d = smoothPath(xs, ys);
      if (s.fill) {
        const defs = svgEl('defs', {});
        const grad = svgEl('linearGradient', { id: 'tk-grad', x1: 0, y1: 0, x2: 0, y2: 1 });
        grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': s.color, 'stop-opacity': 0.22 }));
        grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': s.color, 'stop-opacity': 0 }));
        defs.appendChild(grad);
        els.plot.appendChild(defs);
        els.plot.appendChild(svgEl('path', { d: `${d} L${xs[xs.length - 1].toFixed(2)},${H} L${xs[0].toFixed(2)},${H} Z`, fill: 'url(#tk-grad)' }));
      }
      const attrs = { d, class: 'tk-line', stroke: s.color, 'stroke-width': s.w };
      if (s.dash) attrs['stroke-dasharray'] = s.dash;
      els.plot.appendChild(svgEl('path', attrs));
      // End dot
      const lastI = idx[idx.length - 1];
      const dot = document.createElement('i');
      dot.className = 'tk-end';
      dot.style.left = ((x(lastI) / W) * 100).toFixed(2) + '%';
      dot.style.top = ((y(s.vals[lastI]) / H) * 100).toFixed(2) + '%';
      dot.style.background = s.color;
      els.plot.parentElement.appendChild(dot);
    });

    chartModel = { series, t0, t1, x, y };
  }

  // Touch / hover crosshair with per-series values
  function moveCursor(clientX) {
    if (!chartModel) return;
    const rect = els.plot.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    const i = Math.round(f * (BUCKETS - 1));
    const { series, t0, t1 } = chartModel;
    els.cursor.hidden = false;
    els.cursor.style.left = ((i / (BUCKETS - 1)) * 100).toFixed(2) + '%';
    const t = t0 + (i / (BUCKETS - 1)) * (t1 - t0);
    els.tip.textContent = '';
    const head = document.createElement('div');
    head.className = 'tk-tip-time';
    head.textContent = fmtTime(t, state.range);
    els.tip.appendChild(head);
    series.forEach((s) => {
      const v = s.vals[i];
      if (v === null) return;
      const row = document.createElement('div');
      row.className = 'tk-tip-row';
      const dot = document.createElement('i'); dot.style.background = s.color;
      const name = document.createElement('span'); name.textContent = s.label;
      const val = document.createElement('b'); val.textContent = fmtPrice(v);
      row.appendChild(dot); row.appendChild(name); row.appendChild(val);
      els.tip.appendChild(row);
    });
    els.tip.hidden = false;
    els.tip.classList.toggle('left', f > 0.55);
  }
  function hideCursor() { els.cursor.hidden = true; els.tip.hidden = true; }
  const plotWrap = els.plot.parentElement;
  plotWrap.addEventListener('pointerdown', (e) => moveCursor(e.clientX));
  plotWrap.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse' || e.buttons || e.pressure) moveCursor(e.clientX); });
  plotWrap.addEventListener('pointerleave', hideCursor);
  plotWrap.addEventListener('pointerup', () => { if (plotWrap.dataset.touch) hideCursor(); });
  plotWrap.addEventListener('pointercancel', hideCursor);

  // ---- Chart legend (group pages only) -----------------------------------
  function renderLegend(multiChart) {
    let legend = document.getElementById('tk-chart-legend');
    if (!IS_GROUP) { if (legend) legend.remove(); return; }
    if (!legend) {
      legend = document.createElement('div');
      legend.id = 'tk-chart-legend';
      legend.className = 'tk-chart-legend';
      const plotWrap = els.plot.parentElement;
      plotWrap.parentElement.insertBefore(legend, plotWrap.nextSibling);
    }
    legend.innerHTML = '';
    const LABEL = { mark: 'Cash', xstocks: 'xStocks', ondo: 'Ondo', bstocks: 'bStocks' };
    const COLOR = { mark: '#94a3b8', xstocks: '#60a5fa', ondo: '#a78bfa', bstocks: '#34d399' };
    const hasMark = (multiChart.mark || []).length >= 2;
    if (hasMark) {
      const dot = `<span class="tk-legend-dot" style="background:${COLOR.mark}"></span>`;
      legend.insertAdjacentHTML('beforeend', `<span class="tk-legend-item">${dot}${LABEL.mark}</span>`);
    }
    Object.entries(multiChart.wrappers || {}).forEach(([sym, pts]) => {
      if (pts.length < 2) return;
      const plat = _wrapperPlatform(sym) || 'xstocks';
      const color = COLOR[plat] || '#60a5fa';
      const label = LABEL[plat] || plat;
      const dot = `<span class="tk-legend-dot" style="background:${color}"></span>`;
      legend.insertAdjacentHTML('beforeend', `<span class="tk-legend-item">${dot}${sym}</span>`);
    });
    if (legend.children.length === 0) {
      legend.insertAdjacentHTML('beforeend', '<span class="tk-legend-empty">Tape history starts after first refresh.</span>');
    }
  }

  let chartReq = 0;
  async function loadChart() {
    const req = ++chartReq;
    const chartSym = IS_GROUP ? UNDERLYING : SYMBOL;
    try {
      const data = await getJSON(`/api/chart/${encodeURIComponent(chartSym)}?range=${state.range}`);
      if (req !== chartReq) return;
      if (IS_GROUP && data.wrappers !== undefined) {
        // Multi-series response
        state.multiChart = { mark: data.mark || [], wrappers: data.wrappers || {} };
        state.points = [];
        renderLegend(state.multiChart);
      } else {
        state.points = data.points || [];
        state.multiChart = null;
      }
    } catch (err) {
      if (req !== chartReq) return;
      // keep whatever we had for this range
    }
    drawChart();
  }

  els.ranges.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-range]');
    if (!btn || btn.dataset.range === state.range) return;
    state.range = btn.dataset.range;
    els.ranges.querySelectorAll('.tk-range').forEach((b) => b.classList.toggle('active', b === btn));
    state.points = [];
    drawChart();
    loadChart();
  });

  // ---- Data ---------------------------------------------------------------
  async function getJSON(url) {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(url + ' ' + resp.status);
    return resp.json();
  }

  let pricesBusy = false;
  async function tickPrices() {
    if (document.hidden || pricesBusy) return;
    pricesBusy = true;
    try {
      const data = await getJSON('/api/prices');
      state.prices = data.prices || {};
      state.session = data.session || null;
      state.tapeStale = !!data.tapeStale;
      render();
    } catch (err) { /* keep last prices */ } finally {
      pricesBusy = false;
    }
  }

  let balancesFor = null;
  async function tickBalances() {
    const addr = state.address;
    if (!addr || document.hidden || balancesFor === addr) return;
    balancesFor = addr;
    try {
      const data = await getJSON('/api/balances/' + encodeURIComponent(addr));
      if (addr === state.address && data && data.holdings) {
        state.holdings = data.holdings;
        render();
      }
    } catch (err) { /* keep last holdings */ } finally {
      if (balancesFor === addr) balancesFor = null;
    }
  }

  async function loadAssets() {
    try {
      const data = await getJSON('/api/assets');
      state.assets = data.assets || {};
    } catch (err) {
      setTimeout(loadAssets, 3000);
    }
  }

  const getCtx = () => ({ address: state.address, holdings: state.holdings || {}, prices: state.prices, assets: state.assets });
  const refreshBalances = () => {
    tickBalances();
    setTimeout(tickBalances, 2500); // pick up the settled balance
  };

  // ---- Group (RWA name page: mark + three tapes + cheapest rail) -----
  function fmtPrem(p) {
    if (p === null || p === undefined || !isFinite(p)) return '—';
    const r = Number((p * 100).toFixed(1));
    return (r > 0 ? '+' : '') + r.toString() + '%';
  }
  const premCls = (p) => (!(p > 0) && !(p < 0) ? 'flat' : (p > 0 ? 'pos' : 'neg'));

  function renderTapeRow(g) {
    if (!els.tapeRow) return;
    const cells = els.tapeRow.querySelectorAll('.hm-tape-cell');
    ['xstocks', 'ondo', 'bstocks'].forEach((plat, i) => {
      const w = g.wrappers.find((x) => x.platform === plat);
      const cell = cells[i];
      if (!cell) return;
      cell.textContent = '';
      const lbl = document.createElement('span'); lbl.className = 'hm-tape-lbl'; lbl.textContent = PLAT_LABEL[plat];
      cell.appendChild(lbl);
      if (w && w.tokenPrice) {
        const val = document.createElement('span'); val.className = 'hm-tape-val'; val.textContent = fmtPrice(w.tokenPrice);
        const prem = document.createElement('span'); prem.className = 'hm-tape-prem ' + premCls(w.premium); prem.textContent = fmtPrem(w.premium);
        cell.appendChild(val); cell.appendChild(prem);
        if (state.tapeStale) {
          const note = document.createElement('span'); note.className = 'hm-tape-note'; note.textContent = 'benchmark price \u00b7 live tape unavailable';
          cell.appendChild(note);
        }
      } else if (!w || !w.address) {
        const val = document.createElement('span'); val.className = 'hm-tape-val'; val.textContent = '— no tape';
        cell.appendChild(val);
      } else {
        const val = document.createElement('span'); val.className = 'hm-tape-val'; val.textContent = '—';
        const prem = document.createElement('span'); prem.className = 'hm-tape-prem flat'; prem.textContent = '—';
        cell.appendChild(val); cell.appendChild(prem);
      }
      cell.classList.remove('win', 'rej');
      const a = state.arb;
      if (a && w && w.symbol) {
        const role = w.symbol === a.cheapSymbol ? 'win' : w.symbol === a.richSymbol ? 'rej' : '';
        if (role) {
          cell.classList.add(role);
          const tag = document.createElement('span'); tag.className = 'hm-tape-role';
          tag.textContent = role === 'win' ? 'Winner \u00b7 buy' : 'Rejected \u00b7 rich';
          cell.appendChild(tag);
        }
      }
    });
  }

  function applyGroup(g) {
    state.group = g;
    // Trade the cheapest rail unless the URL named a specific wrapper (e.g. legacy /t/NVDAx).
    // A wrapper with no on-chain address has no tape — never auto-selected or
    // treated as tradeable, even if it's the one the URL asked for.
    const focusW = g.wrappers.find((w) => w.symbol === FOCUS && w.hasTape);
    const cheapW = g.wrappers.find((w) => w.symbol === g.cheapest);
    const anyTradeable = g.wrappers.find((w) => w.hasTape);
    const tradeW = focusW || cheapW || anyTradeable;
    if (!tradeW) {
      // No wrapper for this underlying has a contract yet — nothing to trade.
        SYMBOL = (g.wrappers.find((w) => w.symbol === FOCUS) || g.wrappers[0] || {}).symbol || SYMBOL;
      page.dataset.symbol = SYMBOL;
      return;
    }
    if (els.bar) els.bar.hidden = false;
    SYMBOL = tradeW.symbol;
    page.dataset.symbol = SYMBOL;

    if (els.symText) els.symText.textContent = UNDERLYING;
    if (els.aboutName) els.aboutName.textContent = g.name;
    if (els.aboutText && !els.aboutText.textContent) {
      els.aboutText.textContent = `${g.name} is a BNB Chain tokenized equity. Economic exposure only — no ownership, voting or other legal rights.`;
    }
    if (els.logo) {
      const src = g.logo || g.wrappers.map((w) => w.image).find(Boolean);
      if (src) {
        if (els.logo.tagName === 'IMG') els.logo.src = src;
        else {
          const img = document.createElement('img');
          img.className = els.logo.className; img.id = els.logo.id; img.alt = ''; img.src = src;
          els.logo.replaceWith(img); els.logo = img;
        }
      }
    }
    const tmint = tradeW.mint || tradeW.address || '';
    if (els.urlLink && tmint) {
      els.urlLink.href = tradeW.url || ('https://pancakeswap.finance/swap?chain=bsc&outputCurrency=' + tmint);
      els.urlLink.hidden = false;
    }
    if (!els.copyMenu.hidden) renderCopyMenu();

    renderTapeRow(g);
    render();
    loadChart();
    loadAssets();
  }

  async function loadGroup() {
    try {
      const g = await getJSON(`/api/token/${encodeURIComponent(UNDERLYING)}`);
      applyGroup(g);
      if (IS_GROUP) { loadArb(); refreshFlattenVisibility(); }
      loadEarnings();
      if (/^#swap/.test(location.hash) && !loadGroup.opened && state.address) {
        loadGroup.opened = true;
        const q = new URLSearchParams(location.hash.split('?')[1] || '');  // basket: #swap?pay=USDT&amt=16.67
        openSwap('buy', FOCUS || undefined, undefined, { pay: (q.get('pay') || '').toUpperCase(), amount: q.get('amt') });
      }
    } catch (err) {
      setTimeout(loadGroup, 3000);
    }
  }

  const arbEls = {
    card: $('tk-arb'), net: $('tk-arb-net'), line: $('tk-arb-line'), note: $('tk-arb-note'), ratios: $('tk-arb-ratios'),
    legs: $('tk-arb-legs'), sellBtn: $('tk-arb-sell'), buyBtn: $('tk-arb-buy'),
    rotateBtn: $('tk-rotate-btn'), flattenBtn: $('tk-flatten-btn'),
  };
  const QUICK_SIZE_USD = 10;
  const ROTATE_USD = 50;

  async function loadArb() {
    if (!arbEls.card) return;
    try {
      const data = await getJSON(`/api/agent/arb/${encodeURIComponent(UNDERLYING)}`);
      const hit = data.hit;
      if (!hit) { arbEls.card.hidden = true; return; }
      arbEls.card.hidden = false;
      state.arb = hit;
      if (state.group) renderTapeRow(state.group);
      if (location.hash === '#rotate' && !loadArb.scrolled) {
        loadArb.scrolled = true;
        arbEls.card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      arbEls.net.textContent = (hit.viable ? '+' : '') + Math.round(hit.netBps) + ' bps';
      arbEls.net.className = 'tk-arb-net ' + (hit.viable ? 'pos' : 'neg');
      arbEls.line.textContent = `Winner: buy ${hit.cheapSymbol} \u00b7 Rejected: ${hit.richSymbol} (rich) \u00b7 $${hit.sizeUsd.toFixed(0)} each leg`;
      arbEls.note.textContent = hit.viable
        ? `Gross ${Math.round(hit.grossBps)} bps, ~${Math.round(hit.costBps)} bps costs.`
        : `Gross ${Math.round(hit.grossBps)} bps doesn't clear ~${Math.round(hit.costBps)} bps in costs — not viable at $${hit.sizeUsd.toFixed(0)}.`;
      if (state.tapeStale) arbEls.note.textContent += ' Benchmark price only \u2014 live pool tape unavailable from this host.';
      if (arbEls.ratios) {
        arbEls.ratios.hidden = false;
        arbEls.ratios.textContent = `Shares per token: ${hit.richSymbol} ${hit.richRatio} (${hit.richPrice} \u2192 ${hit.richSharePrice.toFixed(4)}/sh) \u00b7 ${hit.cheapSymbol} ${hit.cheapRatio} (${hit.cheapPrice} \u2192 ${hit.cheapSharePrice.toFixed(4)}/sh)`;
      }
      if (arbEls.legs) {
        arbEls.legs.hidden = false;
        arbEls.sellBtn.textContent = `Sell ${hit.richSymbol}`;
        arbEls.buyBtn.textContent = `Buy ${hit.cheapSymbol}`;
        arbEls.sellBtn.onclick = () => openSwap('sell', hit.richSymbol);
        arbEls.buyBtn.onclick = () => openSwap('buy', hit.cheapSymbol);
      }
    } catch (err) {
      arbEls.card.hidden = true;
    }
  }

  // "Rotate $10": quotes the cross-wrapper arb at a small fixed size and, if
  // viable, opens the sell/buy legs the same way the leg buttons above do.
  async function rotateQuick() {
    if (!arbEls.rotateBtn) return;
    arbEls.rotateBtn.disabled = true;
    try {
      const data = await getJSON(`/api/agent/arb/${encodeURIComponent(UNDERLYING)}?size_usd=${ROTATE_USD}`);
      const hit = data.hit;
      if (!hit) { arbEls.rotateBtn.textContent = 'No rotate available'; return; }
      if (!hit.viable) { arbEls.rotateBtn.textContent = `Rotate $${ROTATE_USD} · not viable`; return; }
      // One sheet at a time: sell leg first, buy leg opens when it closes.
      const opened = openSwap('sell', hit.richSymbol, () => openSwap('buy', hit.cheapSymbol));
      if (!opened) openSwap('buy', hit.cheapSymbol);
    } catch (err) {
      arbEls.rotateBtn.textContent = 'Rotate failed';
    } finally {
      arbEls.rotateBtn.disabled = false;
    }
  }

  // "Flatten $10": sells the richest wrapper for this underlying if it's
  // >2% rich (agent.flatten_candidates' bar), same fixed-size pattern.
  async function flattenQuick() {
    if (!arbEls.flattenBtn) return;
    arbEls.flattenBtn.disabled = true;
    try {
      const data = await getJSON(`/api/agent/flatten/${encodeURIComponent(UNDERLYING)}?size_usd=${QUICK_SIZE_USD}`);
      const hit = data.hit;
      if (!hit) { arbEls.flattenBtn.hidden = true; return; }
      arbEls.flattenBtn.hidden = false;
      openSwap('sell', hit.symbol);
    } catch (err) {
      arbEls.flattenBtn.textContent = 'Flatten failed';
    } finally {
      arbEls.flattenBtn.disabled = false;
    }
  }

  if (arbEls.rotateBtn) arbEls.rotateBtn.addEventListener('click', rotateQuick);
  if (arbEls.flattenBtn) arbEls.flattenBtn.addEventListener('click', flattenQuick);

  // Flatten button visibility follows whether this underlying currently has
  // a >2% rich wrapper — checked once per group load, same cadence as arb.
  async function refreshFlattenVisibility() {
    if (!arbEls.flattenBtn) return;
    try {
      const data = await getJSON(`/api/agent/flatten/${encodeURIComponent(UNDERLYING)}?size_usd=${QUICK_SIZE_USD}`);
      arbEls.flattenBtn.hidden = !data.hit;
      if (data.hit) arbEls.flattenBtn.textContent = `Flatten $${QUICK_SIZE_USD} (${data.hit.symbol})`;
    } catch (err) {
      arbEls.flattenBtn.hidden = true;
    }
  }

  // ---- Earnings stand-down: Yahoo calendar flag only; nothing shown without a date ----
  const earnEls = { card: $('tk-earn'), inEl: $('tk-earn-in'), line: $('tk-earn-line'), btn: $('tk-earn-flatten') };
  async function loadEarnings() {
    if (!earnEls.card || !UNDERLYING) return;
    try {
      const e = await getJSON(`/api/earnings?underlying=${encodeURIComponent(UNDERLYING)}`);
      if (!e.inside24h) { earnEls.card.hidden = true; return; }
      earnEls.card.hidden = false;
      earnEls.inEl.textContent = `in ${Math.max(1, Math.round(e.hoursUntil))}h`;
      earnEls.line.textContent = `${UNDERLYING} reports ${new Date(e.earningsDate).toLocaleString()}. Sell the wrapper into USDT before the print; check again after.`;
    } catch (err) {
      earnEls.card.hidden = true;
    }
  }
  if (earnEls.btn) earnEls.btn.addEventListener('click', async () => {
    earnEls.btn.disabled = true;
    try {
      const d = await getJSON(`/api/agent/flatten/${encodeURIComponent(UNDERLYING)}?size_usd=${QUICK_SIZE_USD}&force=true`);
      if (!d.hit) { earnEls.btn.textContent = 'No route'; return; }
      openSwap('sell', d.hit.symbol);
    } catch (err) {
      earnEls.btn.textContent = 'Flatten failed';
    } finally {
      earnEls.btn.disabled = false;
    }
  });

  // ---- News (this underlying only) -----------------------------------------
  function fmtAgo(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }

  function buildNewsItem(item) {
    const art = document.createElement('article');
    art.className = 'hm-news-item';

    const head = document.createElement('div');
    head.className = 'hm-n-head';
    const time = document.createElement('span');
    time.className = 'hm-n-time';
    time.textContent = fmtAgo(item.publishedAt);
    head.appendChild(time);
    art.appendChild(head);

    const body = document.createElement('p');
    body.className = 'hm-n-body';
    body.textContent = item.body;
    art.appendChild(body);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'hm-n-toggle';
    toggle.hidden = true;
    const tText = document.createElement('span');
    tText.className = 'hm-n-toggle-text';
    tText.textContent = 'Show More';
    toggle.appendChild(tText);
    toggle.addEventListener('click', () => {
      const open = art.classList.toggle('expanded');
      tText.textContent = open ? 'Show Less' : 'Show More';
    });
    art.appendChild(toggle);

    requestAnimationFrame(() => {
      toggle.hidden = !(body.scrollHeight > body.clientHeight + 1);
    });
    return art;
  }

  async function loadNews() {
    if (!els.newsList || !UNDERLYING) return;
    try {
      const data = await getJSON('/api/news');
      const items = (data.items || []).filter((i) => i.underlying === UNDERLYING);
      els.newsList.textContent = '';
      items.forEach((i) => els.newsList.appendChild(buildNewsItem(i)));
      els.newsEmpty.hidden = items.length > 0;
    } catch (err) { /* keep last news */ }
  }

  // ---- Actions ------------------------------------------------------------
  function connect() {
    if (window.MarktapeWallet) window.MarktapeWallet.connectWithPicker();
  }

  // Server keys prices/assets/holdings by UPPERCASE symbol (TSLAon -> TSLAON).
  function keyFor(sym) {
    const u = String(sym || '').toUpperCase();
    return state.prices[u] || (state.assets || {})[u] ? u : sym;
  }

  function openSwap(side, sym, onClose, pre) {
    const label = sym || SYMBOL;
    const key = keyFor(label);
    let stocks = [];
    if (IS_GROUP && state.group) {
      stocks = ['xstocks', 'ondo', 'bstocks'].map((plat) => {
        const w = (state.group.wrappers || []).find((x) => x.platform === plat);
        return { symbol: w ? keyFor(w.symbol) : '', name: w ? w.symbol : '', label: PLAT_LABEL[plat], disabled: !w || !(w.mint || w.address) };
      });
    }
    return !!(window.MarktapeSwap && window.MarktapeSwap.open({
      side, symbol: key, label, title: IS_GROUP ? UNDERLYING : label, stocks, getCtx, onDone: refreshBalances, onClose,
      connect, pay: pre && pre.pay, amount: pre && pre.amount,
    }));
  }
  els.buy.addEventListener('click', () => openSwap('buy'));
  els.sell.addEventListener('click', () => openSwap('sell'));

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) { /* fall back below */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  // Copy button: toggles a dropdown listing each stock's short CA with its own copy button.
  const shortCa = (a) => (a ? a.slice(0, 6) + '...' + a.slice(-4) : DASH);
  const COPY_IC = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>';
  const OK_IC = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5L20 7"/></svg>';

  function renderCopyMenu() {
    els.copyMenu.textContent = '';
    railList().forEach((r) => {
      const row = document.createElement('div');
      row.className = 'tk-copy-row';
      const lbl = document.createElement('b'); lbl.textContent = r.label;
      const ca = document.createElement('span'); ca.textContent = shortCa(r.mint);
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'tk-copy-one'; btn.setAttribute('aria-label', 'Copy ' + r.label + ' address');
      btn.innerHTML = COPY_IC; btn.disabled = !r.mint;
      btn.addEventListener('click', async () => {
        if (!r.mint || !(await copyText(r.mint))) return;
        btn.innerHTML = OK_IC; btn.classList.add('done');
        setTimeout(() => { btn.innerHTML = COPY_IC; btn.classList.remove('done'); }, 1200);
      });
      row.appendChild(lbl); row.appendChild(ca); row.appendChild(btn);
      els.copyMenu.appendChild(row);
    });
  }
  els.copy.addEventListener('click', () => {
    const open = els.copyMenu.hidden;
    if (open) renderCopyMenu();
    els.copyMenu.hidden = !open;
    els.copy.setAttribute('aria-expanded', String(open));
    els.copy.classList.toggle('on', open);
  });

  els.back.addEventListener('click', () => {
    let same = false;
    try { same = !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (_) {}
    if (same && history.length > 1) history.back();
    else location.href = '/';
  });

  // ---- About: clamp + Read More -------------------------------------------
  function measureAbout() {
    if (els.about.classList.contains('expanded')) return;
    els.more.hidden = !(els.aboutText.scrollHeight > els.aboutText.clientHeight + 1);
  }
  els.more.addEventListener('click', () => {
    const open = els.about.classList.toggle('expanded');
    els.more.querySelector('span').textContent = open ? 'Read Less' : 'Read More';
  });
  window.addEventListener('resize', measureAbout);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(measureAbout);

  // ---- Wallet -------------------------------------------------------------
  function setAddress(next) {
    if (next === state.address) return;
    state.address = next;
    state.holdings = null;
    render();
    tickBalances();
  }
  window.addEventListener('marktape:wallet', (e) => setAddress(e.detail.address));

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    tickPrices();
    tickBalances();
  });

  // ---- Boot ---------------------------------------------------------------
  state.address = window.MarktapeWallet ? window.MarktapeWallet.getAddress() : null;
  render();
  measureAbout();
  if (IS_GROUP) {
    loadGroup(); // resolves SYMBOL (cheapest/focused rail), then loads assets + chart
  } else {
    loadAssets();
    loadChart();
  }
  tickPrices();
  tickBalances();
  loadNews();
  setInterval(tickPrices, PRICE_MS);
  setInterval(tickBalances, BALANCE_MS);
  setInterval(loadChart, CHART_MS);
  setInterval(loadNews, 60000);
  setInterval(loadEarnings, 60000);
  if (IS_GROUP) {
    setInterval(loadArb, 30000);
    setInterval(refreshFlattenVisibility, 30000);
  }
})();
