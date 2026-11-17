/* workshop-file-manager UI */
'use strict';

const $ = id => document.getElementById(id);

// ── state ────────────────────────────────────────────────────────────────────
let connected = false;          // the Multicam controller connection specifically
let machineName = '';
let cwd = '';
let rootDir = '\\hard disk2\\release\\jobs';
let refreshRetryTimer = null;
let selectedFiles = new Set();
let deleteBatchRunning = false;

// The file browser is machine-agnostic: everything below is swapped when you
// change tabs. The Multicam uses backslash paths under a jobs root, the
// Woodtron uses forward-slash FTP paths under "/", and each advertises what it
// can do — so the UI never hardcodes either.
let machines = [];
let currentMachine = 'multicam';
let caps = { folders: true, load: true, upload: true, delete: true };
let sep = '\\';

const MACHINE_STORAGE_KEY = 'fm.currentMachine';

function machineInfo(key) {
  return machines.find(m => m.key === key) || null;
}

function currentMachineLabel() {
  const m = machineInfo(currentMachine);
  return m ? m.label : currentMachine;
}

function currentMachineOnline() {
  const m = machineInfo(currentMachine);
  return m ? !!m.online : false;
}

const STATUS_POLL_MS = 5000;
const WATCHER_POLL_MS = 5000;
const MACHINE_POLL_MS = 15000;

// ── init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Come back to whichever machine you were last looking at. The workshop PC's
  // browser sits open all day; landing on the other machine after every reload
  // is a small, constant irritation.
  try {
    const saved = localStorage.getItem(MACHINE_STORAGE_KEY);
    if (saved) currentMachine = saved;
  } catch { /* private window or blocked storage — the default is fine */ }

  loadRecentMachines();
  checkStatus();
  loadWatchers();
  loadMachines({ initial: true });
  setInterval(checkStatus, STATUS_POLL_MS);
  setInterval(loadWatchers, WATCHER_POLL_MS);
  // Slower than the others: each poll probes a possibly-offline machine.
  setInterval(() => loadMachines(), MACHINE_POLL_MS);

  $('connect-btn').addEventListener('click', onConnect);
  $('disconnect-btn').addEventListener('click', onDisconnect);
  $('refresh-btn').addEventListener('click', () => loadFiles());
  $('up-btn').addEventListener('click', navigateUp);
  $('home-btn').addEventListener('click', navigateHome);
  $('search-btn').addEventListener('click', onSearch);
  $('ip-input').addEventListener('keydown', e => { if (e.key === 'Enter') onConnect(); });
  $('select-all').addEventListener('change', toggleSelectAll);
  $('delete-selected-btn').addEventListener('click', deleteSelectedFiles);

  $('details-toggle').addEventListener('click', () => {
    const btn = $('details-toggle');
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!open));
    $('details-body').hidden = open;
  });

  const drop = $('upload-drop');
  const input = $('upload-input');
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('drag-over');
    uploadFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', () => uploadFiles(input.files));
});

// ── connection ────────────────────────────────────────────────────────────────
// This app runs unattended for weeks, and the page often sits open on the
// workshop screen. Poll so the badge tracks reality: the server auto-connects
// on boot and reconnects by itself when the machine is switched back on, and
// neither of those events would otherwise ever reach an open tab.
async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.connected && !connected) {
      applyConnected(d);
      // Only refresh the listing if the Multicam is the machine on screen.
      if (currentMachine === 'multicam') loadFiles();
    } else if (d.connected) {
      updateConnectionBadge(d);
    } else if (connected) {
      applyDisconnected();
    }
  } catch {}
}

function updateConnectionBadge(info) {
  const badge = $('connection-badge');
  if (!badge) return;
  if (!info || !info.connected) {
    badge.textContent = 'Disconnected';
    badge.className = 'badge badge--warning';
    return;
  }
  const label = info.name || info.ip;
  if (info.link_ok === false) {
    badge.textContent = 'Link down — ' + label;
    badge.className = 'badge badge--warning';
  } else {
    badge.textContent = 'Connected to ' + label;
    badge.className = 'badge badge--ok';
  }
}

async function onConnect() {
  const ip = $('ip-input').value.trim();
  if (!ip) { toast('Enter a controller IP address', 'error'); return; }
  $('connect-btn').textContent = 'Connecting…';
  $('connect-btn').disabled = true;
  try {
    const r = await fetch('/api/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip, name: $('name-input').value.trim() }),
    });
    const d = await r.json();
    if (!r.ok) { toast(d.detail || 'Connection failed', 'error'); return; }
    applyConnected(d);
    if (currentMachine === 'multicam') loadFiles();
    loadRecentMachines();
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  } finally {
    $('connect-btn').textContent = 'Connect';
    $('connect-btn').disabled = false;
  }
}

async function onDisconnect() {
  await fetch('/api/disconnect', { method: 'POST' });
  applyDisconnected();
}

function applyConnected(info) {
  connected = true;
  machineName = info.name || '';
  $('ip-input').value = info.ip || '';
  $('name-input').value = info.name || '';

  $('info-name').textContent   = info.name        || info.ip || '—';
  $('info-ip').textContent     = info.ip          || '—';
  $('info-id').textContent     = info.machine_id  || '—';
  $('info-serial').textContent = info.serial      || '—';
  $('info-os').textContent     = info.os_version  || '—';
  $('info-perm').textContent   = info.permissions || '—';
  $('info-units').textContent  = info.unit_convention || '—';
  $('mi-dot').classList.toggle('is-down', info.link_ok === false);

  $('machine-info').style.display = '';
  $('connect-form').style.display = 'none';

  updateConnectionBadge({ connected: true, ...info });
  // Deliberately does NOT touch the files panel: that panel now serves both
  // machines, and the Multicam's connection state has no bearing on what the
  // Woodtron tab should be showing.
  updateNavButtons();
}

function applyDisconnected() {
  connected = false;
  machineName = '';
  $('machine-info').style.display = 'none';
  $('connect-form').style.display = '';
  loadRecentMachines();

  updateConnectionBadge(null);
  setBusyBanner('');
  if (refreshRetryTimer) {
    clearTimeout(refreshRetryTimer);
    refreshRetryTimer = null;
  }

  // Only blank the file browser if it is the Multicam being shown. Losing the
  // controller connection says nothing about the Woodtron, and wiping its
  // listing because the other machine went offline would be baffling.
  if (currentMachine !== 'multicam') return;

  cwd = '';
  selectedFiles.clear();
  renderPath();
  updateNavButtons();
  updateDeleteSelectedButton();
  $('refresh-btn').disabled = true;
  $('files-table').style.display = 'none';
  $('upload-drop').style.display = 'none';
  $('upload-divider').style.display = 'none';
  $('files-empty').style.display = '';
  $('files-empty').innerHTML = `
    <div class="icon">⚡</div>
    <p style="font-weight:500">Connect to a controller to see files</p>
    <p>Enter the machine IP on the left and click Connect</p>`;
}

// ── machines ──────────────────────────────────────────────────────────────────
async function loadMachines({ initial = false } = {}) {
  let data;
  try {
    const r = await fetch('/api/m');
    if (!r.ok) return;
    data = await r.json();
  } catch { return; }

  machines = data.machines || [];
  if (!machines.length) return;

  // A remembered machine that no longer exists (someone removed the Woodtron
  // from .env) must not leave the browser pointing at nothing.
  if (!machineInfo(currentMachine)) currentMachine = machines[0].key;

  renderMachineTabs();
  if (initial) selectMachine(currentMachine, { force: true });
}

function renderMachineTabs() {
  const bar = $('machine-tabs');
  if (!bar) return;
  // One machine is not a choice — don't show a switcher for it.
  if (machines.length < 2) { bar.innerHTML = ''; return; }

  bar.innerHTML = '';
  for (const m of machines) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'machine-tab';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(m.key === currentMachine));

    const dot = document.createElement('span');
    dot.className = 'machine-tab__dot' + (m.online ? '' : ' machine-tab__dot--off');
    dot.title = m.detail || (m.online ? 'Online' : 'Offline');
    tab.appendChild(dot);
    tab.append(m.label);

    tab.addEventListener('click', () => selectMachine(m.key));
    bar.appendChild(tab);
  }
}

function selectMachine(key, { force = false } = {}) {
  if (!force && key === currentMachine) return;
  currentMachine = key;
  try { localStorage.setItem(MACHINE_STORAGE_KEY, key); } catch { /* not important */ }

  const m = machineInfo(key);
  if (m) {
    caps = m.capabilities || caps;
    sep = m.sep || '\\';
    rootDir = m.root || rootDir;
  }
  // Start at the top of the new machine rather than carrying a path across —
  // the two have nothing in common, path-wise.
  cwd = rootDir;
  selectedFiles.clear();

  renderMachineTabs();
  $('files-title').textContent = 'Job files on ' + currentMachineLabel();
  updateDeleteSelectedButton();
  loadFiles(rootDir);
}

// ── file list ─────────────────────────────────────────────────────────────────
function setBusyBanner(message) {
  const banner = $('busy-banner');
  if (!banner) return;
  if (message) {
    banner.textContent = message;
    banner.classList.add('is-visible');
  } else {
    banner.textContent = '';
    banner.classList.remove('is-visible');
  }
}

function scheduleRefreshRetry(delayMs = 2000) {
  if (refreshRetryTimer) clearTimeout(refreshRetryTimer);
  refreshRetryTimer = setTimeout(() => {
    refreshRetryTimer = null;
    if (connected) loadFiles();
  }, delayMs);
}

async function loadFiles(path) {
  $('refresh-btn').textContent = '↻ Loading…';
  $('refresh-btn').disabled = true;
  const requestedFor = currentMachine;
  try {
    const target = path != null ? clampToRoot(path) : null;
    let url = `/api/m/${encodeURIComponent(currentMachine)}/files`;
    if (target != null) url += '?path=' + encodeURIComponent(target);
    const r = await fetch(url);
    const d = await r.json();
    // Switching machines mid-request is easy to do; a slow reply for the
    // machine you just left must not overwrite the one you're now looking at.
    if (requestedFor !== currentMachine) return;
    if (!r.ok) {
      if (r.status === 503) {
        setBusyBanner(d.detail || 'Controller busy — showing last known file list when available.');
        scheduleRefreshRetry();
        return;
      }
      showFilesError(d.detail || 'Could not load files');
      return;
    }
    cwd = d.cwd || cwd;
    rootDir = d.root || rootDir;
    if (d.sep) sep = d.sep;
    if (d.capabilities) caps = d.capabilities;
    renderPath();
    updateNavButtons();
    renderFiles(d.files);
    // Panel chrome follows the machine now on screen, not the Multicam's
    // connection: the Woodtron has no "connect" step at all.
    $('refresh-btn').disabled = false;
    $('upload-drop').style.display = caps.upload ? '' : 'none';
    $('upload-divider').style.display = caps.upload ? '' : 'none';
    if (d.busy || d.stale) {
      setBusyBanner((d.busy_detail || 'Syncing with controller') + ' — file list may be slightly out of date.');
      scheduleRefreshRetry();
    } else {
      setBusyBanner('');
      if (refreshRetryTimer) {
        clearTimeout(refreshRetryTimer);
        refreshRetryTimer = null;
      }
    }
  } catch (e) {
    if (requestedFor === currentMachine) showFilesError('Error: ' + e.message);
  } finally {
    $('refresh-btn').textContent = '↻ Refresh';
    $('refresh-btn').disabled = false;
  }
}

// A machine that's off is the common case, not an exception — say so in the
// panel instead of firing a toast that vanishes before anyone reads it.
function showFilesError(message) {
  toast(message, 'error');
  $('files-table').style.display = 'none';
  $('upload-drop').style.display = 'none';
  $('upload-divider').style.display = 'none';
  const empty = $('files-empty');
  empty.style.display = '';
  empty.innerHTML = '';
  const icon = document.createElement('div');
  icon.className = 'icon';
  icon.textContent = '⚠';
  empty.appendChild(icon);
  const title = document.createElement('p');
  title.style.fontWeight = '500';
  title.textContent = `Can't reach ${currentMachineLabel()}`;
  empty.appendChild(title);
  const detail = document.createElement('p');
  detail.textContent = message;
  empty.appendChild(detail);
}

function normalizePath(path) {
  if (!path) return '';
  let normalized = String(path).trim();
  if (!normalized.startsWith(sep)) normalized = sep + normalized;
  // Strip trailing separators without letting the path collapse to nothing.
  while (normalized.length > 1 && normalized.endsWith(sep)) {
    normalized = normalized.slice(0, -1);
  }
  return normalized || sep;
}

function pathParts(path) {
  return normalizePath(path).split(sep).filter(Boolean);
}

function isAtRoot() {
  return normalizePath(cwd).toLowerCase() === normalizePath(rootDir).toLowerCase();
}

function clampToRoot(path) {
  const normalized = normalizePath(path);
  const root = normalizePath(rootDir);
  const lower = normalized.toLowerCase();
  const rootLower = root.toLowerCase();
  // When the root IS the separator (the Woodtron's "/"), every path is under
  // it and the prefix test below would need a doubled separator to match.
  if (root === sep) return normalized;
  if (lower === rootLower || lower.startsWith(rootLower + sep)) {
    return normalized;
  }
  return root;
}

function isUnderRoot(path) {
  const normalized = normalizePath(path);
  const root = normalizePath(rootDir);
  if (root === sep) return true;
  const lower = normalized.toLowerCase();
  const rootLower = root.toLowerCase();
  return lower === rootLower || lower.startsWith(rootLower + sep);
}

function navigateUp() {
  if (!cwd || isAtRoot()) return;
  const parts = pathParts(cwd);
  const rootParts = pathParts(rootDir);
  if (parts.length <= rootParts.length) {
    loadFiles(rootDir);
    return;
  }
  parts.pop();
  loadFiles(sep + parts.join(sep));
}

function navigateHome() {
  loadFiles(rootDir);
}

function updateNavButtons() {
  const up = $('up-btn');
  const home = $('home-btn');
  if (!up || !home) return;
  const show = !!cwd && !isAtRoot();
  up.style.display = show ? '' : 'none';
  home.style.display = show ? '' : 'none';
  up.disabled = !show;
  home.disabled = !show;
}

function breadcrumbSegments() {
  const rootParts = pathParts(rootDir);
  const cwdParts = pathParts(cwd);
  // The Woodtron's root is "/", which has no final segment to name it — use
  // the machine's own name so the crumb trail still starts somewhere sensible.
  const rootLabel = rootParts.length ? rootParts[rootParts.length - 1] : currentMachineLabel();
  const segments = [{ label: rootLabel, path: rootDir }];
  if (!isUnderRoot(cwd)) return segments;

  let accumulated = normalizePath(rootDir);
  for (let i = rootParts.length; i < cwdParts.length; i++) {
    accumulated = (accumulated === sep ? '' : accumulated) + sep + cwdParts[i];
    segments.push({ label: cwdParts[i], path: accumulated });
  }
  return segments;
}

function renderPath() {
  const bar = $('path-bar');
  if (!bar) return;
  bar.innerHTML = '';
  if (!cwd) return;

  const segments = breadcrumbSegments();
  for (let i = 0; i < segments.length; i++) {
    if (i > 0) {
      const sep = document.createElement('span');
      sep.className = 'path-sep';
      sep.textContent = '›';
      sep.setAttribute('aria-hidden', 'true');
      bar.appendChild(sep);
    }
    const isCurrent = i === segments.length - 1;
    const crumb = document.createElement('button');
    crumb.type = 'button';
    crumb.className = 'path-crumb' + (isCurrent ? ' path-crumb--current' : '');
    crumb.textContent = segments[i].label;
    if (!isCurrent) {
      const target = segments[i].path;
      crumb.addEventListener('click', () => loadFiles(target));
    }
    bar.appendChild(crumb);
  }
}

function renderFiles(files) {
  const tbody = $('files-tbody');
  tbody.innerHTML = '';
  selectedFiles.clear();
  updateDeleteSelectedButton();
  const selectAllCheckbox = $('select-all');
  selectAllCheckbox.checked = false;
  selectAllCheckbox.indeterminate = false;

  if (!files || files.length === 0) {
    $('files-table').style.display = 'none';
    $('files-empty').style.display = '';
    $('files-empty').innerHTML = `
      <div class="icon">📂</div>
      <p style="font-weight:500">No job files on the controller</p>
      <p>Upload a .nc file below</p>`;
    return;
  }

  $('files-empty').style.display = 'none';
  $('files-table').style.display = '';

  // Folders first, then files; folders navigate, files act.
  const dirs = files.filter(f => f.is_dir);
  const docs = files.filter(f => !f.is_dir);

  if (!isAtRoot()) {
    const up = document.createElement('tr');
    up.className = 'frow-dir';
    up.innerHTML = `<td></td><td class="fname">↑ Parent folder</td><td></td><td></td><td></td>`;
    up.addEventListener('click', navigateUp);
    tbody.appendChild(up);
  }

  for (const f of dirs) {
    const tr = document.createElement('tr');
    tr.className = 'frow-dir';

    const checkboxTd = document.createElement('td');  // folders aren't part of batch select
    tr.appendChild(checkboxTd);

    const nameTd = document.createElement('td');
    nameTd.className = 'fname';
    nameTd.textContent = '📁 ' + f.name;
    tr.appendChild(nameTd);

    const sizeTd = document.createElement('td');
    sizeTd.className = 'fsize';
    sizeTd.textContent = '—';
    tr.appendChild(sizeTd);

    const dateTd = document.createElement('td');
    dateTd.className = 'fdate';
    dateTd.textContent = '—';
    tr.appendChild(dateTd);

    const actionsTd = document.createElement('td');
    actionsTd.className = 'factions';
    const delBtn = document.createElement('button');
    delBtn.className = 'button button--sm button--danger';
    delBtn.title = 'Delete this folder and everything inside it';
    delBtn.textContent = '✕';
    // Stop the row's navigate-on-click from firing when deleting.
    delBtn.addEventListener('click', (e) => { e.stopPropagation(); deleteFolder(f.name, delBtn); });
    delBtn.disabled = !caps.delete;
    actionsTd.appendChild(delBtn);
    tr.appendChild(actionsTd);

    tr.addEventListener('click', () => loadFiles(clampToRoot(f.path)));
    tbody.appendChild(tr);
  }

  for (const f of docs) {
    const tr = document.createElement('tr');

    const checkboxTd = document.createElement('td');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.dataset.name = f.name;
    checkbox.addEventListener('change', (e) => {
      e.stopPropagation();
      toggleFileSelection(f.name);
    });
    checkboxTd.appendChild(checkbox);
    tr.appendChild(checkboxTd);

    const nameTd = document.createElement('td');
    nameTd.className = 'fname';
    nameTd.textContent = f.name;
    tr.appendChild(nameTd);

    const sizeTd = document.createElement('td');
    sizeTd.className = 'fsize';
    sizeTd.textContent = f.size ? fmtBytes(f.size) : '—';
    tr.appendChild(sizeTd);

    const dateTd = document.createElement('td');
    dateTd.className = 'fdate';
    dateTd.textContent = f.modified || '—';
    tr.appendChild(dateTd);

    // Built as nodes with listeners rather than an onclick="…" string: job
    // names routinely contain apostrophes and quotes ("Bob's job.nc"), which
    // silently broke the generated handler — and would have let a file name
    // inject script into the page.
    const actionsTd = document.createElement('td');
    actionsTd.className = 'factions';

    // Only machines with a "current job" concept get a Load button. The
    // Woodtron has none — the operator picks the file at the machine — so
    // showing a dead button there would just invite the question.
    if (caps.load) {
      const loadBtn = document.createElement('button');
      loadBtn.className = 'button button--ghost button--sm';
      loadBtn.title = 'Load onto the machine — sets the current job (operator still presses Run)';
      loadBtn.textContent = '⤓ Load' + (machineName ? ' on ' + machineName : '');
      loadBtn.addEventListener('click', () => loadToMachine(f.name, loadBtn));
      actionsTd.appendChild(loadBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'button button--sm button--danger';
    delBtn.title = 'Delete from the controller';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', () => deleteFile(f.name, delBtn));
    actionsTd.appendChild(delBtn);

    tr.appendChild(actionsTd);

    tbody.appendChild(tr);
  }
}

// ── upload ────────────────────────────────────────────────────────────────────
async function uploadFiles(fileList) {
  // Upload into the folder currently on screen, on the machine currently
  // selected — both are captured up front so switching tabs mid-upload can't
  // redirect files to the wrong machine.
  const target = currentMachine;
  const targetPath = cwd;
  const label = currentMachineLabel();
  for (const file of fileList) {
    if (!file.name.toLowerCase().endsWith('.nc')) {
      toast(`${file.name} is not a .nc file — skipped`, 'error');
      continue;
    }
    const fd = new FormData();
    fd.append('file', file);
    fd.append('path', targetPath);
    try {
      const r = await fetch(`/api/m/${encodeURIComponent(target)}/upload`, {
        method: 'POST', body: fd,
      });
      const d = await r.json();
      if (r.ok) {
        toast(`Uploaded ${file.name} (${fmtBytes(d.bytes)}) to ${label}`, 'ok');
      } else {
        toast(d.detail || `Upload failed: ${file.name}`, 'error');
      }
    } catch (e) {
      toast(`Upload error: ${e.message}`, 'error');
    }
  }
  $('upload-input').value = '';
  if (target === currentMachine) loadFiles(targetPath);
}

// ── selection management ─────────────────────────────────────────────────────────
function toggleFileSelection(filename) {
  if (selectedFiles.has(filename)) {
    selectedFiles.delete(filename);
  } else {
    selectedFiles.add(filename);
  }
  updateSelectAllCheckbox();
  updateDeleteSelectedButton();
}

function toggleSelectAll() {
  const checkbox = $('select-all');
  if (checkbox.checked) {
    // Select all files (not folders)
    const tbody = $('files-tbody');
    const rows = tbody.querySelectorAll('tr:not(.frow-dir)');
    rows.forEach(row => {
      const firstTd = row.querySelector('td:first-child');
      const input = firstTd.querySelector('input[type="checkbox"]');
      if (input && !input.checked) {
        const filename = input.dataset.name;
        selectedFiles.add(filename);
        input.checked = true;
      }
    });
  } else {
    selectedFiles.clear();
    const tbody = $('files-tbody');
    const inputs = tbody.querySelectorAll('input[type="checkbox"]');
    inputs.forEach(input => { input.checked = false; });
  }
  updateDeleteSelectedButton();
}

function updateSelectAllCheckbox() {
  const checkbox = $('select-all');
  const tbody = $('files-tbody');
  const rows = tbody.querySelectorAll('tr:not(.frow-dir)');
  if (rows.length === 0) {
    checkbox.checked = false;
    checkbox.indeterminate = false;
    return;
  }
  const checkedCount = selectedFiles.size;
  const totalCount = rows.length;
  if (checkedCount === totalCount) {
    checkbox.checked = true;
    checkbox.indeterminate = false;
  } else if (checkedCount === 0) {
    checkbox.checked = false;
    checkbox.indeterminate = false;
  } else {
    checkbox.checked = false;
    checkbox.indeterminate = true;
  }
}

function updateDeleteSelectedButton() {
  if (deleteBatchRunning) return;
  const btn = $('delete-selected-btn');
  const count = selectedFiles.size;
  btn.textContent = `✕ Delete selected (${count})`;
  if (count > 0) {
    btn.style.display = '';
    btn.disabled = false;
  } else {
    btn.style.display = 'none';
    btn.disabled = true;
  }
}

// ── confirm dialog ──────────────────────────────────────────────────────────────
async function showConfirm({ title, body, confirmLabel, danger }) {
  const overlay = $('confirm-overlay');
  const titleEl = $('confirm-title');
  const bodyEl = $('confirm-body');
  const okBtn = $('confirm-ok');
  const cancelBtn = $('confirm-cancel');

  titleEl.textContent = title;
  if (typeof body === 'string') {
    bodyEl.textContent = body;
  } else {
    bodyEl.innerHTML = '';
    bodyEl.appendChild(body);
  }
  okBtn.textContent = confirmLabel;
  okBtn.className = 'button' + (danger ? ' button--danger' : ' button--primary');

  overlay.classList.add('is-open');

  return new Promise(resolve => {
    const cleanup = () => {
      overlay.classList.remove('is-open');
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onEsc);
      overlay.removeEventListener('click', onBackdrop);
    };

    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    const onEsc = (e) => { if (e.key === 'Escape') { cleanup(); resolve(false); } };
    const onBackdrop = (e) => { if (e.target === overlay) { cleanup(); resolve(false); } };

    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    document.addEventListener('keydown', onEsc);
    overlay.addEventListener('click', onBackdrop);
  });
}

// ── load onto machine ───────────────────────────────────────────────────────────
async function loadToMachine(filename, btn) {
  const ok = await showConfirm({
    title: 'Load job to machine?',
    body: `"${filename}" will become the controller's current job. The operator still presses Run at the machine.`,
    confirmLabel: machineName ? `Load on ${machineName}` : 'Load'
  });
  if (!ok) return;
  const prev = btn.textContent;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch(fileUrl('load', filename), { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      toast(d.message || `Loaded ${filename}`, 'ok');
    } else {
      toast(d.detail || 'Load failed', 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  } finally {
    btn.textContent = prev;
    btn.disabled = false;
  }
}

// Build a per-file URL for the selected machine, carrying the folder we're
// looking at. Every file operation goes through here so none of them can
// forget the machine or the path.
function fileUrl(kind, filename, extra = {}) {
  const base = `/api/m/${encodeURIComponent(currentMachine)}`;
  const params = new URLSearchParams({ path: cwd, ...extra });
  const name = encodeURIComponent(filename);
  if (kind === 'files') return `${base}/files/${name}?${params}`;
  return `${base}/${kind}/${name}?${params}`;
}

// ── delete ────────────────────────────────────────────────────────────────────
async function deleteFile(filename, btn) {
  const ok = await showConfirm({
    title: 'Delete file?',
    body: `Delete "${filename}" from ${currentMachineLabel()}? This can't be undone.`,
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch(fileUrl('files', filename), { method: 'DELETE' });
    const d = await r.json();
    if (r.ok) {
      toast(`Deleted ${filename}`, 'ok');
      loadFiles();
    } else {
      toast(d.detail || 'Delete failed', 'error');
      btn.textContent = '✕';
      btn.disabled = false;
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
    btn.textContent = '✕';
    btn.disabled = false;
  }
}

async function deleteFolder(foldername, btn) {
  const ok = await showConfirm({
    title: 'Delete folder?',
    body: `Delete "${foldername}" and everything inside it from ${currentMachineLabel()}? This can't be undone.`,
    confirmLabel: 'Delete folder',
    danger: true,
  });
  if (!ok) return;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch(fileUrl('files', foldername, { is_dir: 'true' }), { method: 'DELETE' });
    const d = await r.json();
    if (r.ok) {
      toast(`Deleted folder ${foldername}`, 'ok');
      loadFiles();
    } else {
      toast(d.detail || 'Delete failed', 'error');
      btn.textContent = '✕';
      btn.disabled = false;
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
    btn.textContent = '✕';
    btn.disabled = false;
  }
}

async function deleteSelectedFiles() {
  if (deleteBatchRunning || selectedFiles.size === 0) return;

  const fileList = document.createElement('ul');
  fileList.style.fontFamily = 'var(--font-mono)';
  fileList.style.fontSize = '0.85rem';
  fileList.style.maxHeight = '300px';
  fileList.style.overflowY = 'auto';
  fileList.style.margin = '0';
  fileList.style.paddingLeft = '20px';
  fileList.style.lineHeight = '1.6';

  const sortedNames = Array.from(selectedFiles).sort();
  for (const name of sortedNames) {
    const li = document.createElement('li');
    li.textContent = name;
    fileList.appendChild(li);
  }

  const count = selectedFiles.size;
  const ok = await showConfirm({
    title: `Delete ${count} file${count === 1 ? '' : 's'} from ${currentMachineLabel()}?`,
    body: fileList,
    confirmLabel: `Delete ${count}`,
    danger: true
  });
  if (!ok) return;

  deleteBatchRunning = true;
  const btn = $('delete-selected-btn');
  const originalLabel = btn.textContent;
  btn.disabled = true;

  const filesToDelete = Array.from(selectedFiles);
  const failures = [];

  for (let i = 0; i < filesToDelete.length; i++) {
    const filename = filesToDelete[i];
    btn.textContent = `Deleting ${i + 1}/${filesToDelete.length}…`;
    try {
      const r = await fetch(fileUrl('files', filename), { method: 'DELETE' });
      if (!r.ok) {
        failures.push(filename);
      }
    } catch (e) {
      failures.push(filename);
    }
  }

  deleteBatchRunning = false;
  btn.textContent = originalLabel;
  btn.disabled = false;

  selectedFiles.clear();
  updateDeleteSelectedButton();

  const deleted = filesToDelete.length - failures.length;
  if (failures.length === 0) {
    toast(`Deleted ${deleted} file${deleted === 1 ? '' : 's'}`, 'ok');
  } else {
    const failureList = failures.join(', ');
    toast(`Deleted ${deleted}, failed ${failures.length}: ${failureList}`, 'error');
  }

  loadFiles();
}

// ── subnet search ─────────────────────────────────────────────────────────────
async function onSearch() {
  // No IP needed: the server falls back to this PC's own subnet, which is the
  // whole point of Search — you use it when you don't know the address.
  const ip = $('ip-input').value.trim();
  $('search-btn').textContent = '…';
  $('search-btn').disabled = true;
  try {
    const r = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip }),
    });
    const d = await r.json();
    if (!r.ok) { toast(d.detail || 'Search failed', 'error'); return; }
    if (d.found.length === 0) {
      toast(`No controllers found on ${d.subnet || 'this subnet'}`, 'error');
    } else {
      toast(`Found: ${d.found.join(', ')}`, 'ok');
      if (d.found.length === 1) $('ip-input').value = d.found[0];
    }
  } catch (e) {
    toast('Search error: ' + e.message, 'error');
  } finally {
    $('search-btn').textContent = '⌕';
    $('search-btn').disabled = false;
  }
}

// ── watch folders ─────────────────────────────────────────────────────────────
// The auto-send folders are the part of this system nobody watches until a job
// doesn't arrive. This panel answers "did it go?" without reading a log file:
// per machine, whether the Drive folder is visible, what was sent last, what's
// queued, and why anything is stuck.
async function loadWatchers() {
  let data;
  try {
    const r = await fetch('/api/watchers');
    if (!r.ok) return;
    data = await r.json();
  } catch { return; }

  const panel = $('watchers-panel');
  const list = $('watchers-list');
  if (!panel || !list) return;

  const watchers = data.watchers || [];
  if (watchers.length === 0) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = '';
  list.innerHTML = '';
  for (const w of watchers) list.appendChild(renderWatcher(w));
}

// Shorten a Google Drive path for display: the useful part is the last couple
// of folders, not the "\.shortcut-targets-by-id\1zMTCdWG…" prefix. The full
// path stays in the tooltip for when it does matter (e.g. it stopped resolving).
function shortPath(path) {
  const parts = String(path).split('\\').filter(Boolean);
  if (parts.length <= 2) return path;
  return '…\\' + parts.slice(-2).join('\\');
}

function renderWatcher(w) {
  const card = document.createElement('div');
  card.className = 'watcher';

  const head = document.createElement('div');
  head.className = 'watcher__head';

  const name = document.createElement('span');
  name.className = 'watcher__name';
  name.textContent = w.label;
  head.appendChild(name);

  const state = watcherState(w);
  const pill = document.createElement('span');
  pill.className = 'badge badge--' + state.tone;
  pill.textContent = state.label;
  head.appendChild(pill);
  card.appendChild(head);

  const dir = document.createElement('div');
  dir.className = 'watcher__dir';
  // Wrapped in a bidi isolate: the element uses direction:rtl so the ellipsis
  // falls at the START of the path, and without this the leading "…\" would be
  // reordered to the wrong end.
  dir.textContent = '⁦' + shortPath(w.directory) + '⁩';
  dir.title = `${w.directory}\n→ ${w.target}`
    + (w.archive_dir ? `\nDelivered files move to: ${w.archive_dir}\\` : '');
  card.appendChild(dir);

  if (w.last_sent_name) {
    const last = document.createElement('div');
    last.className = 'watcher__last';
    last.append('Last sent ');
    const b = document.createElement('b');
    b.textContent = w.last_sent_name;
    last.appendChild(b);
    last.append(' · ' + (w.last_sent_at || '').slice(11));
    card.appendChild(last);
  }

  // Counts are only worth the line when they say something. "0 waiting" on a
  // healthy watcher is noise; a non-zero count is the whole story.
  const bits = [];
  if (w.pending) bits.push(`${w.pending} waiting`);
  if (w.failed) bits.push(`${w.failed} failed`);
  // A file stuck between "delivered" and "filed away" is worth surfacing: it
  // reached the machine, but the folder still shows it as outstanding work.
  if (w.awaiting_move) bits.push(`${w.awaiting_move} to file away`);
  // Deliberately no "N sent" total: it resets on every restart, so it measures
  // uptime rather than throughput, and "Last sent" above already says the one
  // thing anyone actually checks.
  if (bits.length) {
    const stats = document.createElement('div');
    stats.className = 'watcher__stats';
    stats.textContent = bits.join(' · ');
    card.appendChild(stats);
  }

  if (state.detail) {
    const detail = document.createElement('div');
    detail.className = 'watcher__detail';
    detail.textContent = state.detail;
    card.appendChild(detail);
  }

  // Name what's stuck. A bare "3 waiting" sends someone to the workshop to
  // guess; the names tell them whether it's the job they care about.
  if (w.pending_names && w.pending_names.length) {
    const list = document.createElement('ul');
    list.className = 'watcher__pending';
    for (const n of w.pending_names.slice(0, 6)) {
      const li = document.createElement('li');
      li.textContent = '• ' + n;
      list.appendChild(li);
    }
    if (w.pending > 6) {
      const li = document.createElement('li');
      li.textContent = `• …and ${w.pending - 6} more`;
      list.appendChild(li);
    }
    card.appendChild(list);
  }

  // Two is enough to show it's alive without the card growing past the fold.
  // The full history is in C:\CNCFileManager\logs\file-manager.log.
  const events = (w.events || []).slice(0, 2);
  if (events.length) {
    const feed = document.createElement('ul');
    feed.className = 'watcher__feed';
    for (const ev of events) {
      const li = document.createElement('li');
      li.className = 'watcher__event watcher__event--' + ev.level;
      li.textContent = `${ev.time.slice(11)}  ${ev.message}`;
      feed.appendChild(li);
    }
    card.appendChild(feed);
  }

  return card;
}

function watcherState(w) {
  if (!w.folder_available) {
    return {
      tone: 'warning',
      label: 'Folder missing',
      detail: 'Google Drive may still be starting, or the folder was moved or renamed.',
    };
  }
  if (w.waiting_reason) {
    return { tone: 'warning', label: 'Waiting', detail: w.waiting_reason };
  }
  return {
    tone: 'ok',
    label: 'Watching',
    detail: w.last_error ? `Last error — ${w.last_error} (${w.last_error_at})` : '',
  };
}

// ── recent machines ───────────────────────────────────────────────────────────
async function loadRecentMachines() {
  try {
    const r = await fetch('/api/machines');
    const d = await r.json();
    const list = $('recent-list');
    list.innerHTML = '';
    if (!d.machines || d.machines.length === 0) {
      list.innerHTML = '<p style="font-size:0.82rem;color:var(--ink-faint)">None yet</p>';
      return;
    }
    for (const m of d.machines) {
      const btn = document.createElement('button');
      btn.className = 'machine-btn';
      btn.innerHTML = `<div><div class="m-name">${esc(m.name || m.ip)}</div><div class="m-ip">${esc(m.ip)}</div></div>`;
      btn.addEventListener('click', () => {
        $('ip-input').value = m.ip;
        $('name-input').value = m.name || '';
        onConnect();
      });
      list.appendChild(btn);
    }
  } catch {}
}

// ── utils ─────────────────────────────────────────────────────────────────────
function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, type = '') {
  const c = $('toast-container');
  const t = document.createElement('div');
  t.className = 'toast' + (type ? ' toast--' + type : '');
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// expose for inline onclick handlers
window.loadToMachine = loadToMachine;
window.deleteFile    = deleteFile;
window.showConfirm   = showConfirm;
window.toggleSelectAll = toggleSelectAll;
window.deleteSelectedFiles = deleteSelectedFiles;
