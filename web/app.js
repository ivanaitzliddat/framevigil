const $ = (id) => document.getElementById(id);
const ui = {
  video: $('preview'), device: $('device-select'), start: $('start-button'), stop: $('stop-button'),
  scan: $('scan-button'), auto: $('auto-scan'), interval: $('interval-select'), rules: $('rules-input'),
  save: $('save-rules'), events: $('events-list'), notice: $('notice'), target: $('rules-target'),
  hostDevice: $('host-device'), hostName: $('host-name'), hostAdd: $('host-add'), hostSources: $('host-sources'),
  conditions: $('conditions-list'), conditionName: $('condition-name'), conditionTerms: $('condition-terms'),
  conditionColorEnabled: $('condition-color-enabled'), conditionColor: $('condition-color'), conditionMessage: $('condition-message'),
};
let stream = null;
let browserSourceId = 'browser';
let browserSourceName = 'Browser feed';
let timer = null;
let scanning = false;
let hostSources = [];
let browserRules = [];
let browserRulesLoaded = false;
let shutdownRequestedUntil = 0;
let hostDevices = [];
let hostDevicesInitialized = false;
let previousDeviceIds = new Set();
let previousDisconnectedIds = new Set();
let hostRefreshing = false;
let previewUrls = new Map();
let seenEventIds = new Set();
let historyInitialized = false;
let eventHistory = [];
let activeEvent = null;
let evidenceUrl = null;
let mutes = {};
if (sessionStorage.getItem('monitorAccessCode') && !sessionStorage.getItem('frameVigilAccessCode')) sessionStorage.setItem('frameVigilAccessCode', sessionStorage.getItem('monitorAccessCode'));
sessionStorage.removeItem('monitorAccessCode');
if (localStorage.getItem('monitorMutes') && !localStorage.getItem('frameVigilMutes')) localStorage.setItem('frameVigilMutes', localStorage.getItem('monitorMutes'));
localStorage.removeItem('monitorMutes');
try { mutes = JSON.parse(localStorage.getItem('frameVigilMutes') || '{}'); } catch { /* Ignore damaged browser settings. */ }

function notify(message, error = false) {
  ui.notice.textContent = message;
  ui.notice.classList.toggle('notice-error', error);
}

function showServerOffline() {
  if (stream) stopFeed();
  $('server-offline').hidden = false;
  $('system-state').textContent = 'Server offline';
  $('status-orb').classList.remove('orb-ready');
  $('status-orb').classList.add('orb-offline');
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem('frameVigilAccessCode');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(path, {cache: 'no-store', ...options, headers});
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || 'Request failed.');
    error.unauthorized = response.status === 401;
    throw error;
  }
  return body;
}

async function apiImage(path) {
  const headers = new Headers();
  const token = sessionStorage.getItem('frameVigilAccessCode');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(path, {headers, cache: 'no-store'});
  if (!response.ok) throw new Error('No recent frame.');
  return response.blob();
}

function showAuth() {
  if ($('auth-screen').hidden) {
    $('auth-screen').hidden = false;
    $('auth-input').focus();
  }
}

$('auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  sessionStorage.setItem('frameVigilAccessCode', $('auth-input').value.trim());
  $('auth-error').textContent = '';
  if (!await refresh()) $('auth-error').textContent = 'That access code did not work.';
});

function setCaptureState(active) {
  $('feed-placeholder').hidden = active;
  $('live-chip').hidden = !active;
  $('capture-badge').textContent = active ? 'CONNECTED' : 'OFFLINE';
  $('capture-badge').classList.toggle('connected', active);
  ui.stop.disabled = !active;
  ui.scan.disabled = !active;
  ui.start.textContent = active ? 'Switch feed' : 'Connect feed';
}

async function listDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const previous = ui.device.value;
  const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === 'videoinput');
  ui.device.replaceChildren();
  for (const [index, camera] of cameras.entries()) {
    const option = document.createElement('option');
    option.value = camera.deviceId;
    option.textContent = camera.label || `Video input ${index + 1}`;
    ui.device.append(option);
  }
  if (!cameras.length) {
    const option = document.createElement('option');
    option.textContent = 'No video inputs found';
    option.value = '';
    ui.device.append(option);
  } else if (cameras.some((camera) => camera.deviceId === previous)) {
    ui.device.value = previous;
  }
}

function stopFeed() {
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  ui.video.srcObject = null;
  ui.auto.checked = false;
  stopTimer();
  setCaptureState(false);
  notify('Video input disconnected.');
}

async function startFeed() {
  if (!navigator.mediaDevices?.getUserMedia) {
    notify('This browser cannot access video devices here.', true);
    return;
  }
  try {
    const selected = ui.device.value;
    const next = await navigator.mediaDevices.getUserMedia({audio: false, video: selected ? {deviceId: {exact: selected}} : true});
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = next;
    const track = next.getVideoTracks()[0];
    let clientId = localStorage.getItem('frameVigilClientId');
    if (!clientId || !/^[a-f0-9]{32}$/.test(clientId)) {
      clientId = crypto.randomUUID().replaceAll('-', '');
      localStorage.setItem('frameVigilClientId', clientId);
    }
    const deviceId = track.getSettings().deviceId || selected || track.label || 'default';
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(deviceId));
    const deviceHash = [...new Uint8Array(digest)].map((part) => part.toString(16).padStart(2, '0')).join('').slice(0, 16);
    browserSourceId = `browser:${clientId}:${deviceHash}`;
    browserSourceName = 'Browser camera';
    ui.video.srcObject = next;
    await ui.video.play();
    stream.getVideoTracks()[0].addEventListener('ended', () => {
      if (stream === next) stopFeed();
    }, {once: true});
    await listDevices();
    setCaptureState(true);
    notify('Feed connected. Set alert rules, then scan a frame.');
    resetTimer();
  } catch (error) {
    notify(`Could not connect to video input: ${error.message}`, true);
  }
}

function captureJpeg() {
  return new Promise((resolve, reject) => {
    if (!stream || !ui.video.videoWidth) return reject(new Error('No video frame is ready.'));
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 1280 / ui.video.videoWidth);
    canvas.width = Math.round(ui.video.videoWidth * scale);
    canvas.height = Math.round(ui.video.videoHeight * scale);
    canvas.getContext('2d').drawImage(ui.video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('Could not encode frame.')), 'image/jpeg', 0.82);
  });
}

function playAlertSound() {
  const kind = $('alert-sound').value;
  if (kind === 'none') return;
  try {
    const context = new AudioContext();
    const patterns = {chime: [[740, 0, .18], [990, .2, .18]], buzzer: [[260, 0, .45]], alarm: [[650, 0, .18], [440, .22, .18], [650, .44, .18]]};
    const pattern = patterns[kind] || patterns.chime;
    const volume = Number($('speech-volume').value) / 100;
    for (const [frequency, start, length] of pattern) {
      const tone = context.createOscillator();
      const gain = context.createGain();
      tone.type = kind === 'buzzer' ? 'sawtooth' : 'sine';
      tone.frequency.value = frequency;
      gain.gain.setValueAtTime(.001, context.currentTime + start);
      gain.gain.linearRampToValueAtTime(Math.max(.001, volume * .12), context.currentTime + start + .02);
      gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + start + length);
      tone.connect(gain).connect(context.destination);
      tone.start(context.currentTime + start); tone.stop(context.currentTime + start + length + .01);
    }
    setTimeout(() => context.close(), 1300);
  } catch { /* Audio is optional. */ }
}

function speak(message) {
  if (!('speechSynthesis' in window)) return;
  const utterance = new SpeechSynthesisUtterance(message);
  utterance.rate = Number($('speech-rate').value);
  utterance.volume = Number($('speech-volume').value) / 100;
  const choice = $('speech-voice').value;
  utterance.voice = speechSynthesis.getVoices().find((voice) => voice.voiceURI === choice) || null;
  speechSynthesis.speak(utterance);
}

function listVoices() {
  if (!('speechSynthesis' in window)) return;
  const chosen = localStorage.getItem('speechVoice') || '';
  $('speech-voice').replaceChildren(new Option('System default', ''));
  for (const voice of speechSynthesis.getVoices()) {
    $('speech-voice').append(new Option(`${voice.name} · ${voice.lang}`, voice.voiceURI));
  }
  $('speech-voice').value = chosen;
}

async function scanFrame() {
  if (scanning || !stream) return;
  scanning = true;
  ui.scan.disabled = true;
  ui.scan.textContent = 'Scanning…';
  try {
    const image = await captureJpeg();
    const result = await api('/api/scan', {method: 'POST', headers: {'Content-Type': 'image/jpeg', 'X-FrameVigil-Source': browserSourceId, 'X-FrameVigil-Name': browserSourceName}, body: image});
    notify(result.events.length ? `${result.events.length} alert detected.` : `Scan complete · ${result.recognized_characters} text characters read.`);
    await refresh();
  } catch (error) {
    notify(error.message, true);
    if (error.message.includes('Tesseract')) { ui.auto.checked = false; stopTimer(); }
  } finally {
    scanning = false;
    ui.scan.disabled = !stream;
    ui.scan.textContent = 'Scan current frame';
  }
}

function stopTimer() { if (timer) clearInterval(timer); timer = null; }
function resetTimer() {
  stopTimer();
  if (ui.auto.checked && stream) timer = setInterval(scanFrame, Number(ui.interval.value));
}

async function saveRules() {
  const rules = ui.rules.value.split('\n').map((line) => line.trim()).filter(Boolean);
  try {
    const target = ui.target.value;
    if (target === 'browser') {
      const result = await api('/api/rules', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rules})});
      browserRules = result.rules;
    } else {
      await api(`/api/sources/${encodeURIComponent(target)}/rules`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rules})});
      const source = hostSources.find((item) => item.id === target);
      if (source) source.rules = rules;
    }
    ui.rules.value = rules.join('\n');
    $('rules-count').textContent = `${rules.length} / 20`;
    notify(`${rules.length} alert ${rules.length === 1 ? 'rule' : 'rules'} saved.`);
  } catch (error) { notify(error.message, true); }
}

function selectRuleTarget() {
  const target = ui.target.value;
  const rules = target === 'browser' ? browserRules : hostSources.find((source) => source.id === target)?.rules || [];
  ui.rules.value = rules.join('\n');
  $('rules-count').textContent = `${rules.length} / 20`;
  $('conditions-panel').hidden = target === 'browser';
  renderConditions();
}

function renderConditions() {
  ui.conditions.replaceChildren();
  const source = hostSources.find((item) => item.id === ui.target.value);
  if (!source) return;
  const conditions = source.conditions || [];
  if (!conditions.length) {
    const note = document.createElement('span'); note.className = 'host-empty'; note.textContent = 'No detailed conditions for this source.';
    ui.conditions.append(note);
  }
  for (const condition of conditions) {
    const row = document.createElement('div'); row.className = 'condition-item';
    const detail = document.createElement('div');
    const name = document.createElement('strong'); name.textContent = condition.name;
    const summary = document.createElement('span'); summary.textContent = `${condition.keywords.join(' + ')}${condition.color ? ` · ${condition.color}` : ''}`;
    detail.append(name, summary);
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.textContent = condition.enabled === false ? 'Enable' : 'Disable';
    toggle.addEventListener('click', async () => {
      try {
        await api(`/api/sources/${source.id}/conditions`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({conditions: conditions.map((item) => item.id === condition.id ? {...item, enabled: condition.enabled === false} : item)})});
        await refreshHost(); renderConditions();
      } catch (error) { notify(error.message, true); }
    });
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      try {
        await api(`/api/sources/${source.id}/conditions`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({conditions: conditions.filter((item) => item.id !== condition.id)})});
        await refreshHost();
        renderConditions();
      } catch (error) { notify(error.message, true); }
    });
    row.append(detail, toggle, remove); ui.conditions.append(row);
  }
}

async function addCondition() {
  const source = hostSources.find((item) => item.id === ui.target.value);
  if (!source) return;
  const condition = {
    name: ui.conditionName.value.trim(),
    keywords: ui.conditionTerms.value.split(',').map((part) => part.trim()).filter(Boolean),
    color: ui.conditionColorEnabled.checked ? ui.conditionColor.value : null,
    message: ui.conditionMessage.value.trim(),
    enabled: true,
  };
  try {
    await api(`/api/sources/${source.id}/conditions`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({conditions: [...(source.conditions || []), condition]})});
    ui.conditionName.value = ''; ui.conditionTerms.value = ''; ui.conditionMessage.value = '';
    ui.conditionColorEnabled.checked = false;
    await refreshHost();
    renderConditions();
    notify('Detailed condition saved.');
  } catch (error) { notify(error.message, true); }
}

function renderHostDevices() {
  const previous = ui.hostDevice.value;
  const available = hostDevices.filter((device) => !hostSources.some((source) => source.device_id === device.id));
  ui.hostDevice.replaceChildren();
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = available.length ? 'Select a server device' : 'No unconfigured devices found';
  ui.hostDevice.append(empty);
  for (const device of available) {
    const option = document.createElement('option');
    option.value = device.id;
    option.textContent = device.name;
    ui.hostDevice.append(option);
  }
  if (available.some((device) => device.id === previous)) ui.hostDevice.value = previous;
  ui.hostAdd.disabled = !available.length;
}

function renderDeviceAttention() {
  const unconfigured = hostDevices.filter((device) => !device.id.startsWith('demo:') && !hostSources.some((source) => source.device_id === device.id));
  const disconnected = hostSources.filter((source) => !source.connected);
  $('device-attention').hidden = !unconfigured.length;
  $('device-attention-list').textContent = unconfigured.map((device) => device.name).join(', ');
  $('device-disconnected').hidden = !disconnected.length;
  $('device-disconnected-list').textContent = disconnected.map((source) => source.name).join(', ');
  $('device-attention').querySelector('strong').textContent = `${unconfigured.length} video ${unconfigured.length === 1 ? 'device needs' : 'devices need'} setup`;
  $('device-disconnected').querySelector('strong').textContent = `${disconnected.length} configured ${disconnected.length === 1 ? 'device is' : 'devices are'} disconnected`;
}

function renderRuleTargets() {
  const previous = ui.target.value;
  ui.target.replaceChildren();
  const browser = document.createElement('option');
  browser.value = 'browser'; browser.textContent = "This browser's feed";
  ui.target.append(browser);
  for (const source of hostSources) {
    const option = document.createElement('option');
    option.value = source.id; option.textContent = `Server · ${source.name}`;
    ui.target.append(option);
  }
  if (hostSources.some((source) => source.id === previous)) ui.target.value = previous;
  else if (previous !== 'browser') selectRuleTarget();
}

function renderHostSources() {
  for (const [identifier, url] of previewUrls) {
    if (!hostSources.some((source) => source.id === identifier)) {
      URL.revokeObjectURL(url);
      previewUrls.delete(identifier);
    }
  }
  $('host-count').textContent = `${hostSources.length} ${hostSources.length === 1 ? 'SOURCE' : 'SOURCES'}`;
  ui.hostSources.replaceChildren();
  if (!hostSources.length) {
    const empty = document.createElement('p'); empty.className = 'host-empty';
    empty.textContent = 'No server-side video sources configured yet.';
    ui.hostSources.append(empty);
    return;
  }
  for (const source of hostSources) {
    const card = document.createElement('div'); card.className = 'host-card';
    const preview = document.createElement('div'); preview.className = 'host-preview';
    const image = document.createElement('img'); image.alt = `Latest frame from ${source.name}`; image.hidden = true;
    const hint = document.createElement('span'); hint.textContent = ['running', 'no_signal'].includes(source.status) ? 'Waiting for preview…' : 'Preview unavailable';
    preview.append(image, hint);
    const details = document.createElement('div'); details.className = 'host-details';
    const title = document.createElement('h3'); title.textContent = source.name;
    const badge = document.createElement('span'); badge.className = `host-status ${source.status}`; badge.textContent = source.status.toUpperCase();
    const subtitle = document.createElement('p'); subtitle.textContent = `${source.rules.length} terms · ${(source.conditions || []).length} conditions · Every ${source.interval}s`;
    const error = document.createElement('p'); error.className = 'host-error'; error.textContent = source.error || '';
    const actions = document.createElement('div'); actions.className = 'host-actions';
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'button button-outline';
    toggle.textContent = source.enabled ? 'Stop' : 'Start';
    toggle.addEventListener('click', async () => {
      try {
        await api(`/api/sources/${source.id}/${toggle.textContent.toLowerCase()}`, {method: 'POST'});
        await refreshHost();
      } catch (err) { notify(err.message, true); }
    });
    const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'button button-outline'; edit.textContent = 'Edit rules';
    edit.addEventListener('click', () => { ui.target.value = source.id; selectRuleTarget(); ui.rules.focus(); });
    const rename = document.createElement('button'); rename.type = 'button'; rename.className = 'button button-outline'; rename.textContent = 'Rename';
    rename.addEventListener('click', async () => {
      const name = prompt('Name this source:', source.name);
      if (name === null) return;
      try {
        await api(`/api/sources/${source.id}/name`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
        await refreshHost(); notify('Source renamed.');
      } catch (error) { notify(error.message, true); }
    });
    const interval = document.createElement('button'); interval.type = 'button'; interval.className = 'button button-outline'; interval.textContent = 'Interval';
    interval.addEventListener('click', async () => {
      const value = prompt('Seconds between scans (2–30):', String(source.interval));
      if (value === null) return;
      try {
        await api(`/api/sources/${source.id}/interval`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({interval: Number(value)})});
        await refreshHost(); notify('Scan interval updated.');
      } catch (error) { notify(error.message, true); }
    });
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'button button-text'; remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      try {
        await api(`/api/sources/${source.id}`, {method: 'DELETE'});
        await refreshHost();
      } catch (err) { notify(err.message, true); }
    });
    actions.append(toggle, edit, rename, interval, remove); details.append(title, badge, subtitle, error, actions);
    card.append(preview, details); ui.hostSources.append(card);
    if (['running', 'no_signal'].includes(source.status)) {
      apiImage(`/api/frames/${source.id}`).then((blob) => {
        if (!image.isConnected) return;
        const old = previewUrls.get(source.id);
        const url = URL.createObjectURL(blob);
        image.onload = () => { image.hidden = false; hint.hidden = true; };
        image.src = url;
        previewUrls.set(source.id, url);
        if (old) URL.revokeObjectURL(old);
      }).catch(() => {});
    }
  }
}

async function refreshHost() {
  if (Date.now() < shutdownRequestedUntil || !$('auth-screen').hidden || hostRefreshing) return;
  hostRefreshing = true;
  try {
    const hardware = await api('/api/hardware');
    hostDevices = hardware.devices;
    hostSources = hardware.sources;
    const deviceIds = new Set(hostDevices.filter((device) => !device.id.startsWith('demo:')).map((device) => device.id));
    const disconnectedIds = new Set(hostSources.filter((source) => !source.connected).map((source) => source.id));
    if (hostDevicesInitialized) {
      const added = hostDevices.filter((device) => deviceIds.has(device.id) && !previousDeviceIds.has(device.id) && !hostSources.some((source) => source.device_id === device.id));
      const lost = hostSources.filter((source) => disconnectedIds.has(source.id) && !previousDisconnectedIds.has(source.id));
      const changes = [];
      if (lost.length) changes.push(`${lost.map((source) => source.name).join(', ')} disconnected.`);
      if (added.length) changes.push(`${added.map((device) => device.name).join(', ')} connected. Configure the new device above.`);
      if (changes.length) notify(changes.join(' '), !!lost.length);
    }
    hostDevicesInitialized = true;
    previousDeviceIds = deviceIds;
    previousDisconnectedIds = disconnectedIds;
    renderHostDevices();
    renderDeviceAttention();
    renderRuleTargets();
    renderHostSources();
  } catch (error) { notify(`Could not list server devices: ${error.message}`, true); }
  finally { hostRefreshing = false; }
}

async function addHostSource() {
  if (!ui.hostDevice.value) return notify('Select a device plugged into the server computer.', true);
  ui.hostAdd.disabled = true;
  try {
    const result = await api('/api/sources', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({device_id: ui.hostDevice.value, name: ui.hostName.value.trim(), interval: Number($('host-interval').value)})});
    await api(`/api/sources/${result.source.id}/start`, {method: 'POST'});
    ui.hostName.value = '';
    await refreshHost();
    notify(`${result.source.name} added. Set its alert rules in the detection panel.`);
  } catch (error) { notify(error.message, true); }
  finally { ui.hostAdd.disabled = false; }
}

async function stopAllSources() {
  if (!hostSources.length || !confirm('Stop monitoring every server source?')) return;
  try {
    for (const source of hostSources) await api(`/api/sources/${source.id}/stop`, {method: 'POST'});
    await refreshHost();
    notify('All server sources stopped.');
  } catch (error) { notify(error.message, true); }
}

async function pickColorFromFeed() {
  const source = hostSources.find((item) => item.id === ui.target.value);
  if (!source) return notify('Choose a server source first.', true);
  $('color-error').hidden = true;
  $('color-dialog').showModal();
  try {
    const blob = await apiImage(`/api/frames/${source.id}`);
    const url = URL.createObjectURL(blob);
    const image = new Image(); image.src = url;
    await image.decode();
    const canvas = $('color-canvas');
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    canvas.getContext('2d').drawImage(image, 0, 0);
    URL.revokeObjectURL(url);
  } catch (error) {
    $('color-error').textContent = 'No recent frame is available for this source.';
    $('color-error').hidden = false;
  }
}

function muteKey(event) { return JSON.stringify([event.source_id || 'browser', event.line || event.term]); }
function isMuted(event) { return (mutes[muteKey(event)] || 0) > Date.now(); }
function isAcknowledged(event) { return Number(event.acknowledged_until || 0) * 1000 > Date.now(); }
function saveMutes() {
  mutes = Object.fromEntries(Object.entries(mutes).filter(([, expiry]) => expiry > Date.now()));
  localStorage.setItem('frameVigilMutes', JSON.stringify(mutes));
}

function eventDate(event) {
  const date = new Date(event.time);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function renderEventFilters() {
  const selected = $('event-source').value;
  $('event-source').replaceChildren(new Option('All sources', ''));
  const sources = new Map(eventHistory.map((event) => [event.source_id || 'browser', event.source || 'Browser feed']));
  for (const [id, name] of sources) $('event-source').append(new Option(name, id));
  $('event-source').value = sources.has(selected) ? selected : '';
}

function displayEvents() {
  const search = $('event-search').value.trim().toLocaleLowerCase();
  const source = $('event-source').value;
  const from = $('event-from').value;
  const to = $('event-to').value;
  const order = $('event-sort').value;
  const visible = eventHistory.filter((event) => {
    const date = eventDate(event);
    return (!source || (event.source_id || 'browser') === source)
      && (!from || date >= from) && (!to || date <= to)
      && (!search || `${event.term} ${event.line || ''} ${event.source || ''}`.toLocaleLowerCase().includes(search));
  });
  visible.sort((a, b) => order === 'oldest' ? new Date(a.time) - new Date(b.time)
    : order === 'source' ? (a.source || '').localeCompare(b.source || '')
    : order === 'term' ? a.term.localeCompare(b.term)
    : new Date(b.time) - new Date(a.time));
  ui.events.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement('div'); empty.className = 'empty-events';
    const title = document.createElement('strong'); title.textContent = eventHistory.length ? 'No alerts match these filters' : 'All clear for now';
    const caption = document.createElement('span'); caption.textContent = eventHistory.length ? 'Change or clear the filters to see other alerts.' : 'Detected terms will appear here as the feed is scanned.';
    empty.append(title, caption); ui.events.append(empty);
    return;
  }
  for (const event of visible) {
    const row = document.createElement('button'); row.type = 'button'; row.className = 'event-row event-button';
    row.setAttribute('aria-label', `View detection for ${event.term} from ${event.source || 'Browser feed'}`);
    row.addEventListener('click', () => openEvidence(event));
    const dot = document.createElement('span'); dot.className = `event-dot${isAcknowledged(event) ? ' acknowledged-dot' : isMuted(event) ? ' muted-dot' : ''}`;
    const detail = document.createElement('div');
    const label = document.createElement('strong'); label.textContent = event.term;
    const meta = document.createElement('span'); meta.textContent = `${event.source || 'Browser feed'} · ${isAcknowledged(event) ? `Acknowledged until ${new Date(event.acknowledged_until * 1000).toLocaleTimeString()}` : isMuted(event) ? 'Sound muted' : 'Click to view detection'}`;
    const line = document.createElement('span'); line.className = 'event-line'; line.textContent = event.line || '';
    detail.append(label, meta, line);
    const time = document.createElement('time'); time.textContent = new Date(event.time).toLocaleTimeString();
    row.append(dot, detail, time);
    ui.events.append(row);
  }
}

function renderEvents(events) {
  const unseen = historyInitialized ? events.filter((event) => !seenEventIds.has(event.id)) : [];
  seenEventIds = new Set(events.map((event) => event.id));
  historyInitialized = true;
  const audible = unseen.filter((event) => !isMuted(event));
  if (audible.length) {
    playAlertSound();
    if ($('speech-enabled').checked) for (const event of audible.slice().reverse()) speak(event.message || event.term);
  }
  eventHistory = events;
  if (activeEvent) {
    activeEvent = events.find((event) => event.id === activeEvent.id) || activeEvent;
    refreshAckControls();
  }
  $('event-count').textContent = `${events.length} ${events.length === 1 ? 'EVENT' : 'EVENTS'}`;
  renderEventFilters();
  displayEvents();
}

async function openEvidence(event) {
  activeEvent = event;
  $('evidence-title').textContent = event.term;
  $('evidence-meta').textContent = `${event.source || 'Browser feed'} · ${new Date(event.time).toLocaleString()}`;
  $('evidence-line').textContent = event.line ? `OCR text: ${event.line}` : 'OCR location unavailable for this alert.';
  $('evidence-error').hidden = true;
  $('evidence-stage').hidden = true;
  $('evidence-dialog').showModal();
  refreshMuteControls();
  refreshAckControls();
  try {
    const blob = await apiImage(`/api/evidence/${event.id}`);
    if (activeEvent?.id !== event.id) return;
    if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
    evidenceUrl = URL.createObjectURL(blob);
    const image = $('evidence-image');
    image.src = evidenceUrl;
    await image.decode();
    if (activeEvent?.id !== event.id) {
      URL.revokeObjectURL(evidenceUrl); evidenceUrl = null;
      return;
    }
    const canvas = $('evidence-boxes');
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    context.lineWidth = Math.max(3, canvas.width / 320);
    context.strokeStyle = '#ff4f45';
    context.fillStyle = '#ff4f4530';
    for (const [left, top, right, bottom] of event.boxes || []) {
      context.fillRect(left, top, right - left, bottom - top);
      context.strokeRect(left, top, right - left, bottom - top);
    }
    $('evidence-stage').hidden = false;
  } catch (error) {
    $('evidence-error').textContent = 'The alert image has expired or could not be loaded.';
    $('evidence-error').hidden = false;
  }
}

function closeEvidence() {
  $('evidence-dialog').close();
}

function refreshMuteControls() {
  $('mute-alert').hidden = !activeEvent || isMuted(activeEvent);
  $('unmute-alert').hidden = !activeEvent || !isMuted(activeEvent);
}

function refreshAckControls() {
  const acknowledged = !!activeEvent && isAcknowledged(activeEvent);
  $('ack-alert').hidden = !activeEvent || acknowledged;
  $('unack-alert').hidden = !activeEvent || !acknowledged;
  $('ack-status').textContent = acknowledged ? `This source and matching sentence are quiet until ${new Date(activeEvent.acknowledged_until * 1000).toLocaleString()}.` : 'The same match can alert again after 15 seconds.';
}

async function refresh() {
  if (Date.now() < shutdownRequestedUntil) return false;
  try {
    const [status, recent] = await Promise.all([api('/api/status'), api('/api/events')]);
    $('auth-screen').hidden = true;
    $('server-offline').hidden = true;
    $('status-orb').classList.remove('orb-offline');
    $('shutdown-server').disabled = false;
    $('auth-input').value = '';
    browserRules = status.rules;
    $('ack-duration').value = String(status.ack_duration_seconds);
    if (!browserRulesLoaded && ui.target.value === 'browser') {
      selectRuleTarget();
      browserRulesLoaded = true;
    }
    $('system-state').textContent = status.ocr_available ? 'OCR ready' : 'OCR unavailable';
    $('status-orb').classList.toggle('orb-ready', status.ocr_available);
    $('scan-count').textContent = `${status.scan_count} ${status.scan_count === 1 ? 'SCAN' : 'SCANS'}`;
    $('last-scan').textContent = status.last_scan ? `LAST ${new Date(status.last_scan).toLocaleTimeString()}` : 'NO SCANS YET';
    renderEvents(recent.events);
    if (!hostSources.length) refreshHost();
    return true;
  } catch (error) {
    if (error.unauthorized) showAuth();
    else {
      if (error instanceof TypeError || error instanceof SyntaxError) showServerOffline();
      notify(error.message, true);
    }
    return false;
  }
}

async function shutDownServer() {
  if (!confirm('Shut down FrameVigil on the server computer? All monitoring will stop. To use it again, restart the Python service on that computer.')) return;
  const button = $('shutdown-server');
  button.disabled = true;
  try {
    await api('/api/shutdown', {method: 'POST'});
    shutdownRequestedUntil = Date.now() + 2000;
    showServerOffline();
    notify('FrameVigil shut down. Restart the Python service on the host to use it again.');
  } catch (error) {
    button.disabled = false;
    notify(`Could not shut down FrameVigil: ${error.message}`, true);
  }
}

ui.start.addEventListener('click', startFeed);
ui.stop.addEventListener('click', stopFeed);
ui.scan.addEventListener('click', scanFrame);
ui.save.addEventListener('click', saveRules);
ui.target.addEventListener('change', selectRuleTarget);
ui.hostAdd.addEventListener('click', addHostSource);
$('configure-device').addEventListener('click', () => {
  const next = hostDevices.find((device) => !device.id.startsWith('demo:') && !hostSources.some((source) => source.device_id === device.id));
  if (!next) return;
  ui.hostDevice.value = next.id;
  ui.hostDevice.scrollIntoView({behavior: 'smooth', block: 'center'});
  ui.hostName.focus();
});
$('stop-all').addEventListener('click', stopAllSources);
$('shutdown-server').addEventListener('click', shutDownServer);
$('add-condition').addEventListener('click', addCondition);
$('pick-color').addEventListener('click', pickColorFromFeed);
$('color-close').addEventListener('click', () => $('color-dialog').close());
$('color-canvas').addEventListener('click', (event) => {
  const canvas = event.currentTarget;
  if (!canvas.width || !canvas.height) return;
  const bounds = canvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(canvas.width - 1, Math.floor((event.clientX - bounds.left) * canvas.width / bounds.width)));
  const y = Math.max(0, Math.min(canvas.height - 1, Math.floor((event.clientY - bounds.top) * canvas.height / bounds.height)));
  const [red, green, blue] = canvas.getContext('2d').getImageData(x, y, 1, 1).data;
  ui.conditionColor.value = `#${[red, green, blue].map((part) => part.toString(16).padStart(2, '0')).join('')}`;
  ui.conditionColorEnabled.checked = true;
  $('color-dialog').close();
  notify(`Picked background color ${ui.conditionColor.value}.`);
});
for (const id of ['event-search', 'event-source', 'event-from', 'event-to', 'event-sort']) {
  $(id).addEventListener(id === 'event-search' ? 'input' : 'change', displayEvents);
}
$('event-clear').addEventListener('click', () => {
  for (const id of ['event-search', 'event-source', 'event-from', 'event-to']) $(id).value = '';
  $('event-sort').value = 'newest';
  displayEvents();
});
$('evidence-close').addEventListener('click', closeEvidence);
$('ack-alert').addEventListener('click', async () => {
  if (!activeEvent) return;
  try {
    activeEvent = (await api(`/api/events/${activeEvent.id}/ack`, {method: 'POST'})).event;
    await refresh();
    notify('Alert acknowledged across FrameVigil viewers.');
  } catch (error) { notify(error.message, true); }
});
$('unack-alert').addEventListener('click', async () => {
  if (!activeEvent) return;
  try {
    activeEvent = (await api(`/api/events/${activeEvent.id}/unack`, {method: 'POST'})).event;
    await refresh();
    notify('Acknowledgement removed.');
  } catch (error) { notify(error.message, true); }
});
$('save-ack-duration').addEventListener('click', async () => {
  try {
    await api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ack_duration_seconds: Number($('ack-duration').value)})});
    notify('Acknowledgement duration saved on the server.');
  } catch (error) { notify(error.message, true); }
});
$('clear-acks').addEventListener('click', async () => {
  if (!confirm('Clear every active acknowledgement for all FrameVigil viewers?')) return;
  try {
    await api('/api/acknowledgements/clear', {method: 'POST'});
    await refresh();
    notify('All acknowledgements cleared.');
  } catch (error) { notify(error.message, true); }
});
$('evidence-dialog').addEventListener('close', () => {
  activeEvent = null;
  $('evidence-image').removeAttribute('src');
  if (evidenceUrl) URL.revokeObjectURL(evidenceUrl);
  evidenceUrl = null;
});
$('mute-alert').addEventListener('click', () => {
  if (!activeEvent) return;
  mutes[muteKey(activeEvent)] = Date.now() + Number($('mute-duration').value) * 60000;
  saveMutes(); refreshMuteControls(); displayEvents();
});
$('unmute-alert').addEventListener('click', () => {
  if (!activeEvent) return;
  delete mutes[muteKey(activeEvent)];
  saveMutes(); refreshMuteControls(); displayEvents();
});
$('speech-enabled').checked = localStorage.getItem('speechEnabled') === 'true';
$('speech-rate').value = localStorage.getItem('speechRate') || '1';
$('speech-volume').value = localStorage.getItem('speechVolume') || '80';
$('alert-sound').value = localStorage.getItem('alertSound') || 'chime';
$('speech-enabled').addEventListener('change', () => localStorage.setItem('speechEnabled', String($('speech-enabled').checked)));
$('speech-rate').addEventListener('change', () => localStorage.setItem('speechRate', $('speech-rate').value));
$('speech-volume').addEventListener('input', () => localStorage.setItem('speechVolume', $('speech-volume').value));
$('alert-sound').addEventListener('change', () => localStorage.setItem('alertSound', $('alert-sound').value));
$('speech-voice').addEventListener('change', () => localStorage.setItem('speechVoice', $('speech-voice').value));
$('test-speech').addEventListener('click', () => { playAlertSound(); speak($('speech-test').value.trim() || 'This is a test alert.'); });
listVoices();
if ('speechSynthesis' in window) speechSynthesis.addEventListener('voiceschanged', listVoices);
ui.auto.addEventListener('change', resetTimer);
ui.interval.addEventListener('change', resetTimer);
ui.rules.addEventListener('input', () => { $('rules-count').textContent = `${ui.rules.value.split('\n').filter((line) => line.trim()).length} / 20`; });
if (navigator.mediaDevices) navigator.mediaDevices.addEventListener('devicechange', listDevices);
window.addEventListener('beforeunload', () => { if (stream) stream.getTracks().forEach((track) => track.stop()); });
setCaptureState(false);
refresh();
listDevices();
setInterval(refresh, 10000);
setInterval(refreshHost, 10000);
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); }, 1000);
