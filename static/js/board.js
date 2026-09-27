(function () {
  const REFRESH_MS = 45000;

  function fmtPremium(p) {
    if (p === null || p === undefined) return '—';
    const pct = p * 100;
    const sign = pct > 0 ? '+' : '';
    return `${sign}${pct.toFixed(1)}%`;
  }

  function statusClass(p) {
    if (p === null || p === undefined) return 'status-flat';
    if (p > 0) return 'status-rich';
    if (p < 0) return 'status-cheap';
    return 'status-flat';
  }

  function money(v) {
    if (v === null || v === undefined) return '—';
    return '$' + Number(v).toFixed(2);
  }

  // ---- Sparklines --------------------------------------------------------
  function buildPolyline(points, rich) {
    if (!points || points.length < 2) return '';
    const w = 300, h = 56, pad = 4;
    const min = Math.min(...points), max = Math.max(...points);
    const range = max - min || 1;
    const step = (w - pad * 2) / (points.length - 1);
    const coords = points.map((p, i) => {
      const x = pad + i * step;
      const y = h - pad - ((p - min) / range) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    return coords.join(' ');
  }

  async function loadSparkline(svg) {
    const symbol = svg.dataset.symbol;
    try {
      const resp = await fetch(`/api/sparkline/${symbol}`);
      if (!resp.ok) return;
      const data = await resp.json();
      const pts = data.points || [];
      const el = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      if (pts.length < 2) {
        el.setAttribute('points', '0,28 300,28');
        el.setAttribute('class', 'mkt-sparkline-line status-flat-stroke');
      } else {
        const rich = pts[pts.length - 1] >= pts[0];
        const poly = buildPolyline(pts, rich);
        el.setAttribute('points', poly);
        el.setAttribute('class', `mkt-sparkline-line ${rich ? 'status-rich-stroke' : 'status-cheap-stroke'}`);
      }
      const existing = svg.querySelector('.mkt-sparkline-line');
      if (existing) existing.remove();
      el.setAttribute('fill', 'none');
      el.setAttribute('stroke-width', '1.5');
      el.setAttribute('stroke-linecap', 'round');
      el.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(el);
    } catch (err) {
      console.warn('sparkline failed for', symbol, err);
    }
  }

  function loadAllSparklines() {
    document.querySelectorAll('.mkt-sparkline').forEach(loadSparkline);
  }

  // ---- Cards --------------------------------------------------------------
  function buildCard(t) {
    const div = document.createElement('div');
    div.className = 'mkt-card fade-refresh';
    div.dataset.symbol = t.symbol;
    div.dataset.name = (t.name || '').toLowerCase();
    const pStatus = statusClass(t.premium).replace('status-', 'status-pill-');
    div.innerHTML = `
      <div class="mkt-card-top">
        ${t.image ? `<img class="mkt-card-logo" src="${t.image}" alt="" loading="lazy">` : '<span class="mkt-card-logo mkt-logo-empty"></span>'}
        <span class="mkt-card-ticker">${t.symbol}</span>
        <span class="mkt-card-premium ${pStatus}">${fmtPremium(t.premium)}</span>
      </div>
      <div class="mkt-card-tape">${money(t.tokenPrice)}</div>
      <div class="mkt-card-mark">mark ${money(t.markPrice)}</div>
      <div class="mkt-card-chart">
        <svg class="mkt-sparkline" data-symbol="${t.symbol}" viewBox="0 0 300 56" preserveAspectRatio="none"></svg>
      </div>
      ${t.hasTape
        ? `<a href="/t/${t.symbol}#swap" class="mkt-trade-btn">Trade</a>`
        : `<span class="mkt-trade-btn mkt-trade-btn-disabled" aria-disabled="true">— no tape</span>`}
    `;
    return div;
  }

  async function refreshBoard() {
    try {
      const resp = await fetch('/api/board');
      if (!resp.ok) return;
      const snap = await resp.json();
      const grid = document.getElementById('mkt-cards');
      if (grid && snap.tokens) {
        const query = (document.getElementById('mkt-search') || {}).value || '';
        grid.innerHTML = '';
        snap.tokens.forEach((t) => grid.appendChild(buildCard(t)));
        applySearch(query);
        loadAllSparklines();
      }
    } catch (err) {
      console.warn('board refresh failed', err);
    }
  }

  // ---- Search ---------------------------------------------------------
  function applySearch(rawQuery) {
    const query = (rawQuery || '').trim().toLowerCase();
    const cards = document.querySelectorAll('.mkt-card');
    let visible = 0;
    cards.forEach((card) => {
      const match = !query || card.dataset.symbol.toLowerCase().includes(query) || card.dataset.name.includes(query);
      card.hidden = !match;
      if (match) visible += 1;
    });
    const noResults = document.getElementById('mkt-no-results');
    if (noResults) noResults.hidden = visible !== 0 || cards.length === 0;
  }

  const searchInput = document.getElementById('mkt-search');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => applySearch(e.target.value));
  }

  // ---- Stat strip (AUM / Volume / Holders / Txns) -------------------------
  function fmtCompact(v, prefix) {
    if (v === null || v === undefined || !isFinite(v)) return null;
    const n = Number(v);
    const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (const [div, suffix] of units) {
      if (Math.abs(n) >= div) {
        return prefix + (n / div).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + suffix;
      }
    }
    return prefix + Math.round(n).toLocaleString('en-US');
  }

  function setStat(key, text) {
    if (text === null) return; // keep the last good value on screen
    const el = document.querySelector(`[data-stat="${key}"] .mkt-stat-value`);
    if (el && el.textContent !== text) el.textContent = text;
  }

  async function refreshStats() {
    try {
      const resp = await fetch('/api/market-stats');
      if (!resp.ok) return;
      const s = await resp.json();
      setStat('aum', fmtCompact(s.aum, '$'));
      setStat('volume', fmtCompact(s.volume, '$'));
      setStat('holders', fmtCompact(s.holders, ''));
      setStat('txns', fmtCompact(s.txns, ''));
    } catch (err) {
      console.warn('stats refresh failed', err);
    }
  }

  setInterval(() => { refreshBoard(); refreshStats(); }, REFRESH_MS);

  // ---- Session chip --------------------------------------------------------
  const SESS_MS = 30000;
  const SESS_CLASS = { 'CASH OPEN': 'sess-open', 'PRE-MARKET': 'sess-pre', 'AFTER-HOURS': 'sess-ah', 'WEEKEND': 'sess-we' };
  let lastSession = null;
  function fmtCountdown(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h${m ? ' ' + m + 'm' : ''}` : `${m}m`;
  }
  function tickSessionSub() {
    const sub = document.getElementById('sess-sub');
    const s = lastSession;
    if (!sub || !s) return;
    if (s.cashOpen && typeof s.nyCloseInSec === 'number') {
      sub.textContent = `Close in ${fmtCountdown(s.nyCloseInSec)} · ${s.wat} WAT`;
      s.nyCloseInSec = Math.max(0, s.nyCloseInSec - 1);
    } else if (s.nyCloseAtWat) {
      sub.textContent = `NY close ${s.nyCloseAtWat} WAT`;
    } else {
      sub.textContent = s.wat ? `${s.wat} WAT` : '';
    }
  }
  async function refreshSession() {
    const chip = document.getElementById('sess-chip');
    const label = document.getElementById('sess-label');
    if (!chip) return;
    try {
      const resp = await fetch('/api/session');
      if (!resp.ok) return;
      const s = await resp.json();
      label.textContent = s.label;
      chip.className = 'sess-chip ' + (SESS_CLASS[s.label] || 'sess-we');
      lastSession = s;
      tickSessionSub();
    } catch (err) {
      console.warn('session refresh failed', err);
    }
  }
  refreshSession();
  setInterval(refreshSession, SESS_MS);
  setInterval(tickSessionSub, 60000);

  loadAllSparklines();
})();
