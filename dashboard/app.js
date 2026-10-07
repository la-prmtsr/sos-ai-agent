"use strict";
/* SOS AI - pusat komando.
   Kiri: peta + log agent. Kanan: satu laporan, dibagi empat tab (Laporan, Agent AI, Dispatch, On-chain).
   Laporan dipilih lewat menu samping: Laporan -> red / yellow / green area -> daftar laporan.

   Soal animasi pipeline: di server, kelima agent selesai dalam beberapa detik. Supaya urutannya
   bisa diikuti mata, dashboard MEMUTAR ULANG hasilnya per agent dengan tempo yang lebih lambat.
   Yang ditampilkan tetap data asli: baris log, tool yang dipanggil, dan waktu sebenarnya tiap agent.
   Tahap "On-chain anchor" tidak diperlambat: tahap itu menunggu transaksi sungguhan masuk blok. */

// Harus sama dengan VERSION di backend/app/config.py. Kalau beda, ada file yang belum tertimpa.
const DASHBOARD_VERSION = "11";

const STATUS_LABEL = {
  REPORTED: "Diproses agent",
  VERIFIED: "Menunggu operator",
  APPROVED: "Dalam pengiriman",
  DELIVERED: "Bantuan sampai",
  REJECTED: "Ditolak",
};
const CHAIN_STATUS = ["NONE", "REPORTED", "VERIFIED", "APPROVED", "DELIVERED", "REJECTED"];
const STATUS_ORDER = { REPORTED: 0, VERIFIED: 1, APPROVED: 2, DELIVERED: 3, REJECTED: 4 };

// tahap pipeline, urutannya sama dengan StateGraph di backend/app/agents.py
const STAGES = [
  { key: "triage", match: "AGENT 1", title: "Agent 1: Ingestion & Triage", sub: "Normalisasi payload · trust score pelapor · nama lokasi" },
  { key: "verification", match: "AGENT 2", title: "Agent 2: Geospatial & Sanity Verification", sub: "Penduduk · CCTV · sinyal per jenis bencana · laporan lain" },
  { key: "allocation", match: "AGENT 3", title: "Agent 3: Humanitarian Equity & Allocation", sub: "Manifest bantuan · skor urgensi" },
  { key: "fleet", match: "AGENT 4", title: "Agent 4: Fleet & Reverse Logistics", sub: "Armada · ETA · evakuasi balik" },
  { key: "supervisor", match: "SUPERVISOR", title: "Supervisor: Command Center Synthesis", sub: "Kartu dispatch · alasan · gerbang HITL" },
  { key: "chain", title: "On-chain Anchor", sub: "submitReport() · markVerified()" },
];
const MIN_STAGE_MS = 1100;
const MAX_STAGE_MS = 2200;

const app = {
  cfg: null,
  reports: [],
  session: null, // sesi dari halaman /login: { address, role, profile } atau { role: "guest" }
  reporters: null, // daftar pelapor + trust score (null = backend belum mendukung)
  tab: "dispatch", // tab detail yang terbuka: report | agents | dispatch | chain
  drawer: { open: false, view: "menu", cls: null }, // menu samping
  drawerKey: "",
  expanded: new Set(), // tahap agent yang rinciannya dibuka operator
  telemetryOpen: true,
  known: null, // kode laporan yang sudah pernah terlihat (untuk mendeteksi laporan baru)
  selected: null,
  onchain: {},
  readContract: null,
  signer: null,
  wallet: null,
  isOperator: false,
  busy: null,
  play: null, // { code, idx, t0, ready } saat pipeline sedang diputar
  playTimer: null,
  map: null,
  pinLayer: null,
  overlayLayer: null,
  fitted: false,
  detailKey: "",
  telemetryKey: "",
};

const $ = (id) => document.getElementById(id);

// Sesi dibuat halaman /login (masuk dengan wallet, atau sebagai pengamat).
// Catatan: ini hanya mengatur tampilan. Pengaman sebenarnya ada di smart contract,
// yang hanya menerima keputusan dari wallet operator.
const SESSION_KEY = "sos.session";
function readSession() {
  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
    return s && s.exp > Date.now() ? s : null;
  } catch (_) {
    return null;
  }
}
function storageWorks() {
  try { localStorage.setItem("sos.test", "1"); localStorage.removeItem("sos.test"); return true; } catch (_) { return false; }
}
function logout() {
  try { localStorage.removeItem(SESSION_KEY); } catch (_) { /* abaikan */ }
  window.location.href = "/";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function short(hex, head = 6, tail = 4) {
  return hex ? `${hex.slice(0, head)}…${hex.slice(-tail)}` : "";
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* bukan JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 4000);
}

function levelClass(report) {
  const level = (report.plan && report.plan.urgency_level) || "";
  // Warna prioritas: tinggi = merah, sedang = kuning, rendah = hijau.
  // MERAH / ORANYE / KUNING adalah nama tingkat di laporan lama, dipetakan ke tiga warna yang sama.
  return { TINGGI: "merah", SEDANG: "kuning", RENDAH: "hijau", MERAH: "merah", ORANYE: "kuning", KUNING: "hijau" }[level] || "";
}

function placeName(r) {
  return (r.plan && r.plan.target) || `${r.payload.geo.lat}, ${r.payload.geo.lon}`;
}

function current() {
  return app.reports.find((r) => r.code === app.selected) || null;
}

function isPlaying(r) {
  return !!(app.play && r && app.play.code === r.code);
}

// ------------------------------------------------------------------ mulai
async function init() {
  try {
    app.cfg = await api("/api/config");
  } catch (e) {
    toast("Backend tidak bisa dihubungi. Pastikan uvicorn sedang jalan.", true);
    return;
  }
  const cfg = app.cfg;

  // belum masuk (lewat wallet atau sebagai pengamat): arahkan ke halaman masuk
  app.session = readSession();
  if (cfg.chainEnabled && !app.session && storageWorks()) {
    window.location.replace("/login");
    return;
  }

  if (cfg.chainEnabled) {
    const provider = new ethers.JsonRpcProvider(cfg.rpcUrl, cfg.chainId, { staticNetwork: true });
    app.readContract = new ethers.Contract(cfg.contractAddress, cfg.abi, provider);
  } else {
    $("walletBtn").hidden = true;
  }
  $("simulateBtn").hidden = !cfg.allowSimulate;

  renderNetPill();
  // cek versi DULU, sebelum bagian lain sempat gagal: kalau file tercampur, ini yang harus terlihat
  const mismatch = String(cfg.version || "?") !== DASHBOARD_VERSION;
  const warn = (text) => {
    clearTimeout(toastTimer);
    const el = $("toast");
    el.textContent = text;
    el.classList.add("error");
    el.hidden = false;
  };
  if (mismatch) warn(`Versi tidak cocok: dashboard v${DASHBOARD_VERSION}, backend v${cfg.version || "lama"}. Ada file yang belum tertimpa, atau backend belum di-restart.`);
  try {
    initMap();
  } catch (e) {
    // peta gagal dibuat tidak boleh mematikan seluruh halaman: daftar laporan tetap jalan
    console.error(e);
    if (!mismatch) warn("Peta gagal dimuat. Tekan Ctrl + F5. Kalau masih begini, lihat tab Console (F12).");
  }

  // tombol wallet: belum terhubung -> hubungkan; sudah terhubung -> buka menu (ada "Putuskan wallet")
  $("walletBtn").addEventListener("click", () => (app.wallet ? openDrawer("menu") : connectWallet()));
  $("simulateBtn").addEventListener("click", simulate);
  $("menuBtn").addEventListener("click", () => (app.drawer.open ? closeDrawer() : openDrawer("menu")));
  $("drawerBackdrop").addEventListener("click", closeDrawer);
  $("drawer").addEventListener("click", onDrawerClick);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && app.drawer.open) closeDrawer(); });

  $("telemetryToggle").addEventListener("click", toggleTelemetry);
  try { app.telemetryOpen = localStorage.getItem("sos.telemetry") !== "closed"; } catch (_) { /* penyimpanan browser tidak tersedia */ }
  applyTelemetry();

  if (window.ethereum && window.ethereum.on) {
    window.ethereum.on("accountsChanged", () => { if (app.wallet) connectWallet(); });
    window.ethereum.on("chainChanged", () => { if (app.wallet) connectWallet(); });
  }

  await loadReports();
  setInterval(loadReports, 1500);

  // sudah masuk dengan wallet di halaman /login: sambungkan lagi tanpa bertanya,
  // selama wallet itu masih memberi izin ke situs ini
  if (cfg.chainEnabled && app.session && app.session.address && window.ethereum) {
    try {
      const accounts = await window.ethereum.request({ method: "eth_accounts" });
      if (accounts.some((a) => a.toLowerCase() === app.session.address.toLowerCase())) connectWallet();
    } catch (_) { /* wallet terkunci: operator bisa menekan tombol hubungkan sendiri */ }
  }
}

function renderNetPill() {
  const cfg = app.cfg;
  const el = $("netPill");
  el.classList.toggle("on", cfg.chainEnabled);
  el.innerHTML = `<i></i>${esc(cfg.chainEnabled ? cfg.chainName : "Mode tanpa chain")}`;
}

function applyTelemetry() {
  $("leftPane").classList.toggle("tele-closed", !app.telemetryOpen);
  $("telemetryToggle").setAttribute("aria-expanded", String(app.telemetryOpen));
  if (app.map) app.map.invalidateSize();
}

function toggleTelemetry() {
  app.telemetryOpen = !app.telemetryOpen;
  try { localStorage.setItem("sos.telemetry", app.telemetryOpen ? "open" : "closed"); } catch (_) { /* tidak apa-apa */ }
  applyTelemetry();
}

function timeAgo(seconds) {
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  if (diff < 60) return "baru saja";
  if (diff < 3600) return `${Math.floor(diff / 60)} menit lalu`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} jam lalu`;
  return `${Math.floor(diff / 86400)} hari lalu`;
}

// ---------------------------------------------------------------- laporan
let loading = false;
async function loadReports() {
  if (loading) return;
  loading = true;
  try {
    let reports;
    try {
      reports = await api("/api/reports");
    } catch (e) {
      return; // backend sedang restart; coba lagi di putaran berikutnya
    }
    reports.sort(
      (a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || b.urgency - a.urgency || b.created_at - a.created_at
    );
    app.reports = reports;

    // laporan yang belum pernah terlihat = baru masuk: pilih dan putar pipeline-nya
    const firstLoad = app.known === null;
    if (firstLoad) app.known = new Set();
    const fresh = reports.filter((r) => !app.known.has(r.code));
    reports.forEach((r) => app.known.add(r.code));

    if (app.selected && !reports.some((r) => r.code === app.selected)) app.selected = null;
    if (!firstLoad && fresh.length) {
      const newest = fresh.reduce((a, b) => (b.created_at > a.created_at ? b : a));
      app.selected = newest.code;
      startPlay(newest.code);
      focusOnMap();
      toast("Laporan baru masuk. Agent sedang memverifikasi.");
    } else if (!app.selected && reports.length) {
      app.selected = reports[0].code;
      // laporan yang masih diproses saat halaman dibuka juga diputar
      if (!(reports[0].log || []).length) startPlay(reports[0].code);
    }

    try { app.reporters = await api("/api/reporters"); } catch (e) { /* backend versi lama: panel disembunyikan */ }

    if (app.selected) await refreshOnchain(app.selected);
    renderAll();
  } finally {
    loading = false;
  }
}

function renderAll() {
  renderDrawer();
  renderPins();
  renderMapChips();
  renderDetail();
  renderTelemetry();
}

// ------------------------------------------------------------------ drawer
// Menu samping: Laporan -> kelompok warna (red / yellow / green area) -> daftar laporan.
const CLASSES = [
  { key: "merah", title: "Red area", sub: "Prioritas tinggi" },
  { key: "kuning", title: "Yellow area", sub: "Prioritas sedang" },
  { key: "hijau", title: "Green area", sub: "Prioritas rendah" },
  { key: "lain", title: "Perlu ditinjau", sub: "Ditolak agent atau masih diproses" },
];
const DISASTER_NAME = { GEMPA: "Gempa bumi", BANJIR: "Banjir", LONGSOR: "Tanah longsor", KEBAKARAN: "Kebakaran", ANGIN: "Angin kencang", LAINNYA: "Bencana lain" };

function reportClass(r) {
  return levelClass(r) || "lain";
}

function openDrawer(view, cls) {
  app.drawer = { open: true, view: view || "menu", cls: cls || null };
  renderDrawer();
  const first = $("drawer").querySelector("button");
  if (first) first.focus();
}

function closeDrawer() {
  if (!app.drawer.open) return;
  app.drawer.open = false;
  renderDrawer();
  $("menuBtn").focus();
}

function drawerMenuHtml() {
  const waiting = app.reports.filter((r) => r.status === "VERIFIED").length;
  const untrusted = (app.reporters || []).filter((r) => r.untrusted).length;
  const profile = app.session && app.session.profile;
  let foot;
  if (!app.cfg.chainEnabled) {
    foot = `<p class="role">Mode tanpa chain: keputusan tidak ditandatangani wallet.</p>`;
  } else if (app.wallet) {
    foot = `<div class="who">${profile ? `<strong>${esc(profile.name)}</strong><span class="role">${esc(profile.agency)}, ${esc(profile.unit)}</span>` : ""}
        <span class="addr">${esc(short(app.wallet, 8, 6))}</span>
        <span class="role">${app.isOperator ? "Operator di contract ini" : "Bukan operator di contract ini"}</span></div>
      <button type="button" class="btn btn-quiet" data-dr="logout">Keluar</button>`;
  } else if (app.session && app.session.address) {
    foot = `<p class="role">Kamu masuk dengan wallet ${esc(short(app.session.address))}, tapi wallet belum tersambung di halaman ini.</p>
      <button type="button" class="btn btn-wallet" data-dr="connect">Hubungkan wallet</button>
      <button type="button" class="btn btn-quiet" data-dr="logout">Keluar</button>`;
  } else {
    foot = `<p class="role">Kamu masuk sebagai pengamat: bisa melihat semua laporan dan buktinya, tidak bisa memutuskan.</p>
      <a class="btn btn-wallet" href="/login" style="text-align:center;text-decoration:none">Masuk dengan wallet</a>
      <button type="button" class="btn btn-quiet" data-dr="logout">Keluar</button>`;
  }
  return `<div class="dr-body"><ul class="dr-menu">
      <li><button type="button" data-dr="classes"><span>Laporan</span><span class="count">${waiting ? `${waiting} menunggu keputusan` : `${app.reports.length} laporan`}</span></button></li>
      <li><button type="button" data-dr="reporters"><span>Pelapor</span><span class="count">${untrusted ? `${untrusted} untrusted` : `${(app.reporters || []).length} terdaftar`}</span></button></li>
      <li><button type="button" data-dr="system"><span>Status sistem</span><span class="count">v${esc(DASHBOARD_VERSION)}</span></button></li>
    </ul></div>
    <div class="dr-foot">${foot}</div>`;
}

function drawerClassesHtml() {
  if (!app.reports.length) {
    return `<div class="dr-body"><button type="button" class="back" data-dr="menu">‹ Menu</button><h2 class="dr-title">Laporan</h2>
      <p class="dr-empty">Belum ada laporan. Kirim <code>/lapor</code> ke bot Telegram untuk membuat laporan pertama.</p></div>`;
  }
  const boxes = CLASSES.map((c) => {
    const items = app.reports.filter((r) => reportClass(r) === c.key);
    if (c.key === "lain" && !items.length) return "";
    const waiting = items.filter((r) => r.status === "VERIFIED").length;
    return `<button type="button" class="cls-box ${c.key}" data-cls="${c.key}">
        <span class="cls-title">${esc(c.title)}</span>
        <span class="cls-count">${items.length}</span>
        <span class="cls-sub">${esc(c.sub)}</span>
        <span class="cls-wait">${waiting ? `${waiting} menunggu keputusan operator` : items.length ? "Tidak ada yang menunggu keputusan" : "Belum ada laporan"}</span>
      </button>`;
  }).join("");
  return `<div class="dr-body"><button type="button" class="back" data-dr="menu">‹ Menu</button><h2 class="dr-title">Laporan</h2>
    <p class="dr-sub">Pilih kelompok prioritas untuk melihat laporannya.</p><div class="cls-list">${boxes}</div></div>`;
}

function drawerListHtml() {
  const c = CLASSES.find((x) => x.key === app.drawer.cls) || CLASSES[0];
  const items = app.reports.filter((r) => reportClass(r) === c.key);
  const rows = items.map((r) => {
    const kind = DISASTER_NAME[r.payload.disaster_type] || "Laporan";
    return `<li><button type="button" class="r-row ${c.key}" data-code="${esc(r.code)}" aria-current="${r.code === app.selected}">
        <span class="r-score">${r.plan ? esc(r.urgency) : "…"}</span>
        <span class="r-place">${esc(placeName(r))}</span>
        <span class="r-meta"><span class="status-pill ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span>${esc(kind)}, ${esc(timeAgo(r.created_at))}</span>
      </button></li>`;
  }).join("");
  return `<div class="dr-body"><button type="button" class="back" data-dr="classes">‹ Laporan</button><h2 class="dr-title">${esc(c.title)}</h2>
    <p class="dr-sub">${esc(c.sub)}, ${items.length} laporan</p>
    ${items.length ? `<ol class="r-list">${rows}</ol>` : `<p class="dr-empty">Tidak ada laporan di kelompok ini.</p>`}</div>`;
}

function drawerReportersHtml() {
  const list = app.reporters || [];
  const rows = list.map((r) => `<div class="rep ${r.untrusted ? "untrusted" : ""}">
      <span class="rep-name">${esc(r.name || "Pelapor")}${r.untrusted ? " (untrusted)" : ""}</span>
      <span class="rep-trust">${Number(r.trust).toFixed(2)}</span>
      <span class="rep-meta">${esc(r.reports)} laporan, terakhir ${esc((r.last_verdict || "-").toLowerCase())}. ${r.phone_masked ? `Nomor HP ${esc(r.phone_masked)}` : "Nomor HP belum dibagikan"}${r.volunteer_org ? `. ${esc(r.volunteer_org)}` : ""}</span>
      <div class="bar"><span style="width:${Math.round(Number(r.trust) * 100)}%"></span></div>
      ${r.untrusted ? `<button type="button" class="btn btn-small" data-reset="${esc(r.reporter_id)}" style="grid-column:1/-1;margin-top:8px">Pulihkan dari daftar untrusted</button>` : ""}
    </div>`).join("");
  return `<div class="dr-body"><button type="button" class="back" data-dr="menu">‹ Menu</button><h2 class="dr-title">Pelapor</h2>
    <p class="dr-sub">Trust score tiap pelapor, dari 0 sampai 1.</p>
    ${list.length ? `<div class="reporters">${rows}</div>` : `<p class="dr-empty">Belum ada pelapor dari Telegram.</p>`}</div>`;
}

function drawerSystemHtml() {
  const cfg = app.cfg;
  const contract = !cfg.chainEnabled ? "-" : cfg.explorerUrl
    ? `<a href="${esc(cfg.explorerUrl)}/address/${esc(cfg.contractAddress)}" target="_blank" rel="noopener">${esc(short(cfg.contractAddress, 8, 6))}</a>`
    : esc(short(cfg.contractAddress, 8, 6));
  const row = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  return `<div class="dr-body"><button type="button" class="back" data-dr="menu">‹ Menu</button><h2 class="dr-title">Status sistem</h2>
    <dl class="sys-list" style="margin-top:10px">
      ${row("Jaringan", esc(cfg.chainEnabled ? cfg.chainName : "tanpa chain"))}
      ${row("Smart contract", contract)}
      ${row("Bot Telegram", cfg.telegramEnabled ? "aktif" : "mati")}
      ${row("LLM supervisor", cfg.llmEnabled ? "aktif" : "mati (pakai template)")}
      ${row("Pos asal bantuan", (cfg.staging || []).length ? esc(cfg.staging.length) + " pos BPBD" : "cadangan DEPOT di .env")}
      ${row("Tombol simulasi", cfg.allowSimulate ? "aktif" : "mati")}
      ${row("Versi", "v" + esc(DASHBOARD_VERSION))}
    </dl></div>`;
}

function renderDrawer() {
  const el = $("drawer");
  const d = app.drawer;
  el.classList.toggle("open", d.open);
  $("drawerBackdrop").classList.toggle("open", d.open);
  $("menuBtn").setAttribute("aria-expanded", String(d.open));
  if (!d.open) return;

  const body = { menu: drawerMenuHtml, classes: drawerClassesHtml, list: drawerListHtml, reporters: drawerReportersHtml, system: drawerSystemHtml }[d.view] || drawerMenuHtml;
  // data-logo: logo.js mengganti tanda "SOS" ini dengan logo tim, juga setiap kali menu digambar ulang
  const html = `<div class="dr-head"><span class="brand-mark" aria-hidden="true" data-logo>SOS</span>
      <div><strong>SOS AI</strong><small>Pusat komando bantuan bencana</small></div>
      <button type="button" class="icon-btn" data-dr="close" aria-label="Tutup menu">✕</button></div>${body()}`;
  // gambar ulang hanya kalau isinya berubah, supaya fokus keyboard dan posisi scroll tidak hilang
  if (html === app.drawerKey) return;
  app.drawerKey = html;
  el.innerHTML = html;
}

async function onDrawerClick(event) {
  const btn = event.target.closest("button");
  if (!btn) return;
  if (btn.dataset.code) {
    closeDrawer();
    await select(btn.dataset.code);
    return;
  }
  if (btn.dataset.cls) return openDrawer("list", btn.dataset.cls);
  if (btn.dataset.reset) {
    try {
      await api(`/api/reporters/${btn.dataset.reset}/reset`, { method: "POST" });
      toast("Pelapor dipulihkan dari daftar untrusted.");
      await loadReports();
    } catch (e) {
      toast(e.message, true);
    }
    return;
  }
  const action = btn.dataset.dr;
  if (action === "close") closeDrawer();
  else if (action === "connect") { closeDrawer(); connectWallet(); }
  else if (action === "logout") { if (app.wallet) disconnectWallet(); else logout(); }
  else if (action) openDrawer(action);
}

async function select(code) {
  if (app.play && app.play.code !== code) stopPlay();
  app.selected = code;
  app.expanded.clear();
  const r = current();
  if (r && !(r.log || []).length && !isPlaying(r)) startPlay(code);
  else if (r && !isPlaying(r)) app.tab = "dispatch"; // laporan yang sudah selesai dianalisis dibuka di kartu dispatch
  renderAll();
  focusOnMap();
  await refreshOnchain(code);
  renderDetail();
}

async function refreshOnchain(code) {
  if (!app.readContract) return;
  const report = app.reports.find((r) => r.code === code);
  if (!report) return;
  try {
    const r = await app.readContract.getReport(report.report_id);
    app.onchain[code] = {
      status: CHAIN_STATUS[Number(r.status)],
      payloadHash: r.payloadHash,
      planHash: r.planHash,
      decidedBy: r.decidedBy,
    };
  } catch (e) {
    // RPC tidak bisa diakses langsung dari browser (mis. diblokir CORS): baca lewat backend
    try {
      const r = await api(`/api/reports/${code}/onchain`);
      app.onchain[code] = { status: r.status, payloadHash: r.payload_hash, planHash: r.plan_hash, decidedBy: r.decided_by, viaBackend: true };
    } catch (e2) {
      app.onchain[code] = { error: e.shortMessage || e.message || String(e) };
    }
  }
}

function simulate() {
  toast("Laporan contoh dikirim. Pipeline agent mulai berjalan…");
  // tidak ditunggu: laporan baru akan terdeteksi oleh polling dan langsung diputar
  api("/api/simulate", { method: "POST" }).catch((e) => toast(`Gagal membuat laporan contoh: ${e.message}`, true));
}

// --------------------------------------------------------- membaca log agent
function parseLog(lines) {
  const sections = {}; // key tahap -> { lines: [], ms }
  const chain = [];
  const after = []; // fase 3: keputusan operator, konfirmasi sampai
  const system = [];
  let cur = null;
  for (const line of lines || []) {
    if (line.startsWith(">>> [")) {
      const stage = STAGES.find((s) => s.match && line.includes(s.match));
      cur = stage ? (sections[stage.key] = { lines: [], ms: null }) : null;
      continue;
    }
    if (line.startsWith("[CHAIN] confirmDelivery") || line.startsWith("[OPERATOR]") || line.startsWith("[PELAPOR]")) { after.push(line); cur = null; continue; }
    if (line.startsWith("[CHAIN]")) { chain.push(line); cur = null; continue; }
    if (line.startsWith("[SYSTEM]")) { system.push(line); cur = null; continue; }
    if (cur) {
      const m = line.match(/Selesai dalam (\d+) ms/);
      if (m) cur.ms = Number(m[1]);
      else cur.lines.push(line);
    }
  }
  return { sections, chain, after, system };
}

function chainState(r, parsed) {
  if (!app.cfg.chainEnabled) return "skipped";
  if (r.tx_submit && (r.tx_verify || !r.plan_hash)) return "done";
  if (parsed.chain.some((l) => l.includes("GAGAL"))) return "failed";
  return "running";
}

/** Status tiap tahap untuk laporan r: saat diputar mengikuti app.play, selain itu dari data. */
function stageStates(r) {
  const parsed = parseLog(r.log);
  const ready = (r.log || []).length > 0;
  const playing = isPlaying(r);
  return STAGES.map((stage, i) => {
    const isChain = stage.key === "chain";
    const sec = parsed.sections[stage.key];
    let finalState;
    if (isChain) finalState = chainState(r, parsed);
    else if (!ready) finalState = "running";
    else finalState = sec ? "done" : "skipped";

    let state = finalState;
    if (playing) {
      if (i > app.play.idx) state = "queued";
      else if (i === app.play.idx) state = isChain ? (finalState === "running" ? "running" : finalState) : (ready && !sec ? "skipped" : "running");
    }
    return { ...stage, index: i, state, finalState, ready, ms: sec ? sec.ms : null, lines: isChain ? parsed.chain : sec ? sec.lines : [] };
  });
}

// ------------------------------------------------------------ pemutar pipeline
function startPlay(code) {
  stopPlay();
  app.tab = "agents";
  app.play = { code, idx: 0, t0: performance.now(), ready: false };
  tickPlay();
}

function stopPlay() {
  clearTimeout(app.playTimer);
  app.play = null;
}

function renderPlayStep() {
  renderDetail();
  renderTelemetry();
  renderMapChips();
}

function stageDuration(ms) {
  return Math.min(MAX_STAGE_MS, Math.max(MIN_STAGE_MS, ms || 0));
}

function tickPlay() {
  clearTimeout(app.playTimer);
  if (!app.play) return;
  const r = app.reports.find((x) => x.code === app.play.code);
  if (!r) { stopPlay(); return; }
  const stages = stageStates(r);
  const st = stages[app.play.idx];
  if (!st) { stopPlay(); app.tab = "dispatch"; renderAll(); return; } // pipeline selesai: tampilkan kartu dispatch

  const advance = () => {
    app.play.idx += 1;
    app.play.t0 = performance.now();
    app.play.ready = false;
    renderPlayStep();
    tickPlay();
  };

  if (st.key === "chain") {
    // tahap ini menunggu transaksi sungguhan
    const elapsed = performance.now() - app.play.t0;
    if (st.finalState !== "running" && elapsed >= 700) return advance();
    app.playTimer = setTimeout(tickPlay, 350);
    return;
  }
  if (!st.ready) { app.playTimer = setTimeout(tickPlay, 350); return; } // backend belum selesai
  if (!app.play.ready) {
    // data tahap ini baru tersedia: mulai hitung dari sekarang
    app.play.ready = true;
    app.play.t0 = performance.now();
    renderPlayStep();
  }
  const total = st.state === "skipped" ? 350 : stageDuration(st.ms);
  const left = total - (performance.now() - app.play.t0);
  if (left <= 0) return advance();
  app.playTimer = setTimeout(tickPlay, left);
}

// ----------------------------------------------------------------- detail
function txLink(label, hash) {
  if (!hash) return "";
  const text = `${label} ${short(hash, 10, 6)}`;
  return app.cfg.explorerUrl
    ? `<a href="${esc(app.cfg.explorerUrl)}/tx/${esc(hash)}" target="_blank" rel="noopener">${esc(text)}</a>`
    : `<span class="hash">${esc(text)}</span>`;
}

function hashesMatch(r) {
  const oc = app.onchain[r.code];
  if (!oc || oc.error || !r.plan_text) return false;
  return (
    ethers.keccak256(ethers.toUtf8Bytes(r.payload_text)) === oc.payloadHash &&
    ethers.keccak256(ethers.toUtf8Bytes(r.plan_text)) === oc.planHash
  );
}

function phase1Html(r) {
  const p = r.payload;
  const needs = { BALITA: "Balita", LANSIA: "Lansia", TENDA: "Tenda", MEDIS: "Medis darurat" };
  const roads = { TRUCK_OK: "Mobil/truk aman", TRAIL_BIKE_ONLY: "Hanya motor trail", BOAT_ONLY: "Terisolir air/perahu", UNKNOWN: "Tidak tahu" };
  const disasters = { GEMPA: "Gempa bumi", BANJIR: "Banjir", LONGSOR: "Tanah longsor", KEBAKARAN: "Kebakaran", ANGIN: "Angin kencang", LAINNYA: "Lainnya" };
  const geoSource = { GPS: "dibagikan dari GPS HP", MANUAL: "diketik manual", SIMULASI: "simulasi" }[p.geo_source] || "";
  const simulated = r.reporter_name === "Laporan contoh";
  const row = (who, cls, text) => `<div class="${cls}"><span class="who">${who}</span>${text}</div>`;
  // langkah nomor HP hanya ditampilkan untuk laporan yang punya datanya (nomornya sendiri tidak pernah dikirim ke dashboard)
  const v = r.verification || {};
  let contact = "";
  if (!simulated && "phone_verified" in v) {
    const who = v.volunteer ? `nomor terverifikasi · relawan ${esc(v.volunteer.org)}` : "nomor terverifikasi";
    contact = row("Bot →", "bot", "Bagikan nomor HP (opsional)") + row("User →", "user", v.phone_verified ? `📱 ${who}` : "Lewati");
  }
  return `<section class="panel">
    <div class="panel-head">
      <h3 class="panel-title">Phase 1: laporan dari Telegram</h3>
      <span class="badge ${simulated ? "" : "cyan"}">${simulated ? "Simulasi" : esc(r.reporter_name || "Pelapor")}</span>
    </div>
    <div class="chat">
      ${contact}
      ${row("Bot →", "bot", "Share lokasi bencana")}
      ${row("User →", "user", `📍 ${esc(p.geo.lat)}, ${esc(p.geo.lon)}${geoSource ? ` <span class="src-note">(${esc(geoSource)})</span>` : ""}`)}
      ${p.disaster_type ? row("Bot →", "bot", "Bencana apa yang terjadi?") + row("User →", "user", esc(disasters[p.disaster_type] || p.disaster_type)) : ""}
      ${row("Bot →", "bot", "Estimasi jumlah pengungsi?")}
      ${row("User →", "user", `${esc(p.headcount_range)} jiwa`)}
      ${row("Bot →", "bot", "Kelompok rentan & kebutuhan mendesak?")}
      ${row("User →", "user", p.needs.length ? esc(p.needs.map((n) => needs[n] || n).join(", ")) : "(tidak ada yang dipilih)")}
      ${row("Bot →", "bot", "Kondisi akses jalan ke posko?")}
      ${row("User →", "user", esc(roads[p.road_status] || p.road_status))}
      ${row("Bot →", "bot", "Ada korban kritis yang butuh evakuasi?")}
      ${row("User →", "user", p.critical_count ? `Ada, ${esc(p.critical_count)} orang` : "Tidak ada")}
      <div class="inbound">[TG_INBOUND] Payload ${esc(r.code)} terstruktur → StateGraph</div>
    </div>
  </section>`;
}

function lineClass(line) {
  if (line.includes("PERINGATAN") || line.includes("GAGAL") || line.includes("TIDAK VALID")) return "l-warn";
  if (line.startsWith("[CHAIN]") || line.startsWith("[OPERATOR]") || line.startsWith("[PELAPOR]")) return "l-chain";
  if (line.startsWith("[Tool]")) return "l-tool";
  return "";
}

function stageHtml(st) {
  const labels = { queued: "Menunggu giliran", running: "Sedang berjalan", done: "Selesai", skipped: "Dilewati", failed: "Gagal" };
  let label = labels[st.state];
  if (st.state === "done" && st.ms !== null) label = `Selesai, ${st.ms} ms`;
  if (st.key === "chain" && st.state === "skipped") label = "Mode tanpa chain";

  let bar = "<span></span>";
  if (st.state === "running") {
    if (st.key === "chain" || !st.ready || !app.play) bar = `<span class="wait"></span>`;
    else {
      const elapsed = Math.max(0, performance.now() - app.play.t0);
      bar = `<span class="fill" style="animation-duration:${stageDuration(st.ms)}ms;animation-delay:-${Math.round(elapsed)}ms"></span>`;
    }
  }
  const sub = st.key === "chain" && app.cfg.chainEnabled ? `${st.sub} di ${app.cfg.chainName}` : st.sub;
  const hasLines = st.state !== "queued" && st.lines.length;
  // rincian terbuka otomatis saat tahap berjalan; setelah itu hanya kalau operator membukanya
  const open = hasLines && (st.state === "running" || app.expanded.has(st.key));
  const lines = hasLines
    ? `<div class="stage-lines">${st.lines.map((l) => `<span class="${lineClass(l)}">${esc(l.replace(/^\s{2}- /, "· "))}</span>`).join("\n")}</div>`
    : "";
  return `<details class="stage ${st.state}" data-stage="${st.key}" ${open ? "open" : ""}>
    <summary>
      <div class="stage-head"><span class="stage-title">${esc(st.title)}</span><span class="stage-state">${esc(label)}</span></div>
      <div class="stage-sub">${esc(sub)}</div>
      <div class="bar">${bar}</div>
    </summary>
    ${lines}
  </details>`;
}

function phase2Html(r, stages) {
  let badge = `<span class="badge cyan">Sedang berjalan</span>`;
  if (!isPlaying(r)) {
    if (r.status === "VERIFIED") badge = `<span class="badge amber">Menunggu operator</span>`;
    else if (r.status === "APPROVED" || r.status === "DELIVERED") badge = `<span class="badge green">Sudah diputuskan</span>`;
    else if (r.status === "REJECTED") badge = `<span class="badge red">Ditolak</span>`;
  }
  const replay = !isPlaying(r) && (r.log || []).length
    ? `<button type="button" class="btn btn-small" data-action="replay">Putar ulang</button>`
    : "";
  return `<section class="panel">
    <div class="panel-head"><h3 class="panel-title">Phase 2: pipeline agent</h3><span style="display:flex;gap:6px;align-items:center">${replay}${badge}</span></div>
    <div class="stages">${stages.map(stageHtml).join("")}</div>
    <p class="hint" style="margin-top:10px">Klik satu tahap untuk melihat tool yang dipanggil dan hasilnya.</p>
  </section>`;
}

function actionsHtml(r) {
  if (app.busy) return `<p class="hint">${esc(app.busy)}</p>`;
  const by = r.decided_by && r.decided_by !== "off-chain" ? ` oleh wallet ${esc(short(r.decided_by))}` : "";
  if (r.status === "APPROVED") return `<p class="hint">Disetujui${by}. Menunggu pelapor mengonfirmasi bantuan sampai.</p>`;
  if (r.status === "DELIVERED") return `<p class="hint">Pelapor sudah mengonfirmasi bantuan sampai. Laporan ditutup.</p>`;
  if (r.status === "REJECTED") return `<p class="hint">Laporan ditolak${by}.</p>`;
  if (r.status !== "VERIFIED") return "";

  // kartu yang direkomendasikan TOLAK tidak punya rencana pengiriman, jadi hanya bisa ditolak
  const rejectOnly = !!(r.plan && r.plan.recommendation !== "KIRIM");
  const buttons = (disabled) => `
    ${rejectOnly ? "" : `<button type="button" class="btn btn-approve" data-action="approve" ${disabled ? "disabled" : ""}>Setujui dan kirim bantuan</button>`}
    <button type="button" class="btn btn-reject" data-action="reject" ${disabled ? "disabled" : ""}>Tolak${rejectOnly ? " laporan" : ""}</button>`;

  if (!app.cfg.chainEnabled) return buttons(false) + `<p class="hint">Mode tanpa chain: keputusan tidak ditandatangani wallet.</p>`;
  if (!app.wallet) {
    return `<button type="button" class="btn btn-cyan" data-action="connect">Hubungkan wallet untuk memutuskan</button>
      <p class="hint">Keputusan operator ditandatangani wallet dan tercatat di blockchain.</p>`;
  }
  if (!app.isOperator) {
    return buttons(true) + `<p class="hint">Wallet ${esc(short(app.wallet))} bukan operator. Minta pemilik contract menambahkannya lewat setOperator.</p>`;
  }
  const oc = app.onchain[r.code];
  if (!oc || oc.error || oc.status !== "VERIFIED") {
    return buttons(true) + `<p class="hint">Belum bisa diputuskan: status di blockchain belum "Menunggu operator". Lihat panel bukti on-chain.</p>`;
  }
  if (!hashesMatch(r)) {
    return buttons(true) + `<p class="hint">Diblokir: data di layar tidak cocok dengan hash on-chain. Jangan setujui sebelum penyebabnya jelas.</p>`;
  }
  if (rejectOnly) return buttons(false) + `<p class="hint">Agent merekomendasikan tolak. Penolakan ditandatangani wallet dan tercatat di blockchain.</p>`;
  return buttons(false) + `<p class="hint">Wallet akan meminta tanda tangan. Hash rencana ini ikut dikirim ke contract.</p>`;
}

/** Baris hasil verifikasi Agent 2 untuk kartu dispatch (kosong untuk laporan versi lama). */
function verificationRows(c) {
  const v = c.verification;
  if (!v) return "";
  const cls = { TERKONFIRMASI: "v-ok", "BELUM TERKONFIRMASI": "v-wait", "TIDAK VALID": "v-bad" }[v.verdict] || "";
  const evidence = v.evidence && v.evidence.length ? v.evidence.map(esc).join("<br>") : "Belum ada bukti lapangan (CCTV, sinyal alam, atau laporan lain)";
  const delta = Number(v.trust_after) - Number(v.trust_before);
  const arrow = delta > 0 ? "▲" : delta < 0 ? "▼" : "";
  // keyakinan sengaja TIDAK memakai merah/kuning/hijau, supaya tiga warna itu hanya berarti prioritas
  const confDots = { TINGGI: "●●●", SEDANG: "●●○", RENDAH: "●○○" }[v.confidence] || "";
  const confidence = v.confidence
    ? `
      <dt>Keyakinan</dt><dd><span class="conf">${confDots}</span> ${esc(v.confidence)} (${esc(v.match_points)} poin)<span class="src">${v.matches && v.matches.length ? v.matches.map(esc).join(" · ") : "Belum ada tanda yang cocok"}</span></dd>`
    : "";
  return `${v.disaster ? `
      <dt>Jenis bencana</dt><dd>${esc(v.disaster)}<span class="src">Lokasi: ${esc(v.location || "-")}</span></dd>` : ""}
      <dt>Verifikasi</dt><dd><span class="${cls}">${esc(v.verdict)}</span><span class="src">Penduduk: ${esc(v.census || "-")}</span><span class="src">${evidence}</span></dd>${confidence}
      <dt>Trust pelapor</dt><dd>${Number(v.trust_before).toFixed(2)} → <span class="${delta < 0 ? "v-bad" : delta > 0 ? "v-ok" : ""}">${Number(v.trust_after).toFixed(2)} ${arrow}</span>${c.reporter_untrusted ? ` <span class="v-bad">· UNTRUSTED</span>` : ""}</dd>`;
}

function dispatchHtml(r) {
  const c = r.plan;
  if (!c) return "";
  const cls = r.status === "REJECTED" ? "rejected" : r.status === "APPROVED" || r.status === "DELIVERED" ? "decided" : "";
  const statusText = {
    VERIFIED: c.recommendation === "KIRIM" ? "Menunggu persetujuan operator" : "Menunggu keputusan operator",
    APPROVED: "Disetujui, dalam pengiriman",
    DELIVERED: "Bantuan sampai",
    REJECTED: "Ditolak operator",
  }[r.status] || r.status;

  if (c.recommendation !== "KIRIM") {
    return `<section class="panel dispatch ${cls}">
      <div class="panel-head"><h3 class="dispatch-title">Supervisor merekomendasikan tolak</h3></div>
      <dl class="d-rows">
        <dt>Lokasi</dt><dd>${esc(c.target)}</dd>${verificationRows(c)}
        <dt>Status</dt><dd>${esc(statusText)}</dd>
      </dl>
      <p class="reasoning">${esc(c.reasoning)}</p>
      <div class="actions">${actionsHtml(r)}</div>
    </section>`;
  }
  const lvl = levelClass(r);
  return `<section class="panel dispatch ${cls}">
    <div class="panel-head"><h3 class="dispatch-title">Kartu dispatch dari supervisor</h3></div>
    <dl class="d-rows">
      <dt>Posko target</dt><dd>${esc(c.target)}</dd>
      <dt>Prioritas</dt><dd><span class="prio ${lvl}">${esc(c.urgency_level)}</span> skor ${esc(c.urgency_score)} dari 100</dd>${verificationRows(c)}
      <dt>Akses jalan</dt><dd>${esc(c.road_access)}<span class="src">${esc(c.road_access_source)}</span></dd>
      <dt>Armada</dt><dd>${esc(c.fleet)}</dd>
      <dt>Pos asal</dt><dd>${esc(c.origin)}${c.distance_km != null ? `<span class="src">${esc(c.distance_km)} km lewat jalan</span>` : ""}</dd>
      <dt>ETA</dt><dd>${esc(c.eta_min)} menit</dd>
      <dt>Muatan berangkat</dt><dd>${c.cargo_out.map(esc).join("<br>")}</dd>
      ${c.cargo_back ? `<dt>Muatan kembali</dt><dd>${esc(c.cargo_back)}</dd>` : ""}
      <dt>Status</dt><dd>${esc(statusText)}</dd>
    </dl>
    <p class="reasoning">${esc(c.reasoning)}<small>Alasan ditulis ${c.reasoning_source === "LLM" ? "LLM dari fakta hasil agent" : "dari template (LLM belum diatur)"}.</small></p>
    ${c.warnings && c.warnings.length ? `<div class="warnings">${c.warnings.map((w) => `<span>⚠ ${esc(w)}</span>`).join("")}</div>` : ""}
    <div class="actions">${actionsHtml(r)}</div>
  </section>`;
}

function proofHtml(r) {
  let body;
  const oc = app.onchain[r.code];
  if (!app.cfg.chainEnabled) {
    body = `<div class="proof-row"><span class="mark">–</span><span>Mode tanpa chain: keputusan hanya tersimpan di database. Isi CONTRACT_ADDRESS dan RELAYER_PRIVATE_KEY di backend/.env supaya setiap keputusan punya bukti on-chain.</span></div>`;
  } else if (!oc) {
    body = `<div class="proof-row"><span class="mark">…</span><span>Membaca blockchain…</span></div>`;
  } else if (oc.error) {
    body = `<div class="proof-row bad"><span class="mark">!</span><span>Blockchain tidak bisa dibaca: ${esc(oc.error)}. Cek RPC ${esc(app.cfg.rpcUrl)}.</span></div>`;
  } else if (oc.status === "NONE") {
    body = `<div class="proof-row bad"><span class="mark">!</span><span>Laporan ini belum tercatat di contract. Penyebab umum: saldo gas wallet backend habis, atau RPC sedang bermasalah. Lihat tahap On-chain anchor di tab Agent AI.</span></div>
      <button type="button" class="btn btn-small" data-action="resync">Catat ulang ke blockchain</button>`;
  } else {
    const payloadOk = ethers.keccak256(ethers.toUtf8Bytes(r.payload_text)) === oc.payloadHash;
    const planOk = r.plan_text ? ethers.keccak256(ethers.toUtf8Bytes(r.plan_text)) === oc.planHash : null;
    const row = (ok, text, hash) =>
      `<div class="proof-row ${ok === null ? "" : ok ? "ok" : "bad"}"><span class="mark">${ok === null ? "…" : ok ? "✓" : "✗"}</span><span>${text}${hash ? `<br><span class="hash">${esc(hash)}</span>` : ""}</span></div>`;
    body =
      row(payloadOk, payloadOk ? "Isi laporan sama dengan hash on-chain" : "Isi laporan BERBEDA dari hash on-chain", oc.payloadHash) +
      row(planOk, planOk === null ? "Rencana belum dicatat" : planOk ? "Kartu dispatch sama dengan hash on-chain" : "Kartu dispatch BERBEDA dari hash on-chain", planOk === null ? "" : oc.planHash) +
      row(true, `Status di contract: ${esc(STATUS_LABEL[oc.status] || oc.status)}` +
        (oc.decidedBy && oc.decidedBy !== ethers.ZeroAddress ? `<br><span class="hash">diputuskan wallet ${esc(oc.decidedBy)}</span>` : ""));
    if (oc.viaBackend) body += `<div class="proof-row"><span class="mark">i</span><span class="hash">Dibaca lewat backend, karena RPC tidak bisa diakses langsung dari browser.</span></div>`;
    const links = [txLink("submitReport", r.tx_submit), txLink("markVerified", r.tx_verify), txLink("confirmDelivery", r.tx_delivery)].filter(Boolean);
    if (links.length) body += `<div class="proof-row"><span class="mark">↗</span><span>${links.join("<br>")}</span></div>`;
  }
  return `<section class="panel"><div class="panel-head"><h3 class="panel-title">Bukti on-chain${app.cfg.chainEnabled ? " di " + esc(app.cfg.chainName) : ""}</h3></div><div class="proof">${body}</div></section>`;
}

function phase3Html(r, parsed) {
  if (!parsed.after.length) return "";
  return `<section class="panel">
    <div class="panel-head"><h3 class="panel-title">Phase 3: eksekusi dan notifikasi</h3><span class="badge green">${esc(STATUS_LABEL[r.status] || r.status)}</span></div>
    <div class="phase3">${parsed.after.map((l) => `<span class="${lineClass(l)}">${esc(l)}</span>`).join("\n")}</div>
  </section>`;
}

// garis waktu on-chain: satu pemberhentian untuk tiap transaksi di smart contract
function timelineHtml(r) {
  const chainOn = app.cfg.chainEnabled;
  const decided = ["APPROVED", "DELIVERED", "REJECTED"].includes(r.status);
  const rejected = r.status === "REJECTED";
  const steps = [
    { label: "Laporan dicatat", done: chainOn ? !!r.tx_submit : true, tx: r.tx_submit },
    { label: "Diverifikasi agent", done: chainOn ? !!r.tx_verify : !!r.plan, tx: r.tx_verify },
    { label: rejected ? "Ditolak operator" : decided ? "Disetujui operator" : "Keputusan operator", done: decided, tx: r.tx_decide, bad: rejected },
    { label: "Bantuan sampai", done: r.status === "DELIVERED", tx: r.tx_delivery, off: rejected },
  ];
  const now = steps.findIndex((s) => !s.done && !s.off);
  const items = steps.map((s, i) => {
    const cls = [s.done ? "done" : i === now ? "now" : "todo", s.bad ? "bad" : "", s.off ? "off" : ""].join(" ").trim();
    let tx = "";
    if (chainOn && s.tx) {
      tx = app.cfg.explorerUrl
        ? `<a href="${esc(app.cfg.explorerUrl)}/tx/${esc(s.tx)}" target="_blank" rel="noopener" title="Buka transaksi di explorer">${esc(short(s.tx, 6, 4))}</a>`
        : `<span class="tx">${esc(short(s.tx, 6, 4))}</span>`;
    }
    return `<li class="${cls}"><span>${esc(s.label)}</span>${tx}</li>`;
  }).join("");
  const note = chainOn
    ? `Setiap langkah adalah transaksi di ${esc(app.cfg.chainName)}`
    : "Mode tanpa chain: langkah-langkah ini belum tercatat di blockchain";
  return `<ol class="timeline" aria-label="Jejak laporan di blockchain">${items}</ol><p class="timeline-note">${note}</p>`;
}

const TABS = [["report", "Laporan"], ["agents", "Agent AI"], ["dispatch", "Dispatch"], ["chain", "On-chain"]];

function setTab(tab) {
  app.tab = tab;
  renderDetail();
}

function renderDetail() {
  const el = $("detail");
  const r = current();
  if (!r) {
    if (app.detailKey !== "empty") {
      el.innerHTML = `<div class="empty"><h2>Belum ada laporan</h2>
        <p>Kirim <code>/lapor</code> ke bot Telegram untuk membuat laporan pertama${app.cfg.allowSimulate ? `, atau tekan "Simulasi laporan" di kanan atas` : ""}.</p></div>`;
      app.detailKey = "empty";
    }
    return;
  }
  const playing = isPlaying(r);
  // selama pipeline diputar, kartu dispatch belum ada: tampilkan tab agent
  const tab = playing && app.tab === "dispatch" ? "agents" : app.tab;

  // gambar ulang hanya kalau ada yang berubah
  const key = JSON.stringify([r.code, r.updated_at, r.status, app.onchain[r.code], app.wallet, app.isOperator, app.busy, tab, [...app.expanded], app.play && [app.play.code, app.play.idx, app.play.ready]]);
  if (key === app.detailKey) return;
  app.detailKey = key;

  const parsed = parseLog(r.log);
  const lvl = levelClass(r);
  const level = (r.plan && r.plan.urgency_level) || "";
  const kind = DISASTER_NAME[r.payload.disaster_type] || "Laporan bencana";

  let body;
  if (tab === "report") body = phase1Html(r);
  else if (tab === "agents") body = phase2Html(r, stageStates(r));
  else if (tab === "chain") body = proofHtml(r) + phase3Html(r, parsed);
  else body = (!playing && dispatchHtml(r)) || `<section class="panel"><p class="hint">Supervisor sedang menyusun kartu dispatch. Lihat tab Agent AI.</p></section>`;

  const dots = { agents: playing ? `<i class="dot"></i>` : "", dispatch: !playing && r.status === "VERIFIED" ? `<i class="dot chain"></i>` : "" };
  el.innerHTML = `
    <div class="d-sticky">
    <header class="d-head">
      <div class="d-top">
        ${level && lvl ? `<span class="prio ${lvl}">Prioritas ${esc(level.toLowerCase())}</span>` : ""}
        <span class="status-pill ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span>
        <span class="d-code">${esc(r.code)}</span>
      </div>
      <h2>${esc(placeName(r))}</h2>
      <p class="d-sub">${esc(kind)}, ${esc(r.payload.headcount_range)} pengungsi, dilaporkan ${esc(timeAgo(r.created_at))}</p>
    </header>
    ${timelineHtml(r)}
    <div class="tabs" role="tablist">${TABS.map(([id, label]) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${id === tab}">${label}${dots[id] || ""}</button>`).join("")}</div>
    </div>
    <div class="tab-body" role="tabpanel">${body}</div>`;

  el.querySelectorAll("[data-tab]").forEach((btn) => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
  el.querySelectorAll("[data-action]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      if (action === "approve") decide(true);
      else if (action === "reject") decide(false);
      else if (action === "connect") connectWallet();
      else if (action === "resync") resync();
      else if (action === "replay") { startPlay(r.code); renderAll(); }
    })
  );
  // ingat tahap mana yang dibuka operator, supaya tidak menutup sendiri saat data diperbarui
  el.querySelectorAll("details.stage > summary").forEach((summary) =>
    summary.addEventListener("click", () => {
      const details = summary.parentElement;
      if (details.open) app.expanded.delete(details.dataset.stage);
      else app.expanded.add(details.dataset.stage);
    })
  );

  if (playing) {
    const running = el.querySelector(".stage.running");
    if (running) running.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  renderOverlay();
}

function renderTelemetry() {
  const el = $("telemetry");
  const cfg = app.cfg;
  const lines = [
    "[SYSTEM] SOS AI multi-agent engine online.",
    `[SYSTEM] Sumber laporan: bot Telegram ${cfg.telegramEnabled ? "aktif" : "mati"} · chain: ${cfg.chainEnabled ? cfg.chainName : "tidak aktif"} · LLM: ${cfg.llmEnabled ? "aktif" : "mati"}`,
  ];
  const r = current();
  if (r) {
    const log = r.log || [];
    if (!isPlaying(r)) lines.push(...log);
    else {
      // saat diputar: tampilkan log sampai tahap yang sedang berjalan saja
      let stageIdx = -1;
      for (const line of log) {
        if (line.startsWith(">>> [")) stageIdx = STAGES.findIndex((s) => s.match && line.includes(s.match));
        const isChainLine = line.startsWith("[CHAIN]") || line.startsWith("[OPERATOR]") || line.startsWith("[PELAPOR]") || line.includes("Pipeline selesai");
        const visible = isChainLine ? app.play.idx >= STAGES.length - 1 : stageIdx <= app.play.idx && (stageIdx < app.play.idx || app.play.ready || stageIdx === -1);
        if (visible) lines.push(line);
      }
      if (!log.length) lines.push(`[TG_INBOUND] Laporan ${r.code} masuk, menunggu hasil agent…`);
    }
  }
  const key = lines.join("\n");
  if (key === app.telemetryKey) return;
  app.telemetryKey = key;
  el.innerHTML = lines
    .map((line) => {
      let cls = "";
      if (line.startsWith(">>>")) cls = "t-agent";
      else if (line.startsWith("[CHAIN]") || line.startsWith("[OPERATOR]") || line.startsWith("[PELAPOR]")) cls = "t-chain";
      if (line.includes("PERINGATAN") || line.includes("GAGAL") || line.includes("TIDAK VALID")) cls = "t-warn";
      const m = !cls && line.match(/^(\[[A-Za-z_ ]+\])(.*)$/);
      return m ? `<span class="t-tag">${esc(m[1])}</span>${esc(m[2])}` : `<span class="${cls}">${esc(line)}</span>`;
    })
    .join("\n");
  el.scrollTop = el.scrollHeight;
}

async function resync() {
  const r = current();
  if (!r) return;
  try {
    toast("Mencatat ulang ke blockchain…");
    await api(`/api/reports/${r.code}/sync`, { method: "POST" });
    await loadReports();
  } catch (e) {
    toast(e.message, true);
  }
}

// ----------------------------------------------------------------- wallet
function nativeCurrency() {
  const symbol = app.cfg.chainId === 97 || app.cfg.chainId === 56 ? "tBNB" : "ETH";
  return { name: symbol, symbol, decimals: 18 };
}

async function ensureChain() {
  const chainIdHex = "0x" + app.cfg.chainId.toString(16);
  // sudah di jaringan yang benar: jangan minta wallet berpindah lagi (mengurangi popup)
  const currentChain = await window.ethereum.request({ method: "eth_chainId" });
  if (String(currentChain).toLowerCase() === chainIdHex) return;
  try {
    await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: chainIdHex }] });
  } catch (e) {
    // 4902 = jaringan belum ada di wallet, jadi tambahkan dulu
    const code = e.code || (e.data && e.data.originalError && e.data.originalError.code);
    if (code !== 4902) throw e;
    const params = { chainId: chainIdHex, chainName: app.cfg.chainName, rpcUrls: [app.cfg.rpcUrl], nativeCurrency: nativeCurrency() };
    if (app.cfg.explorerUrl) params.blockExplorerUrls = [app.cfg.explorerUrl];
    try {
      await window.ethereum.request({ method: "wallet_addEthereumChain", params: [params] });
    } catch (addError) {
      if (addError.code === 4001) throw addError;
      // MetaMask menolak RPC non-HTTPS (node lokal) lewat cara otomatis: harus ditambah manual
      throw new Error(
        `Tambahkan jaringan ini secara manual di wallet: RPC ${app.cfg.rpcUrl}, Chain ID ${app.cfg.chainId}. Lalu pilih jaringan itu dan klik hubungkan lagi.`
      );
    }
  }
}

let connecting = false;

async function connectWallet() {
  if (!window.ethereum) {
    toast("Wallet tidak ditemukan. Pasang ekstensi MetaMask di browser ini, lalu muat ulang halaman.", true);
    return;
  }
  // cegah dua permintaan ke wallet berjalan bersamaan
  if (connecting) return;
  connecting = true;
  try {
    await window.ethereum.request({ method: "eth_requestAccounts" });
    await ensureChain();
    const provider = new ethers.BrowserProvider(window.ethereum);
    app.signer = await provider.getSigner();
    app.wallet = await app.signer.getAddress();
    app.isOperator = await app.readContract.isOperator(app.wallet);
  } catch (e) {
    app.signer = null;
    app.wallet = null;
    app.isOperator = false;
    toast(explain(e), true);
  } finally {
    connecting = false;
  }
  renderWalletBtn();
  renderDrawer();
  renderDetail();
}

function renderWalletBtn() {
  const btn = $("walletBtn");
  btn.classList.toggle("connected", !!app.wallet);
  btn.textContent = app.wallet ? `${short(app.wallet)}${app.isOperator ? " · operator" : ""}` : "Hubungkan wallet";
}

/** "Logout" di aplikasi web3 = memutuskan wallet dari situs ini. */
async function disconnectWallet() {
  try {
    // didukung MetaMask versi baru; kalau tidak didukung, cukup lupakan wallet di sisi dashboard
    await window.ethereum.request({ method: "wallet_revokePermissions", params: [{ eth_accounts: {} }] });
  } catch (_) { /* abaikan */ }
  app.signer = null;
  app.wallet = null;
  app.isOperator = false;
  logout(); // hapus sesi dan kembali ke beranda
}


function explain(e) {
  if (!e) return "Terjadi kesalahan.";
  if (e.code === "ACTION_REJECTED" || e.code === 4001) return "Dibatalkan di wallet.";
  const revert = e.revert && e.revert.name;
  const known = {
    NotOperator: "Wallet ini bukan operator di contract.",
    PlanMismatch: "Rencana di layar tidak sama dengan yang tercatat on-chain. Muat ulang halaman.",
    WrongStatus: "Status laporan di blockchain sudah berubah. Muat ulang halaman.",
  };
  if (revert && known[revert]) return known[revert];
  if (e.code === "INSUFFICIENT_FUNDS") return "Saldo wallet tidak cukup untuk biaya gas. Isi dari faucet testnet.";
  return e.shortMessage || e.message || String(e);
}

async function decide(approve) {
  const r = current();
  if (!r || app.busy) return;
  try {
    if (!app.cfg.chainEnabled) {
      app.busy = "Menyimpan keputusan…";
      renderDetail();
      await api(`/api/reports/${r.code}/decide-offchain`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve }),
      });
    } else {
      app.busy = "Menunggu tanda tangan di wallet. Buka MetaMask dan tekan Confirm…";
      renderDetail();
      await ensureChain();
      const contract = new ethers.Contract(app.cfg.contractAddress, app.cfg.abi, app.signer);
      const tx = approve
        ? await contract.approveDispatch(r.report_id, r.plan_hash)
        : await contract.rejectReport(r.report_id);
      app.busy = `Transaksi ${short(tx.hash, 10, 6)} terkirim, menunggu masuk blok…`;
      renderDetail();
      await tx.wait();
      // hash transaksi keputusan ikut dikirim supaya bisa ditautkan di garis waktu on-chain
      await api(`/api/reports/${r.code}/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tx_hash: tx.hash }),
      });
    }
    toast(approve ? "Laporan disetujui." : "Laporan ditolak.");
  } catch (e) {
    toast(explain(e), true);
  }
  app.busy = null;
  await loadReports();
}

// ------------------------------------------------------------------- peta
// Pos asal bantuan untuk satu laporan. Laporan dari versi lama belum menyimpan pos asal,
// jadi titik pertama rutenya yang dipakai.
function originOf(r) {
  const v = r && r.verification;
  if (!v || !v.valid) return null;
  if (v.origin && v.origin.lat != null) return v.origin;
  const path = v.route && v.route.path;
  if (path && path.length > 1) return { name: (r.plan && r.plan.origin) || "Pos asal", lat: path[0][0], lon: path[0][1] };
  return null;
}

function initMap() {
  // tampilan awal: seluruh Indonesia. Begitu ada laporan, peta menyesuaikan sendiri.
  app.map = L.map("map").setView([-2.5, 118], 5);

  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 18, attribution: "Citra satelit &copy; Esri, Maxar, Earthstar Geographics" }
  );
  const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; kontributor OpenStreetMap",
  });
  satellite.addTo(app.map);
  L.control.layers({ Satelit: satellite, "Peta jalan": streets }, null, { collapsed: true }).addTo(app.map);

  // semua pos asal bantuan (kantor BPBD) sebagai titik kecil; pos yang dipakai laporan
  // terpilih digambar lebih jelas di renderOverlay()
  for (const s of app.cfg.staging || []) {
    L.marker([s.lat, s.lon], {
      icon: L.divIcon({ className: "", html: '<div class="post"></div>', iconSize: [8, 8], iconAnchor: [4, 4] }),
      keyboard: false,
      zIndexOffset: -1000,
    })
      .bindTooltip(s.name)
      .addTo(app.map);
  }

  app.overlayLayer = L.layerGroup().addTo(app.map);
  app.pinLayer = L.layerGroup().addTo(app.map);
}

function renderPins() {
  if (!app.pinLayer) return; // peta gagal dibuat
  app.pinLayer.clearLayers();
  const points = [];
  for (const r of app.reports) {
    const { lat, lon } = r.payload.geo;
    points.push([lat, lon]);
    const o = originOf(r);
    if (o) points.push([o.lat, o.lon]);
    const selected = r.code === app.selected ? " selected" : "";
    L.marker([lat, lon], {
      icon: L.divIcon({ className: "", html: `<div class="pin ${levelClass(r)}${selected}"></div>`, iconSize: [16, 16], iconAnchor: [8, 8] }),
      zIndexOffset: selected ? 1000 : 0,
    })
      .bindTooltip(`${placeName(r)} · ${STATUS_LABEL[r.status] || r.status}`)
      .on("click", () => select(r.code))
      .addTo(app.pinLayer);
  }
  if (!app.fitted && points.length) {
    app.map.fitBounds(points, { paddingTopLeft: [40, 70], paddingBottomRight: [40, 220], maxZoom: 13 });
    app.fitted = true;
  }
}

function renderMapChips() {
  const r = current();
  const target = $("mapTarget");
  const weather = $("mapWeather");
  if (!r) { target.hidden = true; weather.hidden = true; return; }
  target.hidden = false;
  target.innerHTML = `<strong>${esc(placeName(r))}</strong><span>${esc(r.payload.geo.lat)}, ${esc(r.payload.geo.lon)}</span>`;
  const w = r.verification && r.verification.weather;
  // cuaca hanya ditampilkan kalau datanya benar-benar ada, dan baru setelah agent 2 selesai diputar
  const reveal = !isPlaying(r) || app.play.idx > 1;
  if (w && w.ok && reveal) {
    weather.hidden = false;
    weather.textContent = `Hujan 24 jam ${w.rain_24h_mm} mm, angin ${w.wind_kmh} km/jam, ${w.temperature_c}°C`;
  } else weather.hidden = true;
}

function renderOverlay() {
  if (!app.overlayLayer) return;
  app.overlayLayer.clearLayers();
  const r = current();
  const v = r && r.verification;
  if (!v || !v.valid) return;
  if (isPlaying(r) && app.play.idx <= 1) return; // rute muncul setelah agent verifikasi selesai
  if (v.route && v.route.path && v.route.path.length > 1) {
    // garis putus-putus = rute perkiraan (OSRM tidak tersedia)
    L.polyline(v.route.path, { color: "#0b1020", weight: 7, opacity: 0.9 }).addTo(app.overlayLayer);
    L.polyline(v.route.path, { color: "#ffffff", weight: 3, dashArray: v.route.ok ? null : "6 8" }).addTo(app.overlayLayer);
  }
  const o = originOf(r);
  if (o) {
    const trip = v.route ? ` · ${v.route.distance_km} km, ${v.route.duration_min} menit${v.route.ok ? "" : " (perkiraan)"}` : "";
    L.marker([o.lat, o.lon], {
      icon: L.divIcon({ className: "", html: '<div class="post active">P</div>', iconSize: [20, 20], iconAnchor: [10, 10] }),
      keyboard: false,
      zIndexOffset: 500,
    })
      .bindTooltip(`Pos asal: ${o.name}${trip}`)
      .addTo(app.overlayLayer);
  }
  for (const q of (v.earthquakes && v.earthquakes.events) || []) {
    if (q.lat == null) continue;
    L.circleMarker([q.lat, q.lon], { radius: 4 + (q.magnitude || 4) * 2, color: "#ffffff", weight: 2, fillOpacity: 0.1 })
      .bindTooltip(`Gempa M${q.magnitude}, ${q.time_utc} UTC`)
      .addTo(app.overlayLayer);
  }
}

function focusOnMap() {
  const r = current();
  if (!r || !app.pinLayer) return;
  const here = [r.payload.geo.lat, r.payload.geo.lon];
  const o = originOf(r);
  // laporan yang ditolak tidak punya pos asal: cukup dekati titik laporannya
  app.map.fitBounds(o ? [[o.lat, o.lon], here] : [here, here], {
    paddingTopLeft: [50, 80],
    paddingBottomRight: [50, 230],
    maxZoom: o ? 14 : 12,
  });
}

init();
