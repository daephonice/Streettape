(function () {
  const $ = (id) => document.getElementById(id);
  const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
  const usd = (v) => (v == null ? '—' : '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v) => (v == null ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(2) + '%');
  const when = (iso) => new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) + ' ET';
  const reconv = (r) => (r.status === 'converged' ? r.minutes + ' min' : r.status);

  function cell(label, value) {
    const d = el('div', 'sb-cell');
    d.appendChild(el('small', null, label));
    d.appendChild(el('b', null, value));
    return d;
  }

  function render(d) {
    $('sb-sub').textContent = 'closed ' + when(d.closedAt) + ' → opens ' + when(d.opensAt) + (d.complete ? '' : ' · in progress');
    $('sb-note').textContent = 'Official = last cash print at the close. Gap = tape / official − 1. Back = minutes after the open until tape is within ' + (d.reconvergePct * 100) + '% of cash.';
    const box = $('sb-rows'); box.textContent = '';
    if (!d.rows.length) box.appendChild(el('p', 'bd-last-empty', 'No tape stored for this session.'));
    d.rows.forEach((r) => {
      const c = el('section', 'hm-card sb-row');
      const h = el('div', 'bd-bk-head');
      const a = el('a', 'bd-bk-name', r.underlying); a.href = '/t/' + encodeURIComponent(r.underlying);
      h.appendChild(a);
      h.appendChild(el('span', 'bd-bk-state', r.widest ? pct(r.widest.gap) + ' · ' + r.widest.wrapper : 'no gap'));
      c.appendChild(h);
      const g = el('div', 'sb-grid');
      g.appendChild(cell('Friday official' + (r.officialSource === 'fallback' ? ' (fallback)' : ''), r.official == null ? '—' : usd(r.official)));
      g.appendChild(cell('Monday open', usd(r.mondayOpen)));
      g.appendChild(cell('Back to print', reconv(r.reconverge)));
      c.appendChild(g);
      r.wrappers.forEach((w) => {
        const line = el('p', 'bd-last-row',
          w.symbol + (w.thin ? ' (thin)' : '') + ' · low ' + usd(w.minTape) + ' · high ' + usd(w.maxTape) +
          ' · first ' + usd(w.firstTape) + ' · last ' + usd(w.lastTape) +
          (w.maxGap ? ' · max ' + pct(w.maxGap.gap) + ' @ ' + when(w.maxGap.at) : '') +
          ' · back ' + reconv(w.reconverge));
        c.appendChild(line);
      });
      box.appendChild(c);
    });
    const m = d.meta;
    $('sb-meta').textContent = m.snapshots + ' wrapper snapshots · no Friday cash: ' + (m.noFridayCash.join(', ') || 'none') +
      ' · no tape: ' + (m.noTapeWrappers.join(', ') || 'none');
  }

  const q = new URLSearchParams(location.search).get('closed_at');
  fetch('/api/session' + (q ? '?closed_at=' + encodeURIComponent(q) : ''), { cache: 'no-store' })
    .then((r) => r.json().then((j) => { if (!r.ok) throw new Error(j.detail || r.status); return j; }))
    .then(render)
    .catch((e) => { $('sb-sub').textContent = 'failed: ' + e.message; });
})();
