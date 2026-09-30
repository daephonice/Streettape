/* Gap desk (/). Three wrapper columns per underlying: Tape / Official / Fair,
 * plus a dominant session state. Data: /api/board (groups + session), /api/session, /api/agent/scan (desk line + rotate).
 * Fair only shows while cash is shut, mirroring agent.official_vs_fair_line(). */
(function () {
  const REFRESH_MS = 45000;
  const SESS_MS = 30000;
  const ORDER = ['xstocks', 'ondo', 'bstocks'];
  const SESS_CLASS = { 'CASH OPEN': 'sess-open', 'PRE-MARKET': 'sess-pre', 'AFTER-HOURS': 'sess-ah', 'WEEKEND': 'sess-we' };
  const COPY_OPEN = 'Cash is open. Official mark is live, so tape and official should agree.';
  const COPY_SHUT = 'Cash is shut. Official is the last print; Fair is the synthetic reference for where the underlying should trade now. Fair explains why a wrapper is rich; the rotate stays wrapper vs wrapper.';

  const $ = (id) => document.getElementById(id);
  const groupsEl = $('bd-groups');
  const searchEl = $('bd-search');
  let lastSnap = null;
  let session = null;
  let arbMap = {};   // underlying -> viable net arb (from /api/agent/scan)

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

  function row(label, value, cls) {
    const r = el('div', 'bd-r' + (cls ? ' ' + cls : ''));
    r.appendChild(el('span', null, label));
    r.appendChild(el('b', null, value));
    return r;
  }

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
    w.appendChild(row('Tape', t.hasTape ? money(t.tokenPrice) : '—'));
    w.appendChild(row('Official', money(t.markPrice)));
    if (showFair) w.appendChild(row('Fair', money(t.fairPrice), 'bd-r-fair'));
    const badges = el('div', 'bd-badges');
    if (t.thin) {
      const tb = el('span', 'bd-thin', 'thin pool');
      tb.title = 'Onchain pool liquidity is ' + (t.liquidityUsd != null ? '$' + Math.round(t.liquidityUsd).toLocaleString() : 'very low') + '; this price is stale and not a real gap.';
      badges.appendChild(tb);
    } else if (t.hasTape) {
      badges.appendChild(el('span', 'bd-bl', 'vs off'));
      badges.appendChild(badge(t.premiumToOfficial));
      if (showFair) {
        badges.appendChild(el('span', 'bd-bl', 'vs fair'));
        badges.appendChild(badge(t.premiumToFair));
      }
    }
    w.appendChild(badges);
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
    const fw = ws.find((t) => !cashOpen && !g.noYahoo && g.markPrice && t.fairPrice);
    if (fw) {
      const pc = (v) => ((v || 0) * 100 >= 0 ? '+' : '') + ((v || 0) * 100).toFixed(2) + '%';
      art.appendChild(el('p', 'bd-fair-in',
        'Fair (synthetic) = last print × (1 + β × QQQ move) × (1 + news) · β ' + (g.beta || 0).toFixed(2) +
        ' · QQQ ' + pc(g.indexMove) + ' since Fri close · news ' + (((g.newsShock || 0) * 100) >= 0 ? '+' : '') + Math.round((g.newsShock || 0) * 100) + '%'));
    }
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
    } catch (err) {
      console.warn('board refresh failed', err);
    }
  }

  // ---- Desk line (same payload as GET /api/agent/scan) ---------------------
  const bps = (v) => Math.round(v) + ' bps';
  function renderDesk(scan) {
    const line = $('bd-desk-line'), btn = $('bd-desk-btn');
    if (!line || !btn) return;
    const arbs = scan.arbs || [];
    arbMap = {};
    arbs.forEach((a) => { if (a.viable) arbMap[a.underlying] = a; });
    const best = scan.best;
    line.textContent = '';
    if (best) {
      line.className = 'bd-desk-line';
      line.appendChild(el('b', 'pos', best.underlying + ' +' + bps(best.netBps) + ' net'));
      line.appendChild(document.createTextNode(
        ' · sell ' + best.richSymbol + ' → buy ' + best.cheapSymbol +
        ' · gross ' + bps(best.grossBps) + ' − cost ' + bps(best.costBps)));
      if (!(session && session.cashOpen) && best.gapVsFair !== null && best.gapVsFair !== undefined) {
        const pc = (v) => (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
        line.appendChild(document.createTextNode(
          ' · ' + best.richSymbol + ' vs official ' + pc(best.gapVsOfficial) + ' · vs fair ' + pc(best.gapVsFair)));
      }
      btn.hidden = false;
      btn.href = '/t/' + encodeURIComponent(best.underlying) + '#rotate';
      btn.textContent = 'Rotate $' + Math.round(best.sizeUsd || 50);
    } else {
      line.className = 'bd-desk-line dim';
      const top = arbs[0];
      line.textContent = top
        ? 'No viable rotation. Best gap ' + top.underlying + ' gross ' + bps(top.grossBps) + ' does not clear ~' + bps(top.costBps) + ' cost.'
        : 'No viable rotation. No cross-wrapper gap above 1% right now.';
      btn.hidden = true;
    }
    renderBoard();
  }
  async function refreshDesk() {
    if (document.hidden) return;
    try {
      const resp = await fetch('/api/agent/scan', { cache: 'no-store' });
      if (!resp.ok) return;
      renderDesk(await resp.json());
    } catch (err) {
      console.warn('desk refresh failed', err);
    }
  }

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
    if (copy) copy.textContent = s.cashOpen ? COPY_OPEN : COPY_SHUT;
    tickSub();
    // Cash open/shut flips which columns exist (Fair), so redraw from the cached snapshot.
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
  refreshDesk();
  setInterval(refreshBoard, REFRESH_MS);
  setInterval(refreshDesk, REFRESH_MS);
  setInterval(refreshSession, SESS_MS);
  setInterval(tickSub, 60000);
})();
