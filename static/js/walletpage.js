/* /wallet. /api/prices + /api/assets + /api/balances/{addr} + /api/clock. Send is wallet-gated. */
(function () {
  const LIST_MAX = 5;
  const STK_MAX = 4;
  const PLAT_LBL = { xstocks: 'x', ondo: 'Ondo', bstocks: 'b' };
  const PLAT = { xstocks: 'xStocks', ondo: 'Ondo', bstocks: 'bStocks' };
  const SESS = { 'CASH OPEN': 'sess-open', 'PRE-MARKET': 'sess-pre', 'AFTER-HOURS': 'sess-ah', 'WEEKEND': 'sess-we' };
  const $ = (id) => document.getElementById(id);
  const el = {
    total: $('wl-total'), chg: $('wl-chg'), chgD: $('wl-chg-d'), chgR: $('wl-chg-r'),
    actions: $('wl-actions'), send: $('wl-send'), swap: $('wl-swap'),
    hold: $('wl-hold'), empty: $('wl-empty'), all: $('wl-all'),
    stk: $('wl-stk'), stkEmpty: $('wl-stk-empty'), stkAll: $('wl-stk-all'),
    chip: $('sess-chip'), label: $('sess-label'), sub: $('sess-sub'),
  };
  const st = { address: null, holdings: null, prices: {}, assets: {}, open: false, groups: [], groupsLoaded: false, stkOpen: false };
  const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });

  const fmtUsd = (v) => (v > 0 && v < 0.005 ? '<$0.01' : usd.format(v));
  const fmtPrice = (v) => (v >= 1 ? usd.format(v) : '$' + v.toFixed(4));
  const fmtAmt = (v) => v.toLocaleString('en-US', { maximumFractionDigits: v >= 1000 ? 2 : v >= 1 ? 4 : 6 });
  const fmtDelta = (v) => {
    const a = Math.abs(v);
    return (v < 0 ? '\u2212' : v > 0 ? '+' : '') + '$' + a.toFixed(a > 0 && a < 1 ? 3 : 2);
  };
  function pct(p, d) {
    if (p === null || p === undefined || !isFinite(p)) return null;
    const r = Number(p.toFixed(d === undefined ? 1 : d));
    return { text: (r > 0 ? '+' : r < 0 ? '\u2212' : '') + Math.abs(r) + '%', cls: r > 0 ? 'up' : r < 0 ? 'down' : 'flat' };
  }
  function ago(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    return s < 3600 ? Math.max(1, Math.floor(s / 60)) + 'm' : s < 86400 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd';
  }
  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function portfolio() {
    const rows = [];
    let total = 0, prev = 0;
    Object.entries(st.holdings || {}).forEach(([sym, amount]) => {
      const p = st.prices[sym];
      if (!p || !(amount > 0)) return;
      const value = amount * p.price;
      const open = p.change24h == null ? p.price : p.price / (1 + p.change24h / 100);
      total += value; prev += amount * open;
      rows.push({ symbol: sym, amount, price: p.price, change: p.change24h, value });
    });
    rows.sort((a, b) => b.value - a.value);
    return { rows, total, delta: total - prev, deltaPct: prev > 0 ? (total / prev - 1) * 100 : 0 };
  }

  function renderBalance() {
    const on = !!st.address;
    el.actions.classList.toggle('off', !on);
    [el.send, el.swap].forEach((n) => (on ? n.removeAttribute('aria-disabled') : n.setAttribute('aria-disabled', 'true')));
    if (!on) {
      el.total.textContent = '$0.00';
      el.chgD.textContent = '$0.00';
      el.chgR.textContent = '1D · 0%';
      el.chg.className = 'wl-chg flat';
      return;
    }
    if (st.holdings === null || !Object.keys(st.prices).length) { el.total.textContent = '\u2014'; return; }
    const pf = portfolio();
    const info = pct(pf.deltaPct);
    el.total.textContent = usd.format(pf.total);
    el.chgD.textContent = fmtDelta(pf.delta);
    el.chgR.textContent = '1D · ' + (info ? info.text : '0%');
    el.chg.className = 'wl-chg ' + (info ? info.cls : 'flat');
  }

  function holdRow(r) {
    const a = st.assets[r.symbol] || {};
    const row = h('a', 'wl-row');
    row.href = '/t/' + encodeURIComponent(r.symbol);
    const l = h('div');
    l.appendChild(h('div', 'wl-sym', r.symbol));
    l.appendChild(h('div', 'wl-sub', (PLAT[a.platform] || (a.kind === 'stock' ? a.platform : 'BNB Chain') || 'BNB Chain') + ' · ' + fmtAmt(r.amount)));
    const m = h('div', 'wl-amt');
    m.appendChild(h('div', 'wl-u', fmtUsd(r.value)));
    m.appendChild(h('div', 'wl-d', fmtPrice(r.price)));
    const info = pct(r.change);
    const p = h('div', 'wl-pnl ' + (info ? info.cls : 'flat'), info ? info.text : '\u2014');
    row.append(l, m, p);
    return row;
  }

  function renderHoldings() {
    const on = !!st.address;
    const loading = on && st.holdings === null;
    const rows = on && st.holdings ? portfolio().rows : [];
    const shown = st.open ? rows : rows.slice(0, LIST_MAX);
    el.hold.textContent = '';
    shown.forEach((r) => el.hold.appendChild(holdRow(r)));
    el.hold.hidden = shown.length === 0;
    el.empty.hidden = shown.length > 0;
    el.empty.textContent = !on ? 'Connect wallet to see holdings.' : loading ? 'Loading\u2026' : 'No holdings yet.';
    el.all.hidden = rows.length <= LIST_MAX;
    el.all.textContent = st.open ? 'View less' : 'View all';
  }

  const perShare = (w) => (w && w.tokenPrice && w.tokenToShareRatio ? w.tokenPrice / w.tokenToShareRatio : null);

  function wrapCard(plat, w) {
    const c = h('div', 'wl-w');
    c.appendChild(h('div', 'wl-wl', PLAT_LBL[plat]));
    const ps = perShare(w);
    if (w && w.tokenPrice) {
      c.appendChild(h('div', 'wl-wv', fmtPrice(ps !== null ? ps : w.tokenPrice) + (ps !== null ? ' /sh' : '')));
      const info = pct(w.premium == null ? null : w.premium * 100);
      c.appendChild(h('div', 'wl-wp ' + (info ? info.cls : 'flat'), info ? info.text : '\u2014'));
    } else {
      c.classList.add('off');
      c.appendChild(h('div', 'wl-wv', '\u2014'));
      c.appendChild(h('div', 'wl-wr', 'No tape'));
    }
    return c;
  }

  function stockCard(g) {
    const card = h('div', 'wl-sc');
    const head = h('a', 'wl-sh');
    head.href = '/t/' + encodeURIComponent(g.underlying);
    const l = h('div');
    l.appendChild(h('div', 'wl-sym', g.underlying));
    if (g.name) l.appendChild(h('div', 'wl-sub', g.name));
    const noYahoo = (g.wrappers || []).every((w) => w.noYahoo);
    head.append(l, h('div', 'wl-px', noYahoo ? 'Tape only' : g.markPrice ? fmtPrice(g.markPrice) : '\u2014'));
    card.appendChild(head);
    const grid = h('div', 'wl-wg');
    const list = ['xstocks', 'ondo', 'bstocks'].map((plat) => ({ plat, w: (g.wrappers || []).find((x) => x.platform === plat) }));
    list.forEach(({ plat, w }) => grid.appendChild(wrapCard(plat, w)));
    card.appendChild(grid);
    return card;
  }

  function renderStocks() {
    const all = st.groups;
    const rows = st.stkOpen ? all : all.slice(0, STK_MAX);
    el.stk.textContent = '';
    rows.forEach((g) => el.stk.appendChild(stockCard(g)));
    el.stk.hidden = rows.length === 0;
    el.stkEmpty.hidden = rows.length > 0;
    el.stkEmpty.textContent = st.groupsLoaded ? 'No stocks available.' : 'Loading\u2026';
    el.stkAll.hidden = all.length <= STK_MAX;
    el.stkAll.textContent = st.stkOpen ? 'View less' : 'View all';
  }

  async function tickBoard() {
    if (document.hidden) return;
    try {
      const d = await getJSON('/api/board');
      st.groups = d.groups || [];
      st.groupsLoaded = true;
      renderStocks();
      if (d.session) renderSession(d.session);
    } catch (e) { /* keep last */ }
  }

  function renderSession(s) {
    if (!s) return;
    el.label.textContent = s.label;
    el.chip.className = 'sess-chip ' + (SESS[s.label] || 'sess-we');
    el.sub.textContent = s.cashOpen && typeof s.nyCloseInSec === 'number'
      ? `Close in ${Math.floor(s.nyCloseInSec / 3600)}h ${Math.floor((s.nyCloseInSec % 3600) / 60)}m · Lagos ${s.wat} · New York ${s.et}`
      : `Lagos ${s.wat} · New York ${s.et}${s.nyCloseAtWat ? ' · close ' + s.nyCloseAtWat + ' WAT' : ''}`;
  }

  function renderAll() { renderBalance(); renderHoldings(); }

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(url + ' ' + r.status);
    return r.json();
  }

  async function tickPrices() {
    if (document.hidden) return;
    try {
      st.prices = (await getJSON('/api/prices')).prices || {};
      if (Object.keys(st.assets).length === 0 || Object.keys(st.prices).some((s) => !st.assets[s])) {
        st.assets = (await getJSON('/api/assets')).assets || st.assets;
      }
      renderAll();
    } catch (e) { /* keep last */ }
  }

  let balFor = null;
  async function tickBalances() {
    const addr = st.address;
    if (!addr || document.hidden || balFor === addr) return;
    balFor = addr;
    try {
      const d = await getJSON('/api/balances/' + encodeURIComponent(addr));
      if (addr === st.address && d && d.holdings) { st.holdings = d.holdings; renderAll(); }
    } catch (e) { /* keep last */ } finally { if (balFor === addr) balFor = null; }
  }

  async function tickClock() {
    if (document.hidden) return;
    try { renderSession(await getJSON('/api/clock')); } catch (e) { /* keep last */ }
  }

  el.send.addEventListener('click', () => {
    if (!st.address || !window.MarktapeSend) return;
    window.MarktapeSend.open({
      getCtx: () => ({ address: st.address, holdings: st.holdings || {}, prices: st.prices, assets: st.assets }),
      onSent: () => { tickBalances(); setTimeout(tickBalances, 2500); },
    });
  });
  el.swap.addEventListener('click', (e) => { if (!st.address) e.preventDefault(); });
  el.stkAll.addEventListener('click', () => { st.stkOpen = !st.stkOpen; renderStocks(); });
  el.all.addEventListener('click', () => { st.open = !st.open; renderHoldings(); });

  window.addEventListener('marktape:wallet', (e) => {
    if (e.detail.address === st.address) return;
    st.address = e.detail.address; st.holdings = null; renderAll(); tickBalances();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { tickPrices(); tickBalances(); tickClock(); tickBoard(); } });

  st.address = window.MarktapeWallet ? window.MarktapeWallet.getAddress() : null;
  renderAll(); renderStocks();
  tickBoard(); tickPrices(); tickBalances(); tickClock();
  setInterval(tickPrices, 3000);
  setInterval(tickBoard, 5000);
  setInterval(tickBalances, 6000);
  setInterval(tickClock, 30000);
})();
