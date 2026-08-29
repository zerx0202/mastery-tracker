let PATCH = "16.17.1";
const $ = id => document.getElementById(id);
const api = p => fetch("/api" + p).then(r => r.json());
// Data Dragon zna 173 championow, snapshot ma 175 - najnowsi trafiaja tam
// z opoznieniem. Zamiast pustego kwadratu pokazujemy zastepnik.
const BLANK = "data:image/svg+xml," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
     <rect width="40" height="40" rx="5" fill="#1A212C"/>
     <text x="20" y="26" text-anchor="middle" fill="#3D4757"
       font-family="monospace" font-size="18">?</text></svg>`);
const icon = k => k
  ? `https://ddragon.leagueoflegends.com/cdn/${PATCH}/img/champion/${k}.png`
  : BLANK;
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));

const ROMAN = ["I", "II", "III", "IV"];
let GOAL = 4;

/* ---------- szyna milestone ---------- */
function rail(milestone, goal, nextGrade) {
  const cells = [];
  for (let i = 0; i < goal; i++) {
    const cls = i < milestone ? "done" : (i === milestone ? "next" : "");
    cells.push(`<div class="chev ${cls}">${ROMAN[i] || i + 1}</div>`);
  }
  const legend = nextGrade
    ? `Do <b>${ROMAN[milestone] || milestone + 1}</b> trzeba oceny <b>${esc(nextGrade)}</b>`
    : "";
  return `<div class="rail">${cells.join("")}</div>
          <div class="rail-legend">${legend}</div>`;
}

/* ---------- TERAZ ---------- */
function livePanel(d) {
  const cmp = (key, higher) => {
    const now = d.now[key], ref = (d.reference.hit || {})[key];
    if (ref == null) return {cls: "", note: "brak odniesienia"};
    const good = higher ? now >= ref : now <= ref;
    return {cls: good ? "ok" : "warn",
            note: `${good ? "powyżej" : "poniżej"} mediany udanych gier (${ref})`};
  };
  const ROWS = [
    ["ka_per_min", "Zabójstwa + asysty / min", true],
    ["gold_per_min", "Złoto / min (szacowane)", true],
    ["cs_per_min", "CS / min", true],
    ["deaths_per_min", "Zgony / min", false],
  ];
  const rows = ROWS.map(([k, label, hi]) => {
    const c = cmp(k, hi);
    const ref = (d.reference.hit || {})[k];
    return `<div class="kv">
      <span>${label}</span>
      <span style="color:var(--${c.cls === "ok" ? "ok" : "warn"})">
        ${d.now[k]} <span class="dim" style="font-size:11px">/ ${ref ?? "—"}</span></span>
    </div>`;
  }).join("");

  return `<div class="panel" style="border-left:3px solid var(--ok);margin-bottom:22px">
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px">
      <span class="dot"></span>
      <div style="font:700 22px/1 var(--display)">${esc(d.champion || "?")}</div>
      <div class="dim num">${Math.floor(d.minutes)} min · ${d.kills}/${d.deaths}/${d.assists}
        · poziom ${d.level ?? "?"}</div>
      <div style="margin-left:auto" class="chip gold">${
        d.need ? "trzeba " + esc(d.need) : "brak progu"}</div>
    </div>
    ${d.milestone != null ? rail(d.milestone, GOAL, d.need) : ""}
    <div style="margin-top:14px">${rows}</div>
    <div class="tagline">Złoto liczone z ubytków stanu — obejmuje kowadła
      i mikstury, które znikają z ekwipunku.</div>
    <div class="tagline">Porównanie z medianą Twoich gier zakończonych ocenami
      ${esc(d.reference.threshold)} lub lepszymi (${d.reference.hit_games} gier).
      Obrażeń nie da się odczytać w trakcie gry — Riot ich nie udostępnia.</div>
  </div>`;
}

async function renderNow() {
  let live = {active: false};
  try { live = await api("/live"); } catch (e) {}
  $("live-panel") && ($("live-panel").innerHTML = live.active ? livePanel(live) : "");

  let lobby = {active: false};
  try {
    lobby = await api("/lobby");
    if (lobby.detail) throw new Error(lobby.detail);
  } catch (e) {
    $("live-bar").innerHTML = `<div class="live" style="border-color:#6B4E28;
      background:rgba(224,164,88,.07)"><span style="color:var(--warn)">⚠</span>
      <div>Nie udało się odczytać champ selecta: ${esc(e.message)}</div></div>`;
  }

  const inSelect = !!(lobby.active && lobby.targets && lobby.targets.length);
  let targets;

  if (inSelect) {
    targets = lobby.targets;
    $("live-bar").innerHTML = `<div class="live"><span class="dot"></span>
      <div><b>${esc(lobby.queue || "Champ select")}</b> —
      ${lobby.champion_ids.length} w puli, odczyt sprzed ${lobby.age}s</div></div>`;
  } else {
    $("live-bar").innerHTML = "";
    let data;
    try { data = await api("/targets?limit=12"); } catch (e) {
      $("hero").innerHTML = `<div class="hero empty"><div class="empty-state">
        <h3>Backend nie odpowiada</h3><div>${esc(e.message)}</div></div></div>`;
      $("cards").innerHTML = "";
      return;
    }
    if (data.detail) {
      $("hero").innerHTML = `<div class="hero empty"><div class="empty-state">
        <h3>Brak danych</h3><div>${esc(data.detail)}</div></div></div>`;
      $("cards").innerHTML = ""; $("cards-label").textContent = "";
      return;
    }
    GOAL = data.goal;
    targets = data.targets;
  }

  if (!targets.length) {
    $("hero").innerHTML = `<div class="hero empty"><div class="empty-state">
      <h3>Nic do zrobienia w tej puli</h3>
      <div>Żaden z dostępnych championów nie zbliża do milestone ${GOAL}.</div>
      </div></div>`;
    $("cards").innerHTML = ""; $("cards-label").textContent = "";
    return;
  }

  // ---- hero ----
  const b = targets[0];
  const cons = b.expected_games_conservative;
  const gamesLine = (cons && Math.abs(cons - b.expected_games) > 1)
    ? `${Math.round(b.expected_games)}–${Math.round(cons)} gier`
    : `około ${Math.round(b.expected_games)} gier`;

  $("hero").innerHTML = `
    <div class="hero">
      <div>
        <div class="big">${b.steps_remaining}<small>${
          b.steps_remaining === 1 ? "szczebel" : "szczeble"} do celu</small></div>
      </div>
      <div class="hero-side">
        <div class="who"><span class="rank-badge lead">1</span><img onerror="this.src=BLANK" src="${icon(b.key)}"
          alt="">${esc(b.name)}</div>
        ${rail(b.milestone, GOAL, b.next_grade)}
        <div class="range" style="margin-top:8px">${modelNote(b)}</div>
        <div class="range dim" style="margin-top:4px;font-size:11.5px">
          orientacyjnie ${gamesLine}</div>
      </div>
    </div>`;

  const rest = targets.slice(1);

  // Ten sam uklad w obu widokach. Karty rozjezdzaly sie na trzy rozmiary
  // i powtarzaly te sama etykiete; tabela pokazuje wiecej w mniejszym miejscu.
  $("cards-label").textContent = inSelect ? "Kolejność w tej puli" : "Ranking pozostałych";
  $("cards").className = "";

  const shownRows = inSelect ? rest : rest.slice(0, 15);
  const fmt = n => (n ?? 0).toLocaleString("pl-PL");
  const days = ts => ts ? Math.round((Date.now() - ts) / 86400000) : null;

  $("cards").innerHTML = `<table class="pool-table">
    <thead><tr>
      <th style="width:44px"></th><th>Champion</th>
      <th class="r" style="width:74px">Zostało</th>
      <th style="width:72px">Trzeba</th>
      <th class="r" style="width:88px">Twoje gry</th>
      <th class="r" style="width:100px">Maestria</th>
      <th class="r" style="width:92px">Ostatnio</th>
    </tr></thead>
    <tbody>${shownRows.map((t, i) => {
      const own = t.model_own_games ?? 0;
      const d = days(t.last_play);
      return `
      <tr>
        <td class="rank-cell">${i + 2}</td>
        <td><div class="champ-cell"><img onerror="this.src=BLANK" src="${icon(t.key)}" alt="">
          ${esc(t.name)}</div></td>
        <td class="r num">${t.steps_remaining}</td>
        <td><span class="chip ${t.next_grade === "S-" ? "gold" : ""}">${
          esc(t.next_grade || "?")}</span></td>
        <td class="r num" style="${own ? "" : "color:var(--faint)"}">${own || "—"}</td>
        <td class="r num" style="color:var(--dim)">${fmt(t.points)}</td>
        <td class="r num" style="color:var(--dim)">${
          d == null ? "—" : (d === 0 ? "dziś" : d + " dni")}</td>
      </tr>`;
    }).join("")}</tbody></table>
    ${!inSelect && rest.length > shownRows.length
      ? `<div class="msg" style="text-align:center">i ${
          rest.length - shownRows.length} dalszych championów</div>` : ""}`;
}

/* Panel boczny: to, po co dzis trzeba wchodzic na podstrony.
   Ekran glowny ma odpowiadac bez klikania. */
async function renderSide() {
  const box = $("side");
  if (!box) return;
  try {
    const [sp, gr, sys] = await Promise.all([
      api("/split/progress"), api("/grades/history?limit=3"), api("/system/health")]);

    const dist = sp.distribution || {};
    const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
    const bars = Object.entries(dist).map(([ms, n]) => `
      <div class="bar-row" style="grid-template-columns:88px 1fr 34px">
        <span>${ms >= sp.goal ? "cel" : (+ms === 0 ? "brak" : ROMAN[ms - 1] + " ukończ.")}</span>
        <div class="bar"><i class="${ms >= sp.goal ? "ok" : ""}"
          style="width:${100 * n / total}%"></i></div>
        <span style="text-align:right">${n}</span>
      </div>`).join("");

    const last = (gr.grades || []).map(g => `
      <div class="mini">
        <img onerror="this.src=BLANK" src="${icon(g.key)}" alt="">
        <span>${esc(g.name)}</span>
        <span class="chip ${g.grade.startsWith(">=") ? "gold" : ""}"
          style="margin-left:auto">${esc(g.grade)}</span>
      </div>`).join("") || '<div class="sub">brak ocen</div>';

    const ago = ts => {
      if (!ts) return "nigdy";
      const d = sys.now - ts;
      if (d < 90) return d + " s temu";
      if (d < 5400) return Math.round(d / 60) + " min temu";
      if (d < 172800) return Math.round(d / 3600) + " godz. temu";
      return Math.round(d / 86400) + " dni temu";
    };
    // najswiezsze zdarzenie swiadczace o rozegranej grze
    const ls = sys.last_seen || {};
    const lastGrade = Math.max(ls.grade || 0, ls.eog || 0, ls.snapshot || 0) || null;
    const stale = !lastGrade || (sys.now - lastGrade) > 172800;

    box.innerHTML = `
      <div class="panel">
        <div class="eyebrow">Postęp splitu</div>
        ${bars}
        <div class="kv" style="margin-top:10px"><span>Marks zdobyte</span>
          <span>${sp.marks_total}</span></div>
      </div>
      <div class="panel">
        <div class="eyebrow">Ostatnie oceny</div>
        ${last}
        <div class="kv" style="margin-top:10px"><span>Ocen w bazie</span>
          <span>${sys.counts.grade_observation}</span></div>
      </div>
      <div class="panel">
        <div class="eyebrow">Agent</div>
        <div class="kv"><span>Ostatnia gra</span>
          <span style="color:${stale ? "var(--warn)" : "var(--ok)"}">${ago(lastGrade)}</span></div>
        <div class="kv"><span>Mecze w bazie</span><span>${sys.counts.match_player}</span></div>
        ${stale ? `<div class="tagline" style="color:var(--warn)">
          LCU pamięta ~20 gier — bez agenta przepadają</div>` : ""}
      </div>`;
  } catch (e) {
    box.innerHTML = '<div class="panel"><div class="sub">nie udało się wczytać</div></div>';
  }
}

/* Model mowi cos sensownego tylko dla progow, ktore przeszly walidacje.
   Przy S- nie przeszedl, wiec zamiast fikcyjnego procentu piszemy prawde. */
function modelNote(t) {
  if (t.model_p != null) {
    const auc = t.model_auc ? ` · AUC ${t.model_auc}` : "";
    const g = t.model_games ? ` z ${t.model_games} gier` : "";
    return `<span class="g">${(100 * t.model_p).toFixed(0)}%</span> szans na
            <span class="g">${esc(t.next_grade)}</span>${g}${auc}`;
  }
  return `<span class="g">${esc(t.next_grade || "?")}</span> —
          model nie ma jeszcze danych dla tego progu`;
}

/* ---------- OCENY ---------- */
async function renderGrades() {
  const d = await api("/grades/history?limit=60");
  let ready = {};
  try { ready = await api("/model/readiness"); } catch (e) {}
  const ok = th => ((ready[th] || {}).validation || {}).useful;
  if (!d.grades || !d.grades.length) {
    $("grades-body").innerHTML = `<div class="empty-state"><h3>Jeszcze żadnych ocen</h3>
      <div>Zagraj mecz z uruchomionym agentem.</div></div>`;
    return;
  }
  const rows = d.grades.map(g => {
    const cls = g.grade.startsWith(">=") ? "gold" : (
      /^[SA]/.test(g.grade) ? "ok" : "");
    const pa = (ok("A-") && g.p_A != null) ? (100 * g.p_A).toFixed(0) + "%" : "—";
    const ps = (ok("S-") && g.p_S != null) ? (100 * g.p_S).toFixed(0) + "%" : "—";
    return `<tr>
      <td><span class="chip ${cls}">${esc(g.grade)}</span></td>
      <td><div class="champ-cell"><img onerror="this.src=BLANK" src="${icon(g.key)}" alt="">
        ${esc(g.name)}</div></td>
      <td class="num">${g.kills}/${g.deaths}/${g.assists}</td>
      <td class="r num">${g.gpm}</td>
      <td class="r num">${g.dpm}</td>
      <td class="r num">${pa}</td>
      <td class="r num">${ps}</td>
    </tr>`;
  }).join("");
  $("grades-body").innerHTML = `<table>
    <thead><tr><th>Ocena</th><th>Champion</th><th>K/D/A</th>
      <th class="r">Złoto/min</th><th class="r">Obr./min</th>
      <th class="r">Model ≥A-</th><th class="r">Model ≥S-</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- SPLIT ---------- */
async function renderSplit() {
  const d = await api("/split/progress");
  const dist = d.distribution || {};
  const max = Math.max(...Object.values(dist), 1);
  const bars = Object.entries(dist).map(([ms, n]) => `
    <div class="bar-row">
      <span>${ms >= d.goal ? "cel" : (+ms === 0 ? "brak" : ROMAN[ms - 1] + " ukończony")}</span>
      <div class="bar"><i class="${ms >= d.goal ? "ok" : ""}"
        style="width:${100 * n / max}%"></i></div>
      <span style="text-align:right">${n}</span>
    </div>`).join("");

  const ladder = Object.entries(d.ladder || {}).map(([m, s]) => `
    <div class="kv"><span>${+m === 0 ? "start" : ROMAN[m - 1]} → ${ROMAN[+m] || +m + 1}</span>
      <span>${Object.keys(s.require_grades)[0]} ×${s.games} · ${s.reward_marks} marks</span>
    </div>`).join("");

  const since = d.tracking_since
    ? new Date(d.tracking_since * 1000).toLocaleDateString("pl-PL") : "—";

  $("split-body").innerHTML = `
    <div class="grid2" style="margin-top:20px">
      <div class="panel">
        <div class="eyebrow">Rozkład championów</div>
        ${bars}
      </div>
      <div class="panel">
        <div class="eyebrow">Drabinka wymagań</div>
        ${ladder || '<div class="sub">jeszcze nieznana</div>'}
        <div class="kv" style="margin-top:14px"><span>Marks of Mastery zdobyte łącznie</span>
          <span>${d.marks_total}</span></div>
        <div class="kv"><span>Championów na celu</span><span>${d.at_goal}</span></div>
        <div class="kv"><span>Śledzone od</span><span>${since}</span></div>
      </div>
    </div>`;
}

/* ---------- LABORATORIUM ---------- */
const GRADE_ORDER = ["C","C+","B","B+","A","A+",">=A-",">=S-","S-","S","S+"];
async function renderLab() {
  const stat = $("lab-stat").value;
  const d = await api("/lab/distribution?stat=" + stat);
  const keys = Object.keys(d.buckets).sort(
    (a, b) => GRADE_ORDER.indexOf(a) - GRADE_ORDER.indexOf(b));
  if (!keys.length) { $("lab-body").innerHTML = '<div class="msg">brak danych</div>'; return; }

  const all = keys.flatMap(k => d.buckets[k]);
  const lo = Math.min(...all), hi = Math.max(...all);
  const med = a => a.length % 2 ? a[(a.length - 1) / 2]
    : (a[a.length / 2 - 1] + a[a.length / 2]) / 2;

  const rows = keys.map(k => {
    const v = d.buckets[k];
    const m = med(v);
    const pos = hi > lo ? 100 * (m - lo) / (hi - lo) : 50;
    const dots = v.map(x => {
      const q = hi > lo ? 100 * (x - lo) / (hi - lo) : 50;
      return `<i style="position:absolute;left:calc(${q}% - 3px);top:50%;
              transform:translateY(-50%);width:6px;height:6px;border-radius:50%;
              background:var(--dim);opacity:.75"></i>`;
    }).join("");
    const dec = hi < 10 ? 2 : (hi < 100 ? 1 : 0);
    return `<div class="bar-row" style="grid-template-columns:78px 1fr 70px">
      <span class="chip ${k.startsWith(">=") ? "gold" : ""}">${esc(k)}</span>
      <div class="bar" style="height:20px;position:relative;background:var(--panel2)">${dots}
        <i style="position:absolute;left:calc(${pos}% - 1px);top:2px;width:2px;
           height:calc(100% - 4px);background:var(--gold)"></i></div>
      <span style="text-align:right">${m.toFixed(dec)}<span class="dim"
        style="font-size:10px"> ×${v.length}</span></span>
    </div>`;
  }).join("");

  $("lab-body").innerHTML = `<div class="panel">
    <div class="eyebrow">${stat === "deaths_per_min" ? "Mniej znaczy lepiej · " : ""}Zakres
      ${lo.toFixed(hi < 10 ? 2 : 0)} – ${hi.toFixed(hi < 10 ? 2 : 0)} ·
      kropki to pojedyncze gry, złota kreska to mediana, ×n to liczba gier</div>
    ${rows}</div>`;
}

/* ---------- SYSTEM ---------- */
async function renderSystem() {
  const d = await api("/system/health");
  const ago = ts => {
    if (!ts) return "nigdy";
    const s = d.now - ts;
    if (s < 90) return s + " s temu";
    if (s < 5400) return Math.round(s / 60) + " min temu";
    if (s < 172800) return Math.round(s / 3600) + " godz. temu";
    return Math.round(s / 86400) + " dni temu";
  };
  const LABELS = {snapshot: "Snapshot maestrii", grade: "Zapis oceny",
    eog: "Statystyki końcowe", champ_select: "Champ select",
    history_lcu: "Historia z LCU", ddragon: "Data Dragon",
    model_train: "Trening modelu", split_reset: "Reset splitu",
    grade_backfill: "Odzysk ocen"};
  const NAMES = {match_player: "Mecze", grade_observation: "Oceny",
    eog_raw: "Ekrany końcowe", champ_select_pool: "Pule z champ selecta",
    player_stat: "Wiersze statystyk", snapshot: "Snapshoty"};

  const seen = Object.entries(d.last_seen).map(([k, ts]) =>
    `<div class="kv"><span>${LABELS[k] || k}</span><span>${ago(ts)}</span></div>`).join("");
  const counts = Object.entries(d.counts).map(([k, n]) =>
    `<div class="kv"><span>${NAMES[k] || k}</span><span>${n}</span></div>`).join("");

  const m = d.model;
  let ready = {};
  try { ready = await api("/model/readiness"); } catch (e) {}
  const modelRows = Object.entries(ready).map(([th, r]) => {
    const v = r.validation;
    const good = v && v.useful;
    const detail = v
      ? `${(100 * v.accuracy).toFixed(0)}% trafień vs ${(100 * v.baseline_accuracy).toFixed(0)}% przy zgadywaniu`
        + (v.auc ? ` · AUC ${v.auc}` : "")
      : "za mało danych";
    return `<div class="kv"><span>próg ${esc(th)}
      <span class="dim" style="font-size:11.5px">· ${r.samples} obserwacji,
      ${r.positives} pozytywnych</span></span>
      <span style="color:${good ? "var(--ok)" : "var(--warn)"}">${esc(r.verdict)}</span></div>
      <div class="kv" style="border:none;padding-top:0"><span class="dim"
        style="font-size:11.5px">${detail}</span></div>`;
  }).join("");
  const events = d.events.slice(0, 20).map(e =>
    `<tr><td class="num" style="color:var(--dim)">${ago(e.ts)}</td>
     <td>${LABELS[e.kind] || e.kind}</td>
     <td class="num" style="color:var(--dim);font-size:12px">${esc(e.detail || "")}</td></tr>`).join("");

  let pred = {};
  try { pred = await api("/predictions/scorecard"); } catch (e) {}
  $("system-body").innerHTML = `
    <div class="grid2" style="margin-top:20px">
      <div class="panel"><div class="eyebrow">Ostatnia aktywność</div>${seen}</div>
      <div class="panel"><div class="eyebrow">Zebrane dane</div>${counts}
        <div class="kv" style="margin-top:12px"><span>Patch Data Dragon</span>
          <span>${esc(d.ddragon_patch || "—")}</span></div>
      </div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="eyebrow">Model</div>
      <div class="kv"><span>Ocen łącznie</span><span>${m.grades_total}</span></div>
      <div class="kv"><span>W tym dokładnych</span><span>${m.grades_exact}</span></div>
      <div class="kv"><span>Progowych („≥")</span><span>${m.grades_censored}</span></div>
      <div class="kv"><span>Predykcji sprzed gry <span class="dim"
        style="font-size:11.5px">· rozstrzygniętych / czekających</span></span>
        <span id="pred-count">${pred.resolved ?? 0} / ${pred.pending_pools ?? 0}${
        pred.brier != null ? ` · Brier ${pred.brier}` : ""}</span></div>
      <div style="margin-top:14px">${modelRows}</div>
      <div class="kv" style="margin-top:10px"><span class="dim" style="font-size:11.5px">
        Miara to walidacja leave-one-out — każda obserwacja raz jako test.
        Metryki liczone na danych treningowych zawsze wyglądają lepiej.</span></div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="eyebrow">Dziennik zdarzeń</div>
      <table><tbody>${events}</tbody></table>
    </div>`;
}

/* ---------- routing ---------- */
const VIEWS = {
  "#/": ["v-now", async () => { await renderNow(); renderSide(); }],
  "#/oceny": ["v-grades", renderGrades],
  "#/split": ["v-split", renderSplit],
  "#/lab": ["v-lab", renderLab],
  "#/system": ["v-system", renderSystem],
};

async function route() {
  const hash = VIEWS[location.hash] ? location.hash : "#/";
  for (const [h, [id]] of Object.entries(VIEWS)) {
    $(id).hidden = h !== hash;
    document.querySelector(`nav a[href="${h}"]`)?.classList.toggle("on", h === hash);
  }
  try { await VIEWS[hash][1](); }
  catch (e) { console.error(e); }
}

async function ddragon() {
  try {
    const r = await fetch("https://ddragon.leagueoflegends.com/api/versions.json");
    PATCH = (await r.json())[0];
  } catch (e) {}
}

function tick() {
  $("clock").textContent = new Date().toLocaleTimeString("pl-PL",
    {hour: "2-digit", minute: "2-digit"});
}

addEventListener("hashchange", route);
$("lab-stat").addEventListener("change", renderLab);
setInterval(() => { if (location.hash === "#/" || !location.hash) renderNow(); }, 4000);
setInterval(tick, 10000);
(async () => { tick(); await ddragon(); route(); })();
