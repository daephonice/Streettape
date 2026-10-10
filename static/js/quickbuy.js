/* "Buy cheapest X for $N": /api/quickbuy picks and sanity-checks the wrapper, then the existing swap sheet opens. Quote only. */
(function () {
  const $ = (id) => document.getElementById(id);
  const sym = $('qb-sym'), amts = $('qb-amts'), go = $('qb-go'), note = $('qb-note');
  if (!sym || !go) return;
  const on = amts && amts.querySelector('button.on');
  const st = { usd: on ? Number(on.dataset.usd) || 5 : 5, busy: false, prices: {}, assets: {}, holdings: {} };
  const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(url + ' ' + r.status);
    return r.json();
  }
  const label = () => { go.textContent = `Buy cheapest ${sym.value || ''} for $${st.usd}`; };

  async function loadNames() {
    try {
      const d = await getJSON('/api/board');
      const names = (d.groups || []).map((g) => g.underlying);
      if (!names.length) return;
      const cur = sym.value;
      sym.textContent = '';
      names.forEach((n) => { const o = document.createElement('option'); o.value = n; o.textContent = n; sym.appendChild(o); });
      sym.value = names.includes(cur) ? cur : (names.includes('NVDA') ? 'NVDA' : names[0]);
      label();
    } catch (e) { setTimeout(loadNames, 3000); }
  }

  async function loadCtx() {
    const addr = window.MarktapeWallet ? window.MarktapeWallet.getAddress() : null;
    const [p, a, b] = await Promise.all([
      getJSON('/api/prices').catch(() => ({})),
      getJSON('/api/assets').catch(() => ({})),
      addr ? getJSON('/api/balances/' + encodeURIComponent(addr)).catch(() => ({})) : Promise.resolve({}),
    ]);
    st.prices = p.prices || {}; st.assets = a.assets || {}; st.holdings = b.holdings || {};
    return addr;
  }

  amts.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-usd]');
    if (!b) return;
    st.usd = Number(b.dataset.usd);
    amts.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    label();
  });
  sym.addEventListener('change', () => { note.textContent = ''; label(); });

  go.addEventListener('click', async () => {
    if (st.busy || !sym.value) return;
    st.busy = true; go.disabled = true; note.textContent = 'Checking quote…';
    try {
      const r = await getJSON(`/api/quickbuy/${encodeURIComponent(sym.value)}?usd=${st.usd}`);
      const rows = (r.candidates || []).map((c) => {
        const px = c.perShare ? usd.format(c.perShare) + '/sh' : 'no price';
        const g = c.gapBps == null ? '' : ' ' + (c.gapBps > 0 ? '+' : '') + c.gapBps + ' bps';
        return `${c.symbol} ${px}${g} · ${c.win ? 'picked' : c.ok ? 'ok' : 'rejected: ' + c.reason}`;
      });
      if (!r.ok) { note.textContent = [...rows, r.reason || 'Not available right now'].join('\n'); return; }
      note.textContent = [...rows, 'You sign in your wallet.'].join('\n');
      let address = await loadCtx();
      const ok = window.MarktapeSwap && window.MarktapeSwap.open({
        side: 'buy', symbol: String(r.symbol).toUpperCase(), label: r.symbol, title: r.underlying, stocks: [],
        getCtx: () => ({ address: window.MarktapeWallet ? window.MarktapeWallet.getAddress() : address, holdings: st.holdings, prices: st.prices, assets: st.assets }),
        onDone: () => {},
        connect: () => window.MarktapeWallet && window.MarktapeWallet.connectWithPicker(),
        pay: 'USDT', amount: st.usd,
      });
      if (!ok) note.textContent = 'Could not open the swap sheet for this pair';
    } catch (e) {
      note.textContent = 'Quote failed, try again';
    } finally { st.busy = false; go.disabled = false; }
  });

  loadNames();
})();
