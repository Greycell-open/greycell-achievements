// Greycell Achievements web client. One page for both modes:
//   server  signed in to a sync server (hosted or self-hosted): library,
//           privacy, devices, export, account deletion
//   local   `openachievements serve` over this computer's profile folder:
//           library and its watcher, no account
// A `?u=name` URL shows someone's public profile, read-only.
//
// Every piece of text from a pack, a platform or a user goes through esc()
// before it reaches innerHTML. Platform artwork (Steam, RetroAchievements and
// the rest) is shown only in local mode, through this app's own picture cache
// (v1/local/art), so the browser itself never asks those sites; a sync server's
// pages show letter tiles.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* private window */ } },
  };
  const localToken = (document.querySelector('meta[name="oa-local-token"]') || {}).content || "";
  let mode = "server";
  let token = store.get("oa_token");
  let library = null;
  let authTab = "login";
  let steamLinked = false;
  let steamAccount = null;
  const revealed = new Set();                    // hidden achievements shown on purpose, this visit
  let revealAll = store.get("oa_reveal_hidden") === "1";

  // Everything is relative to <base>: "/" on this computer, "/openachievements/"
  // (or wherever the operator mounts it) on a hosted server.
  const BASE_URL = new URL(document.baseURI).href;
  const BASE_PATH = new URL(document.baseURI).pathname;

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    if (mode === "server" && token) headers.Authorization = "Bearer " + token;
    if (mode === "local") headers["X-OA-Token"] = localToken;
    const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    if (res.status === 401 && mode === "server") { signOutLocally(); throw new Error("Signed out"); }
    if (!res.ok) {
      let msg = "Something went wrong (" + res.status + ")";
      try { const d = (await res.json()).detail; msg = (d && d.message) || msg; } catch (e) { /* not json */ }
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  function toast(text) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = text; document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  // Pictures come through this app (v1/local/art), which keeps a copy on this
  // computer; a picture that is not there turns back into the letter tile.
  const glyph = (label) => `<span class="glyph" aria-hidden="true">${esc((label || "?").trim().charAt(0).toUpperCase() || "?")}</span>`;
  function picture(url, label, alt) {
    if (url && /^https:\/\//.test(url) && mode === "local") {
      return `<img src="v1/local/art?u=${encodeURIComponent(url)}" alt="${esc(alt || "")}" loading="lazy" ` +
        `data-glyph="${esc(label || "?")}">`;
    }
    return glyph(label);
  }
  function banner(g) {
    if (mode !== "local") return glyph(g.title || g.game_id);
    return `<img class="banner" src="v1/local/art/game/${encodeURIComponent(g.game_id)}" alt="" loading="lazy" ` +
      `data-glyph="${esc(g.title || g.game_id)}">`;
  }
  document.addEventListener("error", (e) => {         // no picture: the letter tile instead
    const img = e.target;
    if (img && img.tagName === "IMG" && img.dataset.glyph !== undefined) img.outerHTML = glyph(img.dataset.glyph);
  }, true);
  const PROVENANCE_TEXT = {
    "imported": "imported", "local-executable": "played here", "save-derived": "from save",
    "game-log": "from game log", "manual": "ticked by hand", "unverified": "unverified", "emulator": "game reported",
  };
  const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
  const fmtHours = (s) => s >= 3600 ? (s / 3600).toFixed(1) + " h" : s < 60 ? "under a minute" : Math.round(s / 60) + " min";

  // ---- views -----------------------------------------------------------------

  function show(section) {
    for (const id of ["intro", "library", "account"]) $(id).hidden = id !== section;
  }

  // Linked platforms, for the "Link accounts" menu: name of the linked account, or null.
  const links = { steam: null, psn: null, xbox: null, gog: null, ra: null };
  async function refreshLinks() {
    if (mode !== "local") return;
    const get = (p) => api(p).catch(() => ({}));
    const [st, ps, xb, gg, ra] = await Promise.all([get("v1/local/steam"), get("v1/local/psn"), get("v1/local/xbox"),
      get("v1/local/gog"), get("v1/local/retroachievements")]);
    links.steam = st.account || null; links.psn = ps.linked ? ps.online_id : null;
    links.xbox = xb.linked ? xb.gamertag : null; links.gog = gg.linked ? gg.username : null;
    links.ra = ra.linked ? ra.username : null;
    steamAccount = links.steam;
    renderTop();
  }

  const ROADMAP = "https://greycell.app/apps/greycell-achievements-roadmap.html";   // what is coming, on greycell.app

  function renderTop() {
    const bits = [];
    if (mode === "local") {
      bits.push(`<button class="pill add" data-act="add" type="button">+ Add games</button>`);
      const items = [["steam", "Steam"], ["psn", "PlayStation"], ["xbox", "Xbox"], ["gog", "GOG"], ["ra", "RetroAchievements"]];
      const count = items.filter(([k]) => links[k]).length;
      bits.push(`<div class="menu-wrap"><button class="pill" data-act="links" id="linksBtn" type="button" aria-haspopup="menu" ` +
        `aria-expanded="false" aria-controls="linksMenu">Link accounts${count ? ` <span class="badge-count">${count}</span>` : ""} \u25BE</button>` +
        `<div class="menu" id="linksMenu" role="menu" hidden>` +
        items.map(([k, label]) => `<button type="button" role="menuitem" data-act="${k}"><span>${label}</span>` +
          (links[k] ? `<span class="linked">\u2713 ${esc(links[k])}</span>` : `<span class="muted">Link</span>`) + `</button>`).join("") +
        `</div></div>`);
      bits.push(`<div class="menu-wrap"><button class="pill" data-act="settings" id="settingsBtn" type="button" aria-haspopup="menu" ` +
        `aria-expanded="false" aria-controls="settingsMenu">Settings \u25BE</button>` +
        `<div class="menu" id="settingsMenu" role="menu" hidden>` +
        `<button type="button" role="menuitem" data-act="checkupdates"><span>Check for updates</span></button>` +
        `<button type="button" role="menuitem" data-act="sounds"><span>Sounds</span></button>` +
        `<button type="button" role="menuitem" data-act="keeper"><span>Saves</span></button>` +
        `<button type="button" role="menuitem" data-act="privacy"><span>Privacy</span></button>` +
        `<button type="button" role="menuitem" data-act="about"><span>About</span></button>` +
        (appControls.autostart !== null && appControls.autostart !== undefined
          ? `<button type="button" role="menuitem" data-act="autostart"><span>Start at login</span>` +
            (appControls.autostart ? `<span class="linked">\u2713 On</span>` : `<span class="muted">Off</span>`) + `</button>` : "") +
        (appControls.quit ? `<button type="button" role="menuitem" data-act="quitapp"><span>Quit Greycell Achievements</span></button>` : "") +
        `</div></div>`);
      bits.push(`<a class="pill" href="${ROADMAP}" target="_blank" rel="noreferrer noopener">Roadmap</a>`);
      bits.push(`<button class="pill where" data-act="keeper" type="button" title="Where your achievements and saves are kept">On this computer</button>`);
      if (syncConnected) bits.push(`<button class="pill" data-act="sync" type="button">Sync</button>`);   // only with a sync server
    } else if (token) {
      bits.push(`<button class="pill" data-act="library" type="button">Library</button>`);
      bits.push(`<button class="pill" data-act="account" type="button">Account</button>`);
      bits.push(`<button class="pill" data-act="signout" type="button">Sign out</button>`);
    }
    $("topActions").innerHTML = bits.join("");
  }

  const STATUSES = [["playing", "Playing"], ["backlog", "Backlog"], ["wishlist", "Wishlist"],
    ["completed", "Completed"], ["dropped", "Dropped"]];
  const STATUS_NAME = Object.fromEntries(STATUSES);

  function renderSummary(lib) {
    const t = lib.totals;
    const pct = t.achievements ? Math.round(100 * t.unlocked / t.achievements) : 0;
    const count = (s) => lib.games.filter((g) => g.status === s).length;
    const perfect = lib.games.filter((g) => g.total && g.unlocked === g.total).length;
    const played = lib.games.reduce((n, g) => n + (g.playtime_seconds || 0), 0);
    const name = (lib.profile && lib.profile.name) || "";
    $("profileHead").innerHTML = name ? `<h1>${esc(name)}</h1><span>${t.games} games` +
      (played ? ` · ${esc(fmtHours(played))} played` : "") + `</span>` : "";
    $("summary").innerHTML =
      `<div class="stat hero"><b>${t.unlocked}</b><span>of ${t.achievements} achievements (${pct}%)</span></div>` +
      `<div class="stat"><b>${t.points}</b><span>points</span></div>` +
      `<div class="stat"><b>${count("playing")}</b><span>playing</span></div>` +
      `<div class="stat"><b>${count("backlog")}</b><span>in the backlog</span></div>` +
      `<div class="stat"><b>${count("completed")}</b><span>completed</span></div>` +
      `<div class="stat platinum-stat"><b>${perfect}</b><span>platinum</span></div>`;
  }

  const lastPlayed = (g) => g.last_activity || g.last_played || "";
  function ago(iso) {
    if (!iso) return "";
    const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
    if (days < 1) return "played today";
    if (days < 2) return "played yesterday";
    if (days < 30) return `played ${days} days ago`;
    if (days < 365) return `played ${Math.floor(days / 30)} month${days < 60 ? "" : "s"} ago`;
    return `played ${Math.floor(days / 365)} year${days < 730 ? "" : "s"} ago`;
  }
  function sorted(list) {
    const by = $("sort").value;
    const pct = (g) => g.total ? g.unlocked / g.total : -1;
    const name = (g) => (g.title || g.game_id).toLowerCase();
    return [...list].sort(by === "name" ? (a, b) => name(a).localeCompare(name(b))
      : by === "progress" ? (a, b) => pct(b) - pct(a) || name(a).localeCompare(name(b))
      : (a, b) => lastPlayed(b).localeCompare(lastPlayed(a)) || name(a).localeCompare(name(b)));
  }

  function gameCard(g) {
    const pct = g.total ? Math.round(100 * g.unlocked / g.total) : 0;
    const sources = [g.platform, ...(g.linked_games || []).map((l) => l.platform)].filter(Boolean);
    const sub = [ago(lastPlayed(g)), g.playtime_seconds ? fmtHours(g.playtime_seconds) : ""].filter(Boolean).join(" · ")
      || [...new Set(sources)].join(" · ");
    const plat = g.total > 0 && g.unlocked === g.total;
    const badge = (g.status ? `<span class="badge ${esc(g.status)}">${esc(STATUS_NAME[g.status] || g.status)}</span>` : "") +
      (plat ? `<span class="badge platinum">Platinum</span>` : "");
    return `<button type="button" class="game${plat ? " complete" : ""}" data-game="${esc(g.game_id)}">` +
      banner(g) +
      `<span class="game-body"><span class="game-title">${esc(g.title || g.game_id)}</span>` +
      `<span class="game-sub">${esc(sub || "No platform")}${badge}</span>` +
      (g.total ? `<span class="game-sub">${g.unlocked}/${g.total} achievements` : `<span class="game-sub">No achievements known yet`) +
      (g.points_total ? ` · ${g.points}/${g.points_total} pts` : "") + `</span>` +
      `<span class="bar"><i style="width:${pct}%"></i></span></span></button>`;
  }

  // Where a game's achievements come from, for the platform filter.
  const SOURCE_OF = { steam: "Steam", psn: "PlayStation", xbox: "Xbox", gog: "GOG", ra: "RetroAchievements" };
  const sourceOf = (id) => SOURCE_OF[String(id).split("-")[0]] || "Other";
  const gameSources = (g) => [g, ...(g.linked_games || [])].map((x) => [sourceOf(x.game_id), x.platform || ""]);

  function renderPlatforms() {
    const sel = $("platform");
    const keep = sel.value || store.get("oa_platform") || "";
    const groups = {};
    for (const g of library.games) {
      for (const [src, plat] of gameSources(g)) {
        const grp = groups[src] || (groups[src] = { n: new Set(), plats: {} });
        grp.n.add(g.game_id);
        if (plat) (grp.plats[plat] || (grp.plats[plat] = new Set())).add(g.game_id);
      }
    }
    const order = [...Object.values(SOURCE_OF), "Other"].filter((s) => groups[s]);
    let html = `<option value="">All platforms</option>`;
    for (const src of order) {
      const plats = Object.entries(groups[src].plats).sort((a, b) => b[1].size - a[1].size);
      if (plats.length > 1) {
        html += `<optgroup label="${esc(src)}"><option value="src:${esc(src)}">All ${esc(src)} (${groups[src].n.size})</option>` +
          plats.map(([p, ids]) => `<option value="plat:${esc(src)}|${esc(p)}">${esc(p)} (${ids.size})</option>`).join("") + `</optgroup>`;
      } else {
        html += `<option value="src:${esc(src)}">${esc(src)} (${groups[src].n.size})</option>`;
      }
    }
    sel.innerHTML = html;
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
  }

  function onPlatform(g) {
    const v = $("platform").value;
    if (!v) return true;
    if (v.startsWith("src:")) return gameSources(g).some(([s]) => s === v.slice(4));
    const [src, plat] = v.slice(5).split("|");
    return gameSources(g).some(([s, p]) => s === src && p === plat);
  }

  function renderGames() {
    if (!library) return;
    renderPlatforms();
    const q = $("search").value.trim().toLowerCase();
    const f = $("filter").value;
    const match = (g) => {
      const name = (g.title || g.game_id).toLowerCase();
      return onPlatform(g) && (!q || name.includes(q) || (g.platform || "").toLowerCase().includes(q));
    };
    const shelves = f === "home" && !q;
    let shown = 0;
    if (shelves) {
      const groups = [...STATUSES, [null, "Other games"]].map(([s, label]) => {
        const games = sorted(library.games.filter((g) => (g.status || null) === s && onPlatform(g)));
        shown += games.length;
        return games.length ? `<section class="shelf"><h2>${esc(label)} <small>${games.length}</small></h2>` +
          `<div class="games">${games.map(gameCard).join("")}</div></section>` : "";
      });
      $("shelves").innerHTML = groups.join("");
      $("games").innerHTML = "";
    } else {
      const list = library.games.filter((g) => {
        if (!match(g)) return false;
        if (f.startsWith("s:")) return g.status === f.slice(2);
        if (f === "started") return g.unlocked > 0 && g.unlocked < g.total;
        if (f === "complete") return g.total > 0 && g.unlocked === g.total;
        if (f === "untouched") return g.unlocked === 0;
        return true;
      });
      shown = list.length;
      $("shelves").innerHTML = "";
      $("games").innerHTML = sorted(list).map(gameCard).join("");
    }
    const empty = $("empty");
    empty.hidden = shown > 0;
    empty.textContent = library.games.length ? "No games match." :
      (mode === "local" ? "No games yet. Use \u201c+ Add games\u201d to find one, or play: the watcher adds what it recognises."
        : "Nothing synced yet. Connect a device from the Account page.");
  }

  // ---- adding games from the catalogue --------------------------------------------

  let searchTimer = null;
  async function searchCatalogue() {
    const q = $("catalogSearch").value.trim();
    if (!q) { $("catalogResults").innerHTML = ""; return; }
    const data = await api("v1/local/catalog?q=" + encodeURIComponent(q) + "&limit=40");
    $("catalogInfo").textContent = data.catalogued
      ? `${data.catalogued.toLocaleString()} Steam games catalogued so far` : "The catalogue is still empty: the crawl has not reached any games yet.";
    $("catalogResults").innerHTML = data.results.map((r) =>
      `<div class="result"><div><div>${esc(r.title)}</div><div class="muted">${r.achievements} achievements · Steam app ${r.appid}</div></div>` +
      (r.in_library ? `<button type="button" disabled>In library</button>`
        : `<button type="button" data-add="${r.appid}">Add</button>`) + `</div>`).join("") ||
      `<p class="muted">No catalogued game matches. The crawl is still working through Steam, so it may not have reached it yet.</p>`;
  }

  async function addGame(button) {
    button.disabled = true;
    try {
      const r = await api("v1/local/catalog/add", { method: "POST",
        body: JSON.stringify({ appid: Number(button.dataset.add), status: $("addStatus").value }) });
      button.textContent = "Added";
      toast(`${r.title}: ${STATUS_NAME[r.status] || "added"}`);
      await loadLibrary();
    } catch (err) { button.disabled = false; throw err; }
  }

  function openGame(id) {
    const g = library.games.find((x) => x.game_id === id);
    if (!g) return;
    $("gameTitle").textContent = g.title || g.game_id;
    if (g.total > 0 && g.unlocked === g.total) {
      const chip = document.createElement("span");
      chip.className = "badge platinum"; chip.textContent = "Platinum";
      $("gameTitle").append(" ", chip);
    }
    const linked = (g.linked_games || []).map((l) => l.title || l.game_id);
    $("gameMeta").textContent = `${g.unlocked}/${g.total} unlocked · ${g.points} points` +
      (g.playtime_seconds ? ` · played ${fmtHours(g.playtime_seconds)}` : "") +
      (linked.length ? ` · includes ${linked.join(", ")}` : "");
    $("achievementList").innerHTML = g.achievements.map((a) => {
      const chips = [`<span class="chip pts">${a.points} pts</span>`, `<span class="chip">${esc(a.source)}</span>`];
      for (const p of a.provenance) chips.push(`<span class="chip ${esc(p)}">${esc(PROVENANCE_TEXT[p] || p)}</span>`);
      if (a.unlocked_at) chips.push(`<span class="chip">${esc(fmtDate(a.unlocked_at))}</span>`);
      if (a.progress) chips.push(`<span class="chip">${a.progress.value}/${a.progress.target}</span>`);
      const actions = "";
      // Like Steam: a hidden achievement is a card you click to see, and click again to hide.
      const shown = a.secret && (revealAll || revealed.has(a.key)) ? a.secret : a;
      const secret = a.secret ? ` secret" data-reveal="${esc(a.key)}" role="button" tabindex="0" ` +
        `title="${shown === a ? "Click to show this hidden achievement" : "Click to hide it again"}` : "";
      if (a.secret) chips.push(`<span class="chip">${shown === a ? "hidden · click to show" : "hidden"}</span>`);
      return `<div class="ach ${a.unlocked ? "unlocked" : "locked"}${secret}">` + picture(a.icon, shown.name, "") +
        `<div class="ach-body"><div class="ach-name">${esc(shown.name)}</div>` +
        (shown === a && a.secret ? `<div class="ach-desc">Details are hidden until you unlock it.</div>`
          : shown.description ? `<div class="ach-desc">${esc(shown.description)}</div>` : "") +
        `<div class="chips">${chips.join("")}</div></div>${actions}</div>`;
    }).join("") || (mode === "local" && /^(steam-\d+|local-.+)$/.test(g.game_id)
      ? `<p class="muted">No achievement list yet. <button type="button" data-fetchach="${esc(g.game_id)}">${g.game_id.startsWith("local-") ? "Find achievements on Steam" : "Get achievements from Steam"}</button></p>`
      : `<p class="muted">No achievements for this game yet.</p>`);
    $("gameStatusWrap").hidden = mode !== "local";
    renderSaves(id, g);
    $("revealHidden").checked = revealAll;
    $("revealWrap").hidden = !g.achievements.some((a) => a.secret);
    $("gameStatus").value = g.status || "none";
    $("gameDialog").dataset.game = id;
    if (!$("gameDialog").open) $("gameDialog").showModal();
  }

  // ---- saves: kept copies, putting them back, what changed, rules -------------
  let saveView = { id: null, game: null, kept: null, changes: [], suggestions: [] };
  const gid = (id) => encodeURIComponent(id);

  async function renderSaves(id, g) {
    const box = $("savesFound");
    box.hidden = true;
    saveView = { id, game: g, kept: null, changes: [], suggestions: [] };
    if (mode !== "local") return;
    let kept, saves;
    try {
      [kept, saves] = await Promise.all([api(`v1/local/games/${gid(id)}/kept`), api(`v1/local/games/${gid(id)}/saves`)]);
    } catch (err) { return; }
    if ($("gameDialog").dataset.game !== id) return;
    if (!kept.folders.length && !kept.snapshots.length) return;
    saveView.kept = kept;
    const n = kept.snapshots.length, newest = kept.snapshots[0];
    box.innerHTML = `<div class="saves-head"><b>Saves</b> <span>` +
      (newest ? `${n} kept ${n === 1 ? "copy" : "copies"}, newest ${esc(since(newest.taken_at))}`
        : kept.on ? "not kept yet: a copy is made after the game next saves" : "keeping copies is off") + `</span></div>` +
      (kept.folders.length ? `<div>From <code>${esc(kept.folders[0])}</code></div>` : "") +
      `<div>${saves.rules ? "Achievements are read from these saves as you play."
        : g.total ? "No save rules for this game yet. What changed shows what the game wrote, and any value can become a rule."
          : "This game has no achievements: its saves are kept, and its play time counted."}</div>` +
      `<div class="row">` +
      (kept.folders.length ? `<button type="button" data-savekeep="1">Keep a copy now</button>` : "") +
      (n ? `<button type="button" data-saverestorelist="1">Restore\u2026</button>` : "") +
      (n && g.total ? `<button type="button" data-savechanges="1">What changed</button>` : "") + `</div>` +
      `<div class="saves-more" id="savesMore"></div><div class="saves-more" id="savesSuggest"></div>`;
    box.hidden = false;
    if (g.total && n) {
      api(`v1/local/games/${gid(id)}/suggestions`).then((r) => {
        if (saveView.id !== id || !r.suggestions.length) return;
        saveView.suggestions = r.suggestions;
        $("savesSuggest").innerHTML = `<b>Rules learned from your unlocks</b>` + r.suggestions.map((sg, i) =>
          `<div class="save-row"><span><b>${esc(sg.achievement)}</b>: <code>${esc(sg.field)} ${esc(sg.op)} ${esc(JSON.stringify(sg.value))}</code>` +
          ` in ${esc(sg.file)} <span class="muted">(${esc(sg.confidence)} confidence)</span></span>` +
          `<button type="button" data-savesuggest="${i}">Use this rule</button></div>`).join("");
      }).catch(() => {});
    }
  }

  function showRestoreList() {
    const k = saveView.kept;
    $("savesMore").innerHTML = `<b>Put saves back</b><div class="muted">What is there now is kept first. Close the game before.</div>` +
      k.snapshots.map((sn) => `<div class="save-row"><span>${esc(fmtDate(sn.taken_at))} ` +
        `${esc(new Date(sn.taken_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }))} ` +
        `<span class="muted">${sn.files} file${sn.files === 1 ? "" : "s"}, ` +
        `${sn.size < 1024 ? sn.size + " bytes" : sn.size < 1048576 ? Math.round(sn.size / 1024) + " KB" : (sn.size / 1048576).toFixed(1) + " MB"}` +
        (sn.reason === "before-restore" ? ", taken before a restore" : sn.reason === "asked" ? ", kept by you" : "") +
        `</span></span><button type="button" data-saverestore="${esc(sn.id)}">Restore</button></div>`).join("");
  }

  async function showChanges() {
    const r = await api(`v1/local/games/${gid(saveView.id)}/changes`);
    saveView.changes = r.changes.slice().reverse();
    const options = saveView.game.achievements
      .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    $("savesMore").innerHTML = `<b>What the game wrote</b><div class="muted">Newest first. Play to the moment an achievement ` +
      `describes, then pick the value that changed and the achievement it means.</div>` +
      (saveView.changes.map((c, i) => `<div class="save-row"><span><span class="muted">${esc(since(c.at))}</span> ` +
        `<code>${esc(c.field)}</code>: ${esc(JSON.stringify(c.old))} \u2192 <b>${esc(JSON.stringify(c.new))}</b> ` +
        `<span class="muted">${esc(c.file)}</span></span><span class="save-make"><select data-rulefor="${i}">` +
        `<option value="">Make a rule for\u2026</option>${options}</select>` +
        `<button type="button" data-saverule="${i}">Add</button></span></div>`).join("") ||
        `<div class="muted">Nothing changed between the kept copies yet. Play, and look again.</div>`);
  }

  async function addSaveRule(body) {
    await api(`v1/local/games/${gid(saveView.id)}/rules`, { method: "POST", body: JSON.stringify(body) });
    toast("Rule added. It unlocks from the save the next time the game writes it.");
    renderSaves(saveView.id, saveView.game);
  }

  async function saveAction(t) {
    const id = saveView.id;
    if (t.dataset.savekeep) {
      const r = await api(`v1/local/games/${gid(id)}/keep`, { method: "POST" });
      toast(r.taken ? "A copy of the saves is kept." : "Nothing new to keep: the newest copy is the same.");
      return renderSaves(id, saveView.game);
    }
    if (t.dataset.saverestorelist) return showRestoreList();
    if (t.dataset.savechanges) return showChanges();
    if (t.dataset.saverestore) {
      const sn = saveView.kept.snapshots.find((x) => x.id === t.dataset.saverestore);
      if (!confirm(`Put back the saves from ${new Date(sn.taken_at).toLocaleString()}? ` +
        "What is there now is kept first, so this can be undone. Close the game before you do.")) return;
      const r = await api(`v1/local/games/${gid(id)}/restore`, { method: "POST", body: JSON.stringify({ snapshot: sn.id }) });
      toast(`Saves put back: ${r.written} file${r.written === 1 ? "" : "s"} written.`);
      return renderSaves(id, saveView.game);
    }
    if (t.dataset.saverule) {
      const c = saveView.changes[Number(t.dataset.saverule)];
      const achievement = document.querySelector(`select[data-rulefor="${t.dataset.saverule}"]`).value;
      if (!achievement) return toast("Pick the achievement this value means first.");
      const grows = typeof c.new === "number" && typeof c.old === "number" && c.new > c.old;
      return addSaveRule({ achievement, folder: c.folder, file: c.file, field: c.field, op: grows ? ">=" : "==",
                           value: c.new, format: c.format });
    }
    if (t.dataset.savesuggest) {
      const sg = saveView.suggestions[Number(t.dataset.savesuggest)];
      return addSaveRule({ achievement: sg.achievement_id, folder: sg.folder, file: sg.file, field: sg.field,
                           op: sg.op, value: sg.value, format: sg.format });
    }
  }

  // ---- saves on this computer: where they are kept, every game's copies -------
  const bytes = (n) => n < 1024 ? `${n} bytes` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;

  async function renderKeeper(state) {
    const s = state || await api("v1/local/saves");
    const game = (id) => library && library.games.find((x) => x.game_id === id);
    const n = s.games.length, waiting = s.waiting.length;
    const rows = s.games.map((g) => {
      const known = game(g.game_id);
      return `<div class="save-row"><span><b>${esc(known ? known.title || g.game_id : g.game_id)}</b> ` +
        `<span class="muted">${g.copies} ${g.copies === 1 ? "copy" : "copies"}, newest ${esc(since(g.newest))}, ` +
        `${bytes(g.size)}${g.present ? "" : ", no saves on disk now: they come back when the game does"}</span></span>` +
        (known ? `<button type="button" data-game="${esc(g.game_id)}">Open</button>` : "") + `</div>`;
    }).join("");
    $("keeperBody").innerHTML =
      `<label class="toggle"><input type="checkbox" data-keeper="on"${s.on ? " checked" : ""}> Keep copies of my game saves</label>` +
      `<p class="muted">A game's save folder is copied a few seconds after the game saves, never while it runs. ` +
      `Every copy is kept for good, so any of them can come back; a file that did not change is stored once. ` +
      `Copies stay on this computer.</p>` +
      `<div class="saves-found"><div class="saves-head"><b>Kept in</b></div><div><code>${esc(s.folder)}</code></div>` +
      `<div>${n} game${n === 1 ? "" : "s"}, ${bytes(s.stored)} on disk` +
      (waiting ? `; ${waiting} more found, kept after ${waiting === 1 ? "its" : "their"} next save` : "") + `</div>` +
      `<div class="row"><button type="button" data-keeper="open">Open folder</button></div>` +
      `<div class="row keeper-move"><input id="keeperFolder" type="text" spellcheck="false" ` +
      `placeholder="Another folder, like D:\\Game saves" aria-label="Folder to keep saves in">` +
      `<button type="button" data-keeper="move">Move here</button>` +
      (s.default ? "" : `<button type="button" data-keeper="default">Back to the profile folder</button>`) + `</div>` +
      `<div>To have them on another PC as well, move them into a folder OneDrive, Dropbox or Google Drive syncs, ` +
      `and choose that same folder here on the other PC.</div></div>` +
      `<div class="saves-found"><div class="saves-head"><b>Games</b></div>` +
      (rows ? `<div class="saves-more">${rows}</div>` : `<div>Nothing kept yet. Play a game and its saves appear here.</div>`) + `</div>` +
      `<div class="saves-found"><div class="saves-head"><b>How saves come back</b></div>` +
      `<div>Reinstalled a game: once its program is back and its save folder is empty, the newest copy is put back before you play.</div>` +
      `<div>Played it before that and it made a new save: when you close the game, that new save is kept too, then your progress is put back.</div>` +
      `<div>On a new PC, choose the same synced folder; saves kept under another Windows user name go to yours.</div>` +
      `<div>Any time: open the game, then Restore. Whatever is there is kept first, so a restore can be undone.</div>` +
      `<div>A save you delete while the game stays installed is not brought back.</div></div>`;
  }

  async function keeperAction(t) {
    const what = t.dataset.keeper;
    if (what === "open") return api("v1/local/saves/open", { method: "POST" });
    if (what === "move" || what === "default") {
      const folder = what === "default" ? "" : $("keeperFolder").value.trim();
      if (what === "move" && !folder) return toast("Type the folder to keep saves in first.");
      if (!confirm(what === "default" ? "Move the kept saves back into the profile folder?"
        : `Move the kept saves to ${folder}? They are copied and checked before the old copies are removed.`)) return;
      t.disabled = true;
      try {
        const s = await api("v1/local/saves", { method: "POST", body: JSON.stringify({ folder }) });
        toast("Saves are kept in the new folder.");
        return renderKeeper(s);
      } finally { t.disabled = false; }
    }
  }

  // ---- dashboard: now playing, recent unlocks, kept live -----------------------
  let pulseStamp = null;
  let nowPlaying = [];
  let fresh = new Set();
  const SOURCE = { steam: "Steam", "steam-local": "Steam", psn: "PlayStation", xbox: "Xbox", gog: "GOG", "save-file": "Save file", retroachievements: "RA",
                   executable: "Play", "steam-emulator": "Game", test: "Test" };

  function since(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  function unlockedKeys(lib) {
    const keys = new Set();
    for (const g of lib.games) {
      for (const a of g.achievements) if (a.unlocked_at) keys.add(a.key || a.id);
      if (g.total > 0 && g.unlocked === g.total) keys.add("platinum:" + g.game_id);
    }
    return keys;
  }

  function renderDashboard() {
    if (mode !== "local" || !library) return;
    $("dash").hidden = false;
    const byId = new Map(library.games.map((g) => [g.game_id, g]));
    $("nowPlaying").innerHTML = nowPlaying.length ? nowPlaying.map((p) => {
      const g = byId.get(p.game_id) || { title: p.title, unlocked: 0, total: 0 };
      const pct = g.total ? Math.round(100 * g.unlocked / g.total) : 0;
      return `<button type="button" class="now" data-game="${esc(p.game_id)}"><span class="now-body">` +
        `<b>${esc(g.title || p.title)}</b><small>${g.total ? `${g.unlocked}/${g.total} achievements` : "playing"}</small>` +
        `<span class="bar"><i style="width:${pct}%"></i></span></span></button>`;
    }).join("") : `<p class="dash-empty">Nothing running. Start a game and it shows up here.</p>`;
    const recent = [];
    for (const g of library.games) {
      let last = "";
      for (const a of g.achievements) if (a.unlocked_at) { recent.push({ a, g }); if (a.unlocked_at > last) last = a.unlocked_at; }
      // A finished game earns its Platinum with its last unlock.
      if (g.total > 0 && g.unlocked === g.total && last) recent.push({ g, plat: last });
    }
    const when = (x) => x.plat || x.a.unlocked_at;
    recent.sort((x, y) => (when(y) > when(x) ? 1 : when(y) < when(x) ? -1 : (y.plat ? 1 : 0) - (x.plat ? 1 : 0)));
    $("recentUnlocks").innerHTML = recent.slice(0, 8).map(({ a, g, plat }) => {
      if (plat) {
        return `<li data-game="${esc(g.game_id)}" class="plat${fresh.has("platinum:" + g.game_id) ? " fresh" : ""}">` +
          `<span class="aname">Platinum</span><span class="gname">${esc(g.title)}</span>` +
          `<span class="src">All ${g.total}</span><time datetime="${esc(plat)}">${esc(since(plat))}</time></li>`;
      }
      const src = (a.sources || [a.source]).map((s) => SOURCE[s] || s).filter(Boolean)[0] || "";
      return `<li data-game="${esc(g.game_id)}" class="${fresh.has(a.key || a.id) ? "fresh" : ""}">` +
        `<span class="aname">${esc(a.name)}</span><span class="gname">${esc(g.title)}</span>` +
        (src ? `<span class="src">${esc(src)}</span>` : "") +
        `<time datetime="${esc(a.unlocked_at)}">${esc(since(a.unlocked_at))}</time></li>`;
    }).join("") || `<li class="dash-empty">No unlocks yet. They appear here the moment a game records one.</li>`;
  }

  // Account syncs run in the background watcher; this bar only shows them, so
  // closing a dialog never stops one. A click opens that account's dialog.
  function renderSyncs(list) {
    const bar = $("syncBar");
    bar.hidden = !list.length;
    bar.innerHTML = list.map((s) => {
      const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
      const text = s.running
        ? `${esc(s.name)}: bringing in ${s.done} of ${s.total || "?"} games` + (s.current ? ` \u00b7 ${esc(s.current)}` : "")
        : `${esc(s.name)}: ${esc(s.error)}`;
      return `<button type="button" class="sync-item${s.running ? "" : " warn"}" data-act="${esc(s.key)}">` +
        `<span class="sync-text">${text}</span>` +
        (s.running ? `<span class="bar"><i style="width:${pct}%"></i></span>` : "") + `</button>`;
    }).join("") + (list.some((s) => s.running) ? `<span class="sync-note">Runs in the background. Browse freely.</span>` : "");
  }

  let appVersion = null, updating = false, updateFailed = false, syncConnected = false;
  let appControls = {};                         // Start at login and Quit, where the app has no tray (Linux)
  async function pulse() {
    try {
      const p = await api("v1/local/pulse");
      if (appVersion && p.version && p.version !== appVersion) { location.reload(); return; }   // updated: new page
      appVersion = appVersion || p.version;
      // Found by the startup check: show the bar once. Never redraw it over an
      // install attempt or its error, which must stay readable.
      if (p.update && p.update.available && !updating && !updateFailed && $("updateNote").hidden) showUpdate(p.update);
      if (updating) {
        const u = await api("v1/local/update").catch(() => ({}));
        if (u.progress && u.progress.phase === "error") {
          updating = false; updateFailed = true;
          $("updateNote").textContent = `The update did not install: ${u.progress.error}`;
        }
      }
      $("liveDot").classList.toggle("off", !p.watching);
      const playingChanged = JSON.stringify(p.playing) !== JSON.stringify(nowPlaying);
      nowPlaying = p.playing;
      if (pulseStamp !== null && p.stamp !== pulseStamp) {
        const before = library ? unlockedKeys(library) : new Set();
        pulseStamp = p.stamp;
        await loadLibrary();
        fresh = new Set([...unlockedKeys(library)].filter((k) => !before.has(k)));
        renderDashboard();
        return;
      }
      pulseStamp = p.stamp;
      renderSyncs(p.syncs || []);
      if (playingChanged) renderDashboard();
      else document.querySelectorAll("#recentUnlocks time").forEach((t) => { t.textContent = since(t.dateTime); });
    } catch (e) { $("liveDot").classList.add("off"); }
  }

  function startPulse() {
    if (mode !== "local" || startPulse.on) return;
    startPulse.on = true;
    pulse();
    setInterval(() => { if (!document.hidden) pulse(); }, 3000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) pulse(); });
  }

  // ---- Xbox ----------------------------------------------------------------------
  async function renderXbox() {
    const s = await api("v1/local/xbox");
    let body;
    if (!s.client_id) {
      body = `<div class="steam-step"><h3>Step 1: this app's Microsoft registration</h3>` +
        `<p>Xbox signs you in on Microsoft's own page. Microsoft needs the app registered once, free, in the ` +
        `<a href="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noreferrer noopener">Azure portal</a>: ` +
        `New registration, "Personal Microsoft accounts only", redirect URI (Public client/native) ` +
        `<code>http://localhost:8788/v1/local/xbox/return</code>, then Authentication, "Allow public client flows": Yes.</p>` +
        `<label>Application (client) ID <input id="xboxClient" autocomplete="off" spellcheck="false" placeholder="1a2b3c4d-..."></label>` +
        `<div class="row"><button type="button" class="go" id="xboxSaveClient">Save</button></div></div>`;
    } else if (!s.linked) {
      body = `<div class="steam-step"><h3>Step 2: sign in with Microsoft</h3>` +
        `<p>You sign in on Microsoft's page; this app never sees your password. It asks only to read your Xbox profile ` +
        `and achievements, and keeps the session in Windows Credential Manager on this PC.</p>` +
        `<div class="row"><a class="button" href="v1/local/xbox/signin">Sign in with Microsoft</a></div></div>`;
    } else {
      const y = s.sync || {};
      let state;
      if (!s.has_session) state = `<p class="warn">The saved session is missing. Disconnect and sign in again.</p>`;
      else if (y.error) state = `<p class="warn">${esc(y.error)}</p>`;
      else if (y.running) state = `<p>Bringing in your achievements\u2026 ${y.done || 0} of ${y.total || "?"} games` +
        (y.current ? ` (${esc(y.current)})` : "") + `. It goes gently, about one game every ten seconds. You can close this: it keeps going.</p>`;
      else if (y.last_sync) state = `<p class="ok">Up to date (${esc(y.titles || 0)} games with achievements). New ones arrive within minutes.</p>`;
      else state = `<p>Starting the first sync\u2026</p>`;
      body = `<div class="steam-step"><h3>Signed in: ${esc(s.gamertag)}</h3>${state}</div>` +
        `<div class="row"><button type="button" id="xboxDisconnect">Disconnect</button></div>`;
    }
    $("xboxBody").innerHTML = body;
    clearInterval(renderXbox.timer);
    if (s.linked && $("xboxDialog").open) renderXbox.timer = setInterval(() => { if ($("xboxDialog").open) renderXbox().catch(() => {}); }, 4000);
  }

  function showUpdate(u) {
    if (u.current) $("footVersion").textContent = u.current;
    if (!u.available || $("installUpdate")) return;
    const note = $("updateNote");
    note.textContent = `Greycell Achievements ${u.available.version} is out (you have ${u.current}). `;
    if (u.installable) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "pill"; b.id = "installUpdate"; b.textContent = "Install update";
      note.append(b, " It downloads, installs and restarts by itself. Your achievements stay where they are.");
    } else if (u.download) {
      const a = document.createElement("a");
      a.href = u.download; a.target = "_blank"; a.rel = "noreferrer noopener";
      a.textContent = "Download it here";
      note.append(a, ". Your achievements stay where they are.");
    } else {
      note.append("Close the app and start it again to update.");
    }
    note.hidden = false;
  }

  // ---- Settings menu: Check for updates, About ----------------------------------------
  function closeMenus() {
    for (const m of document.querySelectorAll(".menu")) m.hidden = true;
    for (const b of document.querySelectorAll('[aria-haspopup="menu"]')) b.setAttribute("aria-expanded", "false");
  }
  async function checkForUpdates() {
    toast("Checking for updates\u2026");
    const u = await api("v1/local/update?force=true").catch(() => null);
    const busy = u && u.progress && ["downloading", "installing"].includes(u.progress.phase);
    if (updating || busy) return toast("The update is already on its way; the app restarts by itself.");
    if (!u || u.reached === false) return toast("Could not check for updates. Try again later.");
    if (u.available) {
      updateFailed = false;                      // asked again: offer it again
      if (!$("installUpdate")) $("updateNote").hidden = true;
      showUpdate(u);
      return toast(u.installable ? `Version ${u.available.version} is available: Install update is at the top.`
        : `Version ${u.available.version} is available: see the note at the top.`);
    }
    toast(`You have the latest version (${u.current}).`);
  }
  async function openAbout() {
    $("aboutDialog").showModal();
    const u = await api("v1/local/update").catch(() => ({}));
    $("aboutVersion").textContent = u.current ? `Version ${u.current}` : "Version unknown";
  }

  // ---- RetroAchievements -----------------------------------------------------------
  async function renderRa() {
    const s = await api("v1/local/retroachievements");
    let body;
    if (!s.linked) {
      body = `<div class="steam-step"><h3>Your username and Web API key</h3>` +
        `<p>RetroAchievements has an official API: each player gets a personal Web API key. ` +
        `The key is kept only in Windows Credential Manager on this PC; your password is never asked for.</p>` +
        `<ol class="psn-steps"><li>Sign in on <a href="https://retroachievements.org/" target="_blank" rel="noreferrer noopener">retroachievements.org</a>.</li>` +
        `<li>Open <a href="${esc(s.key_page)}" target="_blank" rel="noreferrer noopener">Settings</a> and copy your <code>Web API Key</code>.</li>` +
        `<li>Paste it here with your username.</li></ol>` +
        `<label>Username <input id="raUser" autocomplete="off" spellcheck="false" placeholder="RetroAchievements username"></label>` +
        `<label>Web API key <input id="raKey" type="password" autocomplete="off" spellcheck="false" placeholder="Web API key"></label>` +
        `<div class="row"><button type="button" class="go" id="raConnect">Connect</button></div></div>`;
    } else {
      const y = s.sync || {};
      let state;
      if (!s.has_key) state = `<p class="warn">The saved key is missing. Disconnect and connect again.</p>`;
      else if (y.error) state = `<p class="warn">${esc(y.error)}</p>`;
      else if (y.running) state = `<p>Bringing in your achievements\u2026 ${y.done || 0} of ${y.total || "?"} games` +
        (y.current ? ` (${esc(y.current)})` : "") + `. You can close this: it keeps going.</p>`;
      else if (y.last_sync) state = `<p class="ok">Up to date (${esc(y.titles || 0)} games). New unlocks arrive within about two minutes.</p>`;
      else state = `<p>Starting the first sync\u2026</p>`;
      body = `<div class="steam-step"><h3>Connected: <a href="${esc(s.profile_url)}" target="_blank" rel="noreferrer noopener">${esc(s.username)}</a></h3>${state}` +
        `<p class="muted">Softcore and hardcore unlocks are kept apart, with RetroAchievements points.</p></div>` +
        `<div class="row"><button type="button" id="raDisconnect">Disconnect</button></div>`;
    }
    $("raBody").innerHTML = body;
    clearInterval(renderRa.timer);
    if (s.linked && $("raDialog").open) renderRa.timer = setInterval(() => { if ($("raDialog").open) renderRa().catch(() => {}); }, 4000);
  }

  // ---- GOG -----------------------------------------------------------------------
  async function renderGog() {
    const s = await api("v1/local/gog");
    let body;
    if (!s.linked) {
      body = `<div class="steam-step"><h3>Your GOG username</h3>` +
        `<p>GOG lets no other app sign you in, so this reads your public GOG profile: your games, and the ` +
        `achievements on each game's profile page. Nothing to sign in to, nothing secret kept. It is unofficial: ` +
        `GOG could change these pages.</p>` +
        `<ol class="psn-steps"><li>On <a href="https://www.gog.com/account/settings/privacy" target="_blank" rel="noreferrer noopener">gog.com, Settings, Privacy</a>, ` +
        `make your profile visible to everyone.</li>` +
        `<li>Type your username, or paste your profile link (<code>gog.com/u/…</code>).</li></ol>` +
        `<label>Username <input id="gogUser" autocomplete="off" spellcheck="false" placeholder="GOG username"></label>` +
        `<div class="row"><button type="button" class="go" id="gogConnect">Connect</button></div></div>`;
    } else {
      const y = s.sync || {};
      let state;
      if (y.error) state = `<p class="warn">${esc(y.error)}</p>`;
      else if (y.running) state = `<p>Bringing in your achievements\u2026 ${y.done || 0} of ${y.total || "?"} games` +
        (y.current ? ` (${esc(y.current)})` : "") + `. It goes gently, one game every ten seconds. You can close this: it keeps going.</p>`;
      else if (y.last_sync) state = `<p class="ok">Up to date (${esc(y.titles || 0)} games with achievements). ` +
        `GOG updates a profile after GOG Galaxy syncs, so new ones arrive within minutes of that.</p>`;
      else state = `<p>Starting the first sync\u2026</p>`;
      body = `<div class="steam-step"><h3>Connected: <a href="${esc(s.profile_url)}" target="_blank" rel="noreferrer noopener">${esc(s.username)}</a></h3>${state}</div>` +
        `<div class="row"><button type="button" id="gogDisconnect">Disconnect</button></div>`;
    }
    $("gogBody").innerHTML = body;
    clearInterval(renderGog.timer);
    if (s.linked && $("gogDialog").open) renderGog.timer = setInterval(() => { if ($("gogDialog").open) renderGog().catch(() => {}); }, 4000);
  }

  // ---- PlayStation ---------------------------------------------------------------
  async function renderPsn() {
    const s = await api("v1/local/psn");
    let body;
    if (!s.linked) {
      body = `<div class="steam-step"><h3>Connect with a sign-in code</h3>` +
        `<p>Sony has no public way for apps to sign you in, so this uses the code its own website uses. ` +
        `It is unofficial: Sony could change it. Your password is never asked for, and the session is kept only ` +
        `in Windows Credential Manager on this PC.</p>` +
        `<ol class="psn-steps"><li>Sign in on <a href="https://www.playstation.com/" target="_blank" rel="noreferrer noopener">playstation.com</a>. ` +
        `Safest: use a spare PSN account, not your main one.</li>` +
        `<li>Open <a href="${esc(s.npsso_page)}" target="_blank" rel="noreferrer noopener">this Sony page</a> and copy the ` +
        `<code>npsso</code> value (64 letters and digits).</li>` +
        `<li>Paste it here. With a spare account, also give your main PSN ID (your trophies must be visible to anyone).</li></ol>` +
        `<label>Code <input id="psnCode" type="password" autocomplete="off" spellcheck="false" placeholder="npsso code"></label>` +
        `<label>PSN ID whose trophies to bring in <input id="psnId" autocomplete="off" spellcheck="false" placeholder="leave empty for the signed-in account"></label>` +
        `<div class="row"><button type="button" class="go" id="psnConnect">Connect</button></div></div>`;
    } else {
      const y = s.sync || {};
      let state;
      if (!s.has_session) state = `<p class="warn">The saved session is missing. Disconnect and connect again.</p>`;
      else if (y.error) state = `<p class="warn">${esc(y.error)}</p>`;
      else if (y.running) state = `<p>Bringing in your trophies\u2026 ${y.done || 0} of ${y.total || "?"} games` +
        (y.current ? ` (${esc(y.current)})` : "") + `. It goes gently, about one game every ten seconds. You can close this: it keeps going.</p>`;
      else if (y.last_sync) state = `<p class="ok">Up to date (${esc(y.titles || 0)} games with trophies). New trophies arrive within minutes.</p>`;
      else state = `<p>Starting the first sync\u2026</p>`;
      body = `<div class="steam-step"><h3>Connected: ${esc(s.online_id)}</h3>${state}` +
        (s.own_account ? "" : `<p class="muted">Read with another account's session, as you chose.</p>`) +
        `<p class="muted">The connection lasts about two months; then paste a fresh code.</p></div>` +
        `<div class="row"><button type="button" id="psnDisconnect">Disconnect</button></div>`;
    }
    $("psnBody").innerHTML = body;
    clearInterval(renderPsn.timer);
    if (s.linked && $("psnDialog").open) renderPsn.timer = setInterval(() => { if ($("psnDialog").open) renderPsn().catch(() => {}); }, 4000);
  }

  // ---- privacy: what goes online without being asked ------------------------------
  const PRIVACY = [
    ["updates", "Check for new versions", "Looks at greycell.app when the app starts and once a day. Off: only when you choose Check for updates."],
    ["pictures", "Achievement pictures and game banners", "Fetched once each from the platforms' image hosts, then kept on this computer."],
    ["rarity", "How rare your Steam achievements are", "Asks Steam for each Steam game's unlock percentages, a few seconds apart."],
    ["stats", "Share anonymous stats with greycell.app", "Once an hour: how many achievements unlocked and which, games completed or added. Never who you are; your library stays here."],
  ];
  async function firstRunNotice() {                 // said once, the first time the app opens (owner, 2026-10-03)
    const s = await api("v1/local/privacy").catch(() => null);
    if (!s || !s.notice) return;
    $("noticeDialog").showModal();
    api("v1/local/privacy/notice", { method: "POST" }).catch(() => {});
  }
  async function renderPrivacy() {
    const s = await api("v1/local/privacy");
    $("privacyBody").innerHTML = PRIVACY.map(([key, label, hint]) =>
      `<label class="toggle"><input type="checkbox" data-privacy="${key}"${s[key] ? " checked" : ""}> ${esc(label)}</label>` +
      `<p class="muted">${esc(hint)}</p>`).join("");
  }

  // ---- popups and sounds --------------------------------------------------------
  async function renderSounds() {
    const s = await api("v1/local/notify");
    const toggle = (key, label, hint) => `<label class="toggle"><input type="checkbox" data-setting="${key}"` +
      `${s[key] ? " checked" : ""}> ${esc(label)}</label>` + (hint ? `<p class="muted">${esc(hint)}</p>` : "");
    const group = (kind, key, title) => `<fieldset class="sound-group"><legend>${esc(title)}</legend>` +
      s.choices[kind].map((c) => {
        const custom = c.id === "custom";
        const has = !custom || (s.custom && s.custom[kind]);
        return `<div class="sound-row"><label><input type="radio" name="${key}" value="${esc(c.id)}"` +
          `${s[key] === c.id ? " checked" : ""}${has ? "" : " disabled"} data-setting="${key}"> ${esc(c.name)}</label>` +
          `<span class="sound-actions">` +
          (custom ? `<label class="pill pick">Choose file<input type="file" accept=".wav,audio/wav" data-custom="${kind}" hidden></label>` : "") +
          (has ? `<button type="button" class="pill" data-preview="${esc(c.id)}" data-kind="${kind}">\u25B6 Play</button>` : "") +
          `</span></div>`;
      }).join("") +
      `</fieldset>`;
    $("soundBody").innerHTML =
      toggle("enabled", "Show a popup when I unlock something") +
      toggle("sound", "Play a sound with it") +
      toggle("steam", "Popups for Steam, Xbox, GOG and RetroAchievements too",
        "Off by default: they show their own popups. Xbox PC games pop a minute or two late. PlayStation trophies are earned on the console, so they never pop up here.") +
      `<label class="rare-below">Rare means fewer than <select data-setting="rare_below">` +
        (s.rare_choices || [1, 5, 10]).map((v) => `<option value="${v}"${Number(s.rare_below) === v ? " selected" : ""}>${v}%</option>`).join("") +
        `</select> of Steam players have it.</label>` +
      `<div class="sound-groups">${group("unlock", "unlock_sound", "Unlock sound")}` +
        `${group("rare", "rare_sound", "Rare achievement sound")}${group("platinum", "platinum_sound", "Platinum sound")}</div>`;
  }

  document.addEventListener("change", async (e) => {
    const kind = e.target.dataset && e.target.dataset.custom;
    if (kind && e.target.files && e.target.files[0]) {
      try {
        const res = await fetch(`v1/local/notify/custom/${kind}`, { method: "POST",
          headers: { "X-OA-Token": localToken, "Content-Type": "audio/wav" }, body: e.target.files[0] });
        if (!res.ok) throw new Error(((await res.json()).detail || {}).message || "That file could not be used");
        toast("Your sound is set");
        api("v1/local/notify/preview", { method: "POST", body: JSON.stringify({ kind, name: "custom" }) }).catch(() => {});
      } catch (err) { toast(err.message); }
      return renderSounds();
    }
    const key = e.target.dataset && e.target.dataset.setting;
    if (!key) return;
    const value = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    try {
      await api("v1/local/notify", { method: "POST", body: JSON.stringify({ [key]: value }) });
      if (e.target.type === "radio") {
        api("v1/local/notify/preview", { method: "POST",
          body: JSON.stringify({ kind: { platinum_sound: "platinum", rare_sound: "rare" }[key] || "unlock", name: value }) }).catch(() => {});
      }
    } catch (err) { toast(err.message); renderSounds(); }
  });

  async function loadLibrary() {
    library = await api("v1/library");
    renderSummary(library);
    renderGames();
    renderDashboard();
    startPulse();
    show("library");
    const open = $("gameDialog").open && $("gameDialog").dataset.game;
    if (open) openGame(open);
  }

  async function renderAccount() {
    const me = await api("v1/me");
    const devices = (await api("v1/devices")).devices;
    const origin = BASE_URL.replace(/\/$/, "");               // the server address, with its path
    $("account").innerHTML =
      `<div class="panel"><h2>${esc(me.username)}</h2>` +
      `<p class="muted">${me.event_count} events stored on this server. Your own computer keeps the full copy.</p></div>` +
      `<div class="panel"><h2>Connect a computer</h2>` +
      `<p class="muted">Install the Greycell Achievements client on it, then run:</p>` +
      `<p><code>openachievements server connect ${esc(origin)} ${esc(me.username)}</code><br><code>openachievements sync</code></p>` +
      `<p class="muted">On a new computer, <code>openachievements profile restore ${esc(origin)} ${esc(me.username)}</code> downloads everything.</p></div>` +
      `<div class="panel"><h2>Privacy</h2>` +
      `<label class="toggle"><input type="checkbox" id="publicToggle"${me.privacy.public ? " checked" : ""}> Public profile</label>` +
      `<p class="muted">Off by default. When on, anyone with the link can see your library: ` +
      `<a href="u/${encodeURIComponent(me.username)}">${esc(BASE_URL)}u/${esc(me.username)}</a></p></div>` +
      `<div class="panel"><h2>Devices</h2><ul class="devices">` +
      devices.map((d) => `<li><span>${esc(d.device_name || d.kind)}<br><span class="muted">${d.revoked_at ? "signed out" : "last seen " + esc(fmtDate(d.last_used_at || d.created_at))}</span></span>` +
        (d.device_id && !d.revoked_at ? `<button type="button" data-revoke="${esc(d.device_id)}">Sign out</button>` : "") + `</li>`).join("") +
      `</ul></div>` +
      `<div class="panel"><h2>Your data</h2><div class="row">` +
      `<button type="button" id="exportBtn">Download all events (.jsonl)</button>` +
      `<button type="button" class="danger" id="deleteBtn">Delete account from this server</button></div>` +
      `<p class="muted">Deleting removes what this server holds. Your local profile folder is not touched.</p></div>`;
    show("account");
  }

  async function renderSiteStats(info) {
    const s = await fetch("v1/stats").then((r) => r.ok ? r.json() : Promise.reject(r));
    const n = (x) => Number(x || 0).toLocaleString();
    $("siteStats").innerHTML =
      `<div class="stats-row"><div><b>${n(s.accounts)}</b><span>players</span></div>` +
      `<div><b>${n(s.unlocks)}</b><span>achievements unlocked</span></div>` +
      `<div><b>${n(s.games)}</b><span>games tracked</span></div></div>` +
      (s.top.length ? `<h2>Public profiles</h2><ol class="leaders">` + s.top.map((p) =>
        `<li><a href="u/${encodeURIComponent(p.username)}">${esc(p.username)}</a><span>${n(p.unlocks)}</span></li>`).join("") + `</ol>` : "") +
      `<p class="muted">The library lives in the app on your computer and works offline; an account here is a copy you can sync, share and restore.` +
      (info.source_url ? ` <a href="${esc(info.source_url)}" rel="noopener">Get the app</a>.` : "") + `</p>`;
    $("siteStats").hidden = false;
  }

  async function publicView(name) {
    mode = "public";
    renderTop();
    try {
      const data = await fetch("v1/public/" + encodeURIComponent(name)).then((r) => r.ok ? r.json() : Promise.reject(r));
      library = { games: data.games || [], totals: data.totals, accounts: [] };
      renderSummary(library);
      renderGames();
      document.title = name + " on Greycell Achievements";
      show("library");
    } catch (e) {
      $("empty").hidden = false; $("empty").textContent = "No public profile by that name."; show("library");
    }
  }

  // ---- Steam ---------------------------------------------------------------------

  async function renderSteam() {
    const st = await api("v1/local/steam");
    steamAccount = st.account; links.steam = st.account || null; steamLinked = st.linked; renderTop();
    let body;
    if (!st.account) {
      body = `<div class="steam-step"><h3>Sign in through Steam</h3>` +
        `<p>You sign in on Steam's own page; Greycell Achievements only learns which account is yours: no password, no key. ` +
        `Then every game that account plays on this PC comes in, with its achievements, as Steam records them.</p>` +
        `<div class="row"><a class="button" href="v1/local/steam/signin">Sign in through Steam</a></div></div>`;
    } else {
      const pc = st.linked ? `<p class="ok">Following Steam on this PC: every game this account has played here, and new achievements as Steam records them.</p>`
        : `<p>This account has not played on this PC, or Steam is not installed here. Its games come in once it plays here.</p>` +
          `<div class="row"><button type="button" id="steamFiles">Look again</button></div>`;
      body = `<div class="steam-step"><h3>Signed in as Steam account ${esc(st.account)}</h3>${pc}` +
        `<p class="muted">Steam only shows a player's whole library, including games never played, to an API key; ` +
        `Greycell Achievements does not use one, so games you own but never played on this PC are not listed.</p></div>` +
        `<div class="row"><button type="button" id="steamForget">Forget this account</button></div>`;
    }
    $("steamBody").innerHTML = body;
  }

  // ---- auth --------------------------------------------------------------------

  function setAuthTab(tab) {
    authTab = tab;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    $("authSubmit").textContent = tab === "login" ? "Sign in" : "Create account";
    $("pwHint").hidden = tab === "login";
    $("authForm").password.minLength = tab === "login" ? 1 : 10;
    $("authForm").password.autocomplete = tab === "login" ? "current-password" : "new-password";
    $("authError").textContent = "";
  }

  async function submitAuth(e) {
    e.preventDefault();
    const f = e.target;
    const body = { username: f.username.value.trim(), password: f.password.value };
    $("authSubmit").disabled = true; $("authError").textContent = "";
    try {
      if (authTab === "register") await api("v1/accounts", { method: "POST", body: JSON.stringify(body) });
      const s = await api("v1/sessions", { method: "POST", body: JSON.stringify({ ...body, device_name: "Web browser" }) });
      token = s.token; store.set("oa_token", token);
      f.password.value = "";
      renderTop(); await loadLibrary();
    } catch (err) {
      $("authError").textContent = err.message;
    } finally { $("authSubmit").disabled = false; }
  }

  function signOutLocally() {
    token = null; store.set("oa_token", null); library = null;
    renderTop(); show("intro");
  }

  // ---- events ------------------------------------------------------------------

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button, [data-game], [data-reveal]");
    if (!t) return;
    try {
      if (t.dataset.game) return openGame(t.dataset.game);
      if (Object.keys(t.dataset).some((k) => k.startsWith("save"))) return await saveAction(t);
      if (t.dataset.tab) return setAuthTab(t.dataset.tab);
      if (t.dataset.act === "library") return loadLibrary();
      if (t.dataset.act === "steam") { $("steamDialog").showModal(); return renderSteam(); }
      if (t.id === "closeSteam") return $("steamDialog").close();
      if (t.dataset.act === "sounds") { $("soundDialog").showModal(); return renderSounds(); }
      if (t.dataset.act === "privacy") { $("privacyDialog").showModal(); return renderPrivacy(); }
      if (t.dataset.act === "keeper") { closeMenus(); $("keeperDialog").showModal(); return renderKeeper(); }
      if (t.dataset.keeper) return await keeperAction(t);
      if (t.id === "closeKeeper") return $("keeperDialog").close();
      if (t.id === "closePrivacy") return $("privacyDialog").close();
      if (t.id === "noticeOk") return $("noticeDialog").close();
      if (t.id === "noticeOff") {
        await api("v1/local/privacy", { method: "POST", body: JSON.stringify({ stats: false }) }).catch((err) => toast(err.message));
        $("noticeDialog").close();
        return toast("Anonymous stats are off. Turn them back on in Settings, Privacy.");
      }
      if (t.id === "closeSound") return $("soundDialog").close();
      if (t.dataset.act === "links" || t.dataset.act === "settings") {
        const menu = $(t.dataset.act + "Menu"), open = menu.hidden;
        closeMenus();
        menu.hidden = !open; t.setAttribute("aria-expanded", String(open));
        if (open) { const first = menu.querySelector("button"); if (first) first.focus(); }
        return;
      }
      if (t.closest && t.closest(".menu")) closeMenus();
      if (t.dataset.act === "checkupdates") return checkForUpdates();
      if (t.dataset.act === "about") return openAbout();
      if (t.dataset.act === "autostart") {
        const r = await api("v1/local/app/autostart", { method: "POST", body: JSON.stringify({ on: !appControls.autostart }) });
        appControls.autostart = r.autostart; renderTop();
        return toast(r.autostart ? "Greycell Achievements starts when you log in." : "It no longer starts when you log in.");
      }
      if (t.dataset.act === "quitapp") {
        if (!confirm("Quit Greycell Achievements? Popups stop until you open it again.")) return;
        await api("v1/local/app/quit", { method: "POST" });
        document.body.innerHTML = `<main class="wrap"><h1>Greycell Achievements has quit</h1>` +
          `<p class="muted">Open it again from your app menu to see your library.</p></main>`;
        return;
      }
      if (t.dataset.act === "ra") { $("raDialog").showModal(); return renderRa(); }
      if (t.dataset.act === "gog") { $("gogDialog").showModal(); return renderGog(); }
      if (t.dataset.act === "psn") { $("psnDialog").showModal(); return renderPsn(); }
      if (t.dataset.act === "xbox") { $("xboxDialog").showModal(); return renderXbox(); }
      if (t.id === "closeXbox") { clearInterval(renderXbox.timer); return $("xboxDialog").close(); }
      if (t.id === "xboxSaveClient") {
        try {
          await api("v1/local/xbox/client", { method: "POST", body: JSON.stringify({ client_id: $("xboxClient").value }) });
        } catch (err) { toast(err.message); }
        return renderXbox();
      }
      if (t.id === "xboxDisconnect") {
        if (!confirm("Disconnect Xbox? Games and achievements already here stay; the saved session is deleted.")) return;
        await api("v1/local/xbox/disconnect", { method: "POST" }); return renderXbox();
      }
      if (t.id === "closeAbout") return $("aboutDialog").close();
      if (t.id === "installUpdate") {
        t.disabled = true; t.textContent = "Downloading\u2026";
        try {
          await api("v1/local/update/install", { method: "POST" });
          $("updateNote").textContent = appControls.quit       // Linux: the app replaces itself, no installer window
            ? "Downloading the update. The app restarts by itself, and this page reloads when it is back."
            : "Downloading the update. The installer opens in a moment, the app restarts " +
              "by itself, and this page reloads when it is back.";
          updating = true;
        } catch (err) { t.disabled = false; t.textContent = "Install update"; toast(err.message); }
        return;
      }
      if (t.id === "closeRa") { clearInterval(renderRa.timer); return $("raDialog").close(); }
      if (t.id === "raConnect") {
        t.disabled = true; t.textContent = "Connecting\u2026";
        try {
          await api("v1/local/retroachievements/connect", { method: "POST", body: JSON.stringify({
            username: $("raUser").value, api_key: $("raKey").value }) });
          toast("Connected to RetroAchievements. Your achievements come in over the next minutes.");
        } catch (err) { toast(err.message); }
        return renderRa();
      }
      if (t.id === "raDisconnect") {
        await api("v1/local/retroachievements/disconnect", { method: "POST" }); return renderRa();
      }
      if (t.id === "closeGog") { clearInterval(renderGog.timer); return $("gogDialog").close(); }
      if (t.id === "gogConnect") {
        t.disabled = true; t.textContent = "Connecting\u2026";
        try {
          await api("v1/local/gog/connect", { method: "POST", body: JSON.stringify({ username: $("gogUser").value }) });
          toast("Connected to GOG. Your achievements come in over the next minutes.");
        } catch (err) { toast(err.message); }
        return renderGog();
      }
      if (t.id === "gogDisconnect") {
        await api("v1/local/gog/disconnect", { method: "POST" }); return renderGog();
      }
      if (t.id === "closePsn") { clearInterval(renderPsn.timer); return $("psnDialog").close(); }
      if (t.id === "psnConnect") {
        t.disabled = true; t.textContent = "Connecting\u2026";
        try {
          await api("v1/local/psn/connect", { method: "POST", body: JSON.stringify({
            npsso: $("psnCode").value, online_id: $("psnId").value || null }) });
          toast("Connected to PlayStation. Your trophies come in over the next minutes.");
        } catch (err) { toast(err.message); }
        return renderPsn();
      }
      if (t.id === "psnDisconnect") {
        if (!confirm("Disconnect PlayStation? Games and trophies already here stay; the saved session is deleted.")) return;
        await api("v1/local/psn/disconnect", { method: "POST" }); return renderPsn();
      }
      if (t.dataset.preview) {
        return api("v1/local/notify/preview", { method: "POST",
          body: JSON.stringify({ kind: t.dataset.kind, name: t.dataset.preview }) }).catch((err) => toast(err.message));
      }
      if (t.id === "steamFiles") {
        t.disabled = true; t.textContent = "Reading Steam\u2019s files\u2026";
        const r = await api("v1/local/steam/link", { method: "POST" });
        toast(`Following Steam on this PC: ${r.unlocks} achievements`); await loadLibrary(); return renderSteam();
      }
      if (t.id === "steamForget") {
        if (!confirm("Forget this Steam account? Games and achievements already here stay.")) return;
        await api("v1/local/steam/forget", { method: "POST" }); steamAccount = null; renderTop(); return renderSteam();
      }
      if (t.dataset.act === "add") { $("addDialog").showModal(); $("catalogSearch").focus(); return searchCatalogue(); }
      if (t.dataset.add) return addGame(t);
      if (t.dataset.reveal) {
        revealed.has(t.dataset.reveal) ? revealed.delete(t.dataset.reveal) : revealed.add(t.dataset.reveal);
        return openGame($("gameDialog").dataset.game);
      }
      if (t.dataset.fetchach) {
        t.disabled = true; t.textContent = "Asking Steam\u2026";
        try {
          const r = await api("v1/local/games/" + encodeURIComponent(t.dataset.fetchach) + "/fetch-achievements", { method: "POST" });
          toast("Achievement list added");
          if (r && r.game_id && r.game_id !== t.dataset.fetchach) $("gameDialog").close();
        } catch (e) { t.disabled = false; t.textContent = "Try again"; throw e; }
        return loadLibrary();
      }
      if (t.id === "closeAdd") return $("addDialog").close();
      if (t.dataset.act === "account") return renderAccount();
      if (t.dataset.act === "signout") { await api("v1/sessions/current", { method: "DELETE" }).catch(() => {}); return signOutLocally(); }
      if (t.dataset.act === "sync") { const r = await api("v1/local/sync", { method: "POST" }); toast(`Sent ${r.uploaded}, received ${r.downloaded}`); return loadLibrary(); }
      if (t.dataset.revoke) { await api("v1/devices/" + encodeURIComponent(t.dataset.revoke), { method: "DELETE" }); return renderAccount(); }
      if (t.id === "closeDialog") return $("gameDialog").close();
      if (t.id === "exportBtn") {
        const res = await fetch("v1/export", { headers: { Authorization: "Bearer " + token } });
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement("a"); a.href = url; a.download = "open-achievements-events.jsonl"; a.click();
        return setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      if (t.id === "deleteBtn") {
        const pw = prompt("This deletes your account and everything this server holds. Your own computer keeps its copy.\n\nType your password to confirm:");
        if (!pw) return;
        await api("v1/accounts/me", { method: "DELETE", body: JSON.stringify({ password: pw }) });
        toast("Account deleted from this server"); return signOutLocally();
      }
    } catch (err) { toast(err.message); }
  });
  document.addEventListener("change", async (e) => {
    if (e.target.dataset && e.target.dataset.keeper === "on") {
      try {
        return renderKeeper(await api("v1/local/saves", { method: "POST", body: JSON.stringify({ on: e.target.checked }) }));
      } catch (err) { toast(err.message); return renderKeeper(); }
    }
    if (e.target.dataset && e.target.dataset.privacy) {
      try {
        await api("v1/local/privacy", { method: "POST", body: JSON.stringify({ [e.target.dataset.privacy]: e.target.checked }) });
      } catch (err) { toast(err.message); }
      return renderPrivacy();
    }
    if (e.target.id === "revealHidden") {
      revealAll = e.target.checked; store.set("oa_reveal_hidden", revealAll ? "1" : null);
      revealed.clear();
      return openGame($("gameDialog").dataset.game);
    }
    if (e.target.id === "publicToggle") {
      try { await api("v1/privacy", { method: "PUT", body: JSON.stringify({ public: e.target.checked }) }); toast(e.target.checked ? "Profile is public" : "Profile is private"); }
      catch (err) { toast(err.message); e.target.checked = !e.target.checked; }
    }
    if (e.target.id === "filter") renderGames();
    if (e.target.id === "platform") { store.set("oa_platform", e.target.value || null); renderGames(); }
    if (e.target.id === "sort") { store.set("oa_sort", e.target.value); renderGames(); }
    if (e.target.id === "gameStatus") {
      const id = $("gameDialog").dataset.game;
      try {
        await api("v1/local/status", { method: "POST", body: JSON.stringify({ game_id: id, status: e.target.value }) });
        toast(e.target.value === "none" ? "Status cleared" : STATUS_NAME[e.target.value]);
        await loadLibrary();
      } catch (err) { toast(err.message); }
    }
  });
  $("search").addEventListener("input", renderGames);
  $("catalogSearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchCatalogue().catch((err) => toast(err.message)), 250);
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeMenus(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = [...document.querySelectorAll(".menu")].find((m) => !m.hidden);
    if (open) { closeMenus(); const btn = $(open.id.replace("Menu", "Btn")); if (btn) btn.focus(); }
  });
  for (const id of ["steamDialog", "psnDialog", "xboxDialog", "gogDialog", "raDialog"]) $(id).addEventListener("close", () => refreshLinks());
  $("addDialog").addEventListener("click", (e) => { if (e.target === $("addDialog")) $("addDialog").close(); });
  $("steamDialog").addEventListener("click", (e) => { if (e.target === $("steamDialog")) $("steamDialog").close(); });
  $("authForm").addEventListener("submit", submitAuth);
  $("gameDialog").addEventListener("click", (e) => { if (e.target === $("gameDialog")) $("gameDialog").close(); });
  $("achievementList").addEventListener("keydown", (e) => {
    const card = e.target.closest && e.target.closest("[data-reveal]");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); card.click(); }
  });
  if (["recent", "name", "progress"].includes(store.get("oa_sort"))) $("sort").value = store.get("oa_sort");

  // ---- start -------------------------------------------------------------------

  (async function start() {
    const here = location.pathname.startsWith(BASE_PATH) ? location.pathname.slice(BASE_PATH.length) : "";
    const personal = here.match(/^u\/([^/]+)\/?$/);
    const u = personal ? decodeURIComponent(personal[1]) : new URLSearchParams(location.search).get("u");
    if (u) return publicView(u);
    const info = await fetch("v1/mode").then((r) => r.json()).catch(() => ({ mode: "server" }));
    mode = info.mode;
    syncConnected = Boolean(info.sync);
    appControls = info.app || {};
    if (mode === "local") firstRunNotice();
    if (mode === "local") {
      $("apiLink").hidden = true;
      const st = await fetch("v1/local/steam").then((r) => r.json()).catch(() => ({}));
      steamLinked = st.linked || false; steamAccount = st.account || null;
      refreshLinks();
      const params = new URLSearchParams(location.search);
      if (params.get("xbox") || params.get("xbox_error")) {
        history.replaceState(null, "", BASE_URL);
        setTimeout(() => { $("xboxDialog").showModal(); renderXbox(); loadLibrary().catch(() => {});
          if (params.get("xbox_error")) toast(params.get("xbox_error")); }, 0);
      }
      if (params.get("steam") || params.get("steam_error")) {
        history.replaceState(null, "", BASE_URL);
        setTimeout(() => { $("steamDialog").showModal(); renderSteam(); loadLibrary().catch(() => {});
          if (params.get("steam_error")) toast(params.get("steam_error")); }, 0);
      }
    }
    if (mode === "server" && !info.registration) $("registerTab").hidden = true;
    if (mode === "local") fetch("v1/local/update").then((r) => r.json()).then(showUpdate).catch(() => {});
    renderTop();

    if (mode === "local" || token) {
      try { await loadLibrary(); } catch (e) { if (mode === "server") signOutLocally(); else toast(e.message); }
    } else {
      show("intro");
      if (mode === "server") renderSiteStats(info).catch(() => {});
    }
  })();
})();
