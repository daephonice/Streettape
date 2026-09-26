/* Token page (/t/SYMBOL). Same data sources as the homepage:
 *   /api/token/{symbol}   group data (mark, three tapes, cheapest/richest rail) for RWA names
 *   /api/prices           shared price cache (polled every second)
 *   /api/balances/{addr}  wallet holdings
 *   /api/chart/{symbol}   price history for the chart
 * Send opens send.js on this page; Buy / Sell open swap.js (frontend only for now).
 * With no holdings only the Buy button shows.
 *
 * Group pages (data-kind="group", e.g. /t/NVDA or the legacy /t/NVDAx): the traded
 * SYMBOL is the cheapest rail (auto-picked, or the wrapper the URL/legacy link named
 * if you want a specific one focused) and Lend only shows when that rail is a Venus
 * bStock wrapper.
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
    price: $('tk-price'), chg: $('tk-chg'), chgAbs: $('tk-chg-abs'), chgPct: $('tk-chg-pct'),
    stats: $('tk-stats'), mc: $('tk-mc'), mark: $('tk-mark'), prem: $('tk-prem'),
    plot: $('tk-plot'), axis: $('tk-axis'), noHist: $('tk-nohist'), ranges: $('tk-ranges'),
    pos: $('tk-pos'), posVal: $('tk-pos-val'), posAmt: $('tk-pos-amt'), posDelta: $('tk-pos-delta'), posPct: $('tk-pos-pct'), posPnl: $('tk-pos-pnl'),
    bar: $('tk-bar'), send: $('tk-send'), sell: $('tk-sell'), buy: $('tk-buy'), lend: $('tk-lend'),
    back: $('tk-back'), share: $('tk-share'), mint: $('tk-mint'), mintText: $('tk-mint-text'),
    about: $('tk-about'), aboutText: $('tk-about-text'), more: $('tk-readmore'),
    logo: $('tk-logo'), symText: $('tk-sym-text'), aboutName: $('tk-about-name'), urlLink: $('tk-url-link'), urlText: $('tk-url-text'),
    tapeRow: $('tk-tape-row'),
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

  function render() {
    const p = state.prices[SYMBOL];

    els.price.textContent = p ? fmtPrice(p.price) : '—';
    const info = p ? pct(p.change24h) : null;
    els.chg.hidden = !info;
    if (info) {
      els.chgAbs.textContent = fmtDelta(p.price - open24h(p));
      setTone(els.chg, 'tk-chg', info.cls);
      els.chgPct.textContent = info.text;
      setTone(els.chgPct, 'tk-pill', info.cls);
    }
    if (els.stats) {
      const show = !!p;
      els.stats.hidden = !show;
      if (show) {
        els.mc.textContent = p.mc ? fmtCompact(p.mc) : '—';
        if (p.noYahoo) {
          els.mark.textContent = 'No Yahoo mark. Tape only.';
          els.mark.className = 'tk-empty-note';
        } else {
          els.mark.textContent = p.mark ? fmtPrice(p.mark) : '—';
          els.mark.className = '';
        }
        if (p.noYahoo) {
          els.prem.textContent = '—';
          els.prem.className = '';
        } else if (p.premium === null || p.premium === undefined || !isFinite(p.premium)) {
          els.prem.textContent = '—';
          els.prem.className = '';
        } else {
          const pctv = Number((p.premium * 100).toFixed(1));
          const tiny = Math.abs(pctv) < 0.3;
          if (tiny && state.session && state.session.cashOpen) {
            els.prem.textContent = 'Gap usually prints after 16:00 ET.';
            els.prem.className = 'tk-empty-note';
          } else {
            els.prem.textContent = (pctv > 0 ? '+' : '') + pctv + '%';
            els.prem.className = pctv > 0 ? 'pos' : pctv < 0 ? 'neg' : 'flat';
          }
        }
      }
    }

    // Portfolio card + bottom bar
    const amount = state.address && state.holdings ? state.holdings[SYMBOL] || 0 : 0;
    const held = amount > 0 && !!p;
    els.pos.hidden = !held;
    els.send.hidden = els.sell.hidden = !held;
    els.bar.classList.toggle('only-buy', !held);
    if (held) {
      els.posVal.textContent = fmtUsd(amount * p.price);
      const assetMeta = state.assets[SYMBOL] || {};
      const m = assetMeta.multiplier || null;
      const note = sharesNote(amount, m);
      els.posAmt.textContent = note || `${fmtAmount(amount)} ${SYMBOL}`;
      const delta = amount * (p.price - open24h(p)); // 24h move of the position
      const pi = pct(p.change24h);
      els.posDelta.textContent = fmtDelta(delta);
      els.posPct.textContent = pi ? pi.text : '';
      setTone(els.posPnl, 'tk-pos-pnl', pi ? pi.cls : 'flat');
    }
    drawChart();
  }

  // ---- Chart --------------------------------------------------------------
  const NS = 'http://www.w3.org/2000/svg';
  const W = 360;
  const H = 230;
  const PAD_T = 16;
  const PAD_B = 16;

  function svgEl(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    Object.keys(attrs).forEach((k) => n.setAttribute(k, attrs[k]));
    return n;
  }

  // Multi-series palette: cash mark + three wrapper platforms
  const SERIES_COLORS = {
    mark:    '#94a3b8', // slate — cash/reference line
    xstocks: '#60a5fa', // blue
    ondo:    '#a78bfa', // purple
    bstocks: '#34d399', // green
  };

  // Map wrapper symbol → platform using the group data
  function _wrapperPlatform(sym) {
    if (!state.group) return null;
    const w = (state.group.wrappers || []).find((x) => x.symbol === sym);
    return w ? w.platform : null;
  }

  function drawChart() {
    els.plot.textContent = '';

    // ---- Multi-series mode (group page after first fetch) ----
    if (IS_GROUP && state.multiChart) {
      const mc = state.multiChart;
      const hasMark = (mc.mark || []).length >= 2;
      const wrapperEntries = Object.entries(mc.wrappers || {}).filter(([, pts]) => pts.length >= 2);
      const hasAny = hasMark || wrapperEntries.length > 0;

      els.noHist.hidden = hasAny;
      els.axis.hidden = !hasAny;
      if (!hasAny) {
        els.noHist.hidden = false;
        els.noHist.textContent = 'Tape history starts after first refresh.';
        return;
      }

      // Collect all values for global min/max
      const allVals = [];
      if (hasMark) mc.mark.forEach(([, v]) => allVals.push(v));
      wrapperEntries.forEach(([, pts]) => pts.forEach(([, v]) => allVals.push(v)));

      const min = Math.min(...allVals);
      const max = Math.max(...allVals);
      const span = max - min || Math.abs(max) * 0.001 || 1;

      // Shared x-axis: use wall-clock timestamps mapped to [0,W]
      const allTs = [];
      if (hasMark) mc.mark.forEach(([t]) => allTs.push(t));
      wrapperEntries.forEach(([, pts]) => pts.forEach(([t]) => allTs.push(t)));
      const minTs = Math.min(...allTs);
      const maxTs = Math.max(...allTs);
      const tsSpan = maxTs - minTs || 1;

      const xTs = (t) => ((t - minTs) / tsSpan) * W;
      const y   = (v) => PAD_T + (H - PAD_T - PAD_B) * (1 - (v - min) / span);

      function pathD(pts) {
        return pts.map(([t, v], i) => `${i ? 'L' : 'M'}${xTs(t).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
      }

      [0, 0.5, 1].forEach((f) => {
        const gy = PAD_T + (H - PAD_T - PAD_B) * f;
        els.plot.appendChild(svgEl('line', { class: 'tk-grid', x1: 0, x2: W, y1: gy, y2: gy }));
      });

      // Draw wrappers first (underneath mark)
      wrapperEntries.forEach(([sym, pts]) => {
        const plat = _wrapperPlatform(sym) || 'xstocks';
        const color = SERIES_COLORS[plat] || UP;
        els.plot.appendChild(svgEl('path', { d: pathD(pts), class: 'tk-line tk-line-wrapper', stroke: color, 'stroke-opacity': '0.75' }));
      });

      // Draw cash mark on top with gradient fill
      if (hasMark) {
        const color = SERIES_COLORS.mark;
        const line = pathD(mc.mark);
        const defs = svgEl('defs', {});
        const grad = svgEl('linearGradient', { id: 'tk-grad', x1: 0, y1: 0, x2: 0, y2: 1 });
        grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.18 }));
        grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }));
        defs.appendChild(grad);
        els.plot.appendChild(defs);
        const lastMark = mc.mark[mc.mark.length - 1];
        els.plot.appendChild(svgEl('path', { d: `${line} L${xTs(lastMark[0]).toFixed(1)},${H} L${xTs(mc.mark[0][0]).toFixed(1)},${H} Z`, fill: 'url(#tk-grad)' }));
        els.plot.appendChild(svgEl('path', { d: line, class: 'tk-line', stroke: color }));
      }

      const labels = els.axis.children;
      labels[0].textContent = fmtPrice(max);
      labels[1].textContent = fmtPrice((max + min) / 2);
      labels[2].textContent = fmtPrice(min);
      return;
    }

    // ---- Single-series mode (crypto / individual wrapper) ----
    const vals = state.points.map((pt) => pt[1]);
    const live = state.prices[SYMBOL];
    if (live && vals.length) vals.push(live.price);
    els.noHist.hidden = vals.length >= 2;
    els.axis.hidden = vals.length < 2;
    if (vals.length < 2) return;

    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || Math.abs(max) * 0.001 || 1;
    const x = (i) => (i / (vals.length - 1)) * W;
    const y = (v) => PAD_T + (H - PAD_T - PAD_B) * (1 - (v - min) / span);
    const color = vals[vals.length - 1] >= vals[0] ? UP : DOWN;

    [0, 0.5, 1].forEach((f) => {
      const gy = PAD_T + (H - PAD_T - PAD_B) * f;
      els.plot.appendChild(svgEl('line', { class: 'tk-grid', x1: 0, x2: W, y1: gy, y2: gy }));
    });

    const line = vals.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    const defs = svgEl('defs', {});
    const grad = svgEl('linearGradient', { id: 'tk-grad', x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.22 }));
    grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }));
    defs.appendChild(grad);
    els.plot.appendChild(defs);
    els.plot.appendChild(svgEl('path', { d: `${line} L${W},${H} L0,${H} Z`, fill: 'url(#tk-grad)' }));
    els.plot.appendChild(svgEl('path', { d: line, class: 'tk-line', stroke: color }));

    const labels = els.axis.children;
    labels[0].textContent = fmtPrice(max);
    labels[1].textContent = fmtPrice((max + min) / 2);
    labels[2].textContent = fmtPrice(min);
  }

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

  // ---- Group (RWA name page: mark + three tapes + cheapest/Lend rail) -----
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
          const note = document.createElement('span'); note.className = 'hm-tape-note'; note.textContent = 'tape delayed';
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
    });
  }

  function applyGroup(g) {
    state.group = g;
    // Trade the cheapest rail unless the URL named a specific wrapper (e.g. legacy /t/NVDAx).
    const focusW = g.wrappers.find((w) => w.symbol === FOCUS);
    const cheapW = g.wrappers.find((w) => w.symbol === g.cheapest);
    const tradeW = focusW || cheapW || g.wrappers[0];
    if (!tradeW) return;
    SYMBOL = tradeW.symbol;
    page.dataset.symbol = SYMBOL;

    if (els.symText) els.symText.textContent = UNDERLYING;
    if (els.aboutName) els.aboutName.textContent = g.name;
    if (els.aboutText && !els.aboutText.textContent) {
      els.aboutText.textContent = `${g.name} is a BNB Chain tokenized equity. Economic exposure only — no ownership, voting or other legal rights.`;
    }
    if (els.logo) {
      const src = g.wrappers.map((w) => w.image).find(Boolean);
      if (src && els.logo.tagName === 'IMG') els.logo.src = src;
    }
    if (els.mint) els.mint.dataset.mint = tradeW.mint || '';
    if (els.mintText && tradeW.mint) els.mintText.textContent = tradeW.mint.slice(0, 4) + '...' + tradeW.mint.slice(-4);
    if (els.share && tradeW.mint) els.share.dataset.link = `https://pancakeswap.finance/swap?chain=bsc&outputCurrency=${tradeW.mint}`;
    if (els.urlLink && tradeW.url) {
      els.urlLink.href = tradeW.url;
      els.urlLink.hidden = false;
      if (els.urlText) els.urlText.textContent = tradeW.url.replace(/^https?:\/\//, '').replace(/\/$/, '');
    }

    // Lend only if the traded rail is a Venus bStock wrapper.
    if (els.lend) {
      const canLend = tradeW.platform === 'bstocks';
      els.lend.hidden = !canLend;
      els.lend.href = canLend ? `/lend/${encodeURIComponent(tradeW.symbol)}` : '/lend';
      els.bar.classList.toggle('can-lend', canLend);
    }

    renderTapeRow(g);
    render();
    loadChart();
    loadAssets();
  }

  async function loadGroup() {
    try {
      const g = await getJSON(`/api/token/${encodeURIComponent(UNDERLYING)}`);
      applyGroup(g);
    } catch (err) {
      setTimeout(loadGroup, 3000);
    }
  }

  // ---- Actions ------------------------------------------------------------
  function connect() {
    if (window.MarktapeWallet) window.MarktapeWallet.connectWithPicker();
  }

  els.send.addEventListener('click', () => {
    if (!state.address || !window.MarktapeSend) return;
    window.MarktapeSend.open({ symbol: SYMBOL, getCtx, onSent: refreshBalances });
  });

  function openSwap(side) {
    if (!state.address) { connect(); return; }
    if (window.MarktapeSwap) window.MarktapeSwap.open({ side, symbol: SYMBOL, getCtx, onDone: refreshBalances });
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

  // Share = copy the PancakeSwap link; the icon turns into "Copied" for 2s.
  let shareTimer = null;
  els.share.addEventListener('click', async () => {
    if (els.share.classList.contains('done')) return;
    if (!(await copyText(els.share.dataset.link))) return;
    els.share.classList.add('done');
    clearTimeout(shareTimer);
    shareTimer = setTimeout(() => els.share.classList.remove('done'), 2000);
  });

  let mintTimer = null;
  const mintShort = els.mintText.textContent;
  els.mint.addEventListener('click', async () => {
    if (!(await copyText(els.mint.dataset.mint))) return;
    els.mintText.textContent = 'Copied';
    clearTimeout(mintTimer);
    mintTimer = setTimeout(() => { els.mintText.textContent = mintShort; }, 1200);
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
  setInterval(tickPrices, PRICE_MS);
  setInterval(tickBalances, BALANCE_MS);
  setInterval(loadChart, CHART_MS);
})();
