/* Gap desk (/). Three wrapper columns per underlying: Tape / Official / Fair,
 * plus a dominant session state. Data: /api/board (groups + session), /api/session, /api/agent/scan (weekend book + rotate).
 * Fair only shows while cash is shut, mirroring agent.official_vs_fair_line(). */
(function () {
  const REFRESH_MS = 45000;
  const SESS_MS = 30000;
  const ORDER = ['xstocks', 'ondo', 'bstocks'];
  const SESS_CLASS = { 'CASH OPEN': 'sess-open', 'PRE-MARKET': 'sess-pre', 'AFTER-HOURS': 'sess-ah', 'WEEKEND': 'sess-we' };
  const copyOpen = () => {
    const w = lastSnap && lastSnap.bookWidest;
    return 'Cash is open, so tape and official agree. ' + (w ? 'Widest gap while cash was shut: ' + w.line + '. ' : 'No shut-session tape stored yet. ');
  };
  const COPY_SHUT = 'Cash is shut. Official is the last print; Fair is the synthetic reference for where the underlying should trade now. Fair explains why a wrapper is rich; the rotate stays wrapper vs wrapper.';

  const USDT = '0x55d398326f99059fF775485246999027B3197955';
  const $ = (id) => document.getElementById(id);
  const groupsEl = $('bd-groups');
  const searchEl = $('bd-search');
  let lastSnap = null;
  let session = null;
  let bookNames = []; // underlyings in Weekend Book order
  let arbMap = {};   // underlying -> Clears book row (from /api/agent/scan)

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  const money = (v) => (v === null || v === undefined || !isFinite(v) ? '—' : '$' + Number(v).toFixed(2));

  function fmtPremium(p) {
    if (p === null || p === undefined || !isFinite(p)) return '—';
    const pct = p * 100;
    return (pct > 0 ? '+' : '') + pct.toFixed(1) + '%';
  }
  const tone = (p) => (p > 0 ? 'pos' : p < 0 ? 'neg' : 'flat');

  function badge(p) {
    return el('span', 'bd-badge ' + tone(p), fmtPremium(p));
  }

  function px(label, value, cls, src) {
    const r = el('div', 'bd-px' + (cls ? ' ' + cls : ''));
    r.appendChild(el('span', 'bd-px-l', label));
    r.appendChild(el('b', null, value));
    if (src) r.appendChild(el('small', 'bd-px-src bd-src-' + src.key, src.text));
    return r;
  }
  const usd2 = (v) => '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // ---- Cards ---------------------------------------------------------------
  function orderWrappers(list) {
    const rank = (w) => { const i = ORDER.indexOf(w.platform); return i === -1 ? ORDER.length : i; };
    return list.slice().sort((a, b) => rank(a) - rank(b));
  }

  function buildWrapper(g, t, cashOpen) {
    const showFair = !cashOpen && !g.noYahoo && !!g.markPrice && !!t.fairPrice;
    const split = g.cheapest && g.richest && g.cheapest !== g.richest && !t.thin;
    const w = el('div', 'bd-w' + (split && t.symbol === g.cheapest ? ' is-cheap' : split && t.symbol === g.richest ? ' is-rich' : ''));
    w.dataset.symbol = t.symbol;
    const top = el('div', 'bd-w-top');
    top.appendChild(el('span', 'bd-w-sym', t.symbol));
    w.appendChild(top);
    const SRC = { binance: 'From Binance', yahoo: 'From Yahoo', 'last-print': 'From Yahoo' };
    const src = t.markSource && SRC[t.markSource] && t.markPrice != null ? { key: t.markSource, text: SRC[t.markSource] } : null;
    const pxs = el('div', 'bd-pxs');
    pxs.appendChild(px('Tape', t.hasTape ? money(t.tokenPrice) : '—'));
    pxs.appendChild(px('Official', money(t.markPrice), '', src));
    if (showFair) pxs.appendChild(px('Fair', money(t.fairPrice), 'bd-px-fair'));
    w.appendChild(pxs);
    if (t.hasTape) {
      const badges = el('div', 'bd-badges');
      badges.appendChild(el('span', 'bd-bl', 'vs off'));
      badges.appendChild(badge(t.premiumToOfficial));
      if (showFair) {
        badges.appendChild(el('span', 'bd-bl', 'vs fair'));
        badges.appendChild(badge(t.premiumToFair));
      }
      if (t.thin) {
        const tb = el('span', 'bd-thin', 'thin pool' + (t.liquidityUsd != null ? ' · ' + usd2(t.liquidityUsd) : ''));
        tb.title = 'Stale price, not a real gap.';
        badges.appendChild(tb);
      }
      w.appendChild(badges);
    }
    if (t.hasTape) {
      const rot = arbMap[g.underlying];
      const a = el('a', 'bd-trade', rot ? 'Rotate' : 'Trade');
      a.href = rot
        ? '/t/' + encodeURIComponent(g.underlying) + '#rotate'
        : '/t/' + encodeURIComponent(t.symbol) + '#swap';
      w.appendChild(a);
    } else {
      const s = el('span', 'bd-trade bd-trade-off', 'no tape');
      s.setAttribute('aria-disabled', 'true');
      w.appendChild(s);
    }
    return w;
  }

  function buildGroup(g, cashOpen) {
    const art = el('article', 'hm-card bd-group');
    art.dataset.underlying = g.underlying;
    art.dataset.name = (g.name || '').toLowerCase();
    const head = el('header', 'bd-g-head');
    const title = el('a', 'bd-g-title');
    title.href = '/t/' + encodeURIComponent(g.underlying);
    title.appendChild(el('b', null, g.underlying));
    title.appendChild(el('span', null, g.name));
    head.appendChild(title);
    if (g.crossSpread !== null && g.crossSpread !== undefined) {
      const sp = el('span', 'bd-g-spread', 'x-wrapper ' + fmtPremium(g.crossSpread));
      sp.title = 'Richest wrapper vs cheapest';
      head.appendChild(sp);
    }
    art.appendChild(head);
    const cols = el('div', 'bd-cols');
    const ws = orderWrappers(g.wrappers || []);
    cols.style.setProperty('--n', String(ws.length || 1));
    ws.forEach((t) => cols.appendChild(buildWrapper(g, t, cashOpen)));
    art.appendChild(cols);
    return art;
  }

  function renderBoard() {
    if (!groupsEl || !lastSnap) return;
    const cashOpen = !!(session && session.cashOpen);
    const groups = lastSnap.groups || [];
    groupsEl.textContent = '';
    if (!groups.length) {
      groupsEl.appendChild(el('p', 'bd-empty', 'Tape is warming up. First print any moment.'));
      return;
    }
    groups.forEach((g) => groupsEl.appendChild(buildGroup(g, cashOpen)));
    applySearch(searchEl ? searchEl.value : '');
  }

  function renderLast() {
    const box = $('bd-last'), rows = $('bd-last-rows'), empty = $('bd-last-empty');
    if (!box || !rows || !empty) return;
    const list = (lastSnap && lastSnap.lastSession) || [];
    box.hidden = !(session && session.cashOpen);
    rows.textContent = '';
    list.forEach((r) => rows.appendChild(el('p', 'bd-last-row', r.line)));
    empty.hidden = list.length > 0;
  }

  async function refreshBoard() {
    if (document.hidden) return;
    try {
      const resp = await fetch('/api/board', { cache: 'no-store' });
      if (!resp.ok) return;
      lastSnap = await resp.json();
      if (lastSnap.session) applySession(lastSnap.session, false);
      const stale = $('bd-hero-stale');
      if (stale) stale.hidden = !lastSnap.tapeStale;
      renderBoard();
      renderLast();
    } catch (err) {
      console.warn('board refresh failed', err);
    }
  }

  // ---- Weekend book (same payload as GET /api/agent/scan) ------------------
  const bps = (v) => Math.round(v) + ' bps';
  function bookRow(r) {
    const clears = r.state === 'clears';
    const row = el('div', 'bd-bk-row ' + (clears ? 'is-clears' : 'is-refused'));
    const head = el('div', 'bd-bk-head');
    const t = el('a', 'bd-bk-name', r.underlying);
    t.href = '/t/' + encodeURIComponent(r.underlying);
    head.appendChild(t);
    head.appendChild(el('span', 'bd-bk-state', clears ? 'Clears' : 'Refused'));
    row.appendChild(head);
    row.appendChild(el('p', 'bd-bk-legs',
      'Sell ' + r.richSymbol + ' ' + money(r.richSharePrice) + ' → buy ' + r.cheapSymbol + ' ' + money(r.cheapSharePrice) + ' per share'));
    if (clears) {
      row.appendChild(el('p', 'bd-bk-line', 'gap ' + bps(r.gapBps) + ' · cost ' + bps(r.costBps) + ' · net +' + bps(r.netBps) + ' at $' + Math.round(r.sizeUsd || 50)));
      const a = el('a', 'bd-bk-btn', 'Rotate $' + Math.round(r.sizeUsd || 50));
      a.href = '/t/' + encodeURIComponent(r.underlying) + '#rotate';
      row.appendChild(a);
    } else {
      row.appendChild(el('p', 'bd-bk-line dim', 'gap ' + bps(r.gapBps) + ' · ' +
        (r.costBps === null || r.costBps === undefined ? r.reason : 'cost ' + bps(r.costBps)) + ' · refused'));
    }
    return row;
  }
  function renderBook(scan) {
    const rows = scan.book || [];
    arbMap = {};
    bookNames = rows.map((r) => r.underlying);
    rows.forEach((r) => { if (r.state === 'clears') arbMap[r.underlying] = r; });
    const box = $('bd-book-rows'), empty = $('bd-book-empty');
    if (box && empty) {
      box.textContent = '';
      rows.forEach((r) => box.appendChild(bookRow(r)));
      empty.textContent = 'No name has two live ratio-backed wrapper tapes right now.';
      empty.hidden = rows.length > 0;
    }
    renderBoard();
  }
  async function refreshBook() {
    if (document.hidden) return;
    try {
      const resp = await fetch('/api/agent/scan', { cache: 'no-store' });
      if (!resp.ok) return;
      renderBook(await resp.json());
    } catch (err) {
      console.warn('book refresh failed', err);
    }
  }

  // ---- Command chips (closed grammar; each chip is one existing endpoint, the panel prints the result) ----
  const out = $('bd-cmd-out');
  const chips = document.querySelectorAll('.bd-chip');
  const pct = (v) => (v === null || v === undefined || !isFinite(v) ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(2) + '%');
  const num = (v, d) => (v === null || v === undefined || !isFinite(v) ? '—' : Number(v).toFixed(d));
  const NOTE = 'Not auto-executed. Confirm and sign in your own wallet.';

  function line(text, cls) { return el('p', 'bd-o-line' + (cls ? ' ' + cls : ''), text); }
  function show(title, nodes, sessLabel) {
    out.textContent = '';
    const h = el('div', 'bd-o-head');
    h.appendChild(el('b', null, title));
    if (sessLabel) h.appendChild(el('span', 'bd-o-sess', sessLabel));
    out.appendChild(h);
    nodes.forEach((n) => out.appendChild(n));
    out.appendChild(el('p', 'bd-o-note', NOTE));
    out.hidden = false;
  }
  async function getJSON(url, opts) {
    const r = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  function legLine(label, l) {
    if (!l) return line(label + ' —', 'dim');
    if (l.noRoute || l.uiOutAmount == null) return line(label + ' no route', 'dim');
    return line(label + ' out ' + num(l.uiOutAmount, 4) + ' · ' + (l.routeLabel || l.provider || '') + ' · impact ' + (l.priceImpactPct == null ? '—' : l.priceImpactPct));
  }

  async function cmdRich() {
    const snap = await getJSON('/api/board');
    const toks = (snap.tokens || []).filter((t) => t.hasTape && t.premium != null)
      .sort((a, b) => Math.abs(b.premium) - Math.abs(a.premium)).slice(0, 12);
    const tbl = el('div', 'bd-o-tbl');
    const hd = el('div', 'bd-o-row bd-o-th');
    ['symbol', 'tape', 'official', 'fair', 'premium'].forEach((c) => hd.appendChild(el('span', null, c)));
    tbl.appendChild(hd);
    toks.forEach((t) => {
      const r = el('div', 'bd-o-row');
      r.appendChild(el('span', null, t.symbol));
      r.appendChild(el('span', null, money(t.tokenPrice)));
      r.appendChild(el('span', null, money(t.markPrice)));
      r.appendChild(el('span', null, money(t.fairPrice)));
      r.appendChild(el('span', 'bd-o-p ' + tone(t.premium), fmtPremium(t.premium)));
      tbl.appendChild(r);
    });
    show("what's rich vs Friday", toks.length ? [tbl] : [line('No tape yet.', 'dim')], (snap.session || {}).label);
  }

  // Rotate chip: one sentence, `rotate into cheapest {TICKER}`. First paint NVDA; each later tap advances the ticker.
  let rotTicker = 'NVDA';
  let rotRan = false;
  function rotCycle() {
    if (bookNames.length) return bookNames;
    return ((lastSnap && lastSnap.groups) || [])
      .filter((g) => (g.wrappers || []).filter((w) => w.hasTape && !w.thin).length >= 2)
      .map((g) => g.underlying);
  }
  function rotAdvance() {
    const list = rotCycle();
    if (!list.length) return;
    const i = list.indexOf(rotTicker);
    rotTicker = list[(i + 1) % list.length];
    const t = $('bd-rot-t');
    if (t) t.textContent = rotTicker;
  }

  async function cmdRotate() {
    if (rotRan) rotAdvance();
    rotRan = true;
    const u = rotTicker;
    const title = 'rotate into cheapest ' + u;
    const rep = await getJSON('/api/agent/scan?underlying=' + encodeURIComponent(u));
    const a = rep.best || (rep.arbs || [])[0];
    const sess = (rep.session || {}).label;
    if (!a) return show(title, [line('No arb: fewer than two live ratio-backed ' + u + ' wrappers.', 'dim')], sess);
    const n = [
      line('Sell ' + a.richSymbol + ' → buy ' + a.cheapSymbol + ' · $' + Math.round(a.sizeUsd || 50)),
      line('viable ' + a.viable + ' · netBps ' + num(a.netBps, 1) + ' · gross ' + num(a.grossBps, 1) + ' · cost ' + num(a.costBps, 1), a.viable ? '' : 'dim'),
      line(a.richSymbol + ' ratio ' + num(a.richRatio, 6) + ' · ' + a.cheapSymbol + ' ratio ' + num(a.cheapRatio, 6)),
    ];
    if (!a.viable) n.push(line('Costs eat the gap.', 'bd-o-warn'));
    show(title, n, sess);
  }

  function sinceET(iso) {
    const t = iso ? new Date(iso) : null;
    if (!t || isNaN(t)) return 'Friday 16:00 ET';
    const f = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'long', hour: '2-digit', minute: '2-digit', hour12: false });
    const p = {};
    f.formatToParts(t).forEach((x) => { p[x.type] = x.value; });
    return p.weekday + ' ' + p.hour.replace('24', '00') + ':' + p.minute + ' ET';
  }

  async function cmdDefensive() {
    const d = await getJSON('/api/agent/defensive?usd=50');
    const n = [line(d.qqqSymbol + ' ' + pct(d.qqqMove) + ' since ' + sinceET(d.since) + ' · threshold ' + (d.threshold * 100).toFixed(1) + '%')];
    if (!d.triggered) {
      n.push(line('triggered false · no quotes.', 'dim'));
    } else {
      n.push(line('triggered true · viable ' + d.viable));
      ((d.sell || {}).legs || []).forEach((l) => n.push(l.filled ? legLine('Sell ' + l.symbol + ' (' + l.underlying + ')', l) : line('Sell ' + l.underlying + ' unfilled: ' + (l.reason || 'no route'), 'dim')));
      const b = d.buy || {};
      n.push(b.filled ? legLine('Buy ' + (b.symbol || d.qqqSymbol), b) : line('Buy ' + (b.underlying || 'QQQ') + ' unfilled: ' + (b.reason || 'no route'), 'dim'));
      if ((d.sell || {}).unfilledUsd) n.push(line('Unfilled weight $' + num(d.sell.unfilledUsd, 2) + ' not reassigned.', 'dim'));
    }
    show('rotate into defensives when volatility spikes', n, (d.session || {}).label);
  }

  async function cmdFlatten() {
    const T = 0.02;
    const rep = await getJSON('/api/agent/scan?threshold=' + T);
    const sess = rep.session || {};
    if (sess.cashOpen) return show('flatten anything richer than 2% while cash is shut', [line('Cash is open.', 'dim')], sess.label);
    const rich = (rep.hits || []).filter((h) => (h.premium || 0) > T).sort((a, b) => b.premium - a.premium);
    if (!rich.length) return show('flatten anything richer than 2% while cash is shut', [line('Nothing richer than 2%.', 'dim')], sess.label);
    const snap = lastSnap || await getJSON('/api/board');
    const bySym = {};
    (snap.tokens || []).forEach((t) => { bySym[t.symbol] = t; });
    const n = [];
    for (const h of rich) {
      const t = bySym[h.symbol];
      if (!t || !t.mint || !t.tokenPrice) { n.push(line('Sell ' + h.symbol + ' ' + fmtPremium(h.premium) + ' · no quote', 'dim')); continue; }
      try {
        const q = await getJSON('/api/swap/order', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ inputMint: t.mint, outputMint: USDT, uiAmount: 50 / t.tokenPrice }),
        });
        n.push(legLine('Sell ' + h.symbol + ' ' + fmtPremium(h.premium) + ' $50', q));
      } catch (e) {
        n.push(line('Sell ' + h.symbol + ' ' + fmtPremium(h.premium) + ' · quote failed (' + e.message + ')', 'dim'));
      }
    }
    show('flatten anything richer than 2% while cash is shut', n, sess.label);
  }

  const CMDS = { rich: cmdRich, rotate: cmdRotate, defensive: cmdDefensive, flatten: cmdFlatten };
  chips.forEach((c) => c.addEventListener('click', async () => {
    chips.forEach((x) => x.classList.toggle('is-on', x === c));
    out.hidden = false;
    out.textContent = '';
    out.appendChild(line('Running…', 'dim'));
    try { await CMDS[c.dataset.cmd](); } catch (e) { out.textContent = ''; out.appendChild(line('Request failed: ' + e.message, 'bd-o-warn')); }
  }));

  // ---- Search --------------------------------------------------------------
  function applySearch(raw) {
    const q = (raw || '').trim().toLowerCase();
    const cards = document.querySelectorAll('.bd-group');
    let visible = 0;
    cards.forEach((c) => {
      const match = !q || c.dataset.underlying.toLowerCase().includes(q) || c.dataset.name.includes(q)
        || Array.from(c.querySelectorAll('.bd-w')).some((w) => w.dataset.symbol.toLowerCase().includes(q));
      c.hidden = !match;
      if (match) visible += 1;
    });
    const none = $('bd-no-results');
    if (none) none.hidden = visible !== 0 || cards.length === 0;
  }
  if (searchEl) searchEl.addEventListener('input', (e) => applySearch(e.target.value));

  // ---- Session (dominant chip) ---------------------------------------------
  function fmtCountdown(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h > 0 ? h + 'h' + (m ? ' ' + m + 'm' : '') : m + 'm';
  }
  function tickSub() {
    const sub = $('sess-sub');
    const s = session;
    if (!sub || !s) return;
    if (s.cashOpen && typeof s.nyCloseInSec === 'number') {
      sub.textContent = 'Close in ' + fmtCountdown(s.nyCloseInSec) + ' · Lagos ' + s.wat + ' · New York ' + s.et;
      s.nyCloseInSec = Math.max(0, s.nyCloseInSec - 60);
    } else if (s.nyCloseAtWat) {
      sub.textContent = 'Lagos ' + s.wat + ' · New York ' + s.et + ' · NY close ' + s.nyCloseAtWat + ' WAT';
    } else {
      sub.textContent = s.wat ? 'Lagos ' + s.wat + ' · New York ' + s.et : '';
    }
  }
  function applySession(s, rerender) {
    const prevOpen = session ? !!session.cashOpen : null;
    session = s;
    const chip = $('sess-chip'), label = $('sess-label'), hero = $('bd-hero'), copy = $('bd-hero-copy');
    if (label) label.textContent = s.label;
    if (chip) chip.className = 'sess-chip ' + (SESS_CLASS[s.label] || 'sess-we');
    if (hero) hero.dataset.cashOpen = s.cashOpen ? '1' : '0';
    if (copy) {
      copy.textContent = s.cashOpen ? copyOpen() : COPY_SHUT;
      if (s.cashOpen) { const a = el('a', null, 'Session book'); a.href = '/session'; copy.appendChild(a); }
    }
    tickSub();
    // Cash open/shut flips which columns exist (Fair), so redraw from the cached snapshot.
    const bk = $('bd-book'); if (bk) bk.hidden = !!s.cashOpen;
    renderLast();
    if (rerender !== false && prevOpen !== null && prevOpen !== !!s.cashOpen) renderBoard();
  }
  async function refreshSession() {
    if (document.hidden) return;
    try {
      const resp = await fetch('/api/session', { cache: 'no-store' });
      if (!resp.ok) return;
      applySession(await resp.json());
    } catch (err) {
      console.warn('session refresh failed', err);
    }
  }

  // ---- Boot ----------------------------------------------------------------
  const hero0 = $('bd-hero');
  if (hero0) session = { cashOpen: hero0.dataset.cashOpen === '1', label: ($('sess-label') || {}).textContent || '' };
  refreshSession();
  refreshBoard();
  refreshBook();
  setInterval(refreshBoard, REFRESH_MS);
  setInterval(refreshBook, REFRESH_MS);
  setInterval(refreshSession, SESS_MS);
  setInterval(tickSub, 60000);
})();
