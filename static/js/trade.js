/* Trade (Swap) panel — /swap page, blue theme.
 * Sell card / swap-direction button / Buy card (read-only) / rate+gasless+
 * warning row / CTA. No in-site keyboard — the native mobile keyboard drives
 * the amount input. Debounced StreetTape quote fetch ("Getting Price...")
 * while typing; warning icon opens a Price Info modal; on success
 * MarktapeSend.toast() shows the shared blue toast.
 *
 * Host (swap-page.js) calls MarktapeTrade.mount(#trd-slot) then
 * MarktapeTrade.open({ getCtx, onDone }). getCtx() ->
 * { address, holdings, prices, assets } (live state, same shape as send.js).
 *
 * Non-custodial: /api/swap/order builds the Ultra order server-side (the API
 * key never reaches the browser), the wallet signs, /api/swap/execute
 * opens PancakeSwap when no unsigned tx is available, which broadcasts it.
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 450;
  const DECIMALS = { BNB: 18, USDT: 18, USDC: 18 };

  const ICON = {
    chevron: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    swap: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m17 4 4 4-4 4"/><path d="M3 8h18"/><path d="m7 20-4-4 4-4"/><path d="M21 16H3"/></svg>',
    info: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
    warn: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    close: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    external: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M7 7h10v10"/></svg>',
    back: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19 5 12l7-7"/><path d="M19 12H5"/></svg>',
  };

  const BNB_RESERVE = 0.0002;

  const decimalsOf = (sym) => DECIMALS[sym] || 6;

  function fmtAmount(v) {
    if (!(v > 0)) return '0';
    const max = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
    return v.toLocaleString('en-US', { maximumFractionDigits: max });
  }

  const usdFmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
  const fmtUsd = (v) => (v > 0 && v < 0.005 ? '<$0.01' : usdFmt.format(v));

  function sanitize(str, maxDec) {
    str = str.replace(',', '.').replace(/[^\d.]/g, '');
    const i = str.indexOf('.');
    if (i !== -1) str = str.slice(0, i + 1) + str.slice(i + 1).replace(/\./g, '').slice(0, maxDec);
    if (str.startsWith('.')) str = '0' + str;
    return str.replace(/^0+(?=\d)/, '').slice(0, 14);
  }

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
      const err = new Error(msg);
      err.deepLink = detail && detail.deepLink;
      throw err;
    }
    return data;
  }

  // ---- DOM -------------------------------------------------------------------
  let root = null;
  const R = {};
  let S = null; // { host, address, sell, buy, sellRaw, buyUi, quoting, order, quoteReq, note, swapping }

  function logoEl(meta, size) {
    const w = document.createElement('span');
    w.className = 'trd-logo';
    w.style.width = w.style.height = size + 'px';
    if (meta && meta.image) {
      const img = new Image();
      img.alt = '';
      img.src = meta.image;
      img.addEventListener('error', () => img.remove());
      w.appendChild(img);
    }
    return w;
  }

  function build() {
    if (root) return;
    root = document.createElement('section');
    root.className = 'trd-panel';
    root.setAttribute('aria-label', 'Swap');
    root.setAttribute('aria-hidden', 'false');
    root.innerHTML = `
      <div class="hm-col trd-col">
        <h2 class="stk-title">Swap</h2>
        <div class="trd-card" data-side="sell">
          <div class="trd-card-row">
            <span class="trd-card-label">Sell</span>
            <span class="trd-bal"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/></svg><span class="trd-bal-sell"></span></span>
          </div>
          <div class="trd-card-main">
            <button type="button" class="trd-pill" data-pill="sell" aria-label="Select token to sell"><span class="trd-pill-logo"></span><span class="trd-pill-sym"></span>${ICON.chevron}</button>
            <div class="trd-amount" data-input="sell" aria-label="Amount to sell">0</div>
          </div>
          <div class="trd-card-sub" data-sub="sell"></div>
        </div>

        <button type="button" class="trd-dir" aria-label="Reverse tokens">${ICON.swap}</button>

        <div class="trd-card" data-side="buy">
          <div class="trd-card-row">
            <span class="trd-card-label">Buy</span>
            <span class="trd-bal"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/></svg><span class="trd-bal-buy"></span></span>
          </div>
          <div class="trd-card-main">
            <button type="button" class="trd-pill" data-pill="buy" aria-label="Select token to buy"><span class="trd-pill-logo"></span><span class="trd-pill-sym"></span>${ICON.chevron}</button>
            <span class="trd-amount trd-amount-out" data-out></span>
          </div>
          <div class="trd-card-sub" data-sub="buy"></div>
        </div>

        <button type="button" class="trd-info-row" data-info-row hidden>
          <span class="trd-rate" data-rate></span>
          <span class="trd-info-r">
            <span class="trd-mode-chip" data-mode hidden></span>
            <span class="trd-gasless" data-gasless hidden>${ICON.info}Gasless</span>
            <span class="trd-warn-chip" data-warn hidden></span>
            ${ICON.chevron}
          </span>
        </button>

        <p class="trd-note" data-note role="alert"></p>
        <button type="button" class="trd-cta" data-cta disabled>Enter Amount</button>
        <div class="trd-keys" data-keys>
          <button type="button" class="trd-key trd-key-act" data-key="max">MAX</button>
          <button type="button" class="trd-key" data-key="1">1</button>
          <button type="button" class="trd-key" data-key="2">2</button>
          <button type="button" class="trd-key" data-key="3">3</button>
          <button type="button" class="trd-key trd-key-act" data-key="75">75%</button>
          <button type="button" class="trd-key" data-key="4">4</button>
          <button type="button" class="trd-key" data-key="5">5</button>
          <button type="button" class="trd-key" data-key="6">6</button>
          <button type="button" class="trd-key trd-key-act" data-key="50">50%</button>
          <button type="button" class="trd-key" data-key="7">7</button>
          <button type="button" class="trd-key" data-key="8">8</button>
          <button type="button" class="trd-key" data-key="9">9</button>
          <button type="button" class="trd-key trd-key-act" data-key="clear">CLEAR</button>
          <button type="button" class="trd-key" data-key=".">.</button>
          <button type="button" class="trd-key" data-key="0">0</button>
          <button type="button" class="trd-key" data-key="back" aria-label="Delete">${ICON.back}</button>
        </div>
      </div>

      <div class="trd-tok" data-tok hidden>
        <div class="trd-tok-back"></div>
        <div class="trd-tok-sheet"><div class="trd-handle"></div><h3 class="trd-title">Select token</h3><div class="trd-tok-list" data-tok-list></div></div>
      </div>

      <div class="trd-info" data-info-modal hidden>
        <div class="trd-info-back"></div>
        <div class="trd-info-sheet">
          <div class="trd-handle"></div>
          <div class="trd-info-head"><h3 class="trd-title">Price Info</h3><button type="button" class="trd-info-close" aria-label="Close">${ICON.close}</button></div>
          <div class="trd-fee-warn" data-fee-warn hidden>
            ${ICON.warn}
            <span><b data-fee-title></b><small data-fee-sub></small></span>
          </div>
          <dl class="trd-info-list">
            <div><dt>Rate</dt><dd data-i-rate></dd></div>
            <div><dt>Price Impact</dt><dd data-i-impact></dd></div>
            <div><dt>Minimum Received</dt><dd data-i-min></dd></div>
            <div><dt>Fees</dt><dd data-i-fees></dd></div>
            <div><dt>Routes</dt><dd data-i-routes></dd></div>
            <div><dt>Input Mint Address</dt><dd data-i-inmint></dd></div>
            <div><dt>Output Mint Address</dt><dd data-i-outmint></dd></div>
          </dl>
        </div>
      </div>`;
    const slot = document.getElementById('trd-slot');
    (slot || document.body).appendChild(root);

    const q = (s) => root.querySelector(s);
    Object.assign(R, {
      cardSell: q('[data-side="sell"]'), cardBuy: q('[data-side="buy"]'),
      pillSell: q('[data-pill="sell"]'), pillBuy: q('[data-pill="buy"]'),
      inputSell: q('[data-input="sell"]'), outBuy: q('[data-out]'),
      balSell: q('.trd-bal-sell'), balBuy: q('.trd-bal-buy'),
      subSell: q('[data-sub="sell"]'), subBuy: q('[data-sub="buy"]'),
      dir: q('.trd-dir'), infoRow: q('[data-info-row]'), rate: q('[data-rate]'),
      modeChip: q('[data-mode]'), gasless: q('[data-gasless]'), warnChip: q('[data-warn]'),
      note: q('[data-note]'), cta: q('[data-cta]'), keys: q('[data-keys]'),
      tok: q('[data-tok]'), tokBack: q('.trd-tok-back'), tokList: q('[data-tok-list]'),
      infoModal: q('[data-info-modal]'), infoBack: q('.trd-info-back'), infoClose: q('.trd-info-close'),
      feeWarn: q('[data-fee-warn]'), feeTitle: q('[data-fee-title]'), feeSub: q('[data-fee-sub]'),
      iRate: q('[data-i-rate]'), iImpact: q('[data-i-impact]'), iMin: q('[data-i-min]'),
      iFees: q('[data-i-fees]'), iRoutes: q('[data-i-routes]'), iIn: q('[data-i-inmint]'), iOut: q('[data-i-outmint]'),
    });

    R.pillSell.addEventListener('click', () => openTokens('sell'));
    R.pillBuy.addEventListener('click', () => openTokens('buy'));
    R.tokBack.addEventListener('click', closeTokens);
    R.tokList.addEventListener('click', (e) => {
      const row = e.target.closest('[data-sym]');
      if (row) selectToken(row.dataset.sym);
    });
    R.keys.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-key]');
      if (btn) pressKey(btn.dataset.key);
    });
    R.dir.addEventListener('click', reverse);
    R.infoRow.addEventListener('click', openInfo);
    R.infoBack.addEventListener('click', closeInfo);
    R.infoClose.addEventListener('click', closeInfo);
    R.cta.addEventListener('click', () => {
      if (!S) return;
      if (!S.address && window.MarktapeWallet) {
        window.MarktapeWallet.connectWithPicker();
        return;
      }
      doSwap();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !S) return;
      if (!R.infoModal.hidden) closeInfo();
      else if (!R.tok.hidden) closeTokens();
    });
    window.addEventListener('marktape:wallet', (e) => {
      if (!S) return;
      S.address = e.detail.address || null;
      if (!S.address) resetQuote();
      else scheduleQuote();
      render();
    });
  }

  function mount(slot) {
    build();
    if (slot && root.parentNode !== slot) slot.appendChild(root);
    return root;
  }

  // ---- Token selector ------------------------------------------------------
  function tokenOrder(c) {
    const syms = Object.keys(c.prices);
    const value = (s) => (c.holdings[s] || 0) * c.prices[s].price;
    const rank = { BNB: 0, USDC: 1, USDT: 2 };
    return syms.sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      if ((va > 0) !== (vb > 0)) return va > 0 ? -1 : 1;
      if (va > 0 && va !== vb) return vb - va;
      const ra = a in rank ? rank[a] : 3;
      const rb = b in rank ? rank[b] : 3;
      return ra - rb || a.localeCompare(b);
    });
  }

  let tokFor = null; // 'sell' | 'buy'
  function openTokens(which) {
    if (S.swapping) return;
    tokFor = which;
    const c = S.host.getCtx();
    const other = which === 'sell' ? S.buy : S.sell;
    R.tokList.textContent = '';
    tokenOrder(c).forEach((sym) => {
      if (sym === other) return; // can't pick the same token both sides
      const meta = c.assets[sym] || { name: sym };
      const bal = c.holdings[sym] || 0;
      const row = document.createElement('button');
      row.type = 'button';
      row.dataset.sym = sym;
      row.className = 'trd-tok-row' + (bal > 0 ? '' : ' empty') + (sym === (which === 'sell' ? S.sell : S.buy) ? ' active' : '');
      row.appendChild(logoEl(meta, 38));
      const main = document.createElement('span');
      main.className = 'trd-tok-main';
      main.innerHTML = '<b></b><small></small>';
      main.firstChild.textContent = sym;
      main.lastChild.textContent = meta.name || sym;
      row.appendChild(main);
      const side = document.createElement('span');
      side.className = 'trd-tok-side';
      side.innerHTML = '<b></b><small></small>';
      side.firstChild.textContent = bal > 0 ? fmtAmount(bal) : '';
      side.lastChild.textContent = bal > 0 && c.prices[sym] ? fmtUsd(bal * c.prices[sym].price) : '';
      row.appendChild(side);
      R.tokList.appendChild(row);
    });
    R.tok.hidden = false;
    document.documentElement.classList.add('trd-tok-lock');
  }

  function closeTokens() {
    R.tok.hidden = true;
    tokFor = null;
    document.documentElement.classList.remove('trd-tok-lock');
  }

  function selectToken(sym) {
    const which = tokFor;
    closeTokens();
    if (!S || !which) return;
    if (which === 'sell') {
      if (sym === S.sell) return;
      S.sell = sym;
      S.sellRaw = '';
    } else {
      if (sym === S.buy) return;
      S.buy = sym;
    }
    resetQuote();
    render();
    scheduleQuote();
  }

  function reverse() {
    if (!S || S.swapping) return;
    const c = S.host.getCtx();
    if (!c.prices[S.buy]) return; // need a price for the new sell side
    [S.sell, S.buy] = [S.buy, S.sell];
    S.sellRaw = S.order && S.order.uiOutAmount ? trimNum(S.order.uiOutAmount, decimalsOf(S.sell)) : '';
    resetQuote();
    render();
    scheduleQuote();
  }

  function trimNum(v, dec) {
    if (!(v > 0)) return '';
    const s = v.toFixed(Math.min(dec, 8));
    return s.replace(/0+$/, '').replace(/\.$/, '');
  }

  function sellBalance() {
    const c = S && S.host ? S.host.getCtx() : { holdings: {} };
    return (c.holdings && c.holdings[S.sell]) || 0;
  }

  function maxSell() {
    let bal = sellBalance();
    if (S.sell === 'BNB') bal = Math.max(0, bal - BNB_RESERVE);
    return bal;
  }

  function setSellRaw(raw) {
    if (!S || S.swapping) return;
    S.sellRaw = sanitize(String(raw || ''), decimalsOf(S.sell));
    scheduleQuote();
    render();
  }

  function pressKey(key) {
    if (!S || S.swapping) return;
    if (key === 'clear') return setSellRaw('');
    if (key === 'back') return setSellRaw((S.sellRaw || '').slice(0, -1));
    if (key === 'max') return setSellRaw(trimNum(maxSell(), decimalsOf(S.sell)));
    if (key === '75') return setSellRaw(trimNum(sellBalance() * 0.75, decimalsOf(S.sell)));
    if (key === '50') return setSellRaw(trimNum(sellBalance() * 0.50, decimalsOf(S.sell)));
    if (key === '.') {
      if ((S.sellRaw || '').includes('.')) return;
      return setSellRaw((S.sellRaw || '0') + '.');
    }
    setSellRaw((S.sellRaw || '') + key);
  }

  // ---- Quote -----------------------------------------------------------------
  function resetQuote() {
    if (!S) return;
    S.order = null;
    S.quoting = false;
    S.note = '';
    clearTimeout(S.quoteTimer);
    S.quoteReq = (S.quoteReq || 0) + 1;
  }

  function scheduleQuote() {
    if (!S) return;
    S.order = null;
    S.note = '';
    clearTimeout(S.quoteTimer);
    const amt = parseFloat(S.sellRaw);
    if (!(amt > 0)) { S.quoting = false; render(); return; }
    S.quoting = true;
    render();
    S.quoteTimer = setTimeout(fetchQuote, DEBOUNCE_MS);
  }

  async function fetchQuote() {
    if (!S) return;
    const sess = S;
    const reqId = ++sess.quoteReq;
    const c = sess.host.getCtx();
    const inMint = (c.assets[sess.sell] || {}).mint;
    const outMint = (c.assets[sess.buy] || {}).mint;
    const amt = parseFloat(sess.sellRaw);
    if (!inMint || !outMint || !(amt > 0)) { sess.quoting = false; render(); return; }
    try {
      const body = { inputMint: inMint, outputMint: outMint, uiAmount: amt };
      if (sess.address) body.taker = sess.address;
      const order = await postJSON('/api/swap/order', body);
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      if (order.uiOutAmount) {
        sess.order = order;
        sess.note = '';
      } else {
        sess.order = null;
        sess.note = order.transaction ? '' : (order.deepLink ? 'Opens PancakeSwap to complete the swap' : 'No route found');
      }
      render();
    } catch (err) {
      if (S !== sess || sess.quoteReq !== reqId) return;
      sess.quoting = false;
      sess.order = null;
      sess.note = err.message || 'Could not get a price';
      render();
    }
  }

  // ---- Render ------------------------------------------------------------------
  function tokenMeta(c, sym) { return c.assets[sym] || { name: sym }; }

  // RFQ vs AMM badge: reflects the live quote's executionMode. Before a quote
  // exists, preview the expected mode from the buy token's platform so the
  // row isn't blank while typing (xstocks -> AMM, ondo -> RFQ, bstocks -> API-defined, unknown).
  function modeLabel(c) {
    const exec = S.order && S.order.executionMode;
    if (exec === 'RFQ') return 'RFQ';
    if (exec === 'SWAP') return 'AMM';
    const platform = (tokenMeta(c, S.buy).platform || tokenMeta(c, S.sell).platform || '').toLowerCase();
    if (platform === 'xstocks') return 'AMM';
    if (platform === 'ondo') return 'RFQ';
    return '';
  }

  function render() {
    if (!S) return;
    const c = S.host.getCtx();

    const sellMeta = tokenMeta(c, S.sell);
    if (R.pillSell.dataset.sym !== S.sell) {
      R.pillSell.querySelector('.trd-pill-logo').replaceChildren(logoEl(sellMeta, 26));
      R.pillSell.querySelector('.trd-pill-sym').textContent = S.sell;
      R.pillSell.dataset.sym = S.sell;
    }
    const buyMeta = tokenMeta(c, S.buy);
    if (R.pillBuy.dataset.sym !== S.buy) {
      R.pillBuy.querySelector('.trd-pill-logo').replaceChildren(logoEl(buyMeta, 26));
      R.pillBuy.querySelector('.trd-pill-sym').textContent = S.buy;
      R.pillBuy.dataset.sym = S.buy;
    }

    const shown = S.sellRaw || '0';
    if (R.inputSell.textContent !== shown) R.inputSell.textContent = shown;
    R.inputSell.classList.toggle('is-empty', !S.sellRaw);

    const sellBal = c.holdings[S.sell] || 0;
    const buyBal = c.holdings[S.buy] || 0;
    R.balSell.textContent = S.address ? fmtAmount(sellBal) : '';
    R.balBuy.textContent = S.address ? fmtAmount(buyBal) : '';

    const sellPrice = (c.prices[S.sell] || {}).price || 0;
    const sellAmt = parseFloat(S.sellRaw) || 0;
    // Sell sub: USD value + shares note when selling a stock token
    if (sellAmt > 0 && sellPrice > 0) {
      const mSell = ((c.assets[S.sell] || {}).multiplier) || null;
      const sellUsd = fmtUsd(sellAmt * sellPrice);
      if (mSell > 0) {
        const shares = sellAmt * mSell;
        R.subSell.textContent = `${sellAmt} tokens \u00d7 ${mSell} \u2248 ${fmtAmount(shares)} shares \u2014 ${sellUsd}`;
      } else {
        R.subSell.textContent = sellUsd;
      }
    } else {
      R.subSell.textContent = '';
    }

    if (S.quoting) {
      R.outBuy.textContent = '';
      R.outBuy.classList.add('is-loading');
    } else {
      R.outBuy.classList.remove('is-loading');
      R.outBuy.textContent = S.order && S.order.uiOutAmount ? fmtAmount(S.order.uiOutAmount) : '0';
    }
    const buyPrice = (c.prices[S.buy] || {}).price || 0;
    const outUi = S.order ? S.order.uiOutAmount : 0;
    let subBuy = '';
    if (outUi > 0 && buyPrice > 0) {
      const mBuy = ((c.assets[S.buy] || {}).multiplier) || null;
      if (mBuy > 0) {
        const shares = outUi * mBuy;
        subBuy = `${fmtAmount(outUi)} tokens \u00d7 ${mBuy} \u2248 ${fmtAmount(shares)} shares`;
      } else {
        subBuy = fmtUsd(outUi * buyPrice);
      }
      if (sellAmt > 0 && sellPrice > 0 && !(mBuy > 0)) {
        const inUsd = sellAmt * sellPrice;
        const outUsd = outUi * buyPrice;
        if (inUsd > 0) {
          const diff = (outUsd / inUsd - 1) * 100;
          if (diff < -0.005) subBuy += ` (${diff.toFixed(1).replace('-', '')}%)`;
        }
      }
    }
    R.subBuy.textContent = subBuy;
    R.subBuy.className = 'trd-card-sub' + (subBuy.includes('(') ? ' neg' : '');

    const hasOrder = !!(S.order && S.order.uiOutAmount); // quote-only is fine before connect
    const mode = modeLabel(c);
    R.infoRow.hidden = !hasOrder && !mode;
    if (hasOrder) {
      R.rate.textContent = S.order.rate ? `1 ${S.sell} \u2248 ${fmtAmount(S.order.rate)} ${S.buy}` : '';
      R.gasless.hidden = !S.order.gasless;
      const feeBps = S.order.transferFeeBps || 0;
      if (feeBps > 0) {
        R.warnChip.hidden = false;
        R.warnChip.innerHTML = ICON.warn + '1';
      } else {
        R.warnChip.hidden = true;
      }
    } else {
      R.rate.textContent = '';
      R.gasless.hidden = true;
      R.warnChip.hidden = true;
    }
    if (mode) {
      R.modeChip.hidden = false;
      R.modeChip.textContent = mode;
      R.modeChip.className = 'trd-mode-chip trd-mode-' + mode.toLowerCase();
    } else {
      R.modeChip.hidden = true;
    }

    if (!S.swapping && !S.note && S.order && !S.order.uiOutAmount && mode === 'RFQ') {
      R.note.textContent = 'No RFQ inventory. AMM still live on xStocks.';
    } else {
      R.note.textContent = S.swapping ? '' : (S.note || '');
    }

    renderCta();
  }

  function renderCta() {
    const b = R.cta;
    if (S.swapping) {
      if (b.dataset.busy !== '1') {
        b.dataset.busy = '1';
        b.innerHTML = '<span class="trd-dots"><i></i><i></i><i></i><i></i></span>Swapping';
      }
      b.disabled = true;
      b.classList.add('busy');
      return;
    }
    if (b.dataset.busy === '1') { b.dataset.busy = ''; b.classList.remove('busy'); }

    const amt = parseFloat(S.sellRaw) || 0;
    const c = S.host.getCtx();
    const bal = c.holdings[S.sell] || 0;
    if (!S.address) { b.textContent = 'Connect wallet'; b.disabled = false; return; }
    if (!(amt > 0)) { b.textContent = 'Enter Amount'; b.disabled = true; return; }
    if (amt > bal * (1 + 1e-9)) { b.textContent = 'Insufficient Balance'; b.disabled = true; return; }
    if (S.quoting) { b.textContent = 'Getting Price....'; b.disabled = true; return; }
    if (!S.order || !S.order.uiOutAmount) { b.textContent = 'Swap'; b.disabled = true; return; }
    b.textContent = 'Swap';
    b.disabled = false;
  }

  // ---- Price Info modal ------------------------------------------------------
  const shortMint = (m) => (m && m.length > 8 ? m.slice(0, 4) + '...' + m.slice(-4) : m || '');

  function openInfo() {
    if (!S || !S.order) return;
    const o = S.order;
    const feeBps = o.transferFeeBps || 0;
    R.feeWarn.hidden = feeBps <= 0;
    if (feeBps > 0) {
      const pct = (feeBps / 100).toFixed(feeBps % 100 === 0 ? 0 : 2);
      R.feeTitle.textContent = `${pct}% Transfer Fees`;
      R.feeSub.textContent = `This token has a transfer fee of ${pct}%`;
    }
    R.iRate.textContent = o.rate ? `1 ${S.sell} \u2248 ${fmtAmount(o.rate)} ${S.buy}` : '\u2014';
    R.iImpact.textContent = o.priceImpactPct !== undefined && o.priceImpactPct !== null
      ? `< ${(Math.abs(parseFloat(o.priceImpactPct)) || 0).toFixed(2)}%` : '\u2014';
    R.iMin.textContent = o.uiMinReceived ? `${fmtAmount(o.uiMinReceived)} ${S.buy}` : '\u2014';
    const feeBpsTotal = o.feeBps || 0;
    R.iFees.textContent = feeBpsTotal ? `${(feeBpsTotal / 100).toFixed(1)}%` : (feeBps ? `${(feeBps / 100).toFixed(1)}%` : '\u2014');
    const execLabel = o.executionMode === 'RFQ' ? 'RFQ' : o.executionMode === 'SWAP' ? 'AMM' : '';
    R.iRoutes.textContent = (o.routes && o.routes.length)
      ? o.routes.join(', ') + (execLabel ? ` (${execLabel})` : '')
      : '\u2014';
    const c = S.host.getCtx();
    R.iIn.innerHTML = '';
    R.iIn.appendChild(document.createTextNode(shortMint((c.assets[S.sell] || {}).mint) + ' '));
    R.iIn.insertAdjacentHTML('beforeend', ICON.external);
    R.iOut.innerHTML = '';
    R.iOut.appendChild(document.createTextNode(shortMint((c.assets[S.buy] || {}).mint) + ' '));
    R.iOut.insertAdjacentHTML('beforeend', ICON.external);

    R.infoModal.hidden = false;
    document.documentElement.classList.add('trd-tok-lock');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      R.infoBack.classList.add('open');
      R.infoModal.querySelector('.trd-info-sheet').classList.add('open');
    }));
  }

  function closeInfo() {
    document.documentElement.classList.remove('trd-tok-lock');
    R.infoBack.classList.remove('open');
    const sheet = R.infoModal.querySelector('.trd-info-sheet');
    sheet.classList.remove('open');
    setTimeout(() => { R.infoModal.hidden = true; }, 260);
  }

  // ---- Swap ------------------------------------------------------------------
  async function doSwap() {
    if (!S || S.swapping || !S.order || !S.order.uiOutAmount) return;
    if (S.order.executionMode !== 'RFQ' && !S.order.transaction && S.order.deepLink) {
      window.open(S.order.deepLink, '_blank', 'noopener'); return;
    }
    if (S.order.executionMode !== 'RFQ' && !S.order.transaction) { scheduleQuote(); return; }
    const sess = S;
    sess.swapping = true;
    sess.note = '';
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
        let signed;
        try {
          signed = await window.MarktapeWallet.signTransactionForSend(sess.order.transaction);
        } catch (err) {
          const rejected = (err && err.code === 4001) || /reject|declin|denied|cancel/i.test(String((err && err.message) || ''));
          throw new Error(rejected ? 'Cancelled' : 'Wallet could not sign the transaction');
        }
        if (S !== sess) return;

        if (!signed.signedTransactionBase64) {
          // Wallet could only sign-and-send itself (already broadcast) — nothing
          // to relay through /execute.
        } else {
          const res = await postJSON('/api/swap/execute', {
            signedTransaction: signed.signedTransactionBase64,
            requestId: sess.order.requestId,
            provider: sess.order.provider || 'binance_web3',
          });
          if (res.status && res.status !== 'Success' && res.status !== 'success') {
            throw new Error('Swap failed on-chain, please try again');
          }
        }
      }

      const host = sess.host;
      const outAmt = sess.order.uiOutAmount;
      const buySym = sess.buy;
      if (S === sess) close();
      if (window.MarktapeSend && window.MarktapeSend.toast) {
        window.MarktapeSend.toast(`Swapped for ${fmtAmount(outAmt)} ${buySym}`);
      }
      if (host.onDone) host.onDone();
    } catch (err) {
      if (S !== sess) return;
      sess.swapping = false;
      sess.note = err.message || 'Something went wrong';
      render();
    }
  }

  // ---- Public ------------------------------------------------------------------
  function pickDefaultSell(c) {
    const value = (s) => (c.holdings[s] || 0) * (c.prices[s] ? c.prices[s].price : 0);
    if (value('BNB') > 0) return 'BNB';
    const held = Object.keys(c.prices).filter((s) => value(s) > 0).sort((a, b) => value(b) - value(a));
    return held[0] || 'BNB';
  }

  function pickDefaultBuy(c, sell) {
    const stock = Object.keys(c.prices).find((s) => c.assets[s] && c.assets[s].kind === 'stock' && s !== sell);
    return stock || Object.keys(c.prices).find((s) => s !== sell) || sell;
  }

  function open(host) {
    build();
    const c = host.getCtx();
    const sell = pickDefaultSell(c);
    S = {
      host, address: c.address || null, sell, buy: pickDefaultBuy(c, sell),
      sellRaw: '', order: null, quoting: false, quoteTimer: null, quoteReq: 0,
      note: '', swapping: false,
    };
    root.setAttribute('aria-hidden', 'false');
    root.classList.add('open');
    render();
  }

  function close() {
    if (!S) return;
    clearTimeout(S.quoteTimer);
    S.sellRaw = '';
    resetQuote();
    S.swapping = false;
    if (R.inputSell) R.inputSell.blur();
    if (R.tok) R.tok.hidden = true;
    if (R.infoModal) R.infoModal.hidden = true;
    document.documentElement.classList.remove('trd-tok-lock');
    render();
  }

  function isOpen() { return !!S; }
  function panelEl() { build(); return root; }
  function refresh() { if (S) render(); }

  window.MarktapeTrade = { open, close, isOpen, panelEl, mount, refresh };
})();
