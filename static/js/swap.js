/* Buy / Sell sheets for the token page (frontend only).
 *
 *   Buy  : pay in BNB or USDC, type an amount (device keyboard), quick amounts.
 *   Sell : receive BNB or USDC, pick a % of the holding (slider / - +).
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
  const PAY = ['BNB', 'USDC'];
  const LOGO = { BNB: '/static/img/bnb.svg', USDC: '/static/img/usdc.svg' };
  const DEC = { BNB: 18, USDC: 18 };
  const QUICK = { BNB: [0.01, 0.05, 0.1], USDC: [10, 50, 100] };
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
        <div class="swp-cur"></div>
        <p class="swp-bal"></p>
        <div class="swp-buy">
          <input class="swp-input" type="text" inputmode="decimal" autocomplete="off" autocorrect="off" spellcheck="false" placeholder="0" aria-label="Amount">
          <div class="swp-quick"></div>
        </div>
        <div class="swp-sell">
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
        <p class="swp-note" role="alert"></p>
      </div>`;
    document.body.appendChild(root);

    const q = (s) => root.querySelector(s);
    Object.assign(R, {
      backdrop: q('.swp-backdrop'), sheet: q('.swp-sheet'), title: q('.swp-title'), cur: q('.swp-cur'),
      bal: q('.swp-bal'), buy: q('.swp-buy'), input: q('.swp-input'), quick: q('.swp-quick'),
      sell: q('.swp-sell'), minus: q('.swp-minus'), plus: q('.swp-plus'), pct: q('.swp-pct'), range: q('.swp-range'),
      cta: q('.swp-cta'), est: q('.swp-est'), note: q('.swp-note'), modeChip: q('.swp-mode-chip'),
    });

    R.backdrop.addEventListener('click', () => { if (S && !S.busy) close(); });
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
    R.range.addEventListener('input', () => setPct(Number(R.range.value)));
    R.minus.addEventListener('click', () => setPct(S.pct - STEP));
    R.plus.addEventListener('click', () => setPct(S.pct + STEP));
    R.cta.addEventListener('click', doSwap);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S && !S.busy) close();
    });
    window.addEventListener('marktape:wallet', (e) => {
      if (S && e.detail.address !== S.address) close();
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
  const payOptions = () => PAY.filter((s) => s !== S.symbol && !(s === 'BNB' && isOndo(S.host.getCtx(), S.symbol)));
  const price = (c, sym) => (c.prices[sym] ? c.prices[sym].price : 0);

  function setNote(msg) { R.note.textContent = msg || ''; }

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
    S.quoting = false;
    clearTimeout(S.quoteTimer);
    S.quoteReq = (S.quoteReq || 0) + 1;
  }

  function scheduleQuote() {
    if (!S) return;
    S.order = null;
    clearTimeout(S.quoteTimer);
    const amt = S.side === 'buy' ? parseFloat(S.raw) || 0 : sellCalcAmount();
    if (!(amt > 0)) { S.quoting = false; return; }
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
    const inSym = buying ? sess.cur : sess.symbol;
    const outSym = buying ? sess.symbol : sess.cur;
    const inMint = (c.assets[inSym] || {}).mint;
    const outMint = (c.assets[outSym] || {}).mint;
    const amt = buying ? (parseFloat(sess.raw) || 0) : sellCalcAmount();
    if (!inMint || !outMint || !(amt > 0)) { sess.quoting = false; render(); return; }
    try {
      const order = await postJSON('/api/swap/order', {
        inputMint: inMint, outputMint: outMint, uiAmount: amt, taker: sess.address,
      });
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      if (order.uiOutAmount || order.deepLink) {
        sess.order = order;
        if (order.sim && order.sim.ok === false) {
          setNote(order.sim.error ? `Simulation failed: ${order.sim.error}` : 'Simulation failed — this swap would likely revert');
        } else if (order.sim && order.sim.ok && order.sim.gas) {
          setNote(`Simulated OK · ~${order.sim.gas} gas`);
        } else {
          setNote(order.transaction ? '' : 'Opens PancakeSwap to complete the swap');
        }
      } else {
        sess.order = null;
        const c2 = sess.host.getCtx();
        const platform = ((c2.assets[sess.symbol] || {}).platform || '').toLowerCase();
        setNote(platform === 'ondo' ? 'No RFQ inventory. AMM still live on xStocks.' : 'No route found');
      }
      render();
    } catch (err) {
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      sess.order = null;
      setNote(err.message || 'Could not get a price');
      render();
    }
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

  function render() {
    if (!S) return;
    const c = S.host.getCtx();
    const buying = S.side === 'buy';
    R.buy.hidden = !buying;
    R.sell.hidden = buying;

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
      else if (d.over) setCta(`Insufficient ${S.cur} balance`, true);
      else if (S.quoting) setCta('Getting price...', true, true);
      else if (!S.order || !S.order.uiOutAmount) setCta(`Buy with ${S.raw.replace(/\.$/, '')} ${S.cur}`, true);
      else if (S.order.sim && S.order.sim.ok === false) setCta('Simulation failed', true);
      else setCta(`Buy with ${S.raw.replace(/\.$/, '')} ${S.cur}`, false);
      if (d.v > 0 && d.out > 0) {
        const m = multiplierOf(c, S.symbol);
        const shares = tokensToShares(d.out, m);
        const sharesStr = shares !== null ? ` × ${m} ≈ ${fmtTok(shares)} shares` : '';
        R.est.textContent = `~${fmtTok(d.out)} tokens${sharesStr} ≈ ${usdFmt.format(d.usdIn)}`;
      } else {
        R.est.textContent = `You will receive in ${S.symbol}`;
      }
    } else {
      const d = sellCalc(c);
      R.bal.textContent = `Balance: ${trunc(d.bal, TOKEN_DEC)} ${S.symbol}`;
      R.pct.textContent = S.pct + '%';
      R.range.value = S.pct;
      R.range.style.setProperty('--pct', S.pct + '%');
      R.minus.disabled = S.busy || S.pct <= 0;
      R.plus.disabled = S.busy || S.pct >= 100;
      R.range.disabled = S.busy;
      if (S.busy) setCta('Selling', true, true);
      else if (!(d.amt > 0)) setCta('Select an amount', true);
      else if (S.quoting) setCta('Getting price...', true, true);
      else if (!S.order || !S.order.uiOutAmount) setCta(`Sell ${trunc(d.amt, TOKEN_DEC)} ${S.symbol}`, true);
      else if (S.order.sim && S.order.sim.ok === false) setCta('Simulation failed', true);
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
      if (sess.order.deepLink && !sess.order.transaction) {
        window.open(sess.order.deepLink, '_blank', 'noopener');
        const host = sess.host;
        close();
        if (host.onSent) host.onSent();
        return;
      }
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
          provider: 'binance_web3',
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
        if (sess.order.sim && sess.order.sim.ok === false) {
          throw new Error(sess.order.sim.error || 'Simulation failed — swap would revert');
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
            provider: sess.order.provider || 'pancake',
          });
          if (res.status && res.status !== 'Success' && res.status !== 'success') {
            throw new Error('Swap failed on-chain, please try again');
          }
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
    if (S || !c.address || !c.prices[host.symbol]) return false;
    const cur = PAY.find((s) => s !== host.symbol && !(s === 'BNB' && isOndo(c, host.symbol)));
    if (!c.prices[cur]) return false;
    build();
    S = {
      host, address: c.address, side: host.side === 'sell' ? 'sell' : 'buy', symbol: host.symbol,
      cur, raw: '', pct: 25, busy: false,
      order: null, quoting: false, quoteTimer: null, quoteReq: 0,
    };
    R.title.textContent = `${S.side === 'buy' ? 'Buy' : 'Sell'} ${host.label || S.symbol}`;
    R.input.value = '';
    setNote('');
    renderCur();
    renderQuick();
    render();
    root.hidden = false;
    document.documentElement.classList.add('snd-lock');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!S) return;
      R.backdrop.classList.add('open');
      R.sheet.classList.add('open');
    }));
    if (S.side === 'buy') {
      setTimeout(() => { if (S && S.side === 'buy') R.input.focus({ preventScroll: true }); }, ANIM_MS + 40);
    } else {
      scheduleQuote();
    }
    return true;
  }

  window.MarktapeSwap = { open };
})();
