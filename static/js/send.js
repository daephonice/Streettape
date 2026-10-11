/* Send flow (homepage + token page): sheet "Send to address" -> sheet
 * "Receiving address" -> full amount panel -> blue toast.
 *
 * The server builds the transfer and relays it through its RPC
 * (/api/send/build, /api/send/submit); the wallet only signs.
 * Host (home.js / token.js) calls MarktapeSend.open({ getCtx, onSent, symbol? })
 * where getCtx() -> { address, holdings, prices, assets } (live state) and
 * `symbol` (optional) preselects the token. MarktapeSend.toast(text) shows the
 * shared blue success toast (also used by swap.js).
 */
(function () {
  'use strict';

  const TOAST_MS = 2500;
  const ANIM_MS = 260;
  const BNB_RESERVE = 0.00005; // left behind when filling max BNB (gas)
  const DECIMALS = { BNB: 18, USDT: 18, USDC: 18 };
  const EVM_RE = /^0x[a-fA-F0-9]{40}$/;

  const ICON = {
    plane: '<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13" fill="none"/></svg>',
    back: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    swap: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="m21 8-4-4-4 4"/><path d="M17 4v16"/></svg>',
    wallet: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/></svg>',
    check: '<svg viewBox="0 0 24 24" width="24" height="24"><circle cx="12" cy="12" r="11" fill="#06241e"/><path d="m7.5 12.5 3 3 6-6.5" fill="none" stroke="#7df9d0" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  const usdFmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
  const fmtUsd = (v) => (v > 0 && v < 0.005 ? '<$0.01' : usdFmt.format(v));
  function fmtAmount(v) {
    const max = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
    return v.toLocaleString('en-US', { maximumFractionDigits: max });
  }
  const decimalsOf = (sym) => DECIMALS[sym] || 6;

  // Truncates (never rounds up) to `dec` decimals, trailing zeros trimmed.
  function trunc(v, dec) {
    if (!(v > 0)) return '0';
    const [i, f = ''] = v.toFixed(Math.min(dec + 3, 20)).split('.');
    const frac = f.slice(0, dec).replace(/0+$/, '');
    return frac ? i + '.' + frac : i;
  }

  function sanitize(str, maxDec) {
    str = str.replace(',', '.').replace(/[^\d.]/g, '');
    const i = str.indexOf('.');
    if (i !== -1) str = str.slice(0, i + 1) + str.slice(i + 1).replace(/\./g, '').slice(0, maxDec);
    if (str.startsWith('.')) str = '0' + str;
    return str.replace(/^0+(?=\d)/, '').slice(0, 14);
  }

  function isValidAddress(s) {
    return EVM_RE.test(s || '');
  }

  const shortAddr = (a) => a.slice(0, 4) + '...' + a.slice(-4);

  async function postJSON(url, body) {
    let resp;
    try {
      resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (_) {
      throw new Error('Network error, try again');
    }
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) throw new Error(data && typeof data.detail === 'string' ? data.detail : 'Something went wrong, try again');
    return data;
  }

  // ---- DOM -----------------------------------------------------------------
  let root = null;
  const R = {};
  let S = null; // open session, null when closed

  function logoEl(meta, size) {
    const w = document.createElement('span');
    w.className = 'snd-logo';
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
    root = document.createElement('div');
    root.className = 'snd-root';
    root.hidden = true;
    root.innerHTML = `
      <div class="snd-backdrop"></div>
      <div class="snd-sheet" role="dialog" aria-modal="true" aria-label="Send">
        <div class="snd-handle"></div>
        <div class="snd-sheet-body"></div>
      </div>
      <section class="snd-panel" aria-label="Enter amount">
        <header class="snd-col snd-head">
          <button type="button" class="snd-back" aria-label="Back">${ICON.back}</button>
          <div><div class="snd-head-title">Enter Amount</div><div class="snd-head-to"></div></div>
        </header>
        <div class="snd-col snd-mid">
          <button type="button" class="snd-pill" aria-label="Select token"><span class="snd-pill-logo"></span><span class="snd-pill-sym"></span>${ICON.chevron}</button>
          <div class="snd-amount-row"><span class="snd-dollar" hidden>$</span><input class="snd-amount" type="text" inputmode="decimal" autocomplete="off" autocorrect="off" spellcheck="false" placeholder="0" aria-label="Amount"></div>
          <div class="snd-sub"><span class="snd-sub-text"></span><button type="button" class="snd-switch" aria-label="Switch between token and USD">${ICON.swap}</button></div>
          <button type="button" class="snd-bal" aria-label="Use available balance">${ICON.wallet}<span></span></button>
          <p class="snd-note" role="alert"></p>
        </div>
        <footer class="snd-col snd-foot"><button type="button" class="snd-cta">Confirm &amp; Send</button></footer>
        <div class="snd-tok" hidden>
          <div class="snd-tok-back"></div>
          <div class="snd-tok-sheet"><div class="snd-handle"></div><h3 class="snd-title">Select token</h3><div class="snd-tok-list"></div></div>
        </div>
      </section>`;
    document.body.appendChild(root);

    const q = (s) => root.querySelector(s);
    Object.assign(R, {
      backdrop: q('.snd-backdrop'), sheet: q('.snd-sheet'), sheetBody: q('.snd-sheet-body'),
      panel: q('.snd-panel'), back: q('.snd-back'), to: q('.snd-head-to'),
      pill: q('.snd-pill'), pillLogo: q('.snd-pill-logo'), pillSym: q('.snd-pill-sym'),
      dollar: q('.snd-dollar'), input: q('.snd-amount'), sub: q('.snd-sub-text'), sw: q('.snd-switch'),
      bal: q('.snd-bal'), balText: q('.snd-bal span'), note: q('.snd-note'), cta: q('.snd-cta'),
      tok: q('.snd-tok'), tokBack: q('.snd-tok-back'), tokList: q('.snd-tok-list'),
    });

    R.backdrop.addEventListener('click', () => { if (S && S.stage !== 'amount') close(); });
    R.back.addEventListener('click', backToAddress);
    R.pill.addEventListener('click', openTokens);
    R.tokBack.addEventListener('click', () => { R.tok.hidden = true; });
    R.tokList.addEventListener('click', (e) => {
      const row = e.target.closest('[data-sym]');
      if (row) selectToken(row.dataset.sym);
    });
    R.input.addEventListener('input', () => {
      S.raw = sanitize(R.input.value, maxDec());
      S.max = false;
      R.input.value = S.raw;
      setNote('');
      renderPanel();
    });
    R.sw.addEventListener('click', switchMode);
    R.bal.addEventListener('click', fillMax);
    R.cta.addEventListener('click', doSend);

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !S || S.sending) return;
      if (!R.tok.hidden) R.tok.hidden = true;
      else close();
    });
    window.addEventListener('marktape:wallet', (e) => {
      if (S && e.detail.address !== S.address) close();
    });
    if (window.visualViewport) {
      const sync = () => {
        const vv = window.visualViewport;
        const kb = Math.round(window.innerHeight - vv.height - vv.offsetTop);
        root.style.setProperty('--snd-kb', (kb > 80 ? kb : 0) + 'px');
      };
      window.visualViewport.addEventListener('resize', sync);
      window.visualViewport.addEventListener('scroll', sync);
    }
  }

  // ---- Stages ----------------------------------------------------------------
  function showMenu() {
    S.stage = 'menu';
    R.sheet.classList.remove('is-address');
    R.sheetBody.innerHTML = `
      <h3 class="snd-title">Send</h3>
      <button type="button" class="snd-opt">
        <span class="snd-opt-ic">${ICON.plane}</span>
        <span class="snd-opt-tx"><b>Send to address</b><small>Send to a BNB Chain address</small></span>
      </button>`;
    R.sheetBody.querySelector('.snd-opt').addEventListener('click', showAddress);
  }

  function showAddress() {
    S.stage = 'address';
    R.sheet.classList.add('is-address');
    R.sheetBody.innerHTML = `
      <div class="snd-card">
        <div class="snd-card-label">Receiving address</div>
        <div class="snd-card-row">
          <input class="snd-addr" type="text" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="go" placeholder="Enter 0x address" aria-label="Receiving address">
          <button type="button" class="snd-paste">Paste</button>
        </div>
      </div>
      <p class="snd-note"></p>
      <button type="button" class="snd-cta snd-continue" hidden>Continue</button>`;
    const input = R.sheetBody.querySelector('.snd-addr');
    const note = R.sheetBody.querySelector('.snd-note');
    const cont = R.sheetBody.querySelector('.snd-continue');
    input.value = S.to;
    cont.hidden = !S.to;

    const onInput = () => {
      S.to = input.value.trim();
      cont.hidden = !S.to;
      note.textContent = '';
    };
    input.addEventListener('input', onInput);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') cont.click(); });
    R.sheetBody.querySelector('.snd-paste').addEventListener('click', async () => {
      try {
        input.value = (await navigator.clipboard.readText()).trim();
        onInput();
      } catch (_) {
        input.focus();
        note.textContent = 'Paste blocked — long-press the field to paste';
      }
    });
    cont.addEventListener('click', () => {
      if (!isValidAddress(S.to)) { note.textContent = 'Enter a valid BNB Chain address'; return; }
      if (S.to === S.address) { note.textContent = "You can't send to your own address"; return; }
      showAmount();
    });
    setTimeout(() => { if (S && S.stage === 'address') input.focus({ preventScroll: true }); }, ANIM_MS);
  }

  function showAmount() {
    S.stage = 'amount';
    if (document.activeElement) document.activeElement.blur();
    R.to.textContent = 'To: ' + shortAddr(S.to);
    R.input.value = S.raw;
    setNote('');
    R.backdrop.classList.remove('open');
    R.sheet.classList.remove('open');
    R.panel.classList.add('open');
    renderPanel();
    S.tick = setInterval(renderPanel, 1000);
    setTimeout(() => { if (S && S.stage === 'amount') R.input.focus({ preventScroll: true }); }, ANIM_MS + 40);
  }

  function backToAddress() {
    if (!S || S.sending) return;
    clearInterval(S.tick);
    R.tok.hidden = true;
    R.input.blur();
    R.panel.classList.remove('open');
    R.backdrop.classList.add('open');
    R.sheet.classList.add('open');
    showAddress();
  }

  function close() {
    if (!S) return;
    clearInterval(S.tick);
    S = null;
    R.input.blur();
    R.tok.hidden = true;
    R.backdrop.classList.remove('open');
    R.sheet.classList.remove('open');
    R.panel.classList.remove('open');
    document.documentElement.classList.remove('snd-lock');
    setTimeout(() => { if (!S) root.hidden = true; }, ANIM_MS + 40);
  }

  // ---- Amount panel ------------------------------------------------------------
  const maxDec = () => (S.mode === 'usd' ? 2 : decimalsOf(S.sym));

  function calc() {
    const c = S.host.getCtx();
    const p = c.prices[S.sym];
    const price = p ? p.price : 0;
    const bal = c.holdings[S.sym] || 0;
    const v = parseFloat(S.raw);
    let coin = 0;
    let usd = 0;
    if (S.max) {
      coin = bal;
      usd = bal * price;
    } else if (v > 0 && price > 0) {
      if (S.mode === 'coin') { coin = v; usd = v * price; } else { usd = v; coin = v / price; }
    }
    const over = !S.max && coin > bal * (1 + 1e-9);
    return { price, bal, coin, usd, over, ok: coin > 0 && price > 0 && !over };
  }

  function setNote(msg) { R.note.textContent = msg || ''; }

  function renderPanel() {
    if (!S || S.stage !== 'amount') return;
    const c = S.host.getCtx();
    const meta = c.assets[S.sym] || { name: S.sym };
    if (R.pillSym.textContent !== S.sym) {
      R.pillLogo.replaceChildren(logoEl(meta, 32));
      R.pillSym.textContent = S.sym;
    }
    R.dollar.hidden = S.mode !== 'usd';
    if (R.input.value !== S.raw) R.input.value = S.raw;
    const len = Math.max(1, S.raw.length);
    R.input.style.width = len + 'ch';
    R.input.parentElement.dataset.size = len <= 7 ? 'l' : len <= 10 ? 'm' : 's';

    const d = calc();
    R.sub.textContent = S.mode === 'coin' ? fmtUsd(d.usd) : `${d.coin > 0 ? fmtAmount(d.coin) : '0'} ${S.sym}`;
    R.balText.textContent = `${fmtAmount(d.bal)} ${S.sym}`;
    R.bal.classList.toggle('over', d.over);
    renderCta(d);
  }

  function renderCta(d) {
    const b = R.cta;
    if (S.sending) {
      if (b.dataset.busy !== '1') {
        b.dataset.busy = '1';
        b.innerHTML = '<span class="snd-dots"><i></i><i></i><i></i><i></i></span>Sending';
      }
      b.disabled = true;
      b.classList.add('busy');
      return;
    }
    if (b.dataset.busy === '1') {
      b.dataset.busy = '';
      b.textContent = 'Confirm & Send';
      b.classList.remove('busy');
    }
    b.disabled = !d.ok;
  }

  function switchMode() {
    if (S.sending) return;
    const d = calc();
    if (S.mode === 'coin') {
      S.mode = 'usd';
      S.raw = d.coin > 0 ? trunc(d.usd, 2) : '';
    } else {
      S.mode = 'coin';
      S.raw = d.coin > 0 ? trunc(d.coin, Math.min(decimalsOf(S.sym), 6)) : '';
    }
    renderPanel();
    R.input.focus({ preventScroll: true });
  }

  function fillMax() {
    if (S.sending) return;
    const d = calc();
    if (!(d.bal > 0)) return;
    let coin = d.bal;
    S.max = false;
    if (S.sym === 'BNB') coin = d.bal - BNB_RESERVE; // keep dust for the network fee
    else S.max = true;                              // tokens: server sends the exact full balance
    if (!(coin > 0)) return;
    S.raw = S.mode === 'coin' ? trunc(coin, decimalsOf(S.sym)) : trunc(coin * d.price, 2);
    setNote('');
    renderPanel();
  }

  // ---- Token selector ------------------------------------------------------------
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

  function openTokens() {
    if (S.sending) return;
    const c = S.host.getCtx();
    R.tokList.textContent = '';
    tokenOrder(c).forEach((sym) => {
      const meta = c.assets[sym] || { name: sym };
      const bal = c.holdings[sym] || 0;
      const row = document.createElement('button');
      row.type = 'button';
      row.dataset.sym = sym;
      row.className = 'snd-tok-row' + (bal > 0 ? '' : ' empty') + (sym === S.sym ? ' active' : '');
      row.appendChild(logoEl(meta, 40));
      const main = document.createElement('span');
      main.className = 'snd-tok-main';
      main.innerHTML = '<b></b><small></small>';
      main.firstChild.textContent = sym;
      main.lastChild.textContent = meta.name || sym;
      row.appendChild(main);
      const side = document.createElement('span');
      side.className = 'snd-tok-side';
      side.innerHTML = '<b></b><small></small>';
      side.firstChild.textContent = bal > 0 ? fmtAmount(bal) : '0';
      side.lastChild.textContent = bal > 0 ? fmtUsd(bal * c.prices[sym].price) : '';
      row.appendChild(side);
      R.tokList.appendChild(row);
    });
    R.tok.hidden = false;
  }

  function selectToken(sym) {
    R.tok.hidden = true;
    if (sym !== S.sym) {
      S.sym = sym;
      S.max = false;
      if (S.mode === 'coin') S.raw = '';
      setNote('');
    }
    renderPanel();
    R.input.focus({ preventScroll: true });
  }

  // ---- Send ----------------------------------------------------------------------
  async function doSend() {
    if (!S || S.sending) return;
    const d = calc();
    if (!d.ok) return;
    const sess = S;
    const dec = decimalsOf(S.sym);
    const amount = S.max ? trunc(d.bal, dec) : S.mode === 'coin' ? S.raw.replace(/\.$/, '') : trunc(d.coin, dec);

    sess.sending = true;
    setNote('');
    renderPanel();
    try {
      const built = await postJSON('/api/send/build', {
        fromAddress: sess.address,
        toAddress: sess.to,
        symbol: sess.sym,
        amount,
        sendMax: sess.max && sess.sym !== 'BNB',
      });
      if (S !== sess) return;

      let signed;
      try {
        signed = await window.MarktapeWallet.signTransactionForSend(built.transaction);
      } catch (err) {
        const rejected = (err && err.code === 4001) || /reject|declin|denied|cancel/i.test(String((err && err.message) || ''));
        throw new Error(rejected ? 'Cancelled' : 'Wallet could not sign the transaction');
      }
      if (S !== sess) return;

      const res = await postJSON('/api/send/submit', {
        signedTransaction: signed.signedTransactionBase64 || null,
        signature: signed.signature || null,
        lastValidBlockHeight: built.lastValidBlockHeight,
        payer: sess.address,
      });

      const host = sess.host;
      if (S === sess) close();
      const [i, f = ''] = String(built.amount).split('.');
      const shown = f.slice(0, 6).replace(/0+$/, '');
      toast(`Sent ${shown ? i + '.' + shown : i} ${built.symbol}${res.status === 'pending' ? ' · confirming' : ''}`);
      if (host.onSent) host.onSent();
    } catch (err) {
      if (S !== sess) return;
      sess.sending = false;
      setNote(err.message || 'Something went wrong');
      renderPanel();
    }
  }

  function toast(text, kind) {
    const el = document.createElement('div');
    el.className = 'snd-toast' + (kind === 'error' ? ' err' : '');
    el.setAttribute('role', 'status');
    el.innerHTML = ICON.check + '<span></span>';
    el.lastChild.textContent = text;
    document.body.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 320);
    }, TOAST_MS);
  }

  // ---- Public --------------------------------------------------------------------
  function open(host) {
    const c = host.getCtx();
    if (S || !c.address || !Object.keys(c.prices).length) return;
    build();
    const value = (s) => (c.holdings[s] || 0) * c.prices[s].price;
    const held = Object.keys(c.prices).filter((s) => value(s) > 0).sort((a, b) => value(b) - value(a));
    S = {
      host, address: c.address, stage: 'menu', to: '',
      sym: host.symbol && c.prices[host.symbol] ? host.symbol : value('BNB') > 0 || !held.length ? 'BNB' : held[0],
      mode: 'coin', raw: '', max: false, sending: false, tick: null,
    };
    root.hidden = false;
    document.documentElement.classList.add('snd-lock');
    showMenu();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!S) return;
      R.backdrop.classList.add('open');
      R.sheet.classList.add('open');
    }));
  }

  window.MarktapeSend = { open, toast };
})();
