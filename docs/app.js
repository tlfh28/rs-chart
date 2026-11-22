(() => {
  const LC = LightweightCharts;
  const $ = s => document.querySelector(s);
  const C = { code:0,name:1,market:2,sector:3,mcap:4,close:5,chg:6,rs:7,rs1w:8,rs1m:9,rs3m:10,nh:11,lead:12,fromHi:13,stale:14 };
  let ROWS = [], BY = {}, meta = {};
  const fmt = n => n == null ? '–' : Number(n).toLocaleString('ko-KR');
  const ds = d => { d = String(d); return `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`; };
  const norm = s => s.toLowerCase().replace(/\s+/g, '');

  // ───────── 데이터 로드 ─────────
  fetch('data/summary.json', { cache: 'no-cache' }).then(r => r.json()).then(j => {
    meta = j.meta; ROWS = j.rows;
    ROWS.forEach(r => BY[r[C.code]] = r);
    $('#asof').textContent = `기준일 ${ds(meta.as_of)} · ${fmt(meta.count)}종목`;
    renderTable();
    const h = location.hash.slice(1);
    openStock(BY[h] ? h : '005930', false);
  }).catch(() => { $('#asof').textContent = '데이터를 불러오지 못했습니다'; });

  // ───────── 검색 ─────────
  const q = $('#q'), sugg = $('#sugg');
  let sel = -1, items = [];
  function search(t) {
    t = norm(t); if (!t) return [];
    const starts = [], incl = [];
    for (const r of ROWS) {
      const n = norm(r[C.name]);
      if (r[C.code].startsWith(t) || n.startsWith(t)) starts.push(r);
      else if (n.includes(t)) incl.push(r);
      
    }
    const by = (a, b) => (b[C.mcap] || 0) - (a[C.mcap] || 0);
    const exact = r => (norm(r[C.name]) === t || r[C.code] === t) ? 1 : 0;
    starts.sort((a, b) => exact(b) - exact(a) || by(a, b));
    return starts.concat(incl.sort(by)).slice(0, 15);
  }
  function showSugg() {
    items = search(q.value); sel = items.length ? 0 : -1;
    if (!items.length) { sugg.hidden = true; return; }
    sugg.innerHTML = items.map((r, i) =>
      `<li data-c="${r[C.code]}" class="${i === sel ? 'on' : ''}"><span>${r[C.name]} <small>${r[C.code]} · ${r[C.market]}</small></span><small>RS ${r[C.rs] ?? '–'}</small></li>`).join('');
    sugg.hidden = false;
  }
  q.addEventListener('input', showSugg);
  q.addEventListener('keydown', e => {
    if (sugg.hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      [...sugg.children].forEach((li, i) => li.classList.toggle('on', i === sel));
    } else if (e.key === 'Enter' && sel >= 0) { pick(items[sel][C.code]); }
    else if (e.key === 'Escape') sugg.hidden = true;
  });
  sugg.addEventListener('mousedown', e => { const li = e.target.closest('li'); if (li) pick(li.dataset.c); });
  q.addEventListener('blur', () => setTimeout(() => sugg.hidden = true, 150));
  function pick(code) { sugg.hidden = true; q.value = ''; q.blur(); openStock(code, true); }
  window.addEventListener('hashchange', () => { const h = location.hash.slice(1); if (BY[h]) openStock(h, false); });

  // ───────── 차트 ─────────
  const el = $('#chart');
  const chart = LC.createChart(el, {
    autoSize: true,
    layout: { background: { color: '#fff' }, textColor: '#374151', fontSize: 11,
      panes: { separatorColor: '#e5e7eb', enableResize: true } },
    grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
    rightPriceScale: { borderColor: '#e5e7eb' },
    timeScale: { borderColor: '#e5e7eb', rightOffset: 4 },
    crosshair: { mode: LC.CrosshairMode.Normal },
    localization: { locale: 'ko-KR', dateFormat: 'yyyy-MM-dd' },
  });
  const candle = chart.addSeries(LC.CandlestickSeries, {
    upColor: '#d6336c', downColor: '#1c7ed6', borderUpColor: '#d6336c', borderDownColor: '#1c7ed6',
    wickUpColor: '#d6336c', wickDownColor: '#1c7ed6', priceFormat: { type: 'price', precision: 0, minMove: 1 },
  }, 0);
  const vol = chart.addSeries(LC.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false }, 0);
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.2 } });
  // RS 점수: 70~100만 표시 (70 미만은 잘라냄), 5점 단위 눈금
  const rss = chart.addSeries(LC.LineSeries, {
    color: '#6741d9', lineWidth: 3, priceFormat: { type: 'price', precision: 0, minMove: 1 },
    crosshairMarkerRadius: 5,
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 70, maxValue: 100 }, margins: { above: 0, below: 0 } }),
  }, 1);
  for (let p = 75; p <= 95; p += 10)   // 75·85·95: 옅은 보조선
    rss.createPriceLine({ price: p, color: '#e9e4fb', lineWidth: 1, lineStyle: LC.LineStyle.Solid, axisLabelVisible: true, axisLabelColor: '#f3f0ff', axisLabelTextColor: '#9c8fd0', title: '' });
  [[70, '#9775fa'], [80, '#7048e8'], [90, '#5f3dc4']].forEach(([p, c]) =>  // 10점 단위: 진한 기준선
    rss.createPriceLine({ price: p, color: c, lineWidth: p === 90 ? 2 : 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: '' }));
  rss.priceScale().applyOptions({ ticksVisible: false, textColor: 'rgba(0,0,0,0)', scaleMargins: { top: 0.03, bottom: 0.03 } });
  function setPaneHeights() {
    const ps = chart.panes(); const H = el.clientHeight;
    if (ps.length === 2) { ps[0].setHeight(Math.round(H * 0.55)); ps[1].setHeight(Math.round(H * 0.45)); }
  }

  let cur = null, curRange = 250;
  async function openStock(code, push) {
    const r = BY[code]; if (!r) return;
    if (push) history.pushState(null, '', '#' + code);
    $('#stock').hidden = false;
    $('#s-name').textContent = r[C.name];
    $('#s-sub').textContent = `${r[C.code]} · ${r[C.market]} · ${r[C.sector] || '업종 정보 없음'} · 시총 ${fmt(r[C.mcap])}억`;
    $('#s-rs').textContent = r[C.rs] ?? '–';
    const chg = r[C.chg];
    const badges = [];
    if (r[C.nh]) badges.push('<span class="badge b-nh">RS 라인 52주 신고가</span>');
    if (r[C.lead]) badges.push('<span class="badge b-lead">RS 라인 선행</span>');
    if (r[C.rs] != null && r[C.rs1m] != null && r[C.rs] - r[C.rs1m] >= 20) badges.push('<span class="badge b-up">RS 급상승</span>');
    if (r[C.stale]) badges.push('<span class="badge">최근 거래 없음</span>');
    $('#s-stats').innerHTML = [
      `<div class="stat">종가<b>${fmt(r[C.close])}</b> <span class="${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}${chg}%</span></div>`,
      `<div class="stat">RS 1주 전<b>${r[C.rs1w] ?? '–'}</b></div>`,
      `<div class="stat">1개월 전<b>${r[C.rs1m] ?? '–'}</b></div>`,
      `<div class="stat">3개월 전<b>${r[C.rs3m] ?? '–'}</b></div>`,
      `<div class="stat">52주 고점 대비<b>${r[C.fromHi] ?? '–'}%</b></div>`,
      ...badges].join('');
    document.title = `${r[C.name]} RS ${r[C.rs] ?? ''} · RS 차트`;
    window.scrollTo({ top: 0, behavior: 'smooth' });

    const d = await fetch(`data/s/${code}.json`, { cache: 'no-cache' }).then(x => x.json());
    cur = d;
    const t = d.d.map(ds);
    candle.setData(t.map((time, i) => ({ time, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i] })));
    vol.setData(t.map((time, i) => ({ time, value: d.v[i], color: d.c[i] >= d.o[i] ? 'rgba(214,51,108,.35)' : 'rgba(28,126,214,.35)' })));
    rss.setData(t.map((time, i) => d.rs[i] == null || d.rs[i] < 70 ? { time } : { time, value: d.rs[i] }));  // 70 미만은 표시 안 함
    setPaneHeights();
    applyRange();
    updateLegend(null);
  }

  function applyRange() {
    if (!cur) return; const n = cur.d.length;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - curRange), to: n + 3 });
  }
  $('#range').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    curRange = +b.dataset.r;
    [...$('#range').children].forEach(x => x.classList.toggle('on', x === b));
    applyRange();
  });

  function updateLegend(p) {
    if (!cur) return;
    let i = cur.d.length - 1;
    if (p && p.time) { const k = cur.d.indexOf(+String(p.time.year ? `${p.time.year}${String(p.time.month).padStart(2,'0')}${String(p.time.day).padStart(2,'0')}` : p.time).replace(/-/g, '')); if (k >= 0) i = k; }
    $('#legend').innerHTML = `${ds(cur.d[i])} &nbsp; 시 <b>${fmt(cur.o[i])}</b> 고 <b>${fmt(cur.h[i])}</b> 저 <b>${fmt(cur.l[i])}</b> 종 <b>${fmt(cur.c[i])}</b> &nbsp; 거래량 <b>${fmt(cur.v[i])}</b> &nbsp; RS <b>${cur.rs[i] ?? '–'}</b>`;
  }
  chart.subscribeCrosshairMove(updateLegend);

  // ───────── 순위표 ─────────
  let sortK = C.rs, asc = false, shown = 100;
  const F = { mkt: $('#f-mkt'), cap: $('#f-cap'), rs: $('#f-rs'), sig: $('#f-sig'), sec: $('#f-sec') };
  Object.values(F).forEach(x => x.addEventListener('input', () => { shown = 100; renderTable(); }));
  $('#more').addEventListener('click', () => { shown += 200; renderTable(); });
  document.querySelectorAll('#tbl th').forEach(th => th.addEventListener('click', () => {
    const k = +th.dataset.k;
    if (k === sortK) asc = !asc; else { sortK = k; asc = k === C.name || k === C.sector; }
    renderTable();
  }));
  $('#tbl tbody').addEventListener('click', e => { const tr = e.target.closest('tr'); if (tr) openStock(tr.dataset.c, true); });

  function renderTable() {
    const mkt = F.mkt.value, cap = +F.cap.value, rsMin = +F.rs.value, sig = F.sig.value, sec = norm(F.sec.value);
    let rows = ROWS.filter(r =>
      (!mkt || r[C.market] === mkt) && (!cap || (r[C.mcap] || 0) >= cap) &&
      (!rsMin || (r[C.rs] || 0) >= rsMin) &&
      (!sig || (sig === 'nh' && r[C.nh]) || (sig === 'lead' && r[C.lead]) ||
        (sig === 'up' && r[C.rs] != null && r[C.rs1m] != null && r[C.rs] - r[C.rs1m] >= 20)) &&
      (!sec || norm(r[C.sector] || '').includes(sec)));
    const k = sortK, sgn = asc ? 1 : -1;
    rows.sort((a, b) => {
      let x = a[k], y = b[k];
      if (k === C.nh) { x = a[C.nh] * 2 + a[C.lead]; y = b[C.nh] * 2 + b[C.lead]; }
      if (x == null) return 1; if (y == null) return -1;
      return (typeof x === 'string' ? x.localeCompare(y, 'ko') : x - y) * sgn;
    });
    document.querySelectorAll('#tbl th').forEach(th => {
      th.classList.toggle('sorted', +th.dataset.k === k); th.classList.toggle('asc', +th.dataset.k === k && asc);
    });
    const cell = (v, cls = '') => `<td class="${cls}">${v ?? '–'}</td>`;
    $('#tbl tbody').innerHTML = rows.slice(0, shown).map(r => {
      const sigs = (r[C.nh] ? '<span class="badge b-nh">신고가</span> ' : '') + (r[C.lead] ? '<span class="badge b-lead">선행</span>' : '');
      const c = r[C.chg];
      return `<tr data-c="${r[C.code]}"><td class="l name"><b>${r[C.name]}</b><small>${r[C.code]}</small></td>` +
        cell(r[C.sector], 'l sec') + cell(fmt(r[C.mcap])) + cell(fmt(r[C.close])) +
        cell(`${c >= 0 ? '+' : ''}${c}%`, c > 0 ? 'up' : c < 0 ? 'down' : '') +
        cell(r[C.rs], 'rs-cell') + cell(r[C.rs1w]) + cell(r[C.rs1m]) + cell(r[C.rs3m]) +
        cell(r[C.fromHi] == null ? '–' : r[C.fromHi] + '%') + `<td>${sigs}</td></tr>`;
    }).join('');
    $('#cnt').textContent = `${fmt(rows.length)}종목 중 ${fmt(Math.min(shown, rows.length))}개 표시`;
    $('#more').hidden = shown >= rows.length;
  }
})();
