/* "Watch X at N% floor": /api/agent/floor reads held wrappers and proposes a sell into USDT when room is thin. Proposal only. */
(function () {
  const $ = (id) => document.getElementById(id);
  const sym = $('fw-sym'), pcts = $('fw-pcts'), go = $('fw-go'), note = $('fw-note'), link = $('fw-link');
  if (!sym || !go) return;
  const st = { pct: 95, busy: false };
  let nt = null;
  const hideAll = () => { note.textContent = ''; note.className = 'hp-note'; link.hidden = true; };
  const arm = () => { clearTimeout(nt); nt = setTimeout(hideAll, 10000); };
  const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
  const label = () => { go.textContent = `Watch ${sym.value || ''} at ${st.pct}% floor`; };

  async function loadNames() {
    try {
      const r = await fetch('/api/board', { cache: 'no-store' });
      const names = ((await r.json()).groups || []).map((g) => g.underlying);
      if (!names.length) return;
      sym.textContent = '';
      names.forEach((n) => { const o = document.createElement('option'); o.value = n; o.textContent = n; sym.appendChild(o); });
      sym.value = names.includes('NVDA') ? 'NVDA' : names[0];
      label();
    } catch (e) { setTimeout(loadNames, 3000); }
  }

  pcts.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-pct]');
    if (!b) return;
    st.pct = Number(b.dataset.pct);
    pcts.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    label();
  });
  sym.addEventListener('change', () => { hideAll(); label(); });

  go.addEventListener('click', async () => {
    const addr = window.MarktapeWallet ? window.MarktapeWallet.getAddress() : null;
    if (!addr) { note.textContent = 'Connect wallet first'; arm(); return; }
    if (st.busy) return;
    st.busy = true; go.disabled = true; link.hidden = true; note.className = 'hp-note'; note.textContent = 'Checking…'; arm();
    try {
      const r = await fetch(`/api/agent/floor?address=${encodeURIComponent(addr)}&underlying=${encodeURIComponent(sym.value)}&floor=${st.pct / 100}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      const head = d.room == null ? '' : `Room ${(d.room * 100).toFixed(2)}% · cushion ${usd.format(d.cushionUsd)} vs ${st.pct}% of ${d.reference}\n`;
      if (!d.proposal) { note.textContent = head + (d.reason || 'cushion ok'); arm(); return; }
      const legs = d.proposal.legs.map((l) => `Sell ${l.amount.toPrecision(5)} ${l.symbol} (~${usd.format(l.usd)}) → ${l.outAmount != null ? Number(l.outAmount).toFixed(2) + ' USDT' : l.reason}`);
      note.className = 'hp-note is-alert';
      note.textContent = [head + d.proposal.reason, ...legs, 'You sign in your wallet.'].join('\n'); arm();
      link.href = `/t/${encodeURIComponent(d.underlying)}`; link.hidden = false; arm();
    } catch (e) { note.textContent = 'Floor check failed, try again'; arm(); }
    finally { st.busy = false; go.disabled = false; }
  });
  loadNames();
})();
