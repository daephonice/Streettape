/* Buy / Sell sheets for the token page (frontend only).
 *
 *   Buy  : pay in BNB or USDC, type an amount (device keyboard), quick amounts.
 *   Sell : receive BNB or USDC, pick a % of the holding (slider / - +).
 *   Rotate: rich wrapper -> cheap wrapper of the same stock, one quote, one signature.
 *
 * A token can't be paid / received in itself, so on the BNB page only USDC is
 * offered and on the USDC page only BNB.
 *
 * Host (token.js) calls MarktapeSwap.open({ side, symbol, getCtx, onDone }) where
 * getCtx() -> { address, holdings, prices, assets } (live state).
 * On success the sheet closes and the shared blue toast (send.js) shows
 * "Swap successful".
 *
 * Non-custodial, same flow as trade.js: /api/swap/order builds the order
 * server-side (debounced while typing / while the sell % changes), the
 * connected wallet signs, /api/swap/execute relays the signed tx.
 */
(function () {
  'use strict';

  const ANIM_MS = 260;
  const DEBOUNCE_MS = 450;
  const PAY = ['BNB', 'USDC', 'USDT'];
  const LOGO = { BNB: '/static/img/logos/bnb.png', USDC: '/static/img/logos/usdc.png', USDT: '/static/img/usdt.svg' };
  const DEC = { BNB: 18, USDC: 18, USDT: 18 };
  const QUICK = { BNB: [0.01, 0.05, 0.1], USDC: [10, 50, 100], USDT: [10, 50, 100] };
  const BNB_RESERVE = 0.0002;      // kept back for network fees / new token account
  const STEP = 5;                 // - / + step for the sell percentage
  const TOKEN_DEC = 9;

  async function postJSON(url, body) {
    let resp;
    try {
      resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (_) {
      throw new Error('Network error, try again');
    }
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) {
      const detail = data && data.detail;
      const msg = typeof detail === 'string' ? detail : (detail && detail.message) || 'Something went wrong, try again';
      throw new Error(msg);
    }
    return data;
  }

  const usdFmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });

  // Truncates (never rounds up) to `dec` decimals, trailing zeros trimmed.
  function trunc(v, dec) {
    if (!(v > 0)) return '0';
    const [i, f = ''] = v.toFixed(Math.min(dec + 3, 20)).split('.');
    const frac = f.slice(0, dec).replace(/0+$/, '');
    return frac ? i + '.' + frac : i;
  }

  function fmtTok(v) {
    if (!(v > 0)) return '0';
    return trunc(v, v >= 1000 ? 2 : v >= 1 ? 4 : 8);
  }

  // Returns multiplier for a symbol from assets, or null.
  function multiplierOf(c, sym) {
    const m = (c.assets[sym] || {}).multiplier;
    return (m > 0) ? m : null;
  }

  // Convert user-typed shares to tokens (if m set), else tokens = raw number.
  // On the buy side the user always types the pay-currency amount (BNB/USDC),
  // not shares; multiplier only affects the output label on received tokens.
  // For sell side: user works in token %, so multiplier only affects the est label.
  function tokensToShares(tokens, m) {
    if (!(m > 0) || !(tokens > 0)) return null;
    return tokens * m;
  }

  function sanitize(str, maxDec) {
    str = str.replace(',', '.').replace(/[^\d.]/g, '');
    const i = str.indexOf('.');
    if (i !== -1) str = str.slice(0, i + 1) + str.slice(i + 1).replace(/\./g, '').slice(0, maxDec);
    if (str.startsWith('.')) str = '0' + str;
    return str.replace(/^0+(?=\d)/, '').slice(0, 14);
  }

  // ---- DOM ------------------------------------------------------------------
  let root = null;
  const R = {};
  let S = null; // open session, null when closed

  function build() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'swp-root';
    root.hidden = true;
    root.innerHTML = `
      <div class="swp-backdrop"></div>
      <div class="swp-sheet" role="dialog" aria-modal="true">
        <div class="snd-handle"></div>
        <h3 class="swp-title"></h3>
        <div class="swp-stk"></div>
        <div class="swp-cur"></div>
        <p class="swp-bal"></p>
        <div class="swp-buy">
          <input class="swp-input" type="text" inputmode="decimal" autocomplete="off" autocorrect="off" spellcheck="false" placeholder="0" aria-label="Amount">
          <div class="swp-quick"></div>
        </div>
        <div class="swp-sell">
          <div class="swp-quick swp-pcts" hidden>
            <button type="button" class="swp-chip" data-pct="25">25%</button>
            <button type="button" class="swp-chip" data-pct="50">50%</button>
            <button type="button" class="swp-chip" data-pct="75">75%</button>
            <button type="button" class="swp-chip" data-pct="100">MAX</button>
          </div>
          <div class="swp-stepper">
            <button type="button" class="swp-step swp-minus" aria-label="Decrease">&minus;</button>
            <div class="swp-pct"></div>
            <button type="button" class="swp-step swp-plus" aria-label="Increase">+</button>
          </div>
          <input class="swp-range" type="range" min="0" max="100" step="1" aria-label="Amount to sell">
          <div class="swp-ticks"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>
        </div>
        <button type="button" class="snd-cta swp-cta"></button>
        <p class="swp-est"></p>
        <span class="swp-mode-chip" hidden></span>
        <dl class="swp-det" hidden>
          <div><dt>Route</dt><dd data-d="route"></dd></div>
          <div><dt>Price impact</dt><dd data-d="impact"></dd></div>
          <div><dt>Fees</dt><dd data-d="fees"></dd></div>
          <div><dt>Min received</dt><dd data-d="min"></dd></div>
          <div><dt>Check proof</dt><dd data-d="proof"></dd></div>
        </dl>
        <p class="swp-note" role="alert"></p>
        <div class="swp-share" hidden></div>
        <p class="swp-proof" hidden></p>
      </div>`;
    document.body.appendChild(root);

    const q = (s) => root.querySelector(s);
    Object.assign(R, {
      backdrop: q('.swp-backdrop'), sheet: q('.swp-sheet'), title: q('.swp-title'), stk: q('.swp-stk'), cur: q('.swp-cur'),
      bal: q('.swp-bal'), buy: q('.swp-buy'), input: q('.swp-input'), quick: q('.swp-quick'),
      sell: q('.swp-sell'), pcts: q('.swp-pcts'), minus: q('.swp-minus'), plus: q('.swp-plus'), pct: q('.swp-pct'), range: q('.swp-range'),
      cta: q('.swp-cta'), est: q('.swp-est'), note: q('.swp-note'), share: q('.swp-share'), proof: q('.swp-proof'), modeChip: q('.swp-mode-chip'), det: q('.swp-det'),
      dRoute: q('[data-d="route"]'), dImpact: q('[data-d="impact"]'), dFees: q('[data-d="fees"]'), dMin: q('[data-d="min"]'), dProof: q('[data-d="proof"]'),
    });

    R.backdrop.addEventListener('click', () => { if (S && !S.busy) close(); });
    R.stk.addEventListener('click', (e) => {
      const b = e.target.closest('[data-stk]');
      if (b && S && !S.busy && !b.disabled) setStock(b.dataset.stk);
    });
    R.cur.addEventListener('click', (e) => {
      const b = e.target.closest('[data-cur]');
      if (b && S && !S.busy) setCur(b.dataset.cur);
    });
    R.quick.addEventListener('click', (e) => {
      const b = e.target.closest('[data-amt]');
      if (!b || !S || S.busy) return;
      S.raw = b.dataset.amt;
      setNote('');
      scheduleQuote();
      render();
    });
    R.input.addEventListener('input', () => {
      S.raw = sanitize(R.input.value, DEC[S.cur]);
      R.input.value = S.raw;
      setNote('');
      scheduleQuote();
      render();
    });
    R.input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !R.cta.disabled) R.cta.click(); });
    R.pcts.addEventListener('click', (e) => {
      const b = e.target.closest('[data-pct]');
      if (b && S && !S.busy) setPct(Number(b.dataset.pct));
    });
    R.range.addEventListener('input', () => setPct(Number(R.range.value)));
    R.minus.addEventListener('click', () => setPct(S.pct - STEP));
    R.plus.addEventListener('click', () => setPct(S.pct + STEP));
    R.cta.addEventListener('click', () => {
      if (S && !S.address) { const h = S.host; close(); if (h.connect) h.connect(); return; }
      doSwap();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S && !S.busy) close();
    });
    window.addEventListener('marktape:wallet', (e) => {
      if (!S) return;
      if (!S.address && e.detail.address) { S.address = e.detail.address; render(); scheduleQuote(); return; }
      if (e.detail.address !== S.address) close();
    });
    if (window.visualViewport) {
      const sync = () => {
        const vv = window.visualViewport;
        const kb = Math.round(window.innerHeight - vv.height - vv.offsetTop);
        root.style.setProperty('--swp-kb', (kb > 80 ? kb : 0) + 'px');
      };
      window.visualViewport.addEventListener('resize', sync);
      window.visualViewport.addEventListener('scroll', sync);
    }
  }

  // ---- State helpers ----------------------------------------------------------
  // Ondo assets on BSC only pair with stablecoins (live: code=40368), so no BNB leg for them.
  const isOndo = (c, sym) => (((c.assets[sym] || {}).platform) || '').toLowerCase() === 'ondo';
  // Live 2026-09-30: Ondo rejects USDC too (40368 "allowed stablecoin(s)"), so Ondo pays in USDT only.
  const allowedPay = (c, sym) => PAY.filter((s) => s !== sym && !(isOndo(c, sym) && s !== 'USDT'));
  const payOptions = () => allowedPay(S.host.getCtx(), S.symbol);
  const price = (c, sym) => (c.prices[sym] ? c.prices[sym].price : 0);

  function setNote(msg) { R.note.textContent = msg || ''; }
  const ps = (v) => '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function renderShare(sc) {
    R.share.textContent = '';
    R.share.hidden = !sc;
    if (!sc) return;
    const line = (t, cls) => { const p = document.createElement('p'); p.textContent = t; if (cls) p.className = cls; R.share.appendChild(p); };
    line(sc.perShare ? `This quote: ${ps(sc.perShare)} per share (${sc.symbol})` : `This quote: no tokens returned (${sc.symbol})`);
    if (sc.others && sc.others.length) line('Others: ' + sc.others.map((o) => `${o.symbol} ${ps(o.perShare)}`).join(' \u00b7 ') + ' per share');
    if (sc.ref) line(`vs ${sc.ref.label} ${ps(sc.ref.price)}: ${sc.ref.devPct > 0 ? '+' : ''}${sc.ref.devPct.toFixed(1)}% per share`);
    line(sc.blocked ? `Blocked: ${sc.reason}` : sc.warn ? `Warning: ${sc.reason}` : 'Check passed: output within 5% of the spend', sc.blocked ? 'neg' : '');
    line('Buys are blocked if output is worth under 95% of the spend. Rotate stays wrapper versus wrapper.', 'swp-share-note');
  }

  function setCur(cur) {
    if (cur === S.cur) return;
    S.cur = cur;
    S.raw = '';
    setNote('');
    resetQuote();
    renderCur();
    renderQuick();
    render();
    scheduleQuote();
  }

  function setStock(sym) {
    if (sym === S.symbol) return;
    S.symbol = sym;
    const c = S.host.getCtx();
    const opts = payOptions();
    if (!opts.includes(S.cur)) S.cur = opts[0];
    S.raw = '';
    setNote('');
    resetQuote();
    renderStk();
    renderCur();
    renderQuick();
    render();
    scheduleQuote();
  }

  function renderStk() {
    const list = S.host.stocks || [];
    R.stk.hidden = list.length < 2;
    R.stk.textContent = '';
    list.forEach((st) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.stk = st.symbol;
      b.disabled = !!st.disabled;
      b.className = 'swp-stk-opt' + (st.symbol === S.symbol ? ' active' : '');
      const t = document.createElement('b'); t.textContent = st.label;
      const n = document.createElement('small'); n.textContent = st.name || st.symbol;
      b.appendChild(t); b.appendChild(n);
      R.stk.appendChild(b);
    });
  }

  function setPct(v) {
    if (S.busy) return;
    S.pct = Math.max(0, Math.min(100, Math.round(v)));
    setNote('');
    scheduleQuote();
    render();
  }

  // ---- Quote ------------------------------------------------------------------
  function resetQuote() {
    if (!S) return;
    S.order = null;
    if (R.share) renderShare(null);
    S.quoting = false;
    clearTimeout(S.quoteTimer);
    S.quoteReq = (S.quoteReq || 0) + 1;
  }

  function scheduleQuote() {
    if (!S) return;
    S.order = null;
    if (R.share) renderShare(null);
    clearTimeout(S.quoteTimer);
    const amt = S.side === 'buy' ? parseFloat(S.raw) || 0 : sellCalcAmount();
    if (!S.address || !(amt > 0)) { S.quoting = false; return; }
    S.quoting = true;
    S.quoteTimer = setTimeout(fetchQuote, DEBOUNCE_MS);
  }

  function sellCalcAmount() {
    if (!S) return 0;
    const c = S.host.getCtx();
    const bal = c.holdings[S.symbol] || 0;
    return S.pct >= 100 ? bal : Math.floor(((bal * S.pct) / 100) * 10 ** TOKEN_DEC) / 10 ** TOKEN_DEC;
  }

  async function fetchQuote() {
    if (!S) return;
    const sess = S;
    const reqId = ++sess.quoteReq;
    const c = sess.host.getCtx();
    const buying = sess.side === 'buy';
    const rotating = sess.side === 'rotate';
    const inSym = buying ? sess.cur : sess.symbol;
    const outSym = buying ? sess.symbol : rotating ? sess.out : sess.cur;
    const inMint = (c.assets[inSym] || {}).mint;
    const outMint = (c.assets[outSym] || {}).mint;
    const amt = buying ? (parseFloat(sess.raw) || 0) : sellCalcAmount();
    if (!inMint || !outMint || !(amt > 0)) { sess.quoting = false; render(); return; }
    if ((inSym === 'BNB' && isOndo(c, outSym)) || (outSym === 'BNB' && isOndo(c, inSym))) {
      sess.quoting = false;
      sess.order = null;
      setNote("Swaps between this token and real-world assets aren't supported yet. Try using a different token.");
      render();
      return;
    }
    try {
      const order = await postJSON('/api/swap/order', {
        inputMint: inMint, outputMint: outMint, uiAmount: amt, taker: sess.address,
      });
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      const rotBlock = rotating ? rotateBlock(c, inSym, outSym, amt, order) : '';
      renderShare(rotating ? null : (order.shareCheck || null));
      if (rotating && (order.unsupported || order.error) && !order.uiOutAmount) {
        sess.order = null;
        setNote(order.unsupported ? order.error : 'No route');
      } else if (rotBlock) {
        sess.order = null;
        setNote('Blocked: ' + rotBlock);
      } else if (order.shareCheck && order.shareCheck.blocked && !rotating) {
        sess.order = null;
        setNote('Blocked: ' + order.shareCheck.reason);
      } else if (order.uiOutAmount) {
        sess.order = order;
        if (order.needsApproval) {
          setNote('Approve this token in the wallet, then confirm the swap.');
        } else if (order.priceImpactTooHigh) {
          setNote('Price impact is high. You can still confirm or cancel in the wallet.');
        } else if (order.approval) {
          setNote('Wallet will ask to approve this token, then confirm the swap.');
        } else {
          setNote('');
        }
      } else {
        sess.order = null;
        const c2 = sess.host.getCtx();
        const platform = ((c2.assets[sess.symbol] || {}).platform || '').toLowerCase();
        setNote(rotating ? 'No route' : platform === 'ondo' ? 'No RFQ inventory. AMM still live on xStocks.' : 'No route found (Binance, PancakeSwap, OpenOcean)');
      }
      render();
    } catch (err) {
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      sess.order = null;
      renderShare(null);
      setNote(err.message || 'Could not get a price');
      render();
    }
  }

  // Rotate guard, same 95% floor as buys: output worth under 95% of input, or near zero.
  function rotateBlock(c, inSym, outSym, amt, order) {
    if (order.priceImpactTooHigh) return 'output worth under 95% of input';
    const pin = price(c, inSym), pout = price(c, outSym);
    const out = order.uiOutAmount || 0;
    if (!order.uiOutAmount) return '';
    if (!(pin > 0) || !(pout > 0)) return '';
    const vin = amt * pin, vout = out * pout;
    if (vout < 0.05 * vin) return 'near-zero output';
    if (vout < 0.95 * vin) return 'output worth under 95% of input';
    return '';
  }

  // ---- Render -------------------------------------------------------------------
  function renderCur() {
    const opts = payOptions();
    R.cur.classList.toggle('single', opts.length === 1);
    R.cur.textContent = '';
    opts.forEach((sym) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.cur = sym;
      b.className = 'swp-cur-opt' + (sym === S.cur ? ' active' : '');
      const img = new Image();
      img.alt = '';
      img.src = LOGO[sym];
      b.appendChild(img);
      b.appendChild(document.createTextNode(sym));
      R.cur.appendChild(b);
    });
  }

  function renderQuick() {
    R.quick.textContent = '';
    QUICK[S.cur].forEach((v) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'swp-chip';
      b.dataset.amt = String(v);
      b.textContent = `${v} ${S.cur}`;
      R.quick.appendChild(b);
    });
  }

  function setCta(text, disabled, busy) {
    const b = R.cta;
    b.disabled = disabled;
    b.classList.toggle('busy', !!busy);
    if (busy) {
      if (b.dataset.busy !== '1') {
        b.dataset.busy = '1';
        b.innerHTML = `<span class="snd-dots"><i></i><i></i><i></i><i></i></span>${text}`;
      }
    } else {
      b.dataset.busy = '';
      if (b.textContent !== text) b.textContent = text;
    }
  }

  function buyCalc(c) {
    const bal = c.holdings[S.cur] || 0;
    const spendable = S.cur === 'BNB' ? Math.max(0, bal - BNB_RESERVE) : bal;
    const v = parseFloat(S.raw) || 0;
    const usdIn = v * price(c, S.cur);
    const out = S.order && S.order.uiOutAmount ? S.order.uiOutAmount : 0;
    return { bal, v, usdIn, out, over: v > spendable + 1e-12 };
  }

  function sellCalc(c) {
    const bal = c.holdings[S.symbol] || 0;
    const amt = sellCalcAmount();
    const usdOut = amt * price(c, S.symbol);
    const out = S.order && S.order.uiOutAmount ? S.order.uiOutAmount : 0;
    return { bal, amt, usdOut, out };
  }

  // RFQ vs AMM badge: reflects the live quote's executionMode; before a quote
  // exists, preview from the token's platform (xstocks -> AMM, ondo -> RFQ).
  function modeLabel(c) {
    const exec = S.order && S.order.executionMode;
    if (exec === 'RFQ') return 'RFQ';
    if (exec === 'SWAP') return 'AMM';
    const platform = ((c.assets[S.symbol] || {}).platform || '').toLowerCase();
    if (platform === 'xstocks') return 'AMM';
    if (platform === 'ondo') return 'RFQ';
    return '';
  }

  function fmtImpact(v) {
    if (v === undefined || v === null || isNaN(parseFloat(v))) return '\u2014';
    const pct = Math.abs(parseFloat(v)) * 100; // fraction -> %
    return pct < 0.01 ? '<0.01%' : pct.toFixed(2) + '%';
  }

  function fmtFees(o) {
    const f = o.fees || {};
    const parts = [];
    if (f.gasBnb && f.gasBnb < 0.1) parts.push(`~${f.gasBnb.toFixed(6)} BNB gas`);
    if (f.tradeFeeUsd) parts.push(`$${f.tradeFeeUsd.toFixed(2)} trade fee`);
    if (o.transferFeeBps) parts.push(`${(o.transferFeeBps / 100).toFixed(2)}% transfer fee`);
    return parts.length ? parts.join(' + ') : '\u2014';
  }

  function renderDetails() {
    const o = S.order;
    R.det.hidden = !(o && o.uiOutAmount);
    if (R.det.hidden) return;
    R.dRoute.textContent = (o.routeLabel || (o.routes || []).join(', ') || '\u2014')
      + (o.fallbackFrom ? ` (${o.fallbackFrom} quote failed)` : '');
    R.dImpact.textContent = fmtImpact(o.priceImpactPct);
    R.dFees.textContent = fmtFees(o);
    const cp = o.checkProof;
    R.dProof.textContent = cp ? `${cp.hash.slice(0, 10)}\u2026${cp.hash.slice(-6)} \u00b7 off-chain` : '\u2014';
    R.dProof.title = cp ? `${cp.note}\n${cp.payload}` : '';
    R.dMin.textContent = o.uiMinReceived ? `${fmtTok(o.uiMinReceived)} ${S.side === 'buy' ? S.symbol : S.side === 'rotate' ? S.outLabel : S.cur}` : '\u2014';
  }

  function render() {
    if (!S) return;
    const c = S.host.getCtx();
    const buying = S.side === 'buy';
    const rotating = S.side === 'rotate';
    const st = (S.host.stocks || []).find((x) => x.symbol === S.symbol);
    R.title.textContent = rotating
      ? `ROTATE ${S.host.label || S.symbol} to ${S.outLabel}`
      : `${buying ? 'Buy' : 'Sell'} ${S.host.title || S.host.label || S.symbol}${st ? ' ' + st.label : ''}`.trim();
    renderDetails();
    R.buy.hidden = !buying;
    R.sell.hidden = buying;
    R.cur.hidden = rotating;
    R.pcts.hidden = !rotating;
    if (rotating) R.pcts.querySelectorAll('.swp-chip').forEach((b) => b.classList.toggle('active', Number(b.dataset.pct) === S.pct));

    const mode = modeLabel(c);
    if (mode) {
      R.modeChip.hidden = false;
      R.modeChip.textContent = mode;
      R.modeChip.className = 'swp-mode-chip swp-mode-' + mode.toLowerCase();
    } else {
      R.modeChip.hidden = true;
    }

    if (buying) {
      const d = buyCalc(c);
      R.bal.textContent = `Balance: ${trunc(d.bal, DEC[S.cur])} ${S.cur}`;
      if (R.input.value !== S.raw) R.input.value = S.raw;
      const len = S.raw.length;
      R.input.style.fontSize = len > 14 ? '32px' : len > 10 ? '42px' : '';
      R.quick.querySelectorAll('.swp-chip').forEach((b) => {
        b.classList.toggle('active', S.raw !== '' && parseFloat(S.raw) === parseFloat(b.dataset.amt));
      });
      if (S.busy) setCta('Buying', true, true);
      else if (!(d.v > 0)) setCta('Enter an amount', true);
      else if (!S.address) setCta('Connect wallet', false);
      else if (d.over) setCta(`Insufficient ${S.cur} balance`, true);
      else if (S.quoting) setCta('Getting price...', true, true);
      else if (!S.order || !S.order.uiOutAmount) setCta(`Buy with ${S.raw.replace(/\.$/, '')} ${S.cur}`, true);
      else setCta(`Buy with ${S.raw.replace(/\.$/, '')} ${S.cur}`, false);
      if (d.v > 0 && d.out > 0) {
        const m = multiplierOf(c, S.symbol);
        const shares = tokensToShares(d.out, m);
        const sharesStr = shares !== null ? ` × ${m} ≈ ${fmtTok(shares)} shares` : '';
        R.est.textContent = `~${fmtTok(d.out)} tokens${sharesStr} ≈ ${usdFmt.format(d.usdIn)}`;
      } else {
        R.est.textContent = `You will receive in ${S.symbol}`;
      }
    } else if (rotating) {
      const d = sellCalc(c);
      R.bal.textContent = `Balance: ${trunc(d.bal, TOKEN_DEC)} ${S.host.label || S.symbol}`;
      R.pct.textContent = S.pct + '%';
      R.range.value = S.pct;
      R.range.style.setProperty('--pct', S.pct + '%');
      R.minus.disabled = S.busy || S.pct <= 0 || !S.address;
      R.plus.disabled = S.busy || S.pct >= 100 || !S.address;
      R.range.disabled = S.busy || !(d.bal > 0);
      const ok = S.order && S.order.uiOutAmount;
      if (S.busy) setCta('Rotating', true, true);
      else if (!S.address) setCta('Connect wallet', false);
      else if (!(d.bal > 0)) setCta('Insufficient balance', true);
      else if (!(d.amt > 0)) setCta('Select an amount', true);
      else if (S.quoting) setCta('Getting price...', true, true);
      else if (!ok) setCta('Rotate', true);
      else setCta('Rotate', false);
      if (d.amt > 0 && ok) {
        const m = multiplierOf(c, S.out);
        const shares = tokensToShares(S.order.uiOutAmount, m);
        const usd = S.order.uiOutAmount * price(c, S.out);
        const ps2 = shares && usd > 0 ? ` \u00b7 ${usdFmt.format(usd / shares)}/share` : '';
        R.est.textContent = `~${fmtTok(S.order.uiOutAmount)} ${S.outLabel}${ps2}`;
      } else {
        R.est.textContent = `You will receive ${S.outLabel}`;
      }
    } else {
      const d = sellCalc(c);
      R.bal.textContent = `Balance: ${trunc(d.bal, TOKEN_DEC)} ${S.symbol}`;
      R.pct.textContent = S.pct + '%';
      R.range.value = S.pct;
      R.range.style.setProperty('--pct', S.pct + '%');
      R.minus.disabled = S.busy || S.pct <= 0;
      R.plus.disabled = S.busy || S.pct >= 100;
      R.range.disabled = S.busy || !(d.bal > 0);
      if (!(d.bal > 0)) { R.minus.disabled = true; R.plus.disabled = true; }
      if (S.busy) setCta('Selling', true, true);
      else if (!(d.bal > 0)) setCta('Insufficient balance', true);
      else if (!(d.amt > 0)) setCta('Select an amount', true);
      else if (S.quoting) setCta('Getting price...', true, true);
      else if (!S.order || !S.order.uiOutAmount) setCta(`Sell ${trunc(d.amt, TOKEN_DEC)} ${S.symbol}`, true);
      else setCta(`Sell ${trunc(d.amt, TOKEN_DEC)} ${S.symbol}`, false);
      if (d.amt > 0 && d.out > 0) {
        const m = multiplierOf(c, S.symbol);
        const shares = tokensToShares(d.amt, m);
        const sharesStr = shares !== null ? ` (${fmtTok(shares)} shares)` : '';
        R.est.textContent = `Selling ${fmtTok(d.amt)} tokens${sharesStr} → ~${fmtTok(d.out)} ${S.cur} ≈ ${usdFmt.format(d.usdOut)}`;
      } else {
        R.est.textContent = `You will receive in ${S.cur}`;
      }
    }
  }

  // ---- Swap -------------------------------------------------------------------
  async function doSwap() {
    if (!S || S.busy || R.cta.disabled || !S.order) return;
    const sess = S;
    sess.busy = true;
    setNote('');
    render();
    try {
      if (sess.order.executionMode === 'RFQ') {
        let userSignature;
        try {
          userSignature = await window.MarktapeWallet.signTypedData(sess.order.typedDataToSign);
        } catch (err) {
          const rejected = (err && err.code === 4001) || /reject|declin|denied|cancel/i.test(String((err && err.message) || ''));
          throw new Error(rejected ? 'Cancelled' : 'Wallet could not sign the order');
        }
        if (S !== sess) return;
        const res = await postJSON('/api/swap/execute', {
          provider: sess.order.provider || 'binance_web3',
          userSignature,
          requestId: sess.order.requestId,
          rfqVendor: sess.order.rfqVendor,
          quoteId: sess.order.quoteId,
          signingScheme: sess.order.signingScheme,
        });
        if (res.status === 'FAILED' || res.status === 'EXPIRED' || res.status === 'CANCELLED') {
          throw new Error('Swap failed, please try again');
        }
      } else {
        if (sess.order.approval) {
          const ap = await window.MarktapeWallet.signTransactionForSend(sess.order.approval);
          await waitReceipt(ap && ap.signature);
        }
        let signed;
        try {
          signed = await window.MarktapeWallet.signTransactionForSend(sess.order.transaction);
        } catch (err) {
          const rejected = (err && err.code === 4001) || /reject|declin|denied|cancel/i.test(String((err && err.message) || ''));
          throw new Error(rejected ? 'Cancelled' : 'Wallet could not sign the transaction');
        }
        if (S !== sess) return;

        if (signed.signature || signed.signedTransactionBase64) {
          const res = await postJSON('/api/swap/execute', {
            signedTransaction: signed.signedTransactionBase64,
            txHash: signed.signature,
            requestId: sess.order.requestId,
            provider: sess.order.provider || 'binance_web3',
          });
          if (res.status && /^(failed|expired|cancelled|error)$/i.test(res.status)) {
            throw new Error('Swap failed on-chain, please try again');
          }
          await waitReceipt(signed.signature);
        }
      }

      const host = sess.host;
      close();
      if (window.MarktapeSend && window.MarktapeSend.toast) window.MarktapeSend.toast('Swap successful');
      if (host.onDone) host.onDone();
    } catch (err) {
      if (S !== sess) return;
      sess.busy = false;
      setNote((err && err.message) || 'Swap failed, try again');
      render();
    }
  }

  // Wallet returns the hash on broadcast, not on confirmation. Throws only on a mined revert; a timeout is not a failure.
  async function waitReceipt(hash) {
    const prov = window.MarktapeWallet && window.MarktapeWallet.getProvider && window.MarktapeWallet.getProvider();
    if (!hash || !prov) return;
    for (let i = 0; i < 45; i++) {
      let r = null;
      try { r = await prov.request({ method: 'eth_getTransactionReceipt', params: [hash] }); } catch (_) {}
      if (r) {
        if (r.status === '0x0' || r.status === 0) throw new Error('Swap failed on-chain, please try again');
        return;
      }
      await new Promise((ok) => setTimeout(ok, 2000));
    }
  }

  // ---- Public ---------------------------------------------------------------------
  function close() {
    if (!S) return;
    clearTimeout(S.quoteTimer);
    const onClose = S.host.onClose;
    S = null;
    if (onClose) setTimeout(onClose, ANIM_MS + 80);
    R.input.blur();
    R.backdrop.classList.remove('open');
    R.sheet.classList.remove('open');
    document.documentElement.classList.remove('snd-lock');
    setTimeout(() => { if (!S) root.hidden = true; }, ANIM_MS + 40);
  }

  function open(host) {
    const c = host.getCtx();
    if (S) return false;
    const rot = host.side === 'rotate' && !!host.out;
    const opts = allowedPay(c, host.symbol);
    const cur = opts.includes(host.pay) ? host.pay : opts[0];
    if (!cur && !rot) return false;
    build();
    S = {
      host, address: c.address || null, side: rot ? 'rotate' : host.side === 'sell' ? 'sell' : 'buy', symbol: host.symbol,
      out: rot ? host.out : null, outLabel: rot ? (host.outLabel || host.out) : null,
      cur, raw: '', pct: 25, busy: false,
      order: null, quoting: false, quoteTimer: null, quoteReq: 0,
    };
    R.input.value = '';
    setNote('');
    const ph = host.proof;
    R.proof.hidden = !ph;
    R.proof.textContent = ph ? `Proof of proposal ${ph.slice(0, 10)}\u2026${ph.slice(-6)} \u00b7 not a trade` : '';
    R.proof.title = ph || '';
    if (S.side !== 'rotate') { renderStk(); renderCur(); renderQuick(); } else { R.stk.hidden = true; }
    render();
    root.hidden = false;
    document.documentElement.classList.add('snd-lock');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!S) return;
      R.backdrop.classList.add('open');
      R.sheet.classList.add('open');
    }));
    const pre = S.side === 'buy' ? sanitize(String(host.amount || ''), DEC[cur]) : '';
    if (pre && parseFloat(pre) > 0) {
      S.raw = pre;
      R.input.value = pre;
      render();
      scheduleQuote();
    } else if (S.side === 'buy') {
      setTimeout(() => { if (S && S.side === 'buy') R.input.focus({ preventScroll: true }); }, ANIM_MS + 40);
    } else {
      scheduleQuote();
    }
    return true;
  }

  window.MarktapeSwap = { open };
})();
