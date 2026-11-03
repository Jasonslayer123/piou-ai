// ==UserScript==
// @name         Multibox Control
// @namespace    multibox-control
// @version      1.0.0
// @description  Mirror keyboard and mouse input from a master tab to other tabs of the same page.
// @author       you
// @match        *://*/*
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const NET = 'multibox-net';
  const panel = document.createElement('div');
  panel.style.cssText = [
    'position:fixed', 'top:8px', 'right:8px', 'z-index:2147483647',
    'background:#111d', 'color:#eee', 'padding:8px 10px', 'border-radius:8px',
    'font:12px monospace', 'display:flex', 'flex-direction:column', 'gap:6px',
    'box-shadow:0 0 8px rgba(0,0,0,.5)', 'user-select:none'
  ].join(';');

  function makeBtn(text, onClick, active) {
    const b = document.createElement('button');
    b.textContent = text;
    b.style.cssText = [
      'border:1px solid #555', 'background:' + (active ? '#3a6' : '#333'),
      'color:#eee', 'padding:4px 8px', 'border-radius:4px', 'cursor:pointer'
    ].join(';');
    b.onclick = onClick;
    return b;
  }

  const status = document.createElement('div');
  status.textContent = 'connecting...';

  const btnMaster = makeBtn('MASTER (input)', () => setRole('master'), false);
  const btnSlave = makeBtn('SLAVE (copy)', () => setRole('slave'), false);
  const btnKeys = makeBtn('keys on', () => toggle('keys'), true);
  const btnMouse = makeBtn('mouse on', () => toggle('mouse'), true);
  const btnCopy = makeBtn('copy movement on', () => toggle('copy'), true);
  const btnStop = makeBtn('STOP movement', () => toggle('stop'), false);

  panel.appendChild(status);
  panel.appendChild(btnMaster);
  panel.appendChild(btnSlave);
  panel.appendChild(btnKeys);
  panel.appendChild(btnMouse);
  panel.appendChild(btnCopy);
  panel.appendChild(btnStop);
  document.documentElement.appendChild(panel);

  const MAX_TABS = 16;
  const TABS_KEY = 'multibox-tabs';
  const BEAT = 2000;
  const STALE = 6000;

  const state = { role: 'slave', keys: true, mouse: true, copy: true, stop: false };
  const channels = [];
  let masterTabId = null;
  let myTabId = Math.random().toString(36).slice(2);
  let alive = {};
  let lastSent = 0;

  const bc = new BroadcastChannel(NET);
  bc.onmessage = (e) => {
    const msg = e.data;
    if (!msg) return;
    if (msg.t === 'beat' || msg.t === 'bye') { beat(msg); return; }
    if (msg.t === 'who') { if (state.role === 'master') send('iam', { id: myTabId }); return; }
    if (msg.t === 'iam') { if (masterTabId === null && !alive[msg.id]) masterTabId = msg.id; return; }
    if (msg.t === 'event' && state.role === 'slave') applyEvent(msg.e);
  };

  window.addEventListener('unload', () => { send('bye', { id: myTabId }); });

  function beat(msg) {
    const now = Date.now();
    alive[msg.id] = now;
    for (const k in alive) if (now - alive[k] > STALE) delete alive[k];
    if (msg.t === 'byebye' ) { delete alive[msg.id]; }
    refreshStatus();
  }

  function send(type, payload) {
    let m = Object.assign({ t: type, id: myTabId }, payload);
    bc.postMessage(m);
    if (window.opener) window.opener.postMessage(m, '*');
  }

  function setRole(r) {
    state.role = r;
    btnMaster.style.background = r === 'master' ? '#3a6' : '#333';
    btnSlave.style.background = r === 'slave' ? '#3a6' : '#333';
    refreshStatus();
    if (r === 'master') {
      masterTabId = null;
      send('iamboss', { id: myTabId });
      pickMaster();
      boot();
    }
  }

  function toggle(kind) {
    state[kind] = !state[kind];
    const map = { keys: btnKeys, mouse: btnMouse, copy: btnCopy, stop: btnStop };
    const b = map[kind];
    const label = { keys: 'keys', mouse: 'mouse', copy: 'copy movement', stop: 'STOP movement' }[kind];
    const on = kind === 'stop' ? state.stop : state[kind];
    b.textContent = (kind === 'stop' && state.stop ? 'STOPPED' : label) + (kind === 'copy' || kind === 'keys' || kind === 'mouse' ? (state[kind] ? ' on' : ' off') : '');
    b.style.background = on ? '#3a6' : '#333';
  }

  function refreshStatus() {
    const now = Date.now();
    const masters = Object.keys(alive).filter((id) => id !== myTabId && now - alive[id] < STALE).length;
    status.textContent = state.role.toUpperCase() + ' | peers: ' + masters + ' | master: ' + (masterTabId && masterTabId.slice(0, 4));
  }

  function register() {
    let tabs;
    try { tabs = JSON.parse(localStorage.getItem(TABS_KEY) || '[]'); } catch (e) { tabs = []; }
    tabs = tabs.filter((id) => id !== myTabId);
    tabs.unshift(myTabId);
    tabs = tabs.slice(0, MAX_TABS);
    localStorage.setItem(TABS_KEY, JSON.stringify(tabs));
    masterTabId = tabs[tabs.length - 1] === myTabId ? null : tabs[tabs.length - 1];
    if (masterTabId === null) setRole('master'); else setRole('slave');
  }

  function pickMaster() {
    let tabs;
    try { tabs = JSON.parse(localStorage.getItem(TABS_KEY) || '[]'); } catch (e) { tabs = []; }
    tabs = tabs.filter((id) => id !== myTabId);
    masterTabId = tabs[0] || null;
  }

  function boot() {
    setInterval(() => { send('beat', { id: myTabId }); }, BEAT);
    send('who', {});
  }

  register();
  refreshStatus();

  document.addEventListener('keydown', capture, true);
  document.addEventListener('keyup', capture, true);
  document.addEventListener('mousemove', capture, true);
  document.addEventListener('mousedown', capture, true);
  document.addEventListener('mouseup', capture, true);
  document.addEventListener('wheel', capture, true);

  function capture(e) {
    if (state.role !== 'master') return;
    if (state.stop) return;
    if (!e.isTrusted) return;
    const now = Date.now();
    if (now - lastSent < 12) return;
    lastSent = now;

    const base = { type: e.type, code: e.code, key: e.key, button: e.button, buttons: e.buttons, alt: e.altKey, ctrl: e.ctrlKey, shift: e.shiftKey };

    if (e.type === 'keydown' || e.type === 'keyup') {
      if (!state.keys) return;
      send('event', { e: base });
    } else if (e.type === 'mousemove') {
      if (!state.mouse) return;
      send('event', { e: Object.assign(base, { dx: e.movementX, dy: e.movementY }) });
    } else if (e.type === 'mousedown' || e.type === 'mouseup') {
      if (!state.mouse) return;
      send('event', { e: Object.assign(base, { cx: e.clientX, cy: e.clientY }) });
    } else if (e.type === 'wheel') {
      send('event', { e: Object.assign(base, { dx: e.deltaX, dy: e.deltaY, dz: e.deltaZ, mode: e.deltaMode }) });
    }
  }

  const cursor = { x: innerWidth / 2, y: innerHeight / 2 };

  function applyEvent(e) {
    if (state.stop || !state.copy) return;
    const opts = { bubbles: true, cancelable: true };
    const mods = { altKey: e.alt, ctrlKey: e.ctrl, shiftKey: e.shift };

    if (e.type === 'keydown' || e.type === 'keyup') {
      document.dispatchEvent(new KeyboardEvent(e.type, Object.assign(opts, mods, {
        code: e.code, key: e.key, keyCode: e.key && e.key.length === 1 ? e.key.toUpperCase().charCodeAt(0) : 0,
        which: e.key && e.key.length === 1 ? e.key.toUpperCase().charCodeAt(0) : 0
      })));
      return;
    }

    if (e.type === 'mousemove') {
      cursor.x += e.dx;
      cursor.y += e.dy;
      dispatchToPoint('mousemove', { clientX: cursor.x, clientY: cursor.y, movementX: e.dx, movementY: e.dy, buttons: e.buttons });
      return;
    }

    if (e.type === 'mousedown' || e.type === 'mouseup') {
      cursor.x = e.cx;
      cursor.y = e.cy;
      dispatchToPoint(e.type, { clientX: cursor.x, clientY: cursor.y, button: e.button, buttons: e.buttons });
      return;
    }

    if (e.type === 'wheel') {
      dispatchToPoint('wheel', { clientX: cursor.x, clientY: cursor.y, deltaX: e.dx, deltaY: e.dy, deltaZ: e.dz, deltaMode: e.mode });
    }
  }

  function dispatchToPoint(type, props) {
    const target = document.elementFromPoint(props.clientX, props.clientY) || document;
    target.dispatchEvent(new MouseEvent(type, Object.assign({ bubbles: true, cancelable: true }, props)));
  }
})();