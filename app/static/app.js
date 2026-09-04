let PATCH = "16.17.1";
const $ = id => document.getElementById(id);
// (partia D) bez sprawdzenia r.ok awarie backendu przebieraly sie za dane:
// 500 na ocenach wygladalo jak "Jeszcze zadnych ocen", 400 renderowalo
// "undefined" w polach - blad ma byc bledem, obsluga w route()/catch-ach
const api = p => fetch("/api" + p).then(async r => {
  if (!r.ok) {
    let d = null;
    try { d = await r.json(); } catch (e) {}
    throw new Error((d && d.detail) || ("HTTP " + r.status));
  }
  return r.json();
});
// Data Dragon zna 173 championow, snapshot ma 175 - najnowsi trafiaja tam
// z opoznieniem. Zamiast pustego kwadratu pokazujemy zastepnik.
const BLANK = "data:image/svg+xml," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
     <rect width="40" height="40" rx="5" fill="#1A212C"/>
     <text x="20" y="26" text-anchor="middle" fill="#3D4757"
       font-family="monospace" font-size="18">?</text></svg>`);
// CDragon dostaje nowych championow szybciej niz Data Dragon - dla postaci
// bez klucza w DD probujemy tam, zanim pokazemy zastepnik.
const CDRAGON = id =>
  `https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/${id}.png`;
const icon = (k, cid) => k
  ? `https://ddragon.leagueoflegends.com/cdn/${PATCH}/img/champion/${k}.png`
  : (cid ? CDRAGON(cid) : BLANK);
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));

const ROMAN = ["I", "II", "III", "IV"];
// szczeble po IV to "bonus milestone'y" (nazewnictwo Riota): cel 5 = bonus 1
const msName = i => ROMAN[i] || ("bonus " + (i - ROMAN.length + 1));
let GOAL = 4;

/* ---------- szyna milestone ---------- */
function rail(milestone, goal, nextGrade, need, have) {
  const cells = [];
  for (let i = 0; i < goal; i++) {
    const cls = i < milestone ? "done" : (i === milestone ? "next" : "");
    cells.push(`<div class="chev ${cls}">${msName(i)}</div>`);
  }
  // krotnosc szczebla (bonus milestone: S- x2) + oceny juz uzbierane
  const count = need > 1
    ? ` ×${need}` + (have ? ` <span class="dim">(masz ${have})</span>` : "") : "";
  const legend = nextGrade
    ? `Do <b>${msName(milestone)}</b> trzeba oceny <b>${esc(nextGrade)}${count}</b>`
    : "";
  return `<div class="rail">${cells.join("")}</div>
          <div class="rail-legend">${legend}</div>`;
}

/* (41) Link do notek patcha na wiki. Wiki nazywa strony numerem
   marketingowym (V26.17), a gameVersion/ddragon zostaly przy wewnetrznym
   (16.17) - od rebrandu numeracji w 2025 marketing = major + 10.
   Kotwica sekcji championa = nazwa ze spacjami jako podkreslenia
   (tak MediaWiki generuje id naglowkow). */
function patchUrl(short, name) {
  if (!short) return null;
  const [maj, min] = String(short).split(".");
  const anchor = name ? "#" + encodeURIComponent(name.replaceAll(" ", "_")) : "";
  return `https://wiki.leagueoflegends.com/en-us/V${+maj + 10}.${min}${anchor}`;
}

/* (48) Mnozniki balansu Mayhema - WYLACZNIE wyswietlanie, nigdy cecha
   modelu (decyzja 2.09; zrodlo i parser po stronie backendu). Jeden fetch
   na zaladowanie strony - wartosci zmieniaja sie raz na patch. */
let BALANCE = null;
async function modeBalance() {
  if (BALANCE === null) {
    try { BALANCE = (await api("/balance")).champions || {}; }
    catch (e) { BALANCE = {}; }
  }
  return BALANCE;
}

/* etykieta PL + czy plus oznacza nerf (jedynie otrzymywane obrazenia) */
const MOD_PL = {
  "Damage Dealt": ["obrażenia", false],
  "Damage Received": ["obr. otrzymywane", true],
  "Healing": ["leczenie", false],
  "Shielding": ["tarcze", false],
  "Ability Haste": ["ability haste", false],
  "Attack Speed": ["prędkość ataku", false],
  "Energy Regen": ["regen. energii", false],
  "Tenacity": ["tenacity", false],
};

function balanceLine(mods) {
  if (!mods) return "";
  const parts = Object.entries(mods).map(([k, v]) => {
    const [label, invert] = MOD_PL[k] || [k.toLowerCase(), false];
    const minus = String(v).startsWith("-");
    const nerf = invert ? !minus : minus;
    return `<span style="color:var(--${nerf ? "warn" : "ok"})">${esc(label)}
      ${esc(v)}</span>`;
  });
  return `<div class="range dim" style="margin-top:4px;font-size:11.5px">
    Mayhem: ${parts.join(" · ")}</div>`;
}

/* (49) Sciaga-z-danych granego championa - "grasz ta postacia 3. raz
   w zyciu": tier, winrate, priorytet skilli, top augmenty. Backend
   cache'uje per patch; tu tylko pamiec na czas zycia strony (udane
   odpowiedzi - nieudane sa tanie, backend trzyma negative-cache). */
const CHEAT = {};
async function cheatsheet(cid) {
  if (!cid) return null;
  if (CHEAT[cid] && CHEAT[cid].ok) return CHEAT[cid];
  try { CHEAT[cid] = await api("/cheatsheet/" + cid); }
  catch (e) { return null; }
  return CHEAT[cid];
}

function cheatLines(cs) {
  if (!cs || !cs.ok) return "";
  // (F6) JSON-LD strony sortuje pryzmatyczne na wierzch, wiec plaskie
  // "top 5" to zawsze same pryzmatyczne (rzadsze z natury) - tiery idą
  // osobno, po 3 z kazdego; plaska lista zostaje fallbackiem dla cache
  // sprzed tej zmiany i przebudowy strony
  const bt = cs.augments_by_tier;
  const tierRows = bt ? ["Prismatic", "Gold", "Silver"].map(t => {
    const list = (bt[t] || []).slice(0, 3);
    if (!list.length) return "";
    const names = list.map(a => `<span title="${
      a.win_rate != null ? a.win_rate + "% WR" : ""}">${esc(a.name)}</span>`).join(" · ");
    return `<div class="kv"><span>Augmenty · ${t}</span>
      <span style="font-size:12px;text-align:right;max-width:62%">${names}</span></div>`;
  }).join("") : "";
  const augs = (cs.augments || []).slice(0, 5).map(esc).join(" · ");
  return `
    <div class="kv"><span>Mayhem tier</span>
      <span>${esc(cs.tier || "?")}${cs.win_rate ? ` · ${cs.win_rate}% WR` : ""}</span></div>
    ${cs.skill_priority ? `<div class="kv"><span>Skille</span>
      <span class="num" title="${esc(cs.skill_sequence || "")}">${esc(cs.skill_priority)}</span></div>` : ""}
    ${tierRows || (augs ? `<div class="kv"><span>Top augmenty</span>
      <span style="font-size:12px;text-align:right;max-width:62%">${augs}</span></div>` : "")}`;
}

/* (G) Zmiany championa w biezacym patchu - inline z werdyktem, zamiast
   linku do calego patcha (uwaga czlowieka 3.09). Zrodlo, parser, heurystyka
   werdyktu i decyzja "wylacznie wyswietlanie": app/patchnotes.py. Cache
   per patch po stronie backendu; tu pamiec na czas zycia strony. */
const NOTES = {};
let NOTES_ALL = null;
async function patchNotesFor(cid) {
  if (!cid) return null;
  if (NOTES[cid] && NOTES[cid].ok) return NOTES[cid];
  try { NOTES[cid] = await api("/patchnotes/" + cid); } catch (e) { return null; }
  return NOTES[cid];
}
async function patchNotesAll() {
  if (NOTES_ALL === null || !NOTES_ALL.ok) {
    try { NOTES_ALL = await api("/patchnotes"); } catch (e) { NOTES_ALL = {}; }
  }
  return NOTES_ALL;
}
const escq = s => esc(s).replaceAll('"', "&quot;");
const VERDICT_PL = {buff: ["▲ buff", "ok"], nerf: ["▼ nerf", "warn"],
                    mixed: ["▲▼ mieszane", "warn"], adjust: ["~ zmiany", ""]};
function verdictChip(v) {
  const t = VERDICT_PL[v];
  return t ? `<span class="chip ${t[1]}" style="margin-left:6px">${t[0]}</span>` : "";
}
function patchBlock(pn) {
  if (!pn || !pn.ok) return "";
  const ch = pn.champion, mh = pn.mayhem;
  if (!ch && !mh) return `<div class="patch-notes"><span class="dim">Patch ${
    esc(pn.patch)}: bez zmian tego championa</span></div>`;
  const val = v => esc(v).replace(/ \/ /g, "/");
  const fmt = c => `${c.flag ? `<b>${esc(c.flag.toUpperCase())}</b> ` : ""}${
    c.ability && c.ability !== "Mayhem" ? esc(c.ability) + " · " : ""}${esc(c.label)}${
    c.before != null ? ` ${val(c.before)} → <b>${val(c.after)}</b>` : `: ${esc(c.after)}`}`;
  const lines = [];
  if (ch) {
    ch.changes.slice(0, 5).forEach(c => lines.push(`<div class="${c.kind}">${fmt(c)}</div>`));
    if (ch.changes.length > 5) lines.push(`<div class="dim">+${ch.changes.length - 5} dalszych</div>`);
  }
  if (mh) mh.changes.slice(0, 3).forEach(c => lines.push(`<div class="${c.kind}">Mayhem · ${fmt(c)}</div>`));
  const v = ch ? ch.verdict : mh.verdict;
  return `<div class="patch-notes"><div>Patch ${esc(pn.patch)}${verdictChip(v)}${
    ch && ch.summary ? ` <span class="dim" title="${escq(ch.summary)}">ⓘ uzasadnienie Riota</span>` : ""
  }</div>${lines.join("")}</div>`;
}

/* ---------- TERAZ ---------- */
function livePanel(d, bal, cheat, pn) {
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
      <img onerror="this.src=BLANK" src="${icon(d.key, d.champion_id)}" alt=""
           style="width:34px;height:34px;border-radius:5px;background:var(--panel2)">
      <div style="font:700 22px/1 var(--display)">${esc(d.champion || "?")}</div>
      <div class="dim num">${Math.floor(d.minutes)} min · ${d.kills}/${d.deaths}/${d.assists}
        · poziom ${d.level ?? "?"}</div>
      <div style="margin-left:auto" class="chip gold">${
        d.need ? "trzeba " + esc(d.need) : "brak progu"}</div>
    </div>
    ${d.milestone != null ? rail(d.milestone, GOAL, d.need, d.need_count, d.need_have) : ""}
    ${balanceLine(bal && bal[d.champion_id])}
    ${patchBlock(pn)}
    ${cheatLines(cheat)}
    <div style="margin-top:14px">${rows}</div>
    <div class="tagline">Złoto liczone z ubytków stanu — obejmuje kowadła
      i mikstury, które znikają z ekwipunku.</div>
    <div class="tagline">Porównanie z medianą Twoich gier zakończonych ocenami
      ${esc(d.reference.threshold)} lub lepszymi (${d.reference.hit_games} gier${
        d.reference.scope === "champion" ? " na tym championie" :
        d.reference.scope === "class" ? ` na klasie ${esc(d.reference.scope_label)}` : ""}).
      Obrażeń nie da się odczytać w trakcie gry — Riot ich nie udostępnia.</div>
  </div>`;
}

// (partia D) epoka renderu: interwal 4 s odpalal rownolegle przebiegi bez
// straznika i wolniejszy STARSZY potrafil nadpisac swiezszy widok dokladnie
// w oknie champ selecta; kazdy zapis DOM za awaitem sprawdza, czy nie
// wystartowal juz nowszy przebieg
let NOW_EPOCH = 0;

async function renderNow() {
  const ep = ++NOW_EPOCH;
  const stale = () => ep !== NOW_EPOCH;

  let live = {active: false};
  try { live = await api("/live"); } catch (e) {}
  const bal = await modeBalance();
  const cheat = live.active ? await cheatsheet(live.champion_id) : null;
  const pnLive = live.active ? await patchNotesFor(live.champion_id) : null;
  if (stale()) return;
  $("live-panel") && ($("live-panel").innerHTML = live.active ? livePanel(live, bal, cheat, pnLive) : "");

  // pasek live-bar skladamy w JEDNEJ zmiennej i ustawiamy raz na koncu -
  // baner sentinela i blad champ selecta byly wstawiane wczesnie
  // i wymazywane w tej samej klatce przez pozniejsze innerHTML (audyt 2.09)
  let barHtml = "";
  let lobby = {active: false};
  try {
    lobby = await api("/lobby");
  } catch (e) {
    barHtml += `<div class="live" style="border-color:#6B4E28;
      background:rgba(224,164,88,.07)"><span style="color:var(--warn)">⚠</span>
      <div>Nie udało się odczytać champ selecta: ${esc(e.message)}</div></div>`;
  }

  try {
    const sen = await api("/sentinel");
    if (sen.open) barHtml = `
      <div class="live" style="border-color:var(--gold)">
        <span style="color:var(--gold)">★</span>
        <div><b>Riot otworzył API Mayhema</b> — match-v5 zwraca gry z kolejki
        2400. Można robić backfill pełnych danych.</div></div>` + barHtml;
  } catch (e) {}

  const inSelect = !!(lobby.active && lobby.targets && lobby.targets.length);
  const lobbyTrade = new Set(inSelect ? (lobby.trade_ids || []) : []);
  let targets;
  let patchMeta = null;

  if (inSelect) {
    targets = lobby.targets;
    patchMeta = lobby.patch;
    barHtml += `<div class="live"><span class="dot"></span>
      <div><b>${esc(lobby.queue || "Champ select")}</b> —
      ${lobby.champion_ids.length} w puli, odczyt sprzed ${lobby.age}s</div></div>`;
    if (stale()) return;
    $("live-bar").innerHTML = barHtml;
  } else {
    let data;
    try { data = await api("/targets?limit=12"); } catch (e) {
      if (stale()) return;
      $("live-bar").innerHTML = barHtml;
      $("hero").innerHTML = `<div class="hero empty"><div class="empty-state">
        <h3>Nie można pobrać rankingu</h3><div>${esc(e.message)}</div></div></div>`;
      $("cards").innerHTML = ""; $("cards-label").textContent = "";
      return;
    }
    if (stale()) return;
    $("live-bar").innerHTML = barHtml;
    GOAL = data.goal;
    targets = data.targets;
    patchMeta = data.patch;
  }

  if (stale()) return;
  if (!targets.length) {
    $("hero").innerHTML = `<div class="hero empty"><div class="empty-state">
      <h3>Nic do zrobienia w tej puli</h3>
      <div>Żaden z dostępnych championów nie zbliża do szczebla ${msName(GOAL - 1)}.</div>
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

  const patchBanner = (patchMeta && patchMeta.fresh)
    ? `<div class="patch-banner">⚠ Patch <a class="patch-link" target="_blank"
        rel="noopener" href="${patchUrl(patchMeta.short)}">${esc(patchMeta.short)}</a>
        od ${patchMeta.games} ${
        patchMeta.games === 1 ? "gry" : "gier"} — normy i model liczone głównie na
        poprzednim patchu, a patch zmienia też mnożniki balansu trybu.</div>` : "";

  const pn = await patchNotesFor(b.champion_id);
  const notesAll = await patchNotesAll();
  if (stale()) return;
  // (G) "notki" prowadza do bloku championa w notkach Riota; wiki zostaje
  // fallbackiem, gdy notek nie ma (nowy patch, awaria fetchu)
  const patchNotes = (pn && pn.anchor_url) || patchUrl(patchMeta && patchMeta.short, b.name);

  $("hero").innerHTML = patchBanner + `
    <div class="hero">
      <div>
        <div class="big">${b.steps_remaining}<small>${
          b.steps_remaining === 1 ? "szczebel" : "szczeble"} do celu</small></div>
      </div>
      <div class="hero-side">
        <div class="who"><span class="rank-badge lead">1</span><img onerror="this.src=BLANK" src="${icon(b.key, b.champion_id)}"
          alt="">${esc(b.name)}${patchNotes ? ` <a class="patch-link" href="${patchNotes}"
          target="_blank" rel="noopener" title="zmiany championa w tym patchu">notki</a>` : ""}</div>
        ${rail(b.milestone, GOAL, b.next_grade, b.next_need, b.next_have)}
        ${inSelect && poolBadges(b, lobbyTrade, inSelect)
          ? `<div style="margin-top:7px;margin-left:-8px">${poolBadges(b, lobbyTrade, inSelect)}</div>` : ""}
        <div class="range" style="margin-top:8px">${modelNote(b)}</div>
        <div class="range dim" style="margin-top:4px;font-size:11.5px">
          orientacyjnie ${gamesLine}</div>
        ${balanceLine(bal[b.champion_id])}
        ${patchBlock(pn)}
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
        <td><div class="champ-cell"><img onerror="this.src=BLANK" src="${icon(t.key, t.champion_id)}" alt="">
          ${esc(t.name)}${verdictChip((notesAll.verdicts || {})[t.champion_id])}${poolBadges(t, lobbyTrade, inSelect)}</div></td>
        <td class="r num">${t.steps_remaining}</td>
        <td><span class="chip ${t.next_grade === "S-" ? "gold" : ""}">${
          esc(t.next_grade || "?")}${t.next_need > 1 ? " ×" + t.next_need : ""}</span></td>
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
    const [sp, gr, sys, pa, mi] = await Promise.all([
      api("/split/progress"), api("/grades/history?limit=3"), api("/system/health"),
      api("/pass").catch(() => ({})), api("/missions").catch(() => ({}))]);

    const dist = sp.distribution || {};
    const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
    const bars = Object.entries(dist).map(([ms, n]) => `
      <div class="bar-row" style="grid-template-columns:88px 1fr 34px">
        <span>${ms >= sp.goal ? "cel" : (+ms === 0 ? "brak" : msName(ms - 1) + " ukończ.")}</span>
        <div class="bar"><i class="${ms >= sp.goal ? "ok" : ""}"
          style="width:${100 * n / total}%"></i></div>
        <span style="text-align:right">${n}</span>
      </div>`).join("");

    const last = (gr.grades || []).map(g => `
      <div class="mini">
        <img onerror="this.src=BLANK" src="${icon(g.key, g.champion_id)}" alt="">
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

    let passHtml = "";
    if (pa && pa.events && pa.events.length) {
      const evs = [...pa.events].sort((a, b) => a.days_left - b.days_left);
      // Zegar MISJI = event SEZONOWY ("Season 3: Act I") - potwierdzone
      // 1.09 w kliencie: to on konczy misje maestrii (potem okienko na
      // odbior nagrod, potem nowy akt z patchem). "Classic Pass" tez ma
      // "Act" w nazwie, ale to inna przepustka; "Mayhem Set" to niezalezna
      // przepustka trybu, bywa przedluzana.
      const mission = evs.find(e => /season/i.test(e.name || ""))
        || evs.find(e => /act|split/i.test(e.name || ""))
        || evs[0];
      const unclaimed = evs.reduce(
        (t, e) => t + ((e.unclaimed || {}).rewardsCount || 0), 0);
      const grace = evs.some(e => e.grace === true);
      const rows = evs.map(e => {
        const pr = e.progress || {};
        const lvl = pr.level != null
          ? ` <small class="dim">poz. ${pr.level}/${pr.totalLevels}${
              pr.level >= pr.totalLevels ? " max" : ""}</small>` : "";
        return `<div class="kv"><span>${esc(e.name || "event")}${lvl}</span>
          <span>za <b>${Math.floor(e.days_left)} dni</b></span></div>`;
      }).join("");
      let proj = "";
      const sim = pa.projection;
      if (sim && sim.median && pa.tempo > 0 && mission) {
        const needDays = Math.ceil(sim.median / pa.tempo);
        const d25 = Math.ceil(sim.p25 / pa.tempo);
        const d75 = Math.ceil(sim.p75 / pa.tempo);
        const slack = Math.floor(mission.days_left) - needDays;
        proj = slack < 0
          ? `<div class="kv" style="color:var(--warn)"><span><b>REŻIM WARIANCJI</b></span>
              <span>brakuje ~${-slack} dni</span></div>
             <div class="tagline" style="color:var(--warn)">Do końca „${esc(mission.name)}"
              za mało czasu na pewniaki — bierz wysokie, choć niepewne P.</div>`
          : `<div class="kv"><span>Projekcja misji
                <small class="dim">(symulacja)</small></span>
              <span>~${needDays} dni <small class="dim">(${d25}–${d75};
                mediana ${sim.median} gier, pula ${sim.pool_size};
                zapas ${slack}, zegar: ${esc(mission.name)})</small></span></div>`;
      } else if (pa.best_expected && pa.tempo > 0 && mission) {
        const needDays = Math.ceil(pa.best_expected / pa.tempo);
        const slack = Math.floor(mission.days_left) - needDays;
        proj = slack < 0
          ? `<div class="kv" style="color:var(--warn)"><span><b>REŻIM WARIANCJI</b></span>
              <span>brakuje ~${-slack} dni</span></div>
             <div class="tagline" style="color:var(--warn)">Do końca „${esc(mission.name)}"
              za mało czasu na pewniaki — bierz wysokie, choć niepewne P.</div>`
          : `<div class="kv"><span>Projekcja misji
                <small class="dim">(dolna granica)</small></span>
              <span>~${needDays} dni <small class="dim">(zapas ${slack},
                zegar: ${esc(mission.name)})</small></span></div>
             <div class="tagline">Projekcja liczy się liderem rankingu w każdej
              grze — losowanie puli realnie ją wydłuża.</div>`;
      }
      passHtml = `
      <div class="panel">
        <div class="eyebrow">Przepustki i deadline'y</div>
        ${rows}
        <div class="kv"><span>Tempo (7 dni)</span><span>${pa.tempo ?? "—"} gier/dzień</span></div>
        ${unclaimed > 0 ? `<div class="kv" style="color:var(--gold)">
          <span>🎁 Nieodebrane nagrody</span><span>${unclaimed}</span></div>` : ""}
        ${grace ? `<div class="tagline" style="color:var(--warn)">
          Któryś event w okresie łaski — zaraz zniknie!</div>` : ""}
        ${proj}
      </div>`;
    }

    let missionsHtml = "";
    const ms = (mi && mi.missions) || [];
    if (ms.length) {
      // pola misji bywaja rozne miedzy wersjami klienta - wyciagamy
      // defensywnie i pokazujemy tylko to, co sie da odczytac
      const rows = ms.filter(m => m.status !== "DUMMY").slice(0, 5).map(m => {
        const title = m.title || m.name || m.internalName || m.id || "misja";
        const o = (m.objectives || [])[0] || {};
        const pr = o.progress || m.progress || {};
        const cur = pr.currentProgress ?? pr.current;
        const tot = pr.totalProgress ?? pr.total;
        const val = (cur != null && tot)
          ? `${cur}/${tot}`
          : (m.status === "COMPLETED" ? "✓" : (m.status || ""));
        return `<div class="kv"><span>${esc(String(title))}</span>
          <span>${esc(String(val))}</span></div>`;
      }).join("");
      missionsHtml = `
      <div class="panel">
        <div class="eyebrow">Misje maestrii</div>
        ${rows}
      </div>`;
    }

    box.innerHTML = passHtml + missionsHtml + `
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
    return `<tr class="grade-row" data-mid="${esc(g.match_id || "")}" style="cursor:pointer" title="kliknij: czemu taka ocena">
      <td><span class="chip ${cls}">${esc(g.grade)}</span></td>
      <td><div class="champ-cell"><img onerror="this.src=BLANK" src="${icon(g.key, g.champion_id)}" alt="">
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
    <tbody>${rows}</tbody></table>
    <div class="tagline" style="margin-top:8px">Kliknij wiersz, żeby zobaczyć,
      co ciągnęło ocenę w górę, a co w dół.</div>`;

  $("grades-body").querySelector("tbody").addEventListener("click", async ev => {
    const tr = ev.target.closest("tr.grade-row");
    if (!tr || !tr.dataset.mid) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("explain-row")) { next.remove(); return; }
    document.querySelectorAll(".explain-row").forEach(e => e.remove());
    const det = document.createElement("tr");
    det.className = "explain-row";
    det.innerHTML = `<td colspan="7" class="dim">liczę…</td>`;
    tr.after(det);
    let ex;
    try {
      ex = await api("/grades/explain?match_id=" + encodeURIComponent(tr.dataset.mid));
    } catch (e) {
      det.innerHTML = `<td colspan="7"><span class="dim">${esc(e.message)}</span></td>`;
      return;
    }
    det.innerHTML = `<td colspan="7">${explainBox(ex)}</td>`;
  });
}

function poolBadges(t, lobbyTrade, inSelect) {
  // jedna rodzina wizualna (ta sama geometria i typografia, roznicowanie
  // wylacznie kolorem) - trzy style gryzly sie na liscie (uwaga z 1.09)
  if (!inSelect) return "";
  return [
    lobbyTrade.has(t.champion_id) ? `<span class="mini-badge trade">wymiana</span>` : "",
    t.explore ? `<span class="mini-badge explore">zbadaj</span>` : "",
    t.pop_tier === "rzadki" ? `<span class="mini-badge rare">słaba populacja</span>` : "",
  ].join("");
}

const FEATURE_PL = {
  gold_per_min: "złoto/min", ka_per_min: "zab.+asysty/min",
  deaths_per_min: "zgony/min", dmg_ratio: "obrażenia (z-score)",
  duration_min: "długość gry",
};

function explainBox(ex) {
  const th = ex.thresholds["A-"] || ex.thresholds["S-"];
  if (!th) return `<span class="dim">model jeszcze nie trenowany</span>`;
  const bars = th.contributions.map(c => {
    const w = Math.min(100, Math.abs(c.pull) * 55);
    const up = c.pull >= 0;
    const bar = `<i class="${up ? "pos" : "neg"}" style="width:${w}%"></i>`;
    return `<div class="pull-row">
      <span class="pull-label">${esc(FEATURE_PL[c.feature] || c.feature)}
        <small class="dim">= ${c.value}</small></span>
      <span class="pull-track"><span class="half l">${up ? "" : bar}</span><b></b><span class="half r">${up ? bar : ""}</span></span>
      <span class="pull-val ${up ? "ok" : "warn"}">${c.pull > 0 ? "+" : ""}${c.pull.toFixed(2)}</span>
    </div>`;
  }).join("");
  const ths = Object.entries(ex.thresholds).map(([k, v]) =>
    `${esc(k)}: <b>${(100 * v.p).toFixed(0)}%</b>${
      v.status === "niewiarygodny"
        ? ` <small class="dim">(próg niewiarygodny)</small>` : ""}`).join(" · ");
  const pct = ex.percentile
    ? `<div class="range" style="margin-top:9px">obrażenia/min ${ex.percentile.value}:
        <b>${ex.percentile.pct}. percentyl</b> z ${ex.percentile.n} obserwacji
        <small class="dim">(zakres: ${esc(ex.percentile.scope)})</small></div>` : "";
  // (6) etykiety augmentów tej gry — kontekst przy ocenie, nie cecha modelu;
  // id spoza słownika (nowy patch przed refreshem) pokazujemy surowo
  const augs = (ex.augments && ex.augments.length)
    ? `<div class="range" style="margin-top:9px">augmenty: ${
        ex.augments.map(a =>
          `<span class="chip ${a.rarity === 2 ? "gold" : ""}">${
            esc(a.name || ("#" + a.id))}</span>`).join(" ")}</div>` : "";
  // (E) pozycja na tle 10 graczy TEGO meczu — kontekst, nie diagnoza:
  // pozycja jest skonfundowana składem (ocena Riota liczy się względem
  // populacji championa, nie wewnątrz lobby)
  const mpct = (ex.match_pct && ex.match_pct.length)
    ? `<div class="range" style="margin-top:9px">na tle meczu: ${
        ex.match_pct.map(m =>
          `${esc(m.label)} <b>${m.rank}.</b><small class="dim">/${m.of}</small>`
        ).join(" · ")}
        <small class="dim">(pozycja zależy od składu — porównuj z normami
        championa, nie „dokręcaj" tej listy)</small></div>` : "";
  return `<div class="explain-box">
    <div class="range">Szansa wg modelu — ${ths}</div>
    <div style="margin-top:9px">${bars}</div>${pct}${mpct}${augs}
    <div class="tagline" style="margin-top:7px">Wkład = waga cechy × odchylenie
      od Twojej normy na tym championie; dodatni ciągnął tę ocenę w górę.</div>
  </div>`;
}

/* ---------- SPLIT ---------- */
async function renderSplit() {
  const d = await api("/split/progress");
  let rc = null;
  try { rc = await api("/recap"); } catch (e) {}
  const dist = d.distribution || {};
  const max = Math.max(...Object.values(dist), 1);
  const bars = Object.entries(dist).map(([ms, n]) => `
    <div class="bar-row">
      <span>${ms >= d.goal ? "cel" : (+ms === 0 ? "brak" : msName(ms - 1) + " ukończony")}</span>
      <div class="bar"><i class="${ms >= d.goal ? "ok" : ""}"
        style="width:${100 * n / max}%"></i></div>
      <span style="text-align:right">${n}</span>
    </div>`).join("");

  const ladder = Object.entries(d.ladder || {}).map(([m, s]) => `
    <div class="kv"><span>${+m === 0 ? "start" : msName(m - 1)} → ${msName(+m)}</span>
      <span>${Object.keys(s.require_grades)[0]} ×${s.games} · ${s.reward_marks} marks</span>
    </div>`).join("");

  const since = d.tracking_since
    ? new Date(d.tracking_since * 1000).toLocaleDateString("pl-PL") : "—";

  let recapHtml = "";
  if (rc && rc.games) {
    const wr = rc.games ? Math.round(100 * rc.wins / rc.games) : 0;
    const gradeChips = Object.entries(rc.grades || {})
      .sort((a, b) => b[1] - a[1])
      .map(([g, n]) => `<span class="chip ${/^[SA]/.test(g) ? "ok" : ""}"
        style="margin:2px">${esc(g)} ×${n}</span>`).join(" ");
    recapHtml = `
      <div class="panel">
        <div class="eyebrow">Podsumowanie splitu (od ${
          new Date(rc.since * 1000).toLocaleDateString("pl-PL")})</div>
        <div class="kv"><span>Gry / winrate</span>
          <span>${rc.games} · ${wr}%</span></div>
        <div class="kv"><span>Czas w grze</span><span>${rc.hours} h</span></div>
        <div class="kv"><span>Różnych championów</span>
          <span>${rc.unique_champions}</span></div>
        <div class="kv"><span>Oceny S / A</span>
          <span>${rc.s_count} / ${rc.a_count}</span></div>
        <div style="margin-top:8px">${gradeChips}</div>
      </div>`;
  }

  // (H) uklad deck jak na TERAZ: trzy panele roznej wysokosci nie balansuja
  // sie w dwoch kolumnach; drabinka idzie do bocznej kolumny, statystyki
  // i wykres marks do glownej
  const distHtml = `
      <div class="panel">
        <div class="eyebrow">Rozkład championów</div>
        ${bars}
      </div>`;
  $("split-body").innerHTML = `
    <div class="deck" style="margin-top:20px">
      <div>
        ${recapHtml ? `<div class="grid2">${distHtml}${recapHtml}</div>` : distHtml}
        <div id="split-chart"></div>
      </div>
      <aside>
        <div class="panel">
          <div class="eyebrow">Drabinka wymagań</div>
          ${ladder || '<div class="sub">jeszcze nieznana</div>'}
          <div class="kv" style="margin-top:14px"><span>Marks of Mastery zdobyte łącznie</span>
            <span>${d.marks_total}</span></div>
          <div class="kv"><span>Championów na celu (${msName(d.goal - 1)})</span><span>${d.at_goal}</span></div>
          <div class="kv"><span>Śledzone od</span><span>${since}</span></div>
        </div>
      </aside>
    </div>`;




  // ---- marks dziennie ----
  let tl = {};
  try { tl = await api("/split/timeline"); } catch (e) { return; }
  const pts = tl.points || [];
  if (pts.length < 2) return;

  const dayKey = t => new Date(t * 1000).toISOString().slice(0, 10);
  const fmtD = k => new Date(k).toLocaleDateString("pl-PL",
    {day: "numeric", month: "short"});

  const endOfDay = new Map();
  for (const q of pts) endOfDay.set(dayKey(q.taken_at), q);
  const daysAll = [];
  for (let t = pts[0].taken_at; t <= pts[pts.length - 1].taken_at + 86399; t += 86400)
    daysAll.push(dayKey(t));
  let prevMarks = pts[0].marks, prevMs3 = pts[0].ms3;
  const dayBars = [];
  for (const k of daysAll) {
    const q = endOfDay.get(k);
    dayBars.push({k, v: q ? q.marks - prevMarks : 0, hit3: q ? q.ms3 > prevMs3 : false});
    if (q) { prevMarks = q.marks; prevMs3 = q.ms3; }
  }

  const total = dayBars.reduce((a, b) => a + b.v, 0);
  const W = 900, H = 220, PAD = 40;
  const vMax = Math.max(...dayBars.map(b => b.v), 1);
  const plotW = W - 2 * PAD, bw = Math.min(46, plotW / dayBars.length * 0.62);
  const xB = i2 => PAD + plotW * (i2 + 0.5) / dayBars.length;
  const yB = v => H - PAD - (H - 2 * PAD) * v / vMax;

  $("split-chart").insertAdjacentHTML("beforeend", `
    <div class="panel" style="margin-top:14px">
      <div class="panel-label">Marks dziennie ·
        <span class="dim">łącznie +${total} od ${fmtD(dayBars[0].k)} ·
        obrysowany słupek = tego dnia champion wszedł na III</span></div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block">
        <line x1="${PAD}" x2="${W - PAD}" y1="${H - PAD}" y2="${H - PAD}"
          stroke="var(--line)"/>
        ${dayBars.map((b, i2) => b.v === 0 ? "" : `
          <rect x="${(xB(i2) - bw / 2).toFixed(1)}" y="${yB(b.v).toFixed(1)}"
            width="${bw.toFixed(1)}" height="${(H - PAD - yB(b.v)).toFixed(1)}"
            rx="3" fill="var(--gold)"
            ${b.hit3 ? 'stroke="#F4E9CF" stroke-width="2"' : ""}/>
          <text x="${xB(i2).toFixed(1)}" y="${(yB(b.v) - 6).toFixed(1)}"
            text-anchor="middle" fill="var(--gold)"
            font-size="12" font-family="var(--mono)">+${b.v}${
            b.hit3 ? ' <tspan fill="#F4E9CF" font-weight="700">· III</tspan>' : ""}</text>`).join("")}
        ${dayBars.map((b, i2) => `
          <text x="${xB(i2).toFixed(1)}" y="${H - PAD + 16}" text-anchor="middle"
            fill="var(--dim)" font-size="10.5" font-family="var(--mono)">${
            fmtD(b.k).replace(" ", "\u00a0")}</text>`).join("")}
      </svg>
    </div>`);
}

/* ---------- LABORATORIUM ---------- */
// pelna drabinka z db.GRADES - brakujace minusy i tier D sortowaly sie
// na indexOf=-1 przed "C", z A- (ocena centralna misji) na czele winowajcow
const GRADE_ORDER = ["D-","D","D+","C-","C","C+","B-","B","B+",
                     "A-","A","A+",">=A-",">=S-","S-","S","S+"];
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

  // (6) augmenty przy ocenach — liczniki OPISOWE (per-augment n jest
  // jednocyfrowe: to jest do oglądania, nie do wniosków ilościowych)
  let ag = null;
  try { ag = await api("/augments/stats"); } catch (e) {}
  if (ag && ag.augments && ag.augments.length) {
    const RAR = {0: "", 1: "gold", 2: "gold"};
    const RAR_TXT = {0: "Silver", 1: "Gold", 2: "Prismatic"};
    const arows = ag.augments.slice(0, 30).map(a => `<tr>
      <td><span class="chip ${RAR[a.rarity] || ""}">${esc(a.name || ("#" + a.id))}</span>
        <small class="dim">${a.rarity != null ? RAR_TXT[a.rarity] : ""}</small></td>
      <td class="r num">${a.games}</td>
      <td class="r num">${a.a_minus}</td>
      <td class="r num">${a.s_minus}</td>
      <td class="r num">${a.wins}</td>
    </tr>`).join("");
    $("lab-body").insertAdjacentHTML("beforeend", `<div class="panel" style="margin-top:14px">
      <div class="eyebrow">Augmenty przy ocenach (opisowo)</div>
      <table><thead><tr><th>Augment</th><th class="r">Gier</th>
        <th class="r">≥A-</th><th class="r">S-</th><th class="r">Win</th></tr></thead>
      <tbody>${arows}</tbody></table>
      <div class="tagline">Liczniki z własnych gier — przy tej próbce to
        kontekst do oglądania, nie materiał na wnioski.</div></div>`);
  }
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
    eog_raw: "Ekrany końcowe", match_timeline: "Timeline'y",
    champ_select_pool: "Pule z champ selecta",
    player_stat: "Wiersze statystyk", snapshot: "Snapshoty"};

  // (A6) progi wieku: brak swiezego snapshotu >48 h znaczy, ze dobowy cron
  // nie domyka dziur w milestone'ach - dokladnie to, co mial krzyczec
  const AGE_WARN = {snapshot: 48 * 3600, snapshot_cron: 48 * 3600};
  const seen = Object.entries(d.last_seen).map(([k, ts]) => {
    const old = AGE_WARN[k] && (d.now - (ts || 0)) > AGE_WARN[k];
    return `<div class="kv"><span>${LABELS[k] || k}</span>
      <span style="color:${old ? "var(--warn)" : "inherit"}">${ago(ts)}</span></div>`;
  }).join("");
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

  // (P4) bramki danych: definicje zyja w STAN.md, tu tylko liczniki
  const gates = (d.gates || []).map(g => {
    const done = g.have >= g.need;
    const w = Math.min(100, 100 * g.have / g.need);
    return `<div class="bar-row" style="grid-template-columns:1fr 80px 58px">
      <span style="font-size:12.5px">${esc(g.label)}</span>
      <div class="bar"><i class="${done ? "ok" : ""}" style="width:${w}%"></i></div>
      <span style="text-align:right;color:${done ? "var(--ok)" : "var(--dim)"}">${
        g.have}/${g.need}</span>
    </div>`;
  }).join("");
  // (P8/F3) potok w dwoch grupach: ALARM = realny przeciek (czerwone przy
  // >0), INFO = poprawne dzialanie albo zaleglosc, ktora agent dociaga sam
  // (dodge, custom, odzysk). Jeden napis "wszystko powyzej zera to przeciek"
  // przy 28 dodge'ach wygladal jak awaria.
  const PIPE_ALARM = {orphan_grades: "Oceny bez meczu",
    eog_no_participants: "Ekrany bez tożsamości",
    eog_bez_oceny: "Ekrany gier misji BEZ oceny (kanał ocen!)",
    games_unlinked_pool: "Gry misji bez przypiętej puli (predykcja wisi)"};
  const PIPE_INFO = {stale_pools: "Pule bez gry (dodge / remake / trening)",
    missing_games: "Gry bez statystyk — agent odzyska sam",
    timeline_missing: "Gry bez timeline — agent dociąga sam"};
  const noGrade = (d.pipeline_detail || {}).eog_bez_oceny || [];
  const pipeRow = (k, label, alarm) => {
    const n = (d.pipeline || {})[k];
    if (n == null) return "";
    const color = alarm ? (n ? "var(--warn)" : "var(--ok)") : "var(--dim)";
    const ids = (k === "eog_bez_oceny" && n && noGrade.length)
      ? `<div class="kv" style="border:none;padding-top:0"><span class="dim"
           style="font-size:11.5px">${noGrade.map(esc).join(", ")}</span></div>` : "";
    return `<div class="kv"><span>${label}</span>
      <span style="color:${color}">${n}</span></div>${ids}`;
  };
  const pipe = Object.entries(PIPE_ALARM).map(([k, l]) => pipeRow(k, l, true)).join("")
    + `<div class="kv" style="border:none;padding:12px 0 2px"><span class="dim"
         style="font-size:11px;letter-spacing:.08em;text-transform:uppercase">informacyjnie</span></div>`
    + Object.entries(PIPE_INFO).map(([k, l]) => pipeRow(k, l, false)).join("");
  // (A6) "ostatni sukces 12 dni temu, bo nikt nie gral" wygladal identycznie
  // jak zdrowie - stad prog wieku takze przy ok:true
  const bk = d.last_backup;
  const bkOld = bk && (d.now - (bk.ts || 0)) > 7 * 86400;
  const backupKv = `<div class="kv"><span>Ostatni backup</span>
    <span style="color:${bk ? (bk.ok && !bkOld ? "var(--ok)" : "var(--warn)") : "var(--dim)"}">${
    bk ? (bk.ok ? "OK" : "BŁĄD") + " · " + ago(bk.ts) : "brak meldunku"}</span></div>`;
  const ra = d.riot_auth;
  const authKv = `<div class="kv"><span>Klucz Riot API</span>
    <span style="color:${ra ? (ra.ok ? "var(--ok)" : "var(--warn)") : "var(--dim)"}">${
    ra ? (ra.ok ? "działa" : `MARTWY (HTTP ${ra.status}) · ` + ago(ra.ts))
       : "brak danych"}</span></div>`;
  const balOld = d.balance_fetched_at && (d.now - d.balance_fetched_at) > 8 * 86400;
  const balKv = `<div class="kv"><span>Mnożniki balansu</span>
    <span style="color:${balOld ? "var(--warn)" : "inherit"}">${
    ago(d.balance_fetched_at)}</span></div>`;

  // (E) watchdog: przyrost maestrii między snapshotami bez śladu eog =
  // grano bez agenta; gry są odzyskiwalne, przepadają live/eventdata
  const gaps = d.agent_gaps || [];
  const fmtTs = ts => new Date(ts * 1000).toLocaleString("pl-PL",
    {day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit"});
  const gapBanner = gaps.length ? `<div class="msg" style="margin-top:16px;
      border:1px solid #6B4E28;border-radius:8px;padding:10px 14px;
      color:var(--warn)">⚠ Grano bez agenta: ${gaps.length} ${
      gaps.length === 1 ? "okno" : "okna"} przyrostu maestrii bez śladu
      ekranu końcowego (ostatnie: ${fmtTs(gaps[gaps.length - 1].from_ts)}–${
      fmtTs(gaps[gaps.length - 1].to_ts)}, +${
      gaps[gaps.length - 1].points_delta} pkt). Gry odzyska P6/backfill,
      zanim wypadną z okna 20 — bezpowrotnie przepadają live i eventdata.
    </div>` : "";

  // (E) czarna skrzynka agenta — WIEK meldunku jest częścią sygnału:
  // martwy agent to starzejący się wpis, nie fałszywe „ok"
  const ah = d.agent_health;
  const ahOld = ah && (d.now - (ah.ts || 0)) > 900;
  const agentPanel = `<div class="panel"><div class="eyebrow">Agent</div>
    ${ah ? `
      <div class="kv"><span>Meldunek</span>
        <span style="color:${ahOld ? "var(--warn)" : "var(--ok)"}">${ago(ah.ts)}</span></div>
      <div class="kv"><span>Kolejka dosyłki</span>
        <span style="color:${ah.queue ? "var(--warn)" : "inherit"}">${ah.queue ?? "—"}</span></div>
      <div class="kv"><span>Odrzucone (.bad)</span>
        <span style="color:${ah.bad ? "var(--warn)" : "inherit"}">${ah.bad ?? "—"}</span></div>
      <div class="kv"><span>WebSocket</span>
        <span style="color:${ah.ws_ok ? "var(--ok)" : "var(--warn)"}">${
        ah.ws_ok ? "połączony" : "polling"}</span></div>`
      : `<div class="msg dim">brak meldunku — agent w tej wersji jeszcze
         nie startował</div>`}
  </div>`;

  let pred = {};
  try { pred = await api("/predictions/scorecard"); } catch (e) {}
  $("system-body").innerHTML = gapBanner + `
    <div class="cols2" style="margin-top:20px">
      <div class="panel"><div class="eyebrow">Ostatnia aktywność</div>${seen}${backupKv}${authKv}${balKv}</div>
      ${agentPanel}
      <div class="panel"><div class="eyebrow">Zebrane dane</div>${counts}
        ${d.custom_games ? `<div class="kv"><span class="dim">w tym treningi
          (custom, poza misją)</span><span class="dim">${d.custom_games}</span></div>` : ""}
        <div class="kv" style="margin-top:12px"><span>Patch Data Dragon</span>
          <span>${esc(d.ddragon_patch || "—")}</span></div>
      </div>
      <div class="panel"><div class="eyebrow">Bramki danych</div>${gates}
        <div class="tagline">Bramki otwierają się same z napływem gier —
          definicje i decyzje w pamięci projektu.</div></div>
      <div class="panel"><div class="eyebrow">Zdrowie potoku</div>${pipe}
        <div class="tagline">Na czerwono tylko to, co wymaga reakcji. Dolne
          liczniki to normalne działanie (dodge, custom) albo zaległości,
          które agent dociąga sam.</div></div>
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
      ${(() => {
        // (B1) kalibracja per próg i per źródło p: "częstości" to estymator,
        // który steruje E(c) — to jego Brier jest miarą uczciwości rankingu
        const SRC = {model: "model", rates: "częstości (E)"};
        const rows = Object.entries(pred.per_threshold || {}).flatMap(([th, srcs]) =>
          Object.entries(srcs).filter(([, c]) => c).map(([src, c]) =>
            `<div class="kv" style="border:none;padding:2px 0">
              <span class="dim" style="font-size:11.5px">${th} · ${SRC[src] || src}
                · n=${c.n}</span>
              <span style="font-size:12px">Brier ${c.brier} · trafienia ${
                (100 * c.hit_rate).toFixed(0)}% (CI ${(100 * c.hit_ci95[0]).toFixed(0)}–${
                (100 * c.hit_ci95[1]).toFixed(0)}) · śr. p ${(100 * c.mean_p).toFixed(0)}%${
                c.spiegelhalter_z != null ? ` · Z=${c.spiegelhalter_z}` : ""}</span>
            </div>`));
        return rows.length ? `<div style="margin-top:6px">${rows.join("")}
          <div class="tagline">|Z| &gt; 2 = kalibracja odrzucona (Spiegelhalter);
            przy n &lt; 20 patrz na CI, nie na punkt.</div></div>` : "";
      })()}
      <div style="margin-top:14px">${modelRows}</div>
      <div class="kv" style="margin-top:10px"><span class="dim" style="font-size:11.5px">
        Miara to walidacja leave-one-out — każda obserwacja raz jako test.
        Metryki liczone na danych treningowych zawsze wyglądają lepiej.</span></div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="eyebrow">Dziennik zdarzeń</div>
      <table><tbody>${events}</tbody></table>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="eyebrow">Konsola LCU</div>
      <div class="sub" style="margin-bottom:10px">Surowy GET do klienta gry —
        wykonuje agent przy najbliższym obiegu (~3 s), wyłącznie odczyty.
        Token zapisu ten sam co w agencie.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input id="probe-token" type="password" placeholder="X-API-Token"
          style="width:170px">
        <input id="probe-path" placeholder="/lol-summoner/v1/current-summoner"
          style="flex:1;min-width:240px">
        <button id="probe-run">Wyślij</button>
      </div>
      <pre id="probe-out" style="white-space:pre-wrap;max-height:320px;
        overflow:auto;margin-top:10px;font-size:12px"></pre>
    </div>`;

  // (42) konsola: zlecenie -> agent wykonuje -> odpytujemy wynik
  try { $("probe-token").value = localStorage.getItem("api_token") || ""; }
  catch (e) {}
  $("probe-run").addEventListener("click", async () => {
    const out = $("probe-out");
    const path = $("probe-path").value.trim();
    if (!path) return;
    try { localStorage.setItem("api_token", $("probe-token").value); }
    catch (e) {}
    out.textContent = "zlecam…";
    let created;
    try {
      const r = await fetch("/api/probe", {method: "POST",
        headers: {"Content-Type": "application/json",
                  "X-API-Token": $("probe-token").value},
        body: JSON.stringify({path})});
      created = await r.json();
      if (!r.ok) throw new Error(created.detail || ("HTTP " + r.status));
    } catch (e) { out.textContent = "błąd zlecenia: " + e.message; return; }
    const t0 = Date.now();
    while (Date.now() - t0 < 30000) {
      await new Promise(res => setTimeout(res, 1000));
      let p;
      try { p = await api("/probe/" + created.id); } catch (e) { continue; }
      if (p.answered_at) {
        let body = p.response || "";
        try { body = JSON.stringify(JSON.parse(body), null, 2); } catch (e) {}
        out.textContent = `HTTP ${p.http_status}${
          p.truncated ? " (odpowiedź przycięta)" : ""}\n${body}`;
        return;
      }
      out.textContent = `czekam na agenta… ${
        Math.round((Date.now() - t0) / 1000)} s`;
    }
    out.textContent = "agent nie odpowiedział w 30 s — klient wyłączony?";
  });
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
  // (partia D) blad renderu ma byc widoczny w widoku, nie tylko w konsoli -
  // zakladka wisiala na wiecznym "wczytywanie…" bez sladu dla czlowieka
  const box = $(VIEWS[hash][0]);
  box.querySelector(".route-error")?.remove();
  try { await VIEWS[hash][1](); }
  catch (e) {
    console.error(e);
    box.insertAdjacentHTML("afterbegin", `<div class="route-error msg"
      style="color:var(--warn)">⚠ Nie udało się załadować widoku:
      ${esc(e.message)}</div>`);
  }
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
