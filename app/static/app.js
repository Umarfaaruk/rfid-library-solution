"use strict";
const $ = (id) => document.getElementById(id);
const state = { token: sessionStorage.getItem("tok"), role: sessionStorage.getItem("role"), user: sessionStorage.getItem("user") };
const staffTabs = ["desk", "rfid", "dash", "admin"];

function toast(msg, ok = true) {
  const t = $("toast");
  t.textContent = msg; t.className = "show " + (ok ? "ok" : "bad");
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = ""), 4500);
  // Audible confirmation for handheld-style operations (FR 06): a short beep, high for success, low for a fault.
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); o.frequency.value = ok ? 880 : 220; o.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.12);
  } catch (_) { /* audio unavailable: the visible toast still shows */ }
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { method: opts.method || (opts.body ? "POST" : "GET"), headers, body: opts.body });
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    if (r.status === 401 && state.token) signOut();
    throw new Error((data && data.message) || (data && data.detail) || r.statusText);
  }
  return data;
}

function show(tab) {
  for (const s of document.querySelectorAll(".tab")) s.classList.add("hidden");
  $(tab).classList.remove("hidden");
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("on", b.dataset.tab === tab);
}

function render() {
  const staff = !!state.token;
  for (const b of document.querySelectorAll("#nav .staff")) b.classList.toggle("hidden", !staff);
  const who = $("who");
  who.textContent = "";
  const btn = document.createElement("button");
  btn.className = "alt";
  if (staff) { who.append(`${state.user} (${state.role}) `); btn.textContent = "Sign out"; btn.onclick = signOut; }
  else { btn.textContent = "Staff sign in"; btn.onclick = () => show("login"); }
  who.append(btn);
}

function signOut() {
  state.token = state.role = state.user = null; sessionStorage.clear(); render(); show("opac");
}

function pretty(x) { return JSON.stringify(x, null, 2); }
function guard(fn) { return async (e) => { e && e.preventDefault && e.preventDefault(); try { await fn(e); } catch (err) { toast(err.message, false); } }; }

// ----- OPAC -----
$("opac-form").onsubmit = guard(async () => {
  const q = $("opac-q").value;
  const data = await api("/opac/search?q=" + encodeURIComponent(q));
  const out = $("opac-out"); out.textContent = "";
  if (!data.results.length) { out.textContent = "No matching titles."; return; }
  for (const b of data.results) {
    const d = document.createElement("div"); d.className = "res";
    const h = document.createElement("strong"); h.textContent = b.title; d.append(h);
    const s = document.createElement("small"); s.textContent = `  ${b.author || "Unknown author"}${b.year ? ", " + b.year : ""}`; d.append(s, document.createElement("br"));
    for (const c of b.copies) {
      const p = document.createElement("span");
      p.className = "pill " + (c.status === "available" && !c.reference ? "ok" : "no");
      p.textContent = `${c.location}: ${c.reference ? "reference only" : c.status}`; d.append(p);
    }
    out.append(d);
  }
});

// ----- sign in -----
async function signedIn(r, user) {
  state.token = r.token; state.role = r.role; state.user = user || r.username || r.role;
  sessionStorage.setItem("tok", state.token); sessionStorage.setItem("role", state.role); sessionStorage.setItem("user", state.user);
  render(); show("desk"); toast("Signed in as " + state.role);
}
$("login-form").onsubmit = guard(async () => {
  const r = await api("/auth/login", { json: { username: $("u").value, password: $("p").value } });
  $("p").value = ""; await signedIn(r, $("u").value);
});
$("card-form").onsubmit = guard(async () => {
  const r = await api("/auth/card-login", { json: { card_uid: $("card").value } }); await signedIn(r, "card user");
});

// ----- desk -----
$("desk-form").onsubmit = guard(async (e) => {
  const act = (e.submitter && e.submitter.dataset.act) || "checkout";
  const member = $("d-member").value.trim(), item = $("d-item").value.trim();
  let r;
  if (act === "member") r = await api("/members/" + encodeURIComponent(member));
  else if (act === "item") r = await api("/catalog/items/" + encodeURIComponent(item));
  else r = await api("/circulation/" + act, { json: act === "checkout" ? { member, item } : { item } });
  $("desk-out").textContent = pretty(r); toast(act + " ok");
});

// ----- RFID -----
const uidList = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
$("tag-form").onsubmit = guard(async () => {
  const r = await api("/rfid/tag", { json: { item: $("t-item").value.trim(), tag_uid: $("t-uid").value.trim() || null } });
  $("rfid-out").textContent = pretty(r); toast("Tag associated");
});
$("inv-form").onsubmit = guard(async () => {
  const s = await api("/rfid/inventory", { json: { location: $("i-loc").value.trim() } });
  await api(`/rfid/inventory/${s.session_id || s.id}/scan`, { json: { uids: uidList($("i-uids").value) } });
  const r = await api(`/rfid/inventory/${s.session_id || s.id}/finish`, { method: "POST" });
  $("rfid-out").textContent = pretty(r);
  const bad = (r.missing || []).length + (r.misplaced || []).length;
  toast(bad ? `${bad} exception(s) found` : "Shelf is complete", !bad);
});
$("gate-form").onsubmit = guard(async () => {
  const r = await api("/rfid/gate/simulate", { json: { uids: uidList($("g-uids").value), people: 1 } });
  $("rfid-out").textContent = pretty(r); toast("Gate event processed");
});

// ----- dashboard -----
async function loadDash() {
  const loc = $("f-loc").value.trim();
  const d = await api("/reports/dashboard" + (loc ? "?location=" + encodeURIComponent(loc) : ""));
  const cards = [
    ["Titles", d.catalogue.titles], ["Items", d.catalogue.items], ["Tagged", d.catalogue.tagged_percent + "%"],
    ["Members", d.members], ["Operators", d.operators], ["RFID clients", d.rfid_clients],
    ["Checkouts", d.circulation.checkouts], ["Check-ins", d.circulation.checkins], ["Renewals", d.circulation.renewals],
    ["Active loans", d.circulation.active_loans], ["Overdue", d.circulation.overdue_now],
    ["Gate alarms", d.gate.alarms], ["Footfall", d.gate.footfall], ["Fines outstanding", d.fines_outstanding],
  ];
  const box = $("cards"); box.textContent = "";
  for (const [k, v] of cards) {
    const c = document.createElement("div"); c.className = "card";
    const b = document.createElement("b"); b.textContent = v; const s = document.createElement("span"); s.textContent = k;
    c.append(b, s); box.append(c);
  }
}
$("dash-form").onsubmit = guard(loadDash);
$("csv").onclick = guard(async () => {
  const txt = await api("/reports/circulation.csv");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([txt], { type: "text/csv" })); a.download = "circulation.csv"; a.click();
});

// ----- admin -----
$("a-audit").onclick = guard(async () => { $("admin-out").textContent = pretty(await api("/admin/audit?limit=30")); });
$("a-backup").onclick = guard(async () => { $("admin-out").textContent = pretty(await api("/admin/backup", { method: "POST" })); toast("Backup created"); });
$("a-notify").onclick = guard(async () => { $("admin-out").textContent = pretty(await api("/admin/notifications/dispatch", { method: "POST" })); });
$("a-health").onclick = guard(async () => { $("admin-out").textContent = pretty(await api("/health")); });

for (const b of document.querySelectorAll("#nav button")) {
  b.onclick = guard(async () => { show(b.dataset.tab); if (b.dataset.tab === "dash") await loadDash(); });
}
render(); show("opac");
