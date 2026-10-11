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
  const COPY_SHUT = 'The cash market freezes. The token keeps trading. Rotate the rich one into the cheap one.';
  async function refreshNow() {
    try {
      const r = await fetch('/', { cache: 'no-store' });
      if (!r.ok) return;
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const fresh = doc.getElementById('bd-now'), cur = $('bd-now');
      if (fresh && cur) { cur.innerHTML = fresh.innerHTML; fillNums(); }
    } catch (err) { console.warn('now block refresh failed', err); }
  }

  const USDT = '0x55d398326f99059fF775485246999027B3197955';
  const $ = (id) => document.getElementById(id);
  const groupsEl = $('bd-groups');
  const searchEl = $('bd-search');
  let lastSnap = null;
  let session = null;
  let bookNames = []; // underlyings in Weekend Book order
  let arbMap = {};   // underlying -> Clears book row (from /api/agent/scan)
  let bookMap = {};  // underlying -> any book row (clears or refused)

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

  const usd2 = (v) => '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pm = (p, title) => { const n = el('span', 'hp-pm ' + tone(p), fmtPremium(p)); if (title) n.title = title; return n; };

  // ---- Board (compact rows) ------------------------------------------------
  function orderWrappers(list) {
    const rank = (w) => { const i = ORDER.indexOf(w.platform); return i === -1 ? ORDER.length : i; };
    return list.slice().sort((a, b) => rank(a) - rank(b));
  }

  function buildRow(g, t, cashOpen, fairCol) {
    const split = g.cheapest && g.richest && g.cheapest !== g.richest && !t.thin;
    const row = el('div', 'hp-row' + (split && t.symbol === g.cheapest ? ' is-cheap' : split && t.symbol === g.richest ? ' is-rich' : ''));
    row.dataset.symbol = t.symbol;
    row.title = (t.platformLabel || t.platform || '') +
      (t.tokenToShareRatio ? ' \u00b7 tokenToShareRatio ' + t.tokenToShareRatio : '') +
      ' \u00b7 cash ' + (t.cashState && t.cashState.open ? 'open' : 'shut') +
      (t.cashState && t.cashState.code ? ' \u00b7 feed ' + t.cashState.code : '');

    const sym = el('div', 'hp-c hp-sym');
    sym.appendChild(el('b', null, t.symbol));
    if (t.tokenToShareRatio) sym.appendChild(el('small', null, Number(t.tokenToShareRatio).toFixed(4) + ' sh'));
    row.appendChild(sym);

    const tape = el('div', 'hp-c');
    tape.appendChild(el('span', 'hp-v', t.hasTape ? money(t.tokenPrice) : '\u2014'));
    if (t.hasTape) {
      if (t.thin) {
        const tp = el('small', 'hp-mu', 'thin pool');
        tp.title = 'Stale price, not a real gap.' + (t.liquidityUsd != null ? ' Liquidity ' + usd2(t.liquidityUsd) : '');
        tape.appendChild(tp);
      } else tape.appendChild(pm(t.premiumToOfficial, 'vs official'));
      if (t.cheapVsPrint && !cashOpen) {
        const m = el('i', 'hp-tag', 'cheap vs print');
        m.title = 'Cheap versus the last print. This is the weekend exposure a floor would insure. StreetTape does not sell the insurance. It shows the gap and the cheaper wrapper.';
        tape.appendChild(m);
      }
    }
    row.appendChild(tape);

    const off = el('div', 'hp-c');
    off.appendChild(el('span', 'hp-v', money(t.markPrice)));
    if (t.markPrice != null) {
      if (t.markSource === 'binance') off.appendChild(el('small', 'hp-mu', 'Binance'));
      else { const lp = el('small', 'hp-mu', 'last print'); lp.title = 'Last cash print. RWA price endpoint returned no mark on this deploy.'; off.appendChild(lp); }
    }
    row.appendChild(off);

    if (fairCol) {
      const f = el('div', 'hp-c');
      if (t.fairPrice) {
        f.appendChild(el('span', 'hp-v hp-fair', money(t.fairPrice)));
        if (t.hasTape) f.appendChild(pm(t.premiumToFair, 'vs fair'));
      } else f.appendChild(el('span', 'hp-v hp-mu', '\u2014'));
      row.appendChild(f);
    }

    if (t.hasTape) {
      const rot = arbMap[g.underlying];
      const a = el('a', 'hp-tb', rot ? 'Rotate' : 'Trade');
      a.href = rot
        ? '/t/' + encodeURIComponent(g.underlying) + '#rotate'
        : '/t/' + encodeURIComponent(t.symbol) + '#swap';
      row.appendChild(a);
    } else {
      const o = el('span', 'hp-tb off', 'no tape');
      o.setAttribute('aria-disabled', 'true');
      row.appendChild(o);
    }
    return row;
  }

  function buildGroup(g, cashOpen) {
    const ws = orderWrappers(g.wrappers || []);
    const fairCol = !cashOpen && !g.noYahoo && !!g.markPrice && ws.some((t) => t.fairPrice);
    const art = el('article', 'hp-board' + (fairCol ? '' : ' hp-nf'));
    art.dataset.underlying = g.underlying;
    art.dataset.name = (g.name || '').toLowerCase();
    const head = el('header', 'hp-board-h');
    const title = el('a', 'hp-board-t');
    title.href = '/t/' + encodeURIComponent(g.underlying);
    title.appendChild(el('b', null, g.underlying));
    title.appendChild(el('span', null, g.name));
    head.appendChild(title);
    if (g.crossSpread !== null && g.crossSpread !== undefined) {
      const sp = el('span', 'hp-spread', 'x-wrapper ' + fmtPremium(g.crossSpread));
      sp.title = 'Richest wrapper vs cheapest';
      head.appendChild(sp);
    }
    art.appendChild(head);
    const hd = el('div', 'hp-row hp-head');
    ['', 'Tape', 'Official'].concat(fairCol ? ['Fair'] : [], ['']).forEach((c) => hd.appendChild(el('span', null, c)));
    art.appendChild(hd);
    ws.forEach((t) => art.appendChild(buildRow(g, t, cashOpen, fairCol)));
    return art;
  }

  function renderBoard() {
    if (!groupsEl || !lastSnap) return;
    const cashOpen = !!(session && session.cashOpen);
    const groups = lastSnap.groups || [];
    groupsEl.textContent = '';
    if (!groups.length) {
      groupsEl.appendChild(el('p', 'hp-empty', 'Tape is warming up. First print any moment.'));
      return;
    }
    groups.forEach((g) => groupsEl.appendChild(buildGroup(g, cashOpen)));
    applySearch(searchEl ? searchEl.value : '');
  }

  function renderLast() {
    const box = $('bd-last'), rows = $('bd-last-rows'), empty = $('bd-last-empty'), sum = $('bd-last-sum');
    if (!box || !rows || !empty) return;
    const list = (lastSnap && lastSnap.lastSession) || [];
    box.hidden = !(session && session.cashOpen);
    rows.textContent = '';
    list.forEach((r) => rows.appendChild(el('p', 'hp-last-row', r.line)));
    empty.hidden = list.length > 0;
    if (sum) sum.textContent = list.length ? list.length + ' rows' : 'none yet';
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
  const sgn = (v) => (v > 0 ? '+' : '') + Math.round(v) + ' bps';
  function bookRow(r) {
    const clears = r.state === 'clears';
    const row = el('div', 'hp-bk-row ' + (clears ? 'is-clears' : 'is-refused'));
    const n = el('div', 'hp-bk-n');
    const t = el('a', null, r.underlying);
    t.href = '/t/' + encodeURIComponent(r.underlying);
    n.appendChild(t);
    n.appendChild(el('span', 'hp-badge ' + (clears ? 'up' : 'down'), clears ? 'CLEARS' : 'REFUSED'));
    row.appendChild(n);
    const d = el('div', 'hp-bk-d');
    d.appendChild(el('p', null, 'Sell ' + r.richSymbol + ' ' + money(r.richSharePrice) + ' \u2192 buy ' + r.cheapSymbol + ' ' + money(r.cheapSharePrice) + ' per share'));
    if (clears) {
      const l = el('p', null, 'gap ' + bps(r.gapBps) + ' \u00b7 cost ' + bps(r.costBps) + ' \u00b7 net ');
      l.appendChild(el('span', 'up', '+' + bps(r.netBps)));
      l.appendChild(document.createTextNode(' at $' + Math.round(r.sizeUsd || 50)));
      d.appendChild(l);
    } else {
      d.appendChild(el('p', null, 'gap ' + bps(r.gapBps) + ' \u00b7 ' +
        (r.costBps === null || r.costBps === undefined ? r.reason : 'cost ' + bps(r.costBps)) + ' \u00b7 refused'));
    }
    row.appendChild(d);
    if (clears) {
      const a = el('a', 'hp-go', 'Rotate');
      a.href = '/t/' + encodeURIComponent(r.underlying) + '#rotate';
      row.appendChild(a);
    } else row.appendChild(el('span', 'hp-go off', '\u2014'));
    return row;
  }
  // Hero gap card numbers: the same Weekend-book row as the card's underlying.
  function fillNums() {
    const box = document.querySelector('#bd-now .hp-nums');
    if (!box) return;
    const r = bookMap[box.dataset.u];
    const set = (id, v, cls) => { const n = $(id); if (!n) return; n.textContent = v; n.parentElement.className = cls || ''; };
    if (!r) return;
    const net = r.netBps != null ? r.netBps : (r.gapBps != null && r.costBps != null ? r.gapBps - r.costBps : null);
    set('gp-gap', r.gapBps == null ? '\u2014' : sgn(r.gapBps), r.gapBps > 0 ? 'up' : r.gapBps < 0 ? 'down' : '');
    set('gp-cost', r.costBps == null ? '\u2014' : bps(r.costBps), '');
    set('gp-net', net == null ? '\u2014' : sgn(net), net > 0 ? 'up' : net < 0 ? 'down' : '');
  }
  function renderBook(scan) {
    const rows = scan.book || [];
    arbMap = {}; bookMap = {};
    bookNames = rows.map((r) => r.underlying);
    rows.forEach((r) => { bookMap[r.underlying] = r; if (r.state === 'clears') arbMap[r.underlying] = r; });
    const box = $('bd-book-rows'), empty = $('bd-book-empty'), sum = $('bd-book-sum');
    if (box && empty) {
      box.textContent = '';
      rows.forEach((r) => box.appendChild(bookRow(r)));
      empty.textContent = 'No name has two live ratio-backed wrapper tapes right now.';
      empty.hidden = rows.length > 0;
    }
    if (sum) {
      const c = rows.filter((r) => r.state === 'clears').length;
      sum.textContent = '';
      if (rows.length) {
        sum.appendChild(el('span', 'hp-s-ok', c + ' clear'));
        sum.appendChild(document.createTextNode(' \u00b7 '));
        sum.appendChild(el('span', 'hp-s-no', (rows.length - c) + ' refused'));
      } else sum.textContent = 'none live';
    }
    fillNums();
    renderBoard();
  }
  async function refreshBook() {
    if (document.hidden) return;
    const empty = $('bd-book-empty');
    const say = (t) => { if (empty) { empty.textContent = t; empty.hidden = false; } };
    if (!bookNames.length) say('Pricing routes…');
    try {
      const resp = await fetch('/api/agent/scan', { cache: 'no-store' });
      if (!resp.ok) return say('Book failed: HTTP ' + resp.status);
      renderBook(await resp.json());
    } catch (err) {
      console.warn('book refresh failed', err);
      say('Book failed: ' + (err && err.message ? err.message : err));
    }
  }

  // ---- Command chips (closed grammar; each chip is one existing endpoint, the panel prints the result) ----
  const out = $('bd-cmd-out');
  const chips = document.querySelectorAll('.hp-tool');
  const pct = (v) => (v === null || v === undefined || !isFinite(v) ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(2) + '%');
  const num = (v, d) => (v === null || v === undefined || !isFinite(v) ? '—' : Number(v).toFixed(d));
  const NOTE = 'Not auto-executed. Confirm and sign in your own wallet.';

  function line(text, cls) { return el('p', 'hp-o-line' + (cls ? ' ' + cls : ''), text); }
  function show(title, nodes, sessLabel) {
    out.textContent = '';
    const h = el('div', 'hp-o-head');
    h.appendChild(el('b', null, title));
    if (sessLabel) h.appendChild(el('span', 'hp-o-sess', sessLabel));
    out.appendChild(h);
    nodes.forEach((n) => out.appendChild(n));
    out.appendChild(el('p', 'hp-o-note', NOTE));
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
    const tbl = el('div', 'hp-o-tbl');
    const hd = el('div', 'hp-o-row hp-o-th');
    ['symbol', 'tape', 'official', 'fair', 'premium'].forEach((c) => hd.appendChild(el('span', null, c)));
    tbl.appendChild(hd);
    toks.forEach((t) => {
      const r = el('div', 'hp-o-row');
      r.appendChild(el('span', null, t.symbol));
      r.appendChild(el('span', null, money(t.tokenPrice)));
      r.appendChild(el('span', null, money(t.markPrice)));
      r.appendChild(el('span', null, money(t.fairPrice)));
      r.appendChild(el('span', 'hp-o-p ' + tone(t.premium), fmtPremium(t.premium)));
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
    if (!a.viable) {
      const noRt = (l) => !l || l.noRoute || l.uiOutAmount == null;
      const why = (l) => String((l && l.fallbackReason) || '').replace(/\s+/g, ' ').trim().slice(0, 220);
      if (noRt(a.sellLeg)) {
        n.push(line('No route selling ' + a.richSymbol + '. Refused.', 'hp-o-warn'));
        if (why(a.sellLeg)) n.push(line(why(a.sellLeg), 'dim'));
      } else if (noRt(a.buyLeg)) {
        n.push(line('No route buying ' + a.cheapSymbol + '. Refused.', 'hp-o-warn'));
        if (why(a.buyLeg)) n.push(line(why(a.buyLeg), 'dim'));
      } else {
        n.push(line('Costs eat the gap.', 'hp-o-warn'));
      }
    }
    n.push(el('a', 'hp-go', 'Open rotate sheet'));
    n[n.length - 1].href = '/t/' + encodeURIComponent(u) + '#rotate';
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

  // ---- Live defensive panel (GET /api/agent/defensive) -----------------------
  const defEl = $('bd-def');
  let defBusy = false;
  function renderDefensive(d) {
    defEl.textContent = '';
    const trig = !!d.triggered;
    const fold = $('bd-def-fold'), sum = $('bd-def-sum');
    if (fold) fold.classList.toggle('is-trig', trig);
    if (sum) sum.textContent = trig ? 'triggered' : 'not triggered';
    const mv = d.qqqMove;
    const mvTxt = mv == null ? d.qqqSymbol + ' no tape on both sides of the Friday close'
      : d.qqqSymbol + ' ' + (mv > 0 ? '+' : '') + Math.round(mv * 10000) + ' bps since ' + sinceET(d.since);
    const subTxt = 'Threshold \u00b1' + Math.round(d.threshold * 10000) + ' bps'
      + (trig ? ' \u00b7 viable ' + d.viable : ' \u00b7 ' + (d.reason || 'move under threshold') + ' \u00b7 no quotes');
    if (!trig) {
      defEl.appendChild(el('p', 'hp-mute', mvTxt + '. ' + subTxt + '.'));
      return;
    }
    const box = el('div', 'hp-alert');
    box.appendChild(el('p', 'hp-alert-h', 'TRIGGERED \u00b7 ' + mvTxt));
    box.appendChild(el('p', 'hp-alert-s', subTxt));
    ((d.sell || {}).legs || []).forEach((l) => box.appendChild(l.filled
      ? legLine('Sell ' + l.symbol + ' $' + num(l.amountUsd, 2), l)
      : line('Sell ' + l.underlying + ' unfilled: ' + (l.reason || 'no route'), 'dim')));
    const b = d.buy || {};
    box.appendChild(b.filled
      ? legLine('Buy ' + (b.symbol || d.qqqSymbol) + ' $' + num(b.amountUsd, 2), b)
      : line('Buy ' + (b.underlying || 'QQQ') + ' unfilled: ' + (b.reason || 'no route'), 'dim'));
    if (d.priceImpactPct != null) box.appendChild(line('Worst leg impact ' + num(d.priceImpactPct, 4), 'dim'));
    defEl.appendChild(box);
  }
  async function refreshDefensive() {
    if (!defEl || document.hidden || defBusy) return;
    defBusy = true;
    try { renderDefensive(await getJSON('/api/agent/defensive?usd=50')); } catch (e) {
      const sum = $('bd-def-sum'); if (sum) sum.textContent = 'unavailable';
      defEl.textContent = ''; defEl.appendChild(el('p', 'hp-mute', 'State unavailable (' + e.message + ')'));
    } finally { defBusy = false; }
  }

  const CMDS = { rich: cmdRich, rotate: cmdRotate, defensive: cmdDefensive, flatten: cmdFlatten };
  function closeOut() {
    chips.forEach((x) => { x.classList.remove('is-on'); x.setAttribute('aria-expanded', 'false'); });
    out.hidden = true;
    out.textContent = '';
  }
  let rotShown = 0;
  chips.forEach((c) => c.setAttribute('aria-expanded', 'false'));
  chips.forEach((c) => c.addEventListener('click', async () => {
    const wasOn = c.classList.contains('is-on');
    if (wasOn && c.dataset.cmd !== 'rotate') return closeOut();
    if (wasOn && c.dataset.cmd === 'rotate' && rotShown >= Math.max(1, rotCycle().length)) {
      rotShown = 0; rotRan = false; rotTicker = rotCycle()[0] || 'NVDA';
      const t = $('bd-rot-t'); if (t) t.textContent = rotTicker;
      return closeOut();
    }
    if (c.dataset.cmd === 'rotate') rotShown += 1;
    chips.forEach((x) => { const on = x === c; x.classList.toggle('is-on', on); x.setAttribute('aria-expanded', on ? 'true' : 'false'); });
    out.hidden = false;
    out.textContent = '';
    out.appendChild(line('Running…', 'dim'));
    try { await CMDS[c.dataset.cmd](); } catch (e) { out.textContent = ''; out.appendChild(line('Request failed: ' + e.message, 'hp-o-warn')); }
  }));

  // ---- Search --------------------------------------------------------------
  function applySearch(raw) {
    const q = (raw || '').trim().toLowerCase();
    const cards = document.querySelectorAll('.hp-board');
    let visible = 0;
    cards.forEach((c) => {
      const match = !q || c.dataset.underlying.toLowerCase().includes(q) || c.dataset.name.includes(q)
        || Array.from(c.querySelectorAll('.hp-row[data-symbol]')).some((w) => w.dataset.symbol.toLowerCase().includes(q));
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
    if (rerender !== false && prevOpen !== null && prevOpen !== !!s.cashOpen) { renderBoard(); refreshNow(); }
  }
  async function refreshSession() {
    if (document.hidden) return;
    try {
      const resp = await fetch('/api/clock', { cache: 'no-store' });
      if (!resp.ok) return;
      applySession(await resp.json());
    } catch (err) {
      console.warn('session refresh failed', err);
    }
  }

  // ---- Panels (closed by default) -------------------------------------------
  document.querySelectorAll('.hp-fold-h').forEach((h) => h.addEventListener('click', () => {
    const open = h.parentElement.classList.toggle('open');
    h.setAttribute('aria-expanded', open ? 'true' : 'false');
  }));

  // ---- Boot ----------------------------------------------------------------
  const hero0 = $('bd-hero');
  if (hero0) session = { cashOpen: hero0.dataset.cashOpen === '1', label: ($('sess-label') || {}).textContent || '' };
  refreshSession();
  refreshBoard();
  refreshBook();
  refreshDefensive();
  setInterval(refreshDefensive, REFRESH_MS);
  setInterval(refreshBoard, REFRESH_MS);
  setInterval(refreshBook, REFRESH_MS);
  setInterval(refreshSession, SESS_MS);
  setInterval(tickSub, 60000);
})();
