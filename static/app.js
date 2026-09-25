(() => {
'use strict';

let ws = null;
let myToken = null;
let reconnectToken = null;
let mySeat = null;
let myRoom = 'main';
let myCards = [];
let currentGameState = null;
let wasMyTurn = false;
let pendingWildCardId = null;

let drewThisTurn = false;
let lastDrawnCardId = null;

let localStream = null;
let camActive = false;
let micActive = false;
let currentCamId = localStorage.getItem('uno_cam_id') || '';
let currentMicId = localStorage.getItem('uno_mic_id') || '';
let iceServers = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:stun1.l.google.com:19302' },
  { urls: 'stun:stun.cloudflare.com:3478' }
];
let keepAliveTimer = null;
const seatToSlotMap = {};
const peerConnections = {};
const remoteStreams = {};

let lastTopCardKey = '';
let lastHandIds = new Set();

let dealInProgress = false;
let dealTimer = null;
let dealRevealed = Object.create(null);

let drawInFlight = false;

let analysisCtx = null;
const audioMeters = {};

const pendingPlays = new Set();

const CARD_COLORS = {
  red: '#e52521',
  yellow: '#fcd116',
  green: '#00a651',
  blue: '#0072ce',
  black: '#3f3f46'
};

class SoundFX {
  constructor() { this.ctx = null; this.enabled = true; }
  init() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) this.ctx = new AudioContext();
    }
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
  }
  playTone(freq, type, duration, gainVal = 0.15) {
    if (!this.enabled) return;
    this.init();
    if (!this.ctx) return;
    try {
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, this.ctx.currentTime);
      gain.gain.setValueAtTime(gainVal, this.ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + duration);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start();
      osc.stop(this.ctx.currentTime + duration);
    } catch(e) {}
  }
  playCard() {
    this.playTone(480, 'sine', 0.1, 0.18);
    setTimeout(() => this.playTone(720, 'triangle', 0.12, 0.15), 50);
  }
  drawCard() { this.playTone(320, 'sine', 0.14, 0.15); }
  dealTick(i = 0) { this.playTone(340 + (i % 6) * 55, 'triangle', 0.05, 0.05); }
  turnChime() {
    this.playTone(523.25, 'triangle', 0.15, 0.2);
    setTimeout(() => this.playTone(659.25, 'triangle', 0.18, 0.2), 120);
    setTimeout(() => this.playTone(783.99, 'sine', 0.25, 0.25), 240);
  }
  unoAlert() {
    this.playTone(880, 'sawtooth', 0.2, 0.25);
    setTimeout(() => this.playTone(1100, 'sawtooth', 0.3, 0.3), 150);
  }
  victory() {
    [523.25, 659.25, 783.99, 1046.50].forEach((f, i) => {
      setTimeout(() => this.playTone(f, 'triangle', 0.3, 0.25), i * 140);
    });
  }
}
const sfx = new SoundFX();

const $ = id => document.getElementById(id);
const lobby = $('lobby');
const reconnectScreen = $('reconnectScreen');
const reconnectText = $('reconnectText');
const btnCancelReconnect = $('btnCancelReconnect');
const gameView = $('game-view');
const inputName = $('inputName');
const inputRoom = $('inputRoom');
const checkCamera = $('checkCamera');
const checkMic = $('checkMic');
const btnJoin = $('btnJoin');
const lobbyErr = $('lobbyErr');
const headerRoomLabel = $('headerRoomLabel');
const btnCopyRoom = $('btnCopyRoom');
const btnCopyRoomIcon = $('btnCopyRoomIcon');
const fxLayer = $('fxLayer');
const tableView = $('tableView');
const myPodSlot = $('myPodSlot');
const myHandPanel = $('myHandPanel');
const micGroup = $('micGroup');
const camGroup = $('camGroup');
const btnToggleCam = $('btnToggleCam');
const btnToggleMic = $('btnToggleMic');
const btnExitTable = $('btnExitTable');
const feltTable = $('feltTable');
const turnBannerText = $('turnBannerText');
const btnDrawDeck = $('btnDrawDeck');
const deckCountLabel = $('deckCountLabel');
const directionArrow = $('directionArrow');
const colorOrb = $('colorOrb');
const discardTopCardContainer = $('discardTopCardContainer');
const btnStartGame = $('btnStartGame');
const myCamBox = $('myCamBox');
const mySeatTag = $('mySeatTag');
const myNameText = $('myNameText');
const myHandCardsContainer = $('myHandCardsContainer');
const myHandCountTag = $('myHandCountTag');
const btnCallUno = $('btnCallUno');
const btnCallUnoCtrl = $('btnCallUnoCtrl');
const centreSeats = $('centreSeats');
const playerCountChip = $('playerCountChip');
const chatForm = $('chatForm');
const chatInput = $('chatInput');
const chatLog = $('chatLog');
const btnMicSettings = $('btnMicSettings');
const btnCamSettings = $('btnCamSettings');
const deviceMenu = $('deviceMenu');
const modalColorChoice = $('modalColorChoice');
const modalConfirmExit = $('modalConfirmExit');
const btnConfirmExitYes = $('btnConfirmExitYes');
const btnConfirmExitNo = $('btnConfirmExitNo');
const modalGameOver = $('modalGameOver');
const gameOverTrophy = $('gameOverTrophy');
const gameOverTitle = $('gameOverTitle');
const gameOverMsg = $('gameOverMsg');
const leaderboardBox = $('leaderboardBox');
const btnPlayAgain = $('btnPlayAgain');
const btnBackToLobby = $('btnBackToLobby');

/* The local player always occupies the bottom tile. The remaining seats fill
   the table clockwise from there: next seat sits to my left, then across from
   me, then to my right. Positions never shuffle when the play direction flips
   — only the turn order reverses, which is what the direction arrow shows. */
const SLOT_NAMES = ['left', 'top', 'right'];
const opponentPods = {};
SLOT_NAMES.forEach(slot => { opponentPods[slot] = $(`pod-${slot}`); });

function showToast(msg, duration = 3500) {
  const tc = document.getElementById('toast-container') || document.body;
  const el = document.createElement('div');
  el.className = 'toast-msg';
  el.textContent = msg;
  tc.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

/* ---------------------------------------------------------------- chat --
   A persistent, scrollable transcript in the style of a Minecraft chat
   window: joins, leaves, round events and messages all stay here until they
   age out of the buffer, so anyone can read back what just happened. */
const CHAT_MAX_LINES = 60;

function appendChatLine(node) {
  if (!chatLog) return;
  const slack = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight;
  chatLog.appendChild(node);
  while (chatLog.childElementCount > CHAT_MAX_LINES) chatLog.firstElementChild.remove();
  if (slack < 48) chatLog.scrollTop = chatLog.scrollHeight;
}

function pushChatMessage({ name, text, mine, system }) {
  if (!chatLog || !text) return null;
  const el = document.createElement('div');
  el.className = 'chat-line' + (system ? ' is-system' : '') + (mine ? ' is-me' : '');
  if (!system) {
    const who = document.createElement('span');
    who.className = 'chat-who';
    who.textContent = (mine ? 'You' : (name || 'Player')) + ':';
    el.appendChild(who);
  }
  const body = document.createElement('span');
  body.className = 'chat-text';
  body.textContent = text;
  el.appendChild(body);
  appendChatLine(el);
  return el;
}

/* Your own line is drawn the moment you hit Enter so it can never appear to
   vanish while the socket is busy. When the server's copy of the same text
   comes back it is folded into the line already on screen rather than
   printed a second time; if it never comes back the line is flagged. */
const pendingChat = [];

function submitChat() {
  const text = chatInput.value.trim();
  if (!text) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    pushChatMessage({ system: true, text: 'Not connected — message not sent' });
    return;
  }
  const entry = { text, el: pushChatMessage({ text, mine: true }) };
  pendingChat.push(entry);
  chatInput.value = '';
  sendServerMessage({ type: 'chat', text });
  setTimeout(() => {
    if (pendingChat.indexOf(entry) !== -1 && entry.el) entry.el.classList.add('is-failed');
  }, 7000);
}

function clearChatLog() {
  if (chatLog) chatLog.innerHTML = '';
  pendingChat.length = 0;
}

if (chatForm) {
  chatForm.addEventListener('submit', e => {
    e.preventDefault();
    submitChat();
  });
}

let audioUnlocked = false;
const remoteVideoEls = new Set();
const remoteAudioEls = new Set();
const audioUnlockBanner = $('audioUnlockBanner');

function updateAudioUnlockBanner() {
  if (!audioUnlockBanner) return;
  const anyMutedRemote = [...remoteAudioEls].some(a => a && a.srcObject && a.muted);
  audioUnlockBanner.classList.toggle('show', !audioUnlocked && anyMutedRemote);
}

function unlockAllAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;
  remoteAudioEls.forEach(a => {
    if (a && a.srcObject) {
      a.muted = false;
      a.play().catch(() => {});
    }
  });
  if (analysisCtx && analysisCtx.state === 'suspended') analysisCtx.resume().catch(() => {});
  if (sfx.ctx && sfx.ctx.state === 'suspended') sfx.ctx.resume().catch(() => {});
  updateAudioUnlockBanner();
}

if (audioUnlockBanner) {
  audioUnlockBanner.addEventListener('click', e => {
    e.stopPropagation();
    unlockAllAudio();
  });
}

function retryPendingPlays() {
  pendingPlays.forEach(v => {
    if (v && v.srcObject) {
      v.muted = true;
      v.play().then(() => pendingPlays.delete(v)).catch(() => {});
    } else {
      pendingPlays.delete(v);
    }
  });
  remoteAudioEls.forEach(a => {
    if (a && a.srcObject) {
      a.muted = !audioUnlocked;
      a.play().catch(() => {});
    }
  });
  updateAudioUnlockBanner();
}

['pointerdown', 'touchstart', 'click', 'keydown'].forEach(evt => {
  gameView.addEventListener(evt, () => { unlockAllAudio(); retryPendingPlays(); }, { passive: true });
});

function ensureAudioContext() {
  if (!analysisCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      try { analysisCtx = new AC(); } catch(e) { analysisCtx = null; }
    }
  }
  if (analysisCtx && analysisCtx.state === 'suspended') {
    analysisCtx.resume().catch(() => {});
  }
  return analysisCtx;
}

function attachAudioMeter(key, stream, meterElGetter) {
  if (!stream || !stream.getAudioTracks().length) return;
  if (!ensureAudioContext()) return;

  let m = audioMeters[key];
  if (m && m.stream === stream) {
    m.getEl = meterElGetter;
    return;
  }
  if (m) {
    try { m.source.disconnect(); } catch(e) {}
  }
  try {
    const source = analysisCtx.createMediaStreamSource(stream);
    const analyser = analysisCtx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    audioMeters[key] = {
      stream, source, analyser,
      data: new Uint8Array(analyser.fftSize),
      level: 0,
      getEl: meterElGetter
    };
  } catch(e) {
    console.warn('Audio meter error:', e);
  }
}

function removeAudioMeter(key) {
  const m = audioMeters[key];
  if (!m) return;
  try { m.source.disconnect(); } catch(e) {}
  delete audioMeters[key];
}

function getLocalMeterEls() {
  const meter = myCamBox.querySelector('.mic-meter');
  return meter ? { meter, fill: meter.querySelector('.mic-meter-fill') } : null;
}

function getOpponentMeterEls(seatNum) {
  const slotNum = seatToSlotMap[seatNum];
  const pod = slotNum ? opponentPods[slotNum] : null;
  const meter = pod ? pod.querySelector('.mic-meter') : null;
  return meter ? { meter, fill: meter.querySelector('.mic-meter-fill') } : null;
}

function audioMeterLoop() {
  for (const key in audioMeters) {
    const m = audioMeters[key];
    if (!m.analyser) continue;
    const els = m.getEl ? m.getEl() : null;
    if (!els || !els.fill) {
      if (els && els.meter) els.meter.classList.remove('active');
      continue;
    }
    els.meter.classList.add('active');
    m.analyser.getByteTimeDomainData(m.data);
    let sum = 0;
    for (let i = 0; i < m.data.length; i++) {
      const v = (m.data[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / m.data.length);
    const target = Math.min(100, Math.round(rms * 340));
    m.level = target > m.level ? target : Math.max(target, m.level * 0.86);
    els.fill.style.height = m.level + '%';
  }
  requestAnimationFrame(audioMeterLoop);
}
requestAnimationFrame(audioMeterLoop);

function updateSeatToSlotMapping(seats) {
  Object.keys(seatToSlotMap).forEach(k => delete seatToSlotMap[k]);
  if (mySeat == null) return;
  const allSeats = Object.keys(seats).map(Number).sort((a, b) => a - b);
  const startIdx = allSeats.indexOf(mySeat);
  if (startIdx < 0) return;

  for (let i = 1; i <= SLOT_NAMES.length && i < allSeats.length; i++) {
    const seatNum = allSeats[(startIdx + i) % allSeats.length];
    seatToSlotMap[seatNum] = SLOT_NAMES[i - 1];
  }
}

async function loadIceConfiguration() {
  try {
    const res = await fetch('/api/ice-config');
    if (!res.ok) throw new Error(`ICE config returned HTTP ${res.status}`);
    iceServers = await res.json();
    const turnCount = iceServers.filter(server => {
      const urls = Array.isArray(server.urls) ? server.urls : [server.urls];
      return urls.some(url => url.startsWith('turn:') || url.startsWith('turns:'));
    }).length;
    debugLog(`ice:loaded turnEntries=${turnCount}`);
    if (!turnCount) {
      console.warn('[WebRTC] No TURN relay configured; some networks may not connect media.');
    }
  } catch(e) {
    console.error('[WebRTC] Could not load ICE configuration:', e);
  }
}

/* Diagnostics: run unoDebug() in the browser console during a call to see
   why media is or is not flowing. Everything it reads is local state. */
const debugLines = [];
function debugLog(message) {
  const line = `${new Date().toISOString()} ${message}`;
  debugLines.push(line);
  if (debugLines.length > 200) debugLines.shift();
}
window.unoDebug = async () => {
  const peers = [];
  for (const [peerToken, entry] of Object.entries(peerConnections)) {
    const report = {
      seat: entry.seat,
      signalingState: entry.pc.signalingState,
      iceConnectionState: entry.pc.iceConnectionState,
      connectionState: entry.pc.connectionState,
      sendVideo: !!entry.senders.video?.track,
      sendAudio: !!entry.senders.audio?.track,
      remoteTracks: (remoteStreams[entry.seat]?.getTracks() || []).map(t => `${t.kind}:${t.readyState}`),
      selectedCandidatePair: null,
    };
    try {
      const stats = await entry.pc.getStats();
      stats.forEach(stat => {
        if (stat.type === 'candidate-pair' && stat.state === 'succeeded') {
          const local = stats.get(stat.localCandidateId);
          const remote = stats.get(stat.remoteCandidateId);
          report.selectedCandidatePair = `${local?.candidateType || '?'} -> ${remote?.candidateType || '?'}`;
        }
      });
    } catch(e) {}
    peers.push(report);
  }
  return { iceServers, peers, log: debugLines.slice(-60) };
};

function getWebSocketUrl(room) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws/${encodeURIComponent(room)}`;
}

function resetJoinButton(message) {
  hideReconnectOverlay();
  lobby.classList.remove('hidden');
  lobby.style.display = 'flex';
  btnJoin.disabled = false;
  btnJoin.innerHTML = '<span>Enter Table</span><span>&rarr;</span>';
  if (message) lobbyErr.textContent = message;
}

function connectWebSocket() {
  const url = getWebSocketUrl(myRoom);
  try {
    ws = new WebSocket(url);
  } catch (err) {
    resetJoinButton('Failed to create WebSocket connection.');
    return;
  }

  ws.onopen = () => {
    const savedToken = localStorage.getItem(`uno_token_${myRoom}`);
    ws.send(JSON.stringify({
      type: 'join',
      name: inputName.value.trim() || 'Player',
      token: savedToken,
      cam_on: camActive,
      mic_on: micActive
    }));
  };

  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch(e) { return; }
    handleServerMessage(msg);
  };

  ws.onerror = () => {
    if (!gameView.classList.contains('active')) {
      resetJoinButton('Unable to connect to server.');
    }
  };

  ws.onclose = () => {
    stopKeepAlivePing();
    if (gameView.classList.contains('active')) {
      showToast('Connection lost. Reconnecting in 3s...');
      setTimeout(connectWebSocket, 3000);
    } else {
      resetJoinButton('Connection closed.');
    }
  };
}

function sendServerMessage(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function startKeepAlivePing() {
  stopKeepAlivePing();
  keepAliveTimer = setInterval(() => sendServerMessage({ type: 'ping' }), 25000);
}

function stopKeepAlivePing() {
  if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'joined': {
      myToken = msg.peer_id;
      reconnectToken = msg.token;
      mySeat = msg.seat;
      localStorage.setItem(`uno_token_${myRoom}`, msg.token);
      localStorage.setItem('uno_last_room', myRoom);
      hideReconnectOverlay();
      lobby.classList.add('hidden');
      lobby.style.display = 'none';
      gameView.classList.add('active');
      gameView.style.display = 'flex';
      headerRoomLabel.textContent = myRoom.toUpperCase();
      setupMyCameraFeed();
      startKeepAlivePing();
      break;
    }

    case 'state': {
      const prevStarted = currentGameState && currentGameState.started;
      currentGameState = msg.state;
      drawInFlight = false;

      const myInfo = currentGameState.seats[String(mySeat)];
      drewThisTurn = myInfo ? !!myInfo.drew_this_turn : false;
      if (!drewThisTurn) lastDrawnCardId = null;

      if (msg.state.started && !prevStarted) {
        lastHandIds = new Set();
        lastTopCardKey = '';
        startDeal(msg.state);
      }
      renderGameState(msg.state);
      break;
    }

    case 'hand': {
      const prevIds = lastHandIds;
      const newCards = (msg.cards || []).filter(c => !prevIds.has(c.id));
      if (drewThisTurn && newCards.length === 1) {
        lastDrawnCardId = newCards[0].id;
      }
      myCards = msg.cards || [];
      if (dealInProgress) {
        /* The hand can land mid-deal: rebuild only the cards that have
           already been thrown, so the reveal order still holds. */
        myHandCardsContainer.innerHTML = '';
        const revealed = dealRevealed.me || 0;
        for (let i = 0; i < revealed; i++) appendMyCard();
      } else {
        renderMyHand();
      }
      break;
    }

    case 'peers':
      syncPeerConnections(msg.peers);
      break;

    case 'webrtc':
      handleWebRTCSignal(msg.from, msg.from_seat, msg.payload);
      break;

    case 'notice':
      pushChatMessage({ system: true, text: msg.message });
      break;

    case 'chat': {
      const isMine = msg.seat === mySeat;
      if (isMine) {
        const i = pendingChat.findIndex(p => p.text === msg.text);
        if (i !== -1) {
          if (pendingChat[i].el) pendingChat[i].el.classList.remove('is-failed');
          pendingChat.splice(i, 1);
          break;
        }
      }
      pushChatMessage({ name: msg.name, text: msg.text, mine: isMine });
      break;
    }

    case 'error':
      showToast(msg.message);
      if (!gameView.classList.contains('active')) {
        localStorage.removeItem(`uno_token_${myRoom}`);
        resetJoinButton(msg.message);
      }
      break;

    case 'kicked':
      if (reconnectToken) localStorage.removeItem(`uno_token_${myRoom}`);
      cleanupSessionLocal();
      resetJoinButton(msg.message || 'The host removed you from the table.');
      break;

    case 'game_over':
      handleGameOver(msg);
      break;
  }
}

function renderGameState(state) {
  /* While the deal animation is in flight, hold back the turn state so the
     table is revealed as the cards land rather than before they do. */
  const dealing = dealInProgress;
  if (dealing) state = Object.assign({}, state, { turn_seat: null });

  updateSeatToSlotMapping(state.seats);

  playerCountChip.textContent = `Players: ${state.player_count || 0}/${state.max_seats || 4}`;

  const assignedSlots = new Set(Object.values(seatToSlotMap));
  SLOT_NAMES.forEach(slot => {
    if (!assignedSlots.has(slot)) renderEmptyOpponentPod(slot);
  });

  for (const [seatStr, playerInfo] of Object.entries(state.seats)) {
    const seatNum = Number(seatStr);
    if (seatNum === mySeat) {
      renderMyPod(playerInfo, state);
    } else {
      const slot = seatToSlotMap[seatNum];
      if (slot) renderActiveOpponentPod(slot, seatNum, playerInfo, state);
    }
  }

  renderCentreSeats(state);

  const isMyTurn = state.started && state.turn_seat === mySeat;
  if (isMyTurn && !wasMyTurn) sfx.turnChime();
  wasMyTurn = isMyTurn;

  if (dealing) {
    turnBannerText.textContent = 'Dealing cards...';
  } else if (!state.started) {
    turnBannerText.textContent = state.player_count < 2
      ? 'Waiting for at least 2 players...'
      : 'Ready! Host can start the game';
  } else if (isMyTurn) {
    const myInfo = state.seats[String(mySeat)];
    turnBannerText.textContent = (myInfo && myInfo.drew_this_turn)
      ? '🃏 Play your drawn card'
      : '⚡ YOUR TURN — Play a card or Draw';
  } else {
    const activePlayer = state.seats[String(state.turn_seat)];
    const activeName = activePlayer ? activePlayer.name : `Seat ${state.turn_seat}`;
    turnBannerText.textContent = `⏳ ${activeName}'s turn`;
  }

  const activeColor = CARD_COLORS[state.current_color] || '#3f3f46';
  feltTable.style.setProperty('--active-color', state.current_color ? activeColor : 'transparent');

  const arrowText = state.direction === 1 ? '\u21bb' : '\u21ba';
  if (directionArrow.textContent !== arrowText) {
    directionArrow.textContent = arrowText;
    directionArrow.classList.remove('direction-pop');
    void directionArrow.offsetWidth;
    directionArrow.classList.add('direction-pop');
  }

  colorOrb.style.background = activeColor;
  colorOrb.style.color = activeColor;
  deckCountLabel.textContent = String(state.draw_pile_count || 0);
  const myState = state.seats[String(mySeat)];
  const myDrew = myState ? !!myState.drew_this_turn : false;
  btnDrawDeck.classList.toggle('disabled', !isMyTurn || !state.started || myDrew);

  if (state.top_card) {
    const key = `${state.top_card.color}_${state.top_card.value}`;
    const changed = lastTopCardKey !== '' && key !== lastTopCardKey;
    discardTopCardContainer.innerHTML = createCardHTML(state.top_card, true);
    if (changed) {
      const el = discardTopCardContainer.querySelector('.uno-card');
      if (el) el.classList.add('drop-in');
    }
    lastTopCardKey = key;
  } else {
    lastTopCardKey = '';
  }

  const isHost = state.host_seat === mySeat;
  btnStartGame.style.display = (isHost && !state.started) ? 'block' : 'none';
  btnStartGame.disabled = state.player_count < 2;

  if (state.started) modalGameOver.classList.remove('active');

  renderMyHand();
}

/* Opponent card counts live in the centre void — one pill per opponent,
   ordered the same way the seats sit around the table (left, across, right),
   so a hand count is never attached to the wrong face on screen. */
const centreSeatEls = {};
let centreSeatKey = '';

function centreSeatOrder() {
  const seats = [];
  SLOT_NAMES.forEach(slot => {
    const seat = Object.keys(seatToSlotMap).find(k => seatToSlotMap[k] === slot);
    if (seat) seats.push(Number(seat));
  });
  return seats;
}

function renderCentreSeats(state) {
  const seats = state ? centreSeatOrder() : [];
  const key = seats.join(',');
  if (key !== centreSeatKey) {
    centreSeatKey = key;
    centreSeats.innerHTML = '';
    Object.keys(centreSeatEls).forEach(k => delete centreSeatEls[k]);
    seats.forEach(seatNum => {
      const el = document.createElement('div');
      el.className = 'centre-seat';
      el.dataset.seat = String(seatNum);
      el.innerHTML = '<span class="cs-card"></span>'
        + '<span class="cs-name"></span>'
        + '<span class="cs-count">0</span>'
        + '<button class="cs-catch" type="button">Catch!</button>';
      el.querySelector('.cs-catch').onclick = () => sendServerMessage({ type: 'catch_uno', target_seat: seatNum });
      centreSeatEls[seatNum] = el;
      centreSeats.appendChild(el);
    });
  }

  seats.forEach(seatNum => {
    const el = centreSeatEls[seatNum];
    const info = state && state.seats[String(seatNum)];
    if (!el || !info) return;
    const count = dealInProgress
      ? (dealRevealed[seatNum] || 0)
      : (state.started ? (info.hand_count || 0) : 0);
    const uncaught = count === 1 && !info.called_uno && !dealInProgress;
    el.querySelector('.cs-name').textContent = info.name || `Seat ${seatNum}`;
    el.querySelector('.cs-count').textContent = uncaught ? 'UNO!' : String(count);
    el.classList.toggle('uno-alert', uncaught);
    el.classList.toggle('is-turn', !!(state.started && state.turn_seat === seatNum));
  });
}

/* Each opponent shows a small fan of face-down cards tucked under their
   camera tile — Figma: 44x66 backs on a 40px pitch, centred on the tile.
   The fan is capped and the pitch compresses so a big hand never overflows. */
const MAX_BACKS_SHOWN = 8;
const FAN_MAX_W = 264;
const FAN_PITCH = 40;

function renderOpponentFan(pod, count) {
  /* During a deal the fan grows one card at a time via appendOpponentBack(). */
  if (dealInProgress) return;
  const tray = pod.querySelector('.opp-card-tray');
  if (!tray) return;
  /* Outside a live round no opponent is holding anything. */
  const live = !!(currentGameState && currentGameState.started);
  const shown = live ? Math.min(count || 0, MAX_BACKS_SHOWN) : 0;
  const existing = tray.querySelector('.opp-card-fan');
  if (!shown) {
    if (existing) tray.innerHTML = '';
    return;
  }
  if (existing && existing.childElementCount === shown) return;

  tray.innerHTML = '';
  const fan = document.createElement('div');
  fan.className = 'opp-card-fan';
  for (let i = 0; i < shown; i++) {
    const back = document.createElement('div');
    back.className = 'card-back';
    back.style.animationDelay = `${i * 40}ms`;
    back.innerHTML = '<div class="back-oval"><span class="back-uno">UNO</span></div>';
    fan.appendChild(back);
  }
  applyFanPitch(fan);
  tray.appendChild(fan);
}

function renderEmptyOpponentPod(slot) {
  const pod = opponentPods[slot];
  if (!pod) return;
  pod.className = 'player-pod empty';
  pod.dataset.seat = '';
  const kickButton = pod.querySelector('.btn-kick-player');
  if (kickButton) kickButton.hidden = true;

  const videoEl = pod.querySelector('video');
  if (videoEl) {
    videoEl.classList.remove('live');
    videoEl.srcObject = null;
    remoteVideoEls.delete(videoEl);
  }
  const audioEl = pod.querySelector('audio.peer-audio');
  if (audioEl) {
    audioEl.srcObject = null;
    audioEl.muted = true;
    remoteAudioEls.delete(audioEl);
  }
  updateAudioUnlockBanner();

  const nameText = pod.querySelector('.cam-name-text');
  if (nameText) nameText.textContent = 'Open Seat';
  renderOpponentFan(pod, 0);
}

function renderActiveOpponentPod(slot, seatNum, info, state) {
  const pod = opponentPods[slot];
  if (!pod) return;

  const isTurn = state.started && state.turn_seat === seatNum;
  pod.className = `player-pod${isTurn ? ' is-turn' : ''}`;
  pod.dataset.seat = String(seatNum);
  const myInfo = state.seats[String(mySeat)];
  const kickButton = pod.querySelector('.btn-kick-player');
  if (kickButton) {
    kickButton.hidden = !myInfo?.is_host;
    kickButton.onclick = event => {
      event.stopPropagation();
      if (window.confirm(`Remove ${info.name || 'this player'} from the table?`)) {
        sendServerMessage({ type: 'kick', target_seat: seatNum });
      }
    };
  }

  const nameText = pod.querySelector('.cam-name-text');
  if (nameText) nameText.textContent = info.name || 'Player';

  const videoEl = pod.querySelector('video');
  const audioEl = pod.querySelector('audio.peer-audio');
  const stream = remoteStreams[seatNum];

  if (videoEl) {
    if (info.cam_on && stream) {
      const vTrack = stream.getVideoTracks()[0];
      if (vTrack && vTrack.readyState === 'live') {
        if (videoEl.srcObject !== stream) {
          videoEl.srcObject = stream;
        }
        videoEl.muted = true;
        videoEl.classList.add('live');
        remoteVideoEls.add(videoEl);
        videoEl.play().catch(() => {});
      } else {
        videoEl.classList.remove('live');
        videoEl.srcObject = null;
        remoteVideoEls.delete(videoEl);
      }
    } else {
      videoEl.classList.remove('live');
      videoEl.srcObject = null;
      remoteVideoEls.delete(videoEl);
    }
  }

  if (audioEl && stream) {
    const aTrack = stream.getAudioTracks()[0];
    if (aTrack) {
      const audioOnlyStream = new MediaStream([aTrack]);
      if (!audioEl.srcObject || audioEl.srcObject.getAudioTracks()[0] !== aTrack) {
        audioEl.srcObject = audioOnlyStream;
      }
      audioEl.muted = !audioUnlocked;
      remoteAudioEls.add(audioEl);
      audioEl.play().catch(() => {});
    } else {
      audioEl.srcObject = null;
      audioEl.muted = true;
      remoteAudioEls.delete(audioEl);
    }
  } else if (audioEl) {
    audioEl.srcObject = null;
    audioEl.muted = true;
    remoteAudioEls.delete(audioEl);
  }
  updateAudioUnlockBanner();

  renderOpponentFan(pod, info.hand_count || 0);
}

function renderMyPod(info, state) {
  mySeatTag.textContent = info.is_host ? 'HOST(YOU)' : 'YOU';
  myNameText.textContent = info.name || 'You';

  const isTurn = state.started && state.turn_seat === mySeat;
  myCamBox.parentElement.classList.toggle('is-turn', isTurn);
}

/* The hand sits at the bottom of the centre void, so the row has to compress
   as the hand grows: cards overlap just enough to always fit. */
function layoutMyHandOverlap() {
  const cards = myHandCardsContainer.children;
  if (!myHandPanel || !cards.length) return;
  const avail = myHandPanel.clientWidth - 10;
  const cardW = cards[0].offsetWidth || 60;
  const gap = Math.max(4, Math.min(10, avail * 0.012));
  let step = cardW + gap;
  if (cards.length > 1) {
    step = Math.min(step, (avail - cardW) / (cards.length - 1));
  }
  myHandCardsContainer.style.setProperty('--card-overlap', `${step - cardW}px`);
}

function renderMyHand() {
  /* While the deal is running the hand is built one card at a time by
     appendMyCard(), so a full re-render here would reveal it all at once. */
  if (dealInProgress) return;

  /* Outside a live round nobody holds cards. This covers the gap between a
     round ending and the next deal, and any stale hand the server sent
     before the host pressed Start Game. */
  if (!currentGameState || !currentGameState.started) {
    myHandCardsContainer.innerHTML = '';
    myHandCardsContainer.classList.add('idle');
    myHandCountTag.textContent = '0 CARDS';
    btnCallUno.style.display = 'none';
    if (btnCallUnoCtrl) btnCallUnoCtrl.style.display = 'none';
    lastHandIds = new Set();
    return;
  }

  myHandCountTag.textContent = `${myCards.length} CARD${myCards.length === 1 ? '' : 'S'}`;

  const isMyTurn = !!(currentGameState && currentGameState.started && currentGameState.turn_seat === mySeat);
  myHandCardsContainer.classList.toggle('idle', !isMyTurn);
  myHandCardsContainer.innerHTML = '';

  const newIds = new Set(myCards.map(c => c.id));

  myCards.forEach((card, idx) => {
    const playable = isMyTurn && isCardPlayable(card) && canPlayCard(card);
    const cardEl = document.createElement('div');
    cardEl.className = 'uno-card card-' + card.color + (isMyTurn ? (playable ? ' playable' : ' not-playable') : '');
    cardEl.innerHTML = getCardInnerMarkup(card);

    if (!lastHandIds.has(card.id)) {
      cardEl.classList.add('draw-in');
      cardEl.style.animationDelay = `${Math.min(idx * 35, 340)}ms`;
    }

    if (playable) cardEl.onclick = () => onCardClicked(card, cardEl);
    myHandCardsContainer.appendChild(cardEl);
  });

  lastHandIds = newIds;
  layoutMyHandOverlap();

  const myInfo = currentGameState && currentGameState.seats[String(mySeat)];
  let canCallUno = false;
  if (isMyTurn && myInfo && myCards.length === 2 && !myInfo.called_uno) {
    canCallUno = myCards.some(c => isCardPlayable(c));
  }
  btnCallUno.style.display = canCallUno ? 'inline-block' : 'none';
  if (btnCallUnoCtrl) btnCallUnoCtrl.style.display = canCallUno ? 'flex' : 'none';
}

/* ---- incremental builders used by the deal ---------------------------- */

/* Indexes into the child count so the hand can be rebuilt from scratch at
   any point (e.g. when the `hand` message lands mid-deal). */
function appendMyCard() {
  const idx = myHandCardsContainer.childElementCount;
  const card = myCards[idx];
  if (!card) return;
  const el = document.createElement('div');
  el.className = 'uno-card card-' + card.color + ' deal-in';
  el.innerHTML = getCardInnerMarkup(card);
  myHandCardsContainer.appendChild(el);
  myHandCountTag.textContent = `${idx + 1} CARD${idx === 0 ? '' : 'S'}`;
  layoutMyHandOverlap();
}

function applyFanPitch(fan) {
  const n = fan.childElementCount;
  if (n > 1) {
    fan.style.setProperty('--fan-pitch', Math.min(FAN_PITCH, (FAN_MAX_W - 44) / (n - 1)) + 'px');
  }
}

function appendOpponentBack(pod) {
  const tray = pod.querySelector('.opp-card-tray');
  if (!tray) return;
  let fan = tray.querySelector('.opp-card-fan');
  if (!fan) {
    fan = document.createElement('div');
    fan.className = 'opp-card-fan';
    tray.appendChild(fan);
  }
  if (fan.childElementCount >= MAX_BACKS_SHOWN) return;
  const back = document.createElement('div');
  back.className = 'card-back';
  back.innerHTML = '<div class="back-oval"><span class="back-uno">UNO</span></div>';
  fan.appendChild(back);
  applyFanPitch(fan);
}

/* ==========================================================================
   DEAL ANIMATION — seven cards are thrown from the deck, one at a time, to
   each seat in turn. Hands are EMPTY until their own cards land: the deal is
   what actually builds every hand on screen, so nothing pops in up front.
   ========================================================================== */
const DEAL_CARDS_PER_PLAYER = 7;
const DEAL_STEP_MS = 130;    // gap between consecutive throws
const DEAL_FLIGHT_MS = 520;  // how long a single card spends in the air

function prefersReducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/* `onLand(key)` fires as each card settles, key being the seat number or
   'me' — that is what grows the hands one card at a time. */
function playDealAnimation(slots, onLand) {
  if (!fxLayer || !btnDrawDeck || prefersReducedMotion()) return 0;
  const podList = slots.map(s => opponentPods[s]).filter(Boolean);
  podList.push(myPodSlot);

  const src = btnDrawDeck.getBoundingClientRect();
  if (!src.width) return 0;

  const cardW = Math.max(34, Math.round(src.width * 0.66));
  const cardH = Math.round(cardW * 1.5);
  const originX = src.left + src.width / 2 - cardW / 2;
  const originY = src.top + src.height / 2 - cardH / 2;

  const targets = podList.filter(Boolean).map(pod => {
    const box = pod.querySelector('.camera-box') || pod;
    const r = box.getBoundingClientRect();
    return {
      key: pod === myPodSlot ? 'me' : Number(pod.dataset.seat),
      x: r.left + r.width / 2,
      y: r.top + r.height / 2
    };
  });
  if (!targets.length) return 0;

  let step = 0;
  for (let round = 0; round < DEAL_CARDS_PER_PLAYER; round++) {
    targets.forEach(target => {
      const el = document.createElement('div');
      el.className = 'fly-card';
      el.style.width = cardW + 'px';
      el.style.height = cardH + 'px';
      el.style.left = originX + 'px';
      el.style.top = originY + 'px';
      fxLayer.appendChild(el);

      const dx = target.x - cardW / 2 - originX + (Math.random() - 0.5) * cardW * 0.35;
      const dy = target.y - cardH / 2 - originY + (Math.random() - 0.5) * cardH * 0.35;
      const spin = (Math.random() - 0.5) * 44;

      const anim = el.animate([
        { transform: 'translate3d(0,0,0) rotate(0deg) scale(1)', opacity: 0, offset: 0 },
        { transform: `translate3d(${dx * 0.5}px, ${dy * 0.5 - 34}px, 0) rotate(${spin * 0.45}deg) scale(1.08)`, opacity: 1, offset: 0.45 },
        { transform: `translate3d(${dx}px, ${dy}px, 0) rotate(${spin}deg) scale(0.85)`, opacity: 1, offset: 1 }
      ], {
        duration: DEAL_FLIGHT_MS,
        delay: step * DEAL_STEP_MS,
        easing: 'cubic-bezier(0.33, 0.62, 0.36, 1)',
        fill: 'backwards'
      });
      anim.onfinish = () => {
        el.remove();
        if (onLand) onLand(target.key);
      };

      sfx.dealTick(step);
      step++;
    });
  }
  return step * DEAL_STEP_MS + DEAL_FLIGHT_MS;
}

function startDeal(state) {
  updateSeatToSlotMapping(state.seats);
  const liveSlots = SLOT_NAMES.filter(slot => Object.values(seatToSlotMap).indexOf(slot) !== -1);

  dealInProgress = true;
  dealRevealed = Object.create(null);
  clearTimeout(dealTimer);

  /* Nothing is in anybody's hand until the deal puts it there. */
  centreSeatKey = '';
  renderCentreSeats(state);
  myHandCardsContainer.innerHTML = '';
  myHandCountTag.textContent = '0 CARDS';
  SLOT_NAMES.forEach(slot => {
    const pod = opponentPods[slot];
    const tray = pod && pod.querySelector('.opp-card-tray');
    if (tray) tray.innerHTML = '';
  });

  const expected = (liveSlots.length + 1) * DEAL_CARDS_PER_PLAYER;
  let dealt = 0;

  const duration = playDealAnimation(liveSlots, key => {
    dealt++;
    dealRevealed[key] = (dealRevealed[key] || 0) + 1;
    if (key === 'me') {
      appendMyCard();
    } else {
      const slot = seatToSlotMap[key];
      const pod = slot && opponentPods[slot];
      if (pod) appendOpponentBack(pod);
      const el = centreSeatEls[key];
      if (el) el.querySelector('.cs-count').textContent = String(dealRevealed[key]);
    }
    if (dealt >= expected) finishDeal();
  });

  if (!duration) {
    /* Reduced motion: skip the theatre and reveal everything at once. */
    finishDeal();
    return;
  }

  dealTimer = setTimeout(finishDeal, duration + 200);
}

function finishDeal() {
  clearTimeout(dealTimer);
  dealTimer = null;
  if (!dealInProgress) return;
  dealInProgress = false;
  if (currentGameState) renderGameState(currentGameState);
  else renderMyHand();
}

/* A played card lifts out of my hand and flies onto the discard pile. */
function animatePlayToDiscard(sourceEl) {
  if (!fxLayer || !sourceEl || !discardTopCardContainer) return;
  const from = sourceEl.getBoundingClientRect();
  const to = discardTopCardContainer.getBoundingClientRect();
  if (!from.width || !to.width) return;

  const cs = getComputedStyle(sourceEl);
  const ghost = document.createElement('div');
  ghost.className = 'fly-card is-face';
  ghost.style.width = from.width + 'px';
  ghost.style.height = from.height + 'px';
  ghost.style.left = from.left + 'px';
  ghost.style.top = from.top + 'px';
  ghost.style.background = cs.background;
  ghost.style.borderColor = cs.borderColor;
  ghost.style.borderWidth = cs.borderWidth;
  ghost.style.borderRadius = cs.borderRadius;
  ghost.style.color = cs.color;
  ghost.style.fontFamily = cs.fontFamily;
  ghost.style.fontWeight = cs.fontWeight;
  ghost.style.setProperty('--cw', from.width + 'px');
  ghost.innerHTML = sourceEl.innerHTML;
  fxLayer.appendChild(ghost);

  const dx = to.left + to.width / 2 - (from.left + from.width / 2);
  const dy = to.top + to.height / 2 - (from.top + from.height / 2);
  const scale = to.width / from.width;

  const anim = ghost.animate([
    { transform: 'translate3d(0,0,0) scale(1) rotate(0deg)', opacity: 1, offset: 0 },
    { transform: `translate3d(${dx * 0.55}px, ${dy * 0.55 - 46}px, 0) scale(${1 + (scale - 1) * 0.4}) rotate(7deg)`, opacity: 1, offset: 0.5 },
    { transform: `translate3d(${dx}px, ${dy}px, 0) scale(${scale}) rotate(-3deg)`, opacity: 1, offset: 1 }
  ], { duration: 400, easing: 'cubic-bezier(0.3, 0.7, 0.4, 1)', fill: 'forwards' });
  anim.onfinish = () => setTimeout(() => ghost.remove(), 80);
}

function canPlayCard(card) {
  if (!drewThisTurn) return true;
  return !!lastDrawnCardId && card.id === lastDrawnCardId;
}

function isCardPlayable(card) {
  if (!currentGameState || !currentGameState.top_card) return true;
  if (card.color === 'black') return true;
  return card.color === currentGameState.current_color || card.value === currentGameState.top_card.value;
}

function onCardClicked(card, cardEl) {
  if (!canPlayCard(card)) return;
  sfx.playCard();
  if (cardEl) {
    animatePlayToDiscard(cardEl);
    cardEl.classList.add('playing-out');
  }
  if (card.color === 'black') {
    pendingWildCardId = card.id;
    modalColorChoice.classList.add('active');
    return;
  }
  sendServerMessage({ type: 'play', card_id: card.id });
}

function formatCardSymbol(val) {
  if (val === 'skip') return '⊘';
  if (val === 'reverse') return '⇄';
  if (val === 'draw2') return '+2';
  if (val === 'wild4') return '+4';
  if (val === 'wild') return '★';
  return val;
}

function getCardInnerMarkup(card) {
  const sym = formatCardSymbol(card.value);
  return `
    <span class="card-corner-tl">${sym}</span>
    <div class="uno-card-oval">
      <span class="card-symbol-center">${sym}</span>
    </div>
    <span class="card-corner-br">${sym}</span>
  `;
}

function createCardHTML(card, isLarge = false) {
  return `<div class="uno-card card-${card.color}${isLarge ? ' lg' : ''}">${getCardInnerMarkup(card)}</div>`;
}

function escapeHTML(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

document.querySelectorAll('.btn-color-choice').forEach(btn => {
  btn.onclick = () => {
    modalColorChoice.classList.remove('active');
    if (pendingWildCardId) {
      sendServerMessage({
        type: 'play',
        card_id: pendingWildCardId,
        chosen_color: btn.dataset.color
      });
      pendingWildCardId = null;
    }
  };
});

btnDrawDeck.onclick = () => {
  if (
    !drawInFlight &&
    !drewThisTurn &&
    currentGameState &&
    currentGameState.started &&
    currentGameState.turn_seat === mySeat
  ) {
    drawInFlight = true;
    sfx.drawCard();
    sendServerMessage({ type: 'draw' });
    setTimeout(() => { drawInFlight = false; }, 5000);
  }
};

function callUnoAction() {
  sfx.unoAlert();
  sendServerMessage({ type: 'call_uno' });
  btnCallUno.style.display = 'none';
  if (btnCallUnoCtrl) btnCallUnoCtrl.style.display = 'none';
}

btnCallUno.onclick = callUnoAction;
if (btnCallUnoCtrl) btnCallUnoCtrl.onclick = callUnoAction;

btnStartGame.onclick = () => sendServerMessage({ type: 'start' });

btnPlayAgain.onclick = () => {
  modalGameOver.classList.remove('active');
  sendServerMessage({ type: 'start' });
};

btnBackToLobby.onclick = () => {
  modalGameOver.classList.remove('active');
  leaveTableAndReturnToLobby();
};

btnExitTable.onclick = () => modalConfirmExit.classList.add('active');
btnConfirmExitNo.onclick = () => modalConfirmExit.classList.remove('active');
btnConfirmExitYes.onclick = () => {
  modalConfirmExit.classList.remove('active');
  leaveTableAndReturnToLobby();
};

function leaveTableAndReturnToLobby() {
  sendServerMessage({ type: 'leave' });
  localStorage.removeItem(`uno_token_${myRoom}`);
  localStorage.removeItem('uno_last_room');
  reconnectToken = null;
  cleanupSessionLocal();
}

let exitBeaconSent = false;
window.addEventListener('pagehide', () => {
  if (exitBeaconSent || !reconnectToken || !myRoom) return;
  exitBeaconSent = true;
  fetch('/api/leave', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ room: myRoom, token: reconnectToken }),
    keepalive: true,
    credentials: 'same-origin'
  }).catch(() => {});
});

function cleanupSessionLocal() {
  stopKeepAlivePing();
  try { if (ws) { ws.onclose = null; ws.close(); } } catch(e) {}
  ws = null;

  Object.values(peerConnections).forEach(entry => { try { entry.pc.close(); } catch(e) {} });
  Object.keys(peerConnections).forEach(k => delete peerConnections[k]);
  Object.keys(audioMeters).forEach(k => removeAudioMeter(k));
  Object.keys(remoteStreams).forEach(k => delete remoteStreams[k]);
  remoteVideoEls.forEach(el => { try { el.pause(); el.srcObject = null; el.classList.remove('live'); } catch(e) {} });
  remoteVideoEls.clear();
  placeholderStream = new MediaStream();

  if (localStream) {
    localStream.getTracks().forEach(t => t.stop());
    localStream = null;
  }
  camActive = false;
  micActive = false;
  myToken = null;
  reconnectToken = null;
  mySeat = null;
  currentGameState = null;
  myCards = [];
  drewThisTurn = false;
  lastDrawnCardId = null;
  drawInFlight = false;
  pendingPlays.clear();

  dealInProgress = false;
  clearTimeout(dealTimer);
  dealTimer = null;
  if (fxLayer) fxLayer.innerHTML = '';
  lastHandIds = new Set();

  centreSeatKey = '';
  centreSeats.innerHTML = '';
  Object.keys(centreSeatEls).forEach(k => delete centreSeatEls[k]);

  SLOT_NAMES.forEach(slot => {
    const pod = opponentPods[slot];
    if (pod) renderOpponentFan(pod, 0);
  });
  closeDeviceMenu();
  clearChatLog();

  gameView.classList.remove('active');
  gameView.style.display = 'none';
  modalGameOver.classList.remove('active');
  modalColorChoice.classList.remove('active');

  hideReconnectOverlay();
  lobby.classList.remove('hidden');
  lobby.style.display = 'flex';
  resetJoinButton('');
}

function handleGameOver(msg) {
  const won = msg.winner_seat === mySeat;
  if (won) sfx.victory();
  gameOverTrophy.textContent = won ? '🏆' : '👏';
  gameOverTitle.textContent = won ? 'You Won!' : `${msg.winner_name} Wins!`;
  gameOverMsg.textContent = won
    ? `You cleared your hand and earn +${msg.round_points || 0} points.`
    : 'Points awarded from cards left in opponents\' hands.';

  leaderboardBox.innerHTML = '';
  const scores = Array.isArray(msg.scores) ? msg.scores : [];
  scores.forEach((row, i) => {
    const isWinner = row.seat === msg.winner_seat;
    const el = document.createElement('div');
    el.className = `lb-row${isWinner ? ' winner' : ''}`;
    el.style.animationDelay = `${i * 70}ms`;
    const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `#${i + 1}`;
    el.innerHTML = `
      <span class="lb-rank">${medal}</span>
      <span class="lb-name">${escapeHTML(row.name)}${row.seat === mySeat ? ' (you)' : ''}</span>
      ${row.round_points ? `<span class="lb-round">+${row.round_points}</span>` : ''}
      <span class="lb-total">${row.score} pts</span>
    `;
    leaderboardBox.appendChild(el);
  });

  btnPlayAgain.style.display = (currentGameState && currentGameState.host_seat === mySeat) ? 'block' : 'none';
  modalGameOver.classList.add('active');
}

function setupMyCameraFeed() {
  const videoEl = myCamBox.querySelector('video');
  if (videoEl) {
    videoEl.muted = true;
    videoEl.volume = 0;
    videoEl.defaultMuted = true;
    if (localStream && camActive) {
      const vTrack = localStream.getVideoTracks()[0];
      if (vTrack && vTrack.readyState === 'live') {
        if (!videoEl.srcObject || videoEl.srcObject.getVideoTracks()[0] !== vTrack) {
          videoEl.srcObject = new MediaStream([vTrack]);
        }
        videoEl.classList.add('live');
        videoEl.play().catch(() => {});
      } else {
        videoEl.classList.remove('live');
        videoEl.srcObject = null;
      }
    } else {
      videoEl.classList.remove('live');
      videoEl.srcObject = null;
    }
  }
}

btnToggleCam.onclick = () => camActive ? disableCamera() : enableCamera();
btnToggleMic.onclick = () => micActive ? disableMic() : enableMic();

const copyInviteLink = () => {
  const inviteUrl = `${location.origin}${location.pathname}?room=${encodeURIComponent(myRoom)}`;
  navigator.clipboard.writeText(inviteUrl).then(() => {
    showToast('Invite link copied to clipboard!');
  }).catch(() => {
    showToast(`Room Code: ${myRoom}`);
  });
};

btnCopyRoom.onclick = copyInviteLink;
if (btnCopyRoomIcon) {
  btnCopyRoomIcon.onclick = e => { e.stopPropagation(); copyInviteLink(); };
}

let handResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(handResizeTimer);
  handResizeTimer = setTimeout(layoutMyHandOverlap, 120);
});

let placeholderStream = new MediaStream();

function getLocalMediaStream() {
  return localStream && (localStream.getTracks().length > 0 ? localStream : null);
}

function syncPeerConnections(peerList) {
  const activeTokens = new Set(peerList.map(p => p.peer_id).filter(t => t !== myToken));
  peerList.forEach(p => {
    if (p.peer_id !== myToken && !peerConnections[p.peer_id]) {
      const pcEntry = createPeerConnection(p.peer_id, p.seat);
      if (myToken > p.peer_id) {
        setTimeout(() => {
          if (pcEntry && pcEntry.pc.signalingState === 'stable' && !pcEntry.makingOffer) {
            forceRenegotiate(p.peer_id);
          }
        }, 100);
      }
    }
  });
  Object.keys(peerConnections).forEach(token => {
    if (!activeTokens.has(token)) {
      const seat = peerConnections[token].seat;
      removeAudioMeter(seat);
      delete remoteStreams[seat];
      try { peerConnections[token].pc.close(); } catch(e) {}
      delete peerConnections[token];
    }
  });
}

function createPeerConnection(peerToken, seatNum) {
  const isPolite = myToken > peerToken;
  const pc = new RTCPeerConnection({ iceServers });
  const audioTransceiver = pc.addTransceiver('audio', { direction: 'sendrecv' });
  const videoTransceiver = pc.addTransceiver('video', { direction: 'sendrecv' });
  const entry = {
    pc, isPolite, seat: seatNum,
    senders: {
      audio: audioTransceiver.sender,
      video: videoTransceiver.sender,
    },
    makingOffer: false,
    ignoreOffer: false,
    pendingCandidates: [],
    hasRemoteDescription: false,
    lastConnectedAt: 0,
    restartTimer: null,
  };
  peerConnections[peerToken] = entry;
  refreshLocalTracksOnPeer(entry);
  debugLog(`pc:create seat=${seatNum} polite=${isPolite} hasLocalVideo=${!!(localStream && localStream.getVideoTracks().length)}`);

  pc.onnegotiationneeded = async () => {
    try {
      if (entry.makingOffer) return;
      entry.makingOffer = true;
      await pc.setLocalDescription();
      sendServerMessage({
        type: 'webrtc',
        target: peerToken,
        payload: { description: pc.localDescription }
      });
    } catch(err) {
      console.error('[WebRTC] Negotiation error:', err);
    } finally {
      entry.makingOffer = false;
    }
  };

  pc.onicecandidate = ev => {
    if (ev.candidate) {
      sendServerMessage({
        type: 'webrtc',
        target: peerToken,
        payload: { candidate: ev.candidate }
      });
    }
  };

  pc.onicecandidateerror = ev => {
    if (ev.errorCode !== 701) {
      console.warn('[WebRTC] ICE error', ev.errorCode, ev.errorText);
    }
  };

  pc.ontrack = ev => {
    if (localStream) {
      const localTrackIds = new Set(localStream.getTracks().map(t => t.id));
      if (localTrackIds.has(ev.track.id)) return;
    }
    debugLog(`pc:ontrack seat=${seatNum} kind=${ev.track.kind} readyState=${ev.track.readyState} streams=${ev.streams.length}`);

    let stream = remoteStreams[seatNum];
    if (!stream) {
      stream = new MediaStream();
      remoteStreams[seatNum] = stream;
    }
    stream.getTracks().forEach(t => {
      if (t.kind === ev.track.kind) stream.removeTrack(t);
    });
    stream.addTrack(ev.track);
    attachRemoteStreamToSeat(seatNum, stream);
    if (ev.track.kind === 'audio') {
      attachAudioMeter(seatNum, stream, () => getOpponentMeterEls(seatNum));
    }
  };

  pc.oniceconnectionstatechange = () => {
    const s = pc.iceConnectionState;
    debugLog(`pc:ice seat=${seatNum} state=${s}`);
    if (s === 'connected' || s === 'completed') {
      entry.lastConnectedAt = Date.now();
      if (entry.restartTimer) { clearTimeout(entry.restartTimer); entry.restartTimer = null; }
    } else if (s === 'failed' || s === 'disconnected') {
      if (!entry.restartTimer) {
        entry.restartTimer = setTimeout(() => {
          entry.restartTimer = null;
          recoverConnection(peerToken);
        }, s === 'failed' ? 800 : 4000);
      }
    }
  };

  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    debugLog(`pc:connection seat=${seatNum} state=${s}`);
    if (s === 'connected') {
      if (remoteStreams[seatNum]) {
        attachRemoteStreamToSeat(seatNum, remoteStreams[seatNum]);
      }
      refreshLocalTracksOnPeer(entry);
    } else if (s === 'failed') {
      recoverConnection(peerToken);
    }
  };

  return entry;
}

function refreshLocalTracksOnPeer(entry) {
  if (!entry || !entry.pc) return;
  const pc = entry.pc;
  const realStream = getLocalMediaStream();
  ['audio', 'video'].forEach(kind => {
    const sender = entry.senders[kind];
    const newTrack = realStream ? realStream.getTracks().find(t => t.kind === kind) : null;
    if (sender && sender.track !== newTrack) sender.replaceTrack(newTrack).catch(err => {
      console.warn(`[WebRTC] ${kind} track replacement failed:`, err);
    });
  });
}

function refreshLocalTracksOnAllPeers() {
  Object.values(peerConnections).forEach(entry => refreshLocalTracksOnPeer(entry));
}

function recoverConnection(peerToken) {
  const entry = peerConnections[peerToken];
  if (!entry) return;
  const { pc } = entry;
  if (pc.signalingState === 'stable' && pc.restartIce) {
    try { pc.restartIce(); } catch(e) {}
  } else if (pc.signalingState === 'stable' || pc.iceConnectionState === 'failed') {
    forceRenegotiate(peerToken);
  }
}

async function forceRenegotiate(peerToken) {
  const entry = peerConnections[peerToken];
  if (!entry) return;
  const { pc } = entry;
  if (pc.signalingState === 'have-local-offer') {
    try { await pc.setLocalDescription({ type: 'rollback' }); } catch(e) {}
  }
  if (pc.signalingState !== 'stable') return;
  try {
    entry.makingOffer = true;
    await pc.setLocalDescription();
    sendServerMessage({
      type: 'webrtc',
      target: peerToken,
      payload: { description: pc.localDescription }
    });
  } catch(err) {
    console.error('[WebRTC] forceRenegotiate error:', err);
  } finally {
    entry.makingOffer = false;
  }
}

async function handleWebRTCSignal(fromToken, fromSeat, payload) {
  let entry = peerConnections[fromToken];
  if (!entry) entry = createPeerConnection(fromToken, fromSeat);
  const { pc, isPolite } = entry;

  try {
    if (payload.description) {
      const desc = payload.description;
      const isOffer = desc.type === 'offer';

      const offerCollision = isOffer && (entry.makingOffer || pc.signalingState !== 'stable');
      entry.ignoreOffer = !isPolite && offerCollision;
      if (entry.ignoreOffer) {
        setTimeout(() => {
          if (pc.signalingState === 'stable' && !entry.makingOffer) {
            forceRenegotiate(fromToken);
          }
        }, 1500);
        return;
      }
      if (offerCollision && isPolite) {
        await pc.setLocalDescription({ type: 'rollback' });
      }

      await pc.setRemoteDescription(desc);
      entry.hasRemoteDescription = true;

      while (entry.pendingCandidates.length > 0) {
        const c = entry.pendingCandidates.shift();
        try { await pc.addIceCandidate(c); } catch(e) {}
      }

      if (isOffer) {
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        sendServerMessage({
          type: 'webrtc',
          target: fromToken,
          payload: { description: pc.localDescription }
        });
      }
    } else if (payload.candidate) {
      if (!entry.hasRemoteDescription) {
        entry.pendingCandidates.push(payload.candidate);
      } else {
        try {
          await pc.addIceCandidate(payload.candidate);
        } catch(err) {
          if (!entry.ignoreOffer) console.warn('[WebRTC] addIceCandidate:', err);
        }
      }
    }
  } catch(err) {
    console.error('[WebRTC] Signal error:', err);
  }
}

function attachRemoteStreamToSeat(seatNum, stream) {
  if (stream) remoteStreams[seatNum] = stream;
  const slotNum = seatToSlotMap[seatNum];
  if (!slotNum) return;
  const pod = opponentPods[slotNum];
  if (!pod) return;

  const videoEl = pod.querySelector('video');
  const audioEl = pod.querySelector('audio.peer-audio');
  const s = remoteStreams[seatNum];
  const info = currentGameState && currentGameState.seats[String(seatNum)];
  const camOn = info ? !!info.cam_on : false;

  if (videoEl && s) {
    if (camOn) {
      const vTrack = s.getVideoTracks()[0];
      if (vTrack && vTrack.readyState === 'live') {
        if (videoEl.srcObject !== s) {
          videoEl.srcObject = s;
        }
        videoEl.muted = true;
        videoEl.classList.add('live');
        remoteVideoEls.add(videoEl);
        videoEl.play().catch(() => {});
      } else {
        videoEl.classList.remove('live');
        videoEl.srcObject = null;
        remoteVideoEls.delete(videoEl);
      }
    } else {
      videoEl.classList.remove('live');
      videoEl.srcObject = null;
      remoteVideoEls.delete(videoEl);
    }
  }

  if (audioEl && s) {
    const aTrack = s.getAudioTracks()[0];
    if (aTrack) {
      const audioOnlyStream = new MediaStream([aTrack]);
      if (!audioEl.srcObject || audioEl.srcObject.getAudioTracks()[0] !== aTrack) {
        audioEl.srcObject = audioOnlyStream;
      }
      audioEl.muted = !audioUnlocked;
      remoteAudioEls.add(audioEl);
      audioEl.play().catch(() => {});
    } else {
      audioEl.srcObject = null;
      audioEl.muted = true;
      remoteAudioEls.delete(audioEl);
    }
  } else if (audioEl) {
    audioEl.srcObject = null;
    audioEl.muted = true;
    remoteAudioEls.delete(audioEl);
  }
  updateAudioUnlockBanner();

  if (info) renderCentreSeats(currentGameState);
}

async function enableCamera() {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    const realStream = getLocalMediaStream();
    const vTrack = realStream ? realStream.getVideoTracks()[0] : null;
    if (vTrack && vTrack.readyState === 'live') {
      vTrack.enabled = true;
    } else {
      const s = await navigator.mediaDevices.getUserMedia({
        video: {
          deviceId: currentCamId ? { ideal: currentCamId } : undefined,
          width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 24 }, facingMode: 'user'
        }
      });
      const newVTrack = s.getVideoTracks()[0];
      if (newVTrack) {
        if (!localStream) localStream = new MediaStream();
        if (vTrack) localStream.removeTrack(vTrack);
        localStream.addTrack(newVTrack);
        currentCamId = newVTrack.getSettings().deviceId || currentCamId;
        localStorage.setItem('uno_cam_id', currentCamId);
      }
    }
    camActive = true;
    camGroup.classList.add('active');
    camGroup.classList.remove('muted');
    localStorage.setItem('uno_pref_cam', '1');
    await refreshLocalTracksOnAllPeers();
    setupMyCameraFeed();
    sendServerMessage({ type: 'media_state', cam_on: true, mic_on: micActive });
    return true;
  } catch(e) {
    console.warn('Camera error:', e);
    showToast('Camera unavailable (check permissions)');
    return false;
  }
}

function disableCamera() {
  if (localStream) {
    localStream.getVideoTracks().forEach(t => {
      t.enabled = false;
      t.stop();
      localStream.removeTrack(t);
    });
  }
  camActive = false;
  camGroup.classList.remove('active');
  camGroup.classList.add('muted');
  localStorage.setItem('uno_pref_cam', '0');
  Object.values(peerConnections).forEach(entry => {
    const sender = entry.senders.video;
    if (sender) sender.replaceTrack(null).catch(() => {});
  });
  setupMyCameraFeed();
  sendServerMessage({ type: 'media_state', cam_on: false, mic_on: micActive });
}

async function enableMic() {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    const realStream = getLocalMediaStream();
    const aTrack = realStream ? realStream.getAudioTracks()[0] : null;
    if (aTrack && aTrack.readyState === 'live') {
      aTrack.enabled = true;
    } else {
      const s = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: currentMicId ? { ideal: currentMicId } : undefined,
          echoCancellation: true, noiseSuppression: true, autoGainControl: true
        }
      });
      const newATrack = s.getAudioTracks()[0];
      if (newATrack) {
        if (!localStream) localStream = new MediaStream();
        if (aTrack) localStream.removeTrack(aTrack);
        localStream.addTrack(newATrack);
        currentMicId = newATrack.getSettings().deviceId || currentMicId;
        localStorage.setItem('uno_mic_id', currentMicId);
      }
    }
    micActive = true;
    micGroup.classList.add('active');
    localStorage.setItem('uno_pref_mic', '1');
    await refreshLocalTracksOnAllPeers();
    attachAudioMeter('local', localStream, getLocalMeterEls);
    sendServerMessage({ type: 'media_state', cam_on: camActive, mic_on: true });
    return true;
  } catch(e) {
    console.warn('Microphone error:', e);
    return false;
  }
}

function disableMic() {
  if (localStream) {
    localStream.getAudioTracks().forEach(t => {
      t.enabled = false;
      t.stop();
      localStream.removeTrack(t);
    });
  }
  micActive = false;
  micGroup.classList.remove('active');
  localStorage.setItem('uno_pref_mic', '0');
  Object.values(peerConnections).forEach(entry => {
    const sender = entry.senders.audio;
    if (sender) sender.replaceTrack(null).catch(() => {});
  });
  removeAudioMeter('local');
  const els = getLocalMeterEls();
  if (els) {
    els.meter.classList.remove('active');
    els.fill.style.height = '0%';
  }
  sendServerMessage({ type: 'media_state', cam_on: camActive, mic_on: false });
}

/* ------------------------------------------------- mic / camera devices --
   The chevron halves of the mic and camera controls open a small picker of
   the devices the browser exposes. Choosing one re-acquires that single
   track and swaps it into every live peer connection, so a switch never
   drops the call. */
let deviceMenuKind = null;

function closeDeviceMenu() {
  deviceMenuKind = null;
  if (!deviceMenu) return;
  deviceMenu.classList.remove('open');
  deviceMenu.innerHTML = '';
}

async function openDeviceMenu(kind, anchor) {
  if (!deviceMenu || !navigator.mediaDevices) return;
  const isCam = kind === 'video';
  deviceMenuKind = kind;
  deviceMenu.innerHTML = '';

  const title = document.createElement('div');
  title.className = 'device-menu-title';
  title.textContent = isCam ? 'Camera' : 'Microphone';
  deviceMenu.appendChild(title);

  let devices = [];
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    devices = all.filter(d => d.kind === (isCam ? 'videoinput' : 'audioinput'));
  } catch (e) {
    devices = [];
  }

  const current = isCam ? currentCamId : currentMicId;

  if (!devices.length) {
    const empty = document.createElement('div');
    empty.className = 'device-empty';
    empty.textContent = 'No devices found';
    deviceMenu.appendChild(empty);
  } else {
    devices.forEach((d, i) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = d.label || `${isCam ? 'Camera' : 'Microphone'} ${i + 1}`;
      if (d.deviceId && d.deviceId === current) btn.classList.add('active');
      btn.onclick = () => { closeDeviceMenu(); switchDevice(kind, d.deviceId); };
      deviceMenu.appendChild(btn);
    });
  }

  deviceMenu.classList.add('open');
  const r = anchor.getBoundingClientRect();
  const w = deviceMenu.offsetWidth;
  const h = deviceMenu.offsetHeight;
  const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  let top = r.top - h - 12;
  if (top < 8) top = r.bottom + 12;
  deviceMenu.style.left = `${left}px`;
  deviceMenu.style.top = `${top}px`;
}

async function switchDevice(kind, deviceId) {
  const isCam = kind === 'video';
  if (!navigator.mediaDevices || !deviceId) return;
  try {
    const s = await navigator.mediaDevices.getUserMedia(
      isCam
        ? { video: { deviceId: { exact: deviceId }, width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 24 } } }
        : { audio: { deviceId: { exact: deviceId }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } }
    );
    const track = isCam ? s.getVideoTracks()[0] : s.getAudioTracks()[0];
    if (!track) throw new Error('no track');

    if (!localStream) localStream = new MediaStream();
    localStream.getTracks()
      .filter(t => t.kind === (isCam ? 'video' : 'audio'))
      .forEach(t => { t.stop(); localStream.removeTrack(t); });
    localStream.addTrack(track);

    if (isCam) {
      currentCamId = deviceId;
      localStorage.setItem('uno_cam_id', currentCamId);
      camActive = true;
      camGroup.classList.add('active');
      camGroup.classList.remove('muted');
      localStorage.setItem('uno_pref_cam', '1');
      setupMyCameraFeed();
    } else {
      currentMicId = deviceId;
      localStorage.setItem('uno_mic_id', currentMicId);
      micActive = true;
      micGroup.classList.add('active');
      localStorage.setItem('uno_pref_mic', '1');
      attachAudioMeter('local', localStream, getLocalMeterEls);
    }

    await refreshLocalTracksOnAllPeers();
    sendServerMessage({ type: 'media_state', cam_on: camActive, mic_on: micActive });
  } catch (e) {
    showToast(`Could not switch ${isCam ? 'camera' : 'microphone'}`);
  }
}

if (btnMicSettings) {
  btnMicSettings.onclick = e => {
    e.stopPropagation();
    if (deviceMenuKind === 'audio') closeDeviceMenu();
    else openDeviceMenu('audio', btnMicSettings);
  };
}
if (btnCamSettings) {
  btnCamSettings.onclick = e => {
    e.stopPropagation();
    if (deviceMenuKind === 'video') closeDeviceMenu();
    else openDeviceMenu('video', btnCamSettings);
  };
}
document.addEventListener('pointerdown', e => {
  if (!deviceMenuKind) return;
  if (deviceMenu && deviceMenu.contains(e.target)) return;
  if (e.target === btnMicSettings || e.target === btnCamSettings) return;
  closeDeviceMenu();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && deviceMenuKind) closeDeviceMenu();
});

function startWebRTCSanityCheck() {
  setInterval(() => {
    Object.entries(peerConnections).forEach(([token, entry]) => {
      const seat = entry.seat;
      const info = currentGameState && currentGameState.seats[String(seat)];
      if (!info) return;
      const stream = remoteStreams[seat];
      const hasVideo = stream && stream.getVideoTracks().some(t => t.readyState !== 'ended');
      const hasAudio = stream && stream.getAudioTracks().some(t => t.readyState !== 'ended');
      const missingVideo = info.cam_on && !hasVideo;
      const missingAudio = info.mic_on && !hasAudio;
      const stuck = entry.pc.signalingState !== 'stable' && entry.pc.signalingState !== 'closed';
      const stalled = entry.pc.connectionState === 'failed' || entry.pc.iceConnectionState === 'failed';

      if ((missingVideo || missingAudio || stuck || stalled) && !entry.makingOffer) {
        recoverConnection(token);
      }

      if (entry.pc.connectionState === 'connected' && entry.pc.signalingState === 'stable') {
        refreshLocalTracksOnPeer(entry);
      }
    });
  }, 3000);
}
startWebRTCSanityCheck();

function showReconnectOverlay(room, name) {
  lobby.classList.add('hidden');
  lobby.style.display = 'none';
  reconnectScreen.classList.remove('hidden');
  reconnectScreen.style.display = 'flex';
  reconnectText.innerHTML = `Rejoining <span class="reconnect-room-tag">${escapeHTML(room.toUpperCase())}</span> as ${escapeHTML(name)}...`;
}

function hideReconnectOverlay() {
  reconnectScreen.classList.add('hidden');
  reconnectScreen.style.display = 'none';
}

btnCancelReconnect.onclick = () => {
  try { if (ws) { ws.onclose = null; ws.close(); } } catch(e) {}
  ws = null;
  resetJoinButton('');
};

async function startSession(room, name, camPref, micPref) {
  myRoom = room;
  try { sfx.init(); } catch(e) {}
  ensureAudioContext();

  await loadIceConfiguration().catch(() => {});

  if (camPref) {
    try { await enableCamera(); } catch(e) { console.warn('Cam init:', e); }
  }
  if (micPref) {
    try { await enableMic(); } catch(e) { console.warn('Mic init:', e); }
  }

  connectWebSocket();
}

window.btnJoinClicked = function() {
  const name = inputName.value.trim();
  const room = (inputRoom.value.trim() || 'main').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 24) || 'main';

  if (!name) {
    lobbyErr.textContent = 'Please enter your name.';
    return;
  }
  lobbyErr.textContent = '';
  btnJoin.disabled = true;
  btnJoin.innerHTML = '<span>Connecting...</span>';
  localStorage.setItem('uno_player_name', name);

  startSession(room, name, checkCamera.checked, checkMic.checked);
};
if (btnJoin) btnJoin.onclick = window.btnJoinClicked;

inputName.addEventListener('keydown', e => { if (e.key === 'Enter') btnJoin.click(); });
inputRoom.addEventListener('keydown', e => { if (e.key === 'Enter') btnJoin.click(); });
checkCamera.addEventListener('change', () => localStorage.setItem('uno_pref_cam', checkCamera.checked ? '1' : '0'));
checkMic.addEventListener('change', () => localStorage.setItem('uno_pref_mic', checkMic.checked ? '1' : '0'));

function boot() {
  const savedName = localStorage.getItem('uno_player_name');
  if (savedName) inputName.value = savedName;

  let urlRoom = '';
  try {
    urlRoom = (new URLSearchParams(location.search).get('room') || '')
      .replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 24);
  } catch(e) {}

  const lastRoom = localStorage.getItem('uno_last_room');
  const targetRoom = urlRoom || lastRoom;
  const tokenForTarget = targetRoom ? localStorage.getItem(`uno_token_${targetRoom}`) : null;

  if (targetRoom && tokenForTarget && savedName) {
    inputRoom.value = targetRoom;
    const camPref = localStorage.getItem('uno_pref_cam');
    const micPref = localStorage.getItem('uno_pref_mic');
    checkCamera.checked = camPref === null ? true : camPref === '1';
    checkMic.checked = micPref === '1';
    showReconnectOverlay(targetRoom, savedName);
    startSession(targetRoom, savedName, checkCamera.checked, checkMic.checked);
    return;
  }

  if (urlRoom) inputRoom.value = urlRoom;
}

boot();

})();
