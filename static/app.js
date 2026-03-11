const $ = (id) => document.getElementById(id);

const state = {
  status: {},
  sample: {},
  ports: [],
};

function fmt(v, digits = 3) {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return v.toFixed(digits);
  return String(v);
}

async function api(path, method = 'POST', body = null) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `${res.status}`);
  }
  return res.json();
}

function fillPorts(ports) {
  state.ports = ports || [];
  ['pump-port', 'starter-port', 'psu-port'].forEach(id => {
    const sel = $(id);
    const old = sel.value;
    sel.innerHTML = '';
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '-- select port --';
    sel.appendChild(empty);
    for (const p of state.ports) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    }
    if (state.ports.includes(old)) sel.value = old;
  });
}

async function refreshPorts() {
  const data = await fetch('/api/ports').then(r => r.json());
  fillPorts(data.ports || []);
}

function updateStatus(status) {
  if (!status) return;
  state.status = { ...state.status, ...status };
  $('stage-badge').textContent = `stage: ${state.status.stage || '-'}`;
  $('log-path').textContent = state.status.log_path || '-';
  const pp = state.status.pump_profile || {};
  $('pump-profile-status').textContent = `${pp.active ? 'active' : 'idle'} ${pp.path || ''}`.trim();

  const connected = state.status.connected || {};
  $('pump-connected').textContent = connected.pump ? 'yes' : 'no';
  $('starter-connected').textContent = connected.starter ? 'yes' : 'no';
  $('psu-output').textContent = (state.sample.psu || {}).output ? 'on' : 'off';

  $('status-box').textContent = JSON.stringify(state.status, null, 2);
}

function updateSample(sample) {
  if (!sample) return;
  state.sample = sample;

  const pump = sample.pump || {};
  const starter = sample.starter || {};
  const psu = sample.psu || {};
  const connected = sample.connected || {};

  $('pump-connected').textContent = connected.pump ? 'yes' : 'no';
  $('starter-connected').textContent = connected.starter ? 'yes' : 'no';
  $('pump-rpm-mech').textContent = fmt(pump.rpm_mech, 1);
  $('pump-duty-now').textContent = fmt(pump.duty, 3);
  $('pump-current').textContent = fmt(pump.current_motor, 2);
  $('pump-cmd-mode').textContent = fmt(pump.cmd_mode, 0);
  $('pump-cmd-value').textContent = fmt(pump.cmd_value, 2);
  $('pump-cmd-rpm').textContent = fmt(pump.cmd_rpm, 1);
  $('pump-cmd-duty').textContent = fmt(pump.cmd_duty, 3);

  $('starter-rpm-mech').textContent = fmt(starter.rpm_mech, 1);
  $('starter-duty-now').textContent = fmt(starter.duty, 3);
  $('starter-current').textContent = fmt(starter.current_motor, 2);
  $('starter-cmd-mode').textContent = fmt(starter.cmd_mode, 0);
  $('starter-cmd-value').textContent = fmt(starter.cmd_value, 2);
  $('starter-cmd-rpm').textContent = fmt(starter.cmd_rpm, 1);
  $('starter-cmd-duty').textContent = fmt(starter.cmd_duty, 3);

  $('psu-v-set').textContent = fmt(psu.v_set, 2);
  $('psu-i-set').textContent = fmt(psu.i_set, 2);
  $('psu-v-out').textContent = fmt(psu.v_out, 2);
  $('psu-i-out').textContent = fmt(psu.i_out, 2);
  $('psu-p-out').textContent = fmt(psu.p_out, 2);
  $('psu-output').textContent = fmt(psu.output, 0);

  $('telemetry-box').textContent = JSON.stringify(sample, null, 2);
}

function showError(err) {
  $('status-box').textContent = `${$('status-box').textContent}\nERROR: ${err}`.trim();
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    $('ws-badge').textContent = 'WS: online';
    setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 15000);
  };

  ws.onclose = () => {
    $('ws-badge').textContent = 'WS: offline';
    setTimeout(connectWs, 1000);
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'snapshot') {
      const snap = msg.payload || {};
      updateStatus(snap.status || {});
      updateSample(snap.sample || {});
      fillPorts(snap.ports || []);
      return;
    }
    if (msg.event === 'status') updateStatus(msg.payload);
    if (msg.event === 'sample') updateSample(msg.payload);
    if (msg.event === 'error') showError(msg.payload);
  };
}

function bind() {
  $('refresh-ports').onclick = refreshPorts;
  $('save-pp').onclick = () => api('/api/pole-pairs', 'POST', {
    pump: Number($('pp-pump').value),
    starter: Number($('pp-starter').value),
  }).catch(e => showError(e));

  document.querySelector('[data-action="pump-connect"]').onclick = () => api('/api/pump/connect', 'POST', { port: $('pump-port').value }).catch(e => showError(e));
  document.querySelector('[data-action="pump-disconnect"]').onclick = () => api('/api/pump/disconnect').catch(e => showError(e));
  document.querySelector('[data-action="starter-connect"]').onclick = () => api('/api/starter/connect', 'POST', { port: $('starter-port').value }).catch(e => showError(e));
  document.querySelector('[data-action="starter-disconnect"]').onclick = () => api('/api/starter/disconnect').catch(e => showError(e));
  document.querySelector('[data-action="psu-connect"]').onclick = () => api('/api/psu/connect', 'POST', { port: $('psu-port').value }).catch(e => showError(e));
  document.querySelector('[data-action="psu-disconnect"]').onclick = () => api('/api/psu/disconnect').catch(e => showError(e));

  $('cmd-ready').onclick = () => api('/api/ready', 'POST', { prefix: $('ready-prefix').value }).catch(e => showError(e));
  $('cmd-update-reset').onclick = () => api('/api/update-reset').catch(e => showError(e));
  $('cmd-run-cycle').onclick = () => api('/api/run-cycle').catch(e => showError(e));
  $('cmd-cooling').onclick = () => api('/api/cooling-cycle', 'POST', { value: Number($('cooling-duty').value) }).catch(e => showError(e));
  $('cmd-stop-all').onclick = () => api('/api/stop-all').catch(e => showError(e));
  $('cmd-valve-on').onclick = () => api('/api/valve/on').catch(e => showError(e));
  $('cmd-valve-off').onclick = () => api('/api/valve/off').catch(e => showError(e));
  $('cmd-pump-profile-start').onclick = () => api('/api/pump-profile/start', 'POST', { path: $('pump-profile-path').value }).catch(e => showError(e));
  $('cmd-pump-profile-stop').onclick = () => api('/api/pump-profile/stop').catch(e => showError(e));

  $('cmd-pump-rpm').onclick = () => api('/api/pump/rpm', 'POST', { value: Number($('pump-rpm').value) }).catch(e => showError(e));
  $('cmd-pump-duty').onclick = () => api('/api/pump/duty', 'POST', { value: Number($('pump-duty').value) }).catch(e => showError(e));
  $('cmd-starter-rpm').onclick = () => api('/api/starter/rpm', 'POST', { value: Number($('starter-rpm').value) }).catch(e => showError(e));
  $('cmd-starter-duty').onclick = () => api('/api/starter/duty', 'POST', { value: Number($('starter-duty').value) }).catch(e => showError(e));

  $('cmd-psu-vi').onclick = () => api('/api/psu/vi', 'POST', { v: Number($('psu-v').value), i: Number($('psu-i').value) }).catch(e => showError(e));
  $('cmd-psu-on').onclick = () => api('/api/psu/output', 'POST', { value: true }).catch(e => showError(e));
  $('cmd-psu-off').onclick = () => api('/api/psu/output', 'POST', { value: false }).catch(e => showError(e));
}

window.addEventListener('DOMContentLoaded', async () => {
  bind();
  connectWs();
  await refreshPorts();
  const snap = await fetch('/api/state').then(r => r.json());
  updateStatus(snap.status || {});
  updateSample(snap.sample || {});
});
