/**
 * The Battle Cats - Web Edition & Room-Code Multiplayer Client (app.js)
 * Renders 100% authentic PONOS .imgcut + .mamodel + .maanim 2D skeletal animations,
 * official UI atlases (img001_tw, img002_tw, nyankoCastle_*, 000_a, battle_soul_000),
 * 4-digit Room Code matchmaking, and In-Browser Custom Character Studio.
 */

const SCREEN_W = 1280;
const SCREEN_H = 720;
const GROUND_Y = 500;
const BATTLE_WORLD_SCALE = 0.4 * (720.0 / 640.0); // 0.45 px/DU (native 0x5cf65c + 0x990658 1138x640 -> 720p uniform 9/8)
const UNIT_RENDER_SCALE = 720.0 / 640.0;          // 1.125 (native 0x338cf4 + 0x33f0d8 uniform 9/8 scale for .mamodel)
const CASTLE_NATIVE_SCALE = 1.27;                 // Native 0x4ae8dc..0x4aea14 Cat Castle multiplier (1.27x)
const BATTLE_MAX_ZOOM_2ROW = 0.8897;              // Native 0x4ba6d4 2-row deck initial/max camera zoom (8897/10000)
let battleZoom = BATTLE_MAX_ZOOM_2ROW;

function getBattleZoomBounds(stageLen) {
  const len = Math.max(800, stageLen || 3200);
  // Native 0x8b296c..0x8b2984: min_zoom = (960000 // (stage_len * 4) + 1) / 100
  const minZoom = Math.min(BATTLE_MAX_ZOOM_2ROW, Math.floor(960000 / (len * 4) + 1) / 100);
  return { minZoom, maxZoom: BATTLE_MAX_ZOOM_2ROW };
}
const TEAM_CAT = 0;   // Right Castle (P1 Host)
const TEAM_ENEMY = 1; // Left Castle (P2 Guest)

// Global App State
let RIGS = {};
let BASE_CATS = [];
let CUSTOM_UNITS = [];
let BUILTIN_UNIT = null;
let SELECTED_DECK = [];
let IMAGES = {};

let currentRoomCode = "";
let playerToken = "";
let myTeam = TEAM_CAT;
let roomState = null;
let eventSource = null;
let pollTimer = null;

// Camera & UI State
let cameraX = 320;
let isDraggingCam = false;
let dragStartX = 0;
let dragStartCamX = 0;
let manualCameraHold = 0;
let activeRow = 0; // 0 = slots 0..4, 1 = slots 5..9
let clientTick = 0;
let previewFrame = 0;
let selectedRarityFilter = "ALL";
let rosterSearchQuery = "";
let onlyTalentedFilter = false;
let selectedCatLevel = 30;
let inspectedItem = null;
let formationPreviewAnim = "walk";
const PENDING_RIGS = new Set();

// Per-unit disabled talent indices: { unitKey: [idx, ...] } (absent/empty = ALL talents ON by default)
let TALENT_OVERRIDES = {};
try {
  const savedOv = localStorage.getItem("bc_talent_overrides_v1");
  if (savedOv) {
    const parsed = JSON.parse(savedOv);
    if (parsed && typeof parsed === "object") TALENT_OVERRIDES = parsed;
  }
} catch (_) {}

function saveTalentOverrides() {
  try {
    localStorage.setItem("bc_talent_overrides_v1", JSON.stringify(TALENT_OVERRIDES));
  } catch (_) {}
}

function getDisabledTalentSet(unitKey) {
  const arr = TALENT_OVERRIDES[unitKey];
  return new Set(Array.isArray(arr) ? arr.map(Number) : []);
}

function toggleUnitTalent(unitKey, talentIdx, enabled) {
  const dis = getDisabledTalentSet(unitKey);
  if (enabled) dis.delete(Number(talentIdx));
  else dis.add(Number(talentIdx));
  if (dis.size === 0) delete TALENT_OVERRIDES[unitKey];
  else TALENT_OVERRIDES[unitKey] = Array.from(dis).sort((a, b) => a - b);
  saveTalentOverrides();
  renderDeckSelector();
  const updated = getAllSelectableUnits().find(x => x.key === unitKey);
  if (updated) inspectCat(updated);
  if (currentRoomCode) {
    sendRoomAction("UPDATE_TALENTS", TALENT_OVERRIDES);
  }
}

function setAllUnitTalents(unitKey, enableAll) {
  const raw = BASE_CATS.find(c => c.key === unitKey);
  if (!raw || !Array.isArray(raw.talents) || raw.talents.length === 0) return;
  if (enableAll) {
    delete TALENT_OVERRIDES[unitKey];
  } else {
    TALENT_OVERRIDES[unitKey] = raw.talents.map(t => Number(t.idx));
  }
  saveTalentOverrides();
  renderDeckSelector();
  const updated = getAllSelectableUnits().find(x => x.key === unitKey);
  if (updated) inspectCat(updated);
  if (currentRoomCode) {
    sendRoomAction("UPDATE_TALENTS", TALENT_OVERRIDES);
  }
  showToast(enableAll ? `已開啟「${raw.name}」全部本能！` : `已關閉「${raw.name}」全部本能！`);
}

function onOnlyTalentedChange(checked) {
  onlyTalentedFilter = !!checked;
  renderDeckSelector();
}

let _lastComboDeckKey = "";
async function refreshActiveCombos() {
  const bar = document.getElementById("activeCombosBar");
  if (!bar) return;
  const key = SELECTED_DECK.slice(0, 10).join(",");
  if (key === _lastComboDeckKey) return;
  _lastComboDeckKey = key;
  try {
    const res = await fetch("/api/check_combos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deck: SELECTED_DECK.slice(0, 10) }),
    });
    const data = await res.json();
    const combos = (data && data.combos) || [];
    if (combos.length === 0) {
      bar.innerHTML = `<span style="color:#6c7e9e;font-size:11px;">目前編隊未觸發貓咪聯組（可搭配同系列角色觸發）</span>`;
    } else {
      bar.innerHTML = combos.map(c =>
        `<span style="display:inline-block;background:rgba(245,185,21,0.16);border:1px solid rgba(245,185,21,0.45);color:#ffe066;font-size:11px;font-weight:800;padding:2px 8px;border-radius:6px;margin-right:6px;">🌟 ${c.name} [${c.size}]</span>`
      ).join("");
    }
  } catch (_) {}
}

function ensureRigLoaded(rigCode) {
  if (!rigCode || RIGS[rigCode] || PENDING_RIGS.has(rigCode)) return;
  ensureRigsLoaded([rigCode]);
}

function ensureRigsLoaded(codes) {
  const missing = [];
  for (const c of codes) {
    if (c && !RIGS[c] && !PENDING_RIGS.has(c)) {
      PENDING_RIGS.add(c);
      missing.push(c);
    }
  }
  if (missing.length === 0) return;
  fetch(`/api/rig?codes=${encodeURIComponent(missing.join(","))}`)
    .then(r => (r.ok ? r.json() : null))
    .then(data => {
      if (data && data.rigs) {
        Object.entries(data.rigs).forEach(([code, rig]) => {
          RIGS[code] = rig;
          if (rig && rig.png) loadImage(`rig_${rig.code}`, rig.png);
        });
      }
    })
    .catch(() => {})
    .finally(() => {
      missing.forEach(c => PENDING_RIGS.delete(c));
    });
}

function onRosterSearchChange(val) {
  rosterSearchQuery = (val || "").trim().toLowerCase();
  renderDeckSelector();
}

// Active Visual FX on Client Canvas
let fxHitSparks = [];
let fxDeathSouls = [];
let fxDust = [];
let castleShake = { 0: 0, 1: 0 };
let fxCannonBeams = [];
let fxFloatingTexts = [];
let fxWaves = [];
let fxSurges = [];
let fxExplosions = [];
let fxWaveBlocks = [];
let fxBossShockwaves = [];
let fxToxic = [];
let fxCurse = [];
let fxWarp = [];
let fxShieldShatters = [];
let cameraShake = { timer: 0, intensity: 0, dx: 0, dy: 0 };
let unitVisualState = new Map();

// Authentic Audio Player with Zero-Allocation Audio Pooling & Throttling
const AUDIO = {
  enabled: true,
  pools: {},
  poolIdx: {},
  lastPlayTime: {},
  bgm: null,
  playSFX(name) {
    if (!this.enabled) return;
    const now = performance.now();
    if (name === "hit" && this.lastPlayTime[name] && (now - this.lastPlayTime[name] < 50)) {
      return;
    }
    this.lastPlayTime[name] = now;
    try {
      const fileMap = {
        deploy: "/audio/snd168.wav",
        hit: "/audio/snd169.wav",
        crit: "/audio/snd170.wav",
        critical: "/audio/snd170.wav",
        knockback: "/audio/snd139.wav",
        cannon_fire: "/audio/snd065.wav",
        wallet_up: "/audio/snd188.wav",
        click: "/audio/snd167.wav",
        victory: "/audio/snd170.wav",
        defeat: "/audio/snd062.ogg",
        death: "/audio/snd139.wav",
        boss_shockwave: "/audio/snd034.ogg",
        boss_death: "/audio/snd189.wav",
        thunderbolt: "/audio/snd194.wav",
        shield_break: "/audio/snd188.wav",
        barrier_break: "/audio/snd188.wav",
        castle_hit: "/audio/snd169.wav"
      };
      const src = fileMap[name];
      if (!src) return;
      if (!this.pools[name]) {
        this.pools[name] = [];
        this.poolIdx[name] = 0;
        const poolSize = (name === "hit" || name === "click") ? 6 : 3;
        for (let i = 0; i < poolSize; i++) {
          const a = new Audio(src);
          a.volume = 0.55;
          this.pools[name].push(a);
        }
      }
      const pool = this.pools[name];
      const idx = this.poolIdx[name];
      this.poolIdx[name] = (idx + 1) % pool.length;
      const sound = pool[idx];
      sound.currentTime = 0;
      sound.play().catch(() => {});
    } catch (_) {}
  },
  playBGM(track = "battle") {
    if (!this.enabled) return;
    try {
      const trackFile = track === "boss" ? "/audio/snd062.ogg" : "/audio/snd034.ogg";
      if (!this.bgm) {
        this.bgm = new Audio(trackFile);
        this.bgm.loop = true;
        this.bgm.volume = 0.35;
      } else if (this.bgm.src && !this.bgm.src.endsWith(trackFile)) {
        this.bgm.pause();
        this.bgm.src = trackFile;
      }
      this.bgm.play().catch(() => {});
    } catch (_) {}
  },
  stopBGM() {
    if (this.bgm) {
      try { this.bgm.pause(); } catch (_) {}
    }
  }
};

// Preload Official Textures
const CORE_TEXTURES = {
  bg000: "/assets/bg000.png",
  img001: "/assets/img001_tw.png",
  img002: "/assets/img002_tw.png",
  fc000: "/assets/fc000.png",
  ec000: "/assets/ec000_tw.png",
  castle_turret: "/assets/nyankoCastle_000_00.png",
  castle_beam: "/assets/nyankoCastle_001_00_00.png",
  castle_plaque: "/assets/nyankoCastle_002_00.png",
  castle_base: "/assets/nyankoCastle_003_00.png",
  hit_a: "/assets/000_a.png",
  soul: "/assets/battle_soul_000.png",
};
for (let i = 0; i < 9; i++) {
  CORE_TEXTURES[`uni_${i}`] = `/assets/uni00${i}_f00.png`;
}

function loadImage(key, url) {
  if (IMAGES[key]) return IMAGES[key];
  const img = new Image();
  img.src = url;
  IMAGES[key] = img;
  return img;
}

function showToast(msg) {
  const box = document.getElementById("toastBox");
  box.textContent = msg;
  box.style.display = "block";
  clearTimeout(box._timer);
  box._timer = setTimeout(() => {
    box.style.display = "none";
  }, 3200);
}

function setPlayerName(value) {
  ["hostPlayerName", "joinPlayerName", "formationPlayerName"].forEach(id => {
    document.getElementById(id).value = value;
  });
  renderDeckSelector();
}

function deckForPlayer(playerName) {
  if (!BUILTIN_UNIT || playerName === BUILTIN_UNIT.player_name || !SELECTED_DECK.includes(BUILTIN_UNIT.key)) {
    return SELECTED_DECK;
  }
  return SELECTED_DECK.filter(key => key !== BUILTIN_UNIT.key);
}

function unitIconUrl(item) {
  return item.portrait || (item.icon ? `/assets/${item.icon}.png` : "");
}

function drawPortrait(ctx, url, centerX, bottomY, maxWidth, maxHeight) {
  const img = loadImage(url, url);
  if (!img.complete || !img.naturalWidth) return false;
  const ratio = Math.min(maxWidth / img.naturalWidth, maxHeight / img.naturalHeight);
  const width = img.naturalWidth * ratio;
  const height = img.naturalHeight * ratio;
  ctx.drawImage(img, centerX - width / 2, bottomY - height, width, height);
  return true;
}

function switchTab(tabName) {
  document.querySelectorAll(".view-section").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));
  document.getElementById(`view-${tabName}`).classList.add("active");
  if (tabName === "lobby") {
    document.getElementById("tabLobbyBtn").classList.add("active");
    refreshRoomList();
  } else if (tabName === "formation") {
    document.getElementById("tabFormationBtn").classList.add("active");
    renderDeckSelector();
  } else if (tabName === "battle") {
    document.getElementById("tabBattleBtn").classList.add("active");
  } else if (tabName === "studio") {
    document.getElementById("tabStudioBtn").classList.add("active");
  }
}

// ============================================================================
// 1. BOOTSTRAP & DECK / STUDIO MANAGEMENT
// ============================================================================

async function initApp() {
  Object.entries(CORE_TEXTURES).forEach(([k, u]) => loadImage(k, u));

  try {
    const res = await fetch("/api/bootstrap");
    const data = await res.json();
    RIGS = data.rigs || {};
    BASE_CATS = data.base_cats || [];
    CUSTOM_UNITS = data.custom_units || [];
    BUILTIN_UNIT = data.builtin_unit || null;

    // Preload only essential FX + starter deck sprite sheets & current deck icons (avoid flooding 779 icon requests on remote links!)
    Object.values(RIGS).forEach(r => {
      if (r && r.png) loadImage(`rig_${r.code}`, r.png);
    });

    // Populate Studio Rig Selector with all 38 official cat rigs + enemy rigs
    const cuRigEl = document.getElementById("cuRig");
    if (cuRigEl && BASE_CATS.length > 0) {
      const existingValues = new Set(Array.from(cuRigEl.options).map(o => o.value));
      BASE_CATS.forEach(c => {
        if (c.rig && !existingValues.has(c.rig)) {
          const opt = document.createElement("option");
          opt.value = c.rig;
          opt.textContent = `${c.rig} - ${c.name} (${c.rarity_label})`;
          cuRigEl.appendChild(opt);
          existingValues.add(c.rig);
        }
      });
    }

    const port = window.location.port || "8080";
    const shareUrl = data.public_url || `http://${data.lan_ip || window.location.hostname}:${port}`;
    const urlEl = document.getElementById("shareUrlBox");
    if (urlEl) urlEl.textContent = shareUrl;

    // Update dynamic roster title with total restored official final-form count
    const titleEl = document.getElementById("rosterPanelTitle");
    if (titleEl) {
      titleEl.textContent = `開戰前配隊選角（涵蓋 6 大稀有度 ${BASE_CATS.length} 隻最終進化型官方貓咪 ＋ 自製角色）`;
    }

    // Initialize balanced multi-rarity 10-slot default deck (Final-form Meatshields + EX/Rare/SR/Uber/Legend)
    const defaultKeys = [
      "cat_000_s", "cat_001_s", "cat_018_s", "cat_037_s", "cat_041_s",
      "cat_032_s", "cat_031_s", "cat_024_s", "cat_025_s", "cat_455_s",
    ];
    SELECTED_DECK = [];
    defaultKeys.forEach(k => {
      if (BASE_CATS.some(c => c.key === k) && SELECTED_DECK.length < 10) {
        SELECTED_DECK.push(k);
      }
    });
    BASE_CATS.forEach(c => {
      if (!SELECTED_DECK.includes(c.key) && SELECTED_DECK.length < 10) {
        SELECTED_DECK.push(c.key);
      }
    });
    const catByKey = new Map(BASE_CATS.map(c => [c.key, c]));
    SELECTED_DECK.forEach(k => {
      const c = catByKey.get(k);
      if (c && c.icon) loadImage(`icon_${c.icon}`, unitIconUrl(c));
    });

    renderDeckSelector();
    renderCustomUnitLibrary();
    renderRoomListUI(data.rooms || []);

    // Check URL hash or query for ?room=XXXX
    const urlParams = new URLSearchParams(window.location.search);
    const autoRoom = urlParams.get("room");
    const forceTeam = urlParams.get("team");
    if (autoRoom) {
      document.getElementById("joinRoomCodeInput").value = autoRoom;
      if (forceTeam !== null) {
        const stRes = await fetch(`/api/room/state?room=${encodeURIComponent(autoRoom)}`);
        if (stRes.ok) {
          const stData = await stRes.json();
          enterBattleRoom(autoRoom, parseInt(forceTeam, 10) || TEAM_CAT, stData);
        }
      } else {
        await joinRoomByCode();
      }
    }
  } catch (err) {
    console.error("Bootstrap error:", err);
  }

  setupBattleControls();
  requestAnimationFrame(renderLoop);
  if (new URLSearchParams(window.location.search).get("oneshot") !== "1") {
    setInterval(refreshRoomList, 4000);
  }
}

function getLevelScaleFactor(level = selectedCatLevel, brackets = null) {
  const lv = Math.max(1, parseInt(level, 10) || 1);
  if (lv === 1) return 1.0;
  const b = Array.isArray(brackets) && brackets.length > 0 ? brackets : [20, 20, 20, 20, 20, 20, 10, 10, 10, 10];
  let totalPct = 100;
  for (let i = 1; i < lv; i++) {
    const idx = Math.min(b.length - 1, Math.floor(i / 10));
    totalPct += b[idx];
  }
  return totalPct / 100.0;
}

function computeScaledStat(baseVal, level = selectedCatLevel, brackets = null) {
  const lv = Math.max(1, parseInt(level, 10) || 1);
  const base = parseInt(baseVal, 10) || 0;
  if (lv === 1) return Math.max(1, base);
  const b = Array.isArray(brackets) && brackets.length > 0 ? brackets : [20, 20, 20, 20, 20, 20, 10, 10, 10, 10];
  let totalX100 = base * 100;
  for (let i = 1; i < lv; i++) {
    const idx = Math.min(b.length - 1, Math.floor(i / 10));
    totalX100 += base * b[idx];
  }
  return Math.max(1, Math.floor((totalX100 + 50) / 100));
}

function changeCatLevel(val) {
  selectedCatLevel = Math.max(1, Math.min(50, parseInt(val, 10) || 1));
  renderDeckSelector();
  if (inspectedItem) {
    const allItems = getAllSelectableUnits();
    const updated = allItems.find(x => x.key === inspectedItem.key);
    if (updated) inspectCat(updated);
  }
  if (currentRoomCode) {
    sendRoomAction("SET_LEVEL", selectedCatLevel);
  }
  const mult = getLevelScaleFactor(selectedCatLevel).toFixed(1);
  showToast(`已切換貓咪等級為 Lv.${selectedCatLevel} (unitlevel.csv ${mult}x 官方成長公式)`);
}

function formatMaxLevelLabel(item) {
  if (item.custom) return "Lv.MAX";
  const baseLv = item.max_base_level || item.max_level || 50;
  const plusLv = item.max_plus_level || 0;
  return plusLv > 0 ? `Lv.${baseLv}+${plusLv}` : `Lv.${baseLv}`;
}

function applyTalentsToClientItem(item) {
  const talents = item.talents;
  if (!Array.isArray(talents) || talents.length === 0) return item;
  const dis = getDisabledTalentSet(item.key);
  const ab = { ...(item.abilities || {}) };
  const targetTraits = [...(item.target_traits || [])];
  const hits = (item.attack_hits || []).map(h => ({ ...h }));
  const traitMap = {
    33: "red", 34: "floating", 35: "black", 36: "metal", 37: "angel",
    38: "alien", 39: "zombie", 40: "relic", 41: "traitless", 57: "aku",
  };
  let hpBonusPct = 0;
  let atkBonusPct = 0;
  let cost = item.cost;
  let cdFrames = item.cd_frames;
  let speed = item.speed;
  let tba = item.tba;
  let enabledCount = 0;

  for (const t of talents) {
    if (dis.has(Number(t.idx))) continue;
    enabledCount++;
    const aid = Number(t.ability_id || 0);
    const mv = t.max_vals || [0, 0, 0, 0];
    const m1 = mv[0] || 0, m2 = mv[1] || 0, m3 = mv[2] || 0, m4 = mv[3] || 0;
    if (aid === 32) hpBonusPct += m1;
    else if (aid === 31) atkBonusPct += m1;
    else if (aid === 25) cost = Math.max(10, cost - m1);
    else if (aid === 26) cdFrames = Math.max(60, cdFrames - m1 * 2);
    else if (aid === 27) speed = Math.max(1, speed + m1);
    else if (aid === 61) tba = Math.max(0, tba - m1);
    else if (traitMap[aid]) { if (!targetTraits.includes(traitMap[aid])) targetTraits.push(traitMap[aid]); }
    else if (aid === 1) { ab.weaken_prob = Math.min(100, (ab.weaken_prob || 0) + m1); ab.weaken_dur = (ab.weaken_dur || 0) + m2; }
    else if (aid === 2) { ab.freeze_prob = Math.min(100, (ab.freeze_prob || 0) + m1); ab.freeze_dur = (ab.freeze_dur || 0) + m2; }
    else if (aid === 3) { ab.slow_prob = Math.min(100, (ab.slow_prob || 0) + m1); ab.slow_dur = (ab.slow_dur || 0) + m2; }
    else if (aid === 5) ab.strong = true;
    else if (aid === 6) ab.resistant = true;
    else if (aid === 7) ab.massive_damage = true;
    else if (aid === 8) ab.kb_prob = Math.min(100, (ab.kb_prob || 0) + m1);
    else if (aid === 10) {
      if (m1 > 0 && !ab.strengthen_hp_pct) ab.strengthen_hp_pct = m1 < 100 ? (100 - m1) : m1;
      ab.strengthen_boost_pct = (ab.strengthen_boost_pct || 0) + m2;
    }
    else if (aid === 11) ab.survive_prob = Math.min(100, (ab.survive_prob || 0) + m1);
    else if (aid === 13) ab.crit_prob = Math.min(100, (ab.crit_prob || 0) + m1);
    else if (aid === 14) ab.zombie_killer = true;
    else if (aid === 15) ab.barrier_break_prob = Math.min(100, (ab.barrier_break_prob || 0) + m1);
    else if (aid === 16) ab.extra_money = true;
    else if (aid === 17) { ab.wave_prob = Math.min(100, (ab.wave_prob || 0) + m1); ab.wave_lvl = Math.max(ab.wave_lvl || 0, Math.max(1, m2)); }
    else if (aid === 29) ab.curse_immune = true;
    else if (aid === 44) ab.weaken_immune = true;
    else if (aid === 45) ab.freeze_immune = true;
    else if (aid === 46) ab.slow_immune = true;
    else if (aid === 47) ab.kb_immune = true;
    else if (aid === 48) ab.wave_immune = true;
    else if (aid === 49) { ab.warp_immune = true; ab.warp_blocker = true; }
    else if (aid === 50) { ab.savage_blow_prob = Math.min(100, (ab.savage_blow_prob || 0) + m1); ab.savage_blow_boost = Math.max(ab.savage_blow_boost || 0, m2 || 200); }
    else if (aid === 51) { ab.dodge_prob = Math.min(100, (ab.dodge_prob || 0) + m1); ab.dodge_dur = Math.max(ab.dodge_dur || 0, m2 || 30); }
    else if (aid === 53) ab.toxic_immune = true;
    else if (aid === 55) ab.surge_immune = true;
    else if (aid === 56 || aid === 65) {
      ab.surge_prob = Math.min(100, (ab.surge_prob || 0) + m1);
      ab.surge_lvl = Math.max(ab.surge_lvl || 0, Math.max(1, m2));
      if (aid === 65) ab.mini_surge = true;
    }
    else if (aid === 58) ab.shield_pierce_prob = Math.min(100, (ab.shield_pierce_prob || 0) + m1);
    else if (aid === 59) ab.soul_strike = true;
    else if (aid === 60) { ab.curse_prob = Math.min(100, (ab.curse_prob || 0) + m1); ab.curse_dur = (ab.curse_dur || 0) + m2; }
    else if (aid === 62) { ab.wave_prob = Math.min(100, (ab.wave_prob || 0) + m1); ab.wave_lvl = Math.max(ab.wave_lvl || 0, Math.max(1, m2)); ab.mini_wave = true; }
    else if (aid === 63) ab.colossus_slayer = true;
    else if (aid === 64) ab.behemoth_slayer = true;
    else if (aid === 66) ab.sage_slayer = true;
    else if (aid === 67) { ab.explosion_prob = Math.min(100, (ab.explosion_prob || 0) + m1); if (m2 > 0) ab.explosion_start = Math.floor(m2 / 4); }
    else if (aid === 68) ab.explosion_immune = true;
    else if (aid === 69) ab.metal_killer_pct = Math.max(ab.metal_killer_pct || 0, m1 || 5);
  }

  let hp = item.hp;
  let ap = item.ap;
  if (hpBonusPct > 0) hp = Math.max(1, Math.floor((hp * (100 + hpBonusPct)) / 100));
  if (atkBonusPct > 0) {
    if (hits.length > 0) {
      hits.forEach(h => { h.damage = Math.max(1, Math.floor((h.damage * (100 + atkBonusPct)) / 100)); });
      ap = hits.reduce((acc, h) => acc + h.damage, 0);
    } else {
      ap = Math.max(1, Math.floor((ap * (100 + atkBonusPct)) / 100));
    }
  }
  return {
    ...item,
    hp,
    ap,
    cost: Math.max(15, Math.round(cost * 1.5)),
    cd_frames: cdFrames,
    speed,
    tba,
    attack_hits: hits,
    abilities: ab,
    target_traits: targetTraits,
    enabled_talent_count: enabledCount,
  };
}

function getAllSelectableUnits() {
  const builtin = BUILTIN_UNIT;
  const canSelectBuiltin = builtin && document.getElementById("formationPlayerName").value.trim() === builtin.player_name;
  return [
    ...(canSelectBuiltin ? [{
      key: builtin.key,
      name: builtin.name,
      cost: builtin.cost,
      base_hp: builtin.hp,
      base_ap: builtin.attack_power,
      hp: builtin.hp,
      ap: builtin.attack_power,
      range: builtin.attack_range,
      speed: builtin.speed,
      kb_count: builtin.knockback_count,
      foreswing: builtin.attack_windup,
      tba: builtin.attack_interval,
      cd_frames: builtin.cooldown_frames,
      is_area: builtin.target_type === "area",
      portrait: builtin.portrait,
      rarity: builtin.rarity,
      rarity_label: builtin.rarity_label,
      desc: "玩家名稱符合條件時可選用的內建角色。",
      custom: true,
    }] : []),
    ...BASE_CATS.map(c => {
      const brackets = c.level_brackets || null;
      const maxLv = c.max_level || 50;
      const rawHits = Array.isArray(c.attack_hits) ? c.attack_hits : [];
      const scaledHits = rawHits.map(h => ({
        ...h,
        damage: computeScaledStat(h.damage || 1, maxLv, brackets),
      }));
      const scaledAp = c.max_ap || (scaledHits.length > 0
        ? scaledHits.reduce((acc, h) => acc + h.damage, 0)
        : computeScaledStat(c.ap || 100, maxLv, brackets));
      const scaledHp = c.max_hp || computeScaledStat(c.hp || 1000, maxLv, brackets);
      const baseItem = {
        key: c.key,
        name: c.name,
        cost: c.cost,
        base_hp: c.hp || 1000,
        base_ap: c.ap || 100,
        hp: scaledHp,
        ap: scaledAp,
        max_level: maxLv,
        max_base_level: c.max_base_level || maxLv,
        max_plus_level: c.max_plus_level || 0,
        form: c.form || "s",
        form_label: c.form_label || "三階",
        attack_hits: scaledHits,
        range: c.range || 140,
        speed: c.speed || 10,
        kb_count: c.kb_count || 3,
        foreswing: c.foreswing || 10,
        tba: c.tba || 30,
        cd_frames: c.cd_frames || 60,
        is_area: !!c.is_area,
        rig: c.rig || "000_s",
        icon: c.icon || "uni000_s00",
        rarity: c.rarity || "normal",
        rarity_label: c.rarity_label || "基本",
        scale: c.scale || UNIT_RENDER_SCALE,
        traits: c.traits || [],
        target_traits: c.target_traits || [],
        abilities: c.abilities || {},
        talents: Array.isArray(c.talents) ? c.talents : [],
        desc: c.desc || "",
        custom: false,
      };
      return (Array.isArray(baseItem.talents) && baseItem.talents.length > 0)
        ? applyTalentsToClientItem(baseItem)
        : { ...baseItem, cost: Math.max(15, Math.round(baseItem.cost * 1.5)) };
    }),
    ...CUSTOM_UNITS.filter(cu => (cu.key || cu.id) !== builtin?.key).map(cu => ({
      key: cu.key || cu.id,
      name: cu.name,
      cost: cu.cost || 300,
      base_hp: cu.hp || 10000,
      base_ap: cu.ap || 2000,
      hp: cu.hp || 10000,
      ap: cu.ap || 2000,
      attack_hits: [],
      range: cu.range || 250,
      speed: cu.speed || 20,
      kb_count: cu.kb_count || 3,
      foreswing: cu.pre_atk_frames || 10,
      tba: cu.tba_frames || 20,
      cd_frames: cu.cd_frames || 60,
      is_area: cu.is_area_attack !== false,
      rig: cu.base_rig || "004_f",
      icon: "",
      rarity: "custom",
      rarity_label: "自製",
      scale: cu.scale || UNIT_RENDER_SCALE,
      traits: [],
      target_traits: [],
      abilities: cu.abilities || {},
      desc: "玩家於角色工坊自訂的專屬原創角色。",
      custom: true,
    })),
  ];
}

function getAbilityTags(item) {
  const ab = item.abilities || {};
  const tags = [];
  tags.push(item.is_area ? "範圍攻擊" : "單體攻擊");
  if (Array.isArray(item.attack_hits) && item.attack_hits.length > 1) {
    tags.push(`${item.attack_hits.length}段連續攻擊`);
  }
  if (ab.ld_start && ab.ld_range) {
    const r1 = Math.min(ab.ld_start, ab.ld_start + ab.ld_range);
    const r2 = Math.max(ab.ld_start, ab.ld_start + ab.ld_range);
    tags.push(`遠方攻擊 (${r1}~${r2})`);
  }
  if (ab.crit_prob > 0) tags.push(`暴擊 ${ab.crit_prob}%`);
  if (ab.savage_blow_prob > 0) tags.push(`渾身一擊 ${ab.savage_blow_prob}%`);
  if (ab.explosion_prob > 0) tags.push(`爆波 ${ab.explosion_prob}%`);
  if (ab.wave_prob > 0) tags.push(`Lv.${ab.wave_lvl || 2} 波動 ${ab.wave_prob}%`);
  if (ab.surge_prob > 0) tags.push(`Lv.${ab.surge_lvl || 1} 烈波 ${ab.surge_prob}%`);
  if (ab.freeze_prob > 0) tags.push(`暫停 ${ab.freeze_prob}%`);
  if (ab.slow_prob > 0) tags.push(`緩速 ${ab.slow_prob}%`);
  if (ab.kb_prob > 0) tags.push(`擊退 ${ab.kb_prob}%`);
  if (ab.weaken_prob > 0) tags.push(`降攻 ${ab.weaken_prob}%`);
  if (ab.strengthen_hp_pct > 0) tags.push(`血量${ab.strengthen_hp_pct}%攻擊+${ab.strengthen_boost_pct}%`);
  if (ab.survive_prob > 0) tags.push(`死前存活 ${ab.survive_prob}%`);
  if (ab.insane_damage) tags.push("極度傷害 (5x)");
  else if (ab.massive_damage) tags.push("超大傷害 (3x)");
  if (ab.insanely_tough) tags.push("極度耐打 (1/6)");
  else if (ab.resistant) tags.push("很耐打 (1/4)");
  if (ab.strong || ab.strong_against) tags.push("善於攻擊");
  if (ab.zombie_killer) tags.push("終結不死");
  if (ab.barrier_break_prob > 0) tags.push(`破盾 ${ab.barrier_break_prob}%`);
  if (ab.wave_immune) tags.push("波動免疫");
  if (ab.freeze_immune) tags.push("暫停免疫");
  if (ab.slow_immune) tags.push("緩速免疫");
  if (ab.kb_immune) tags.push("擊退免疫");
  return tags;
}

function syncDeckIfInRoom() {
  if (currentRoomCode && roomState) {
    const playerName = myTeam === TEAM_CAT ? roomState.host_name : roomState.guest_name;
    const deck = deckForPlayer(playerToken ? playerName : "");
    if (deck.length > 0) sendRoomAction("UPDATE_DECK", deck);
  }
}

function setFormationPreviewAnim(animName) {
  formationPreviewAnim = animName;
  previewFrame = 0;
  ["Walk", "Attack", "Idle", "Kb"].forEach(suffix => {
    const btn = document.getElementById(`animBtn${suffix}`);
    if (btn) {
      btn.style.background = suffix.toLowerCase() === animName ? "#2b426b" : "#1d283e";
      btn.style.color = suffix.toLowerCase() === animName ? "#ffcc33" : "#dce6f7";
    }
  });
}

function inspectCat(item) {
  if (!item) return;
  inspectedItem = item;
  previewFrame = 0;
  if (item.rig) ensureRigLoaded(item.rig);

  const badgeEl = document.getElementById("inspRarityBadge");
  if (badgeEl) {
    badgeEl.className = `rarity-badge rarity-${item.rarity}`;
    badgeEl.textContent = item.custom ? item.rarity_label : `${item.rarity_label}・${item.form_label || "三階"}`;
  }
  const nameEl = document.getElementById("inspName");
  if (nameEl) nameEl.textContent = `${item.name} (${formatMaxLevelLabel(item)})`;

  const animationControls = document.getElementById("formationAnimationControls");
  if (animationControls) animationControls.style.display = item.portrait ? "none" : "flex";

  let hitBreakdownHtml = "";
  if (Array.isArray(item.attack_hits) && item.attack_hits.length > 1) {
    const parts = item.attack_hits.map(h => h.damage.toLocaleString()).join(" + ");
    hitBreakdownHtml = `<div style="color:#ffd166;font-size:11px;">多段傷害拆解：<strong>${parts}</strong>（共 ${item.attack_hits.length} 段）</div>`;
  }

  const statsEl = document.getElementById("inspStats");
  if (statsEl) {
    statsEl.innerHTML = `
      <div>費用：<strong style="color:#ffe066;">${item.cost} 元</strong> ｜ 再產生：<strong>${(item.cd_frames / 30).toFixed(2)} 秒</strong> ｜ KB：<strong>${item.kb_count || 3}</strong></div>
      <div>體力 (HP)：<strong style="color:#7bed9f;">${item.hp.toLocaleString()}</strong> ｜ 總攻擊力 (AP)：<strong style="color:#ff9f43;">${item.ap.toLocaleString()}</strong></div>
      ${hitBreakdownHtml}
      <div>射程：<strong>${item.range} (${item.is_area ? "範圍攻擊" : "單體攻擊"})</strong> ｜ 跑速：<strong>${item.speed}</strong> ｜ 出手：<strong>${item.foreswing || 10}F</strong></div>
      ${item.portrait ? `<div>攻擊間隔：<strong>${item.tba}F</strong></div>` : ""}
    `;
  }
  const abEl = document.getElementById("inspAbilities");
  if (abEl) {
    abEl.innerHTML = getAbilityTags(item)
      .map(t => `<span class="ability-tag">${t}</span>`)
      .join("");
  }
  const descEl = document.getElementById("inspDesc");
  if (descEl) {
    descEl.textContent = item.desc ? item.desc.replace(/\|/g, " ") : "官方貓咪大戰爭正版授權數據角色。";
  }

  // Character Tuning Panel: Official PONOS Talents & Ultra Talents ON/OFF toggles
  const talEl = document.getElementById("inspTalentsBox");
  if (talEl) {
    const talents = Array.isArray(item.talents) ? item.talents : [];
    if (item.portrait) {
      talEl.innerHTML = "";
    } else if (talents.length === 0) {
      talEl.innerHTML = `<div style="font-size:12px;color:#6c7d99;padding:4px 0;">此角色目前在官方 v15.6.1 尚無開放本能。</div>`;
    } else {
      const dis = getDisabledTalentSet(item.key);
      const enabledCnt = talents.filter(t => !dis.has(Number(t.idx))).length;
      const rowsHtml = talents.map(t => {
        const isOn = !dis.has(Number(t.idx));
        const ultraTag = t.is_ultra ? `<span style="background:#7c3aed;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px;margin-right:4px;font-weight:800;">超本能</span>` : "";
        return `
          <label class="talent-toggle-row ${isOn ? "on" : "off"}">
            <input type="checkbox" ${isOn ? "checked" : ""} onchange="toggleUnitTalent('${item.key}', ${t.idx}, this.checked)" />
            <span class="talent-text">
              <span class="talent-title">${ultraTag}${t.name}</span>
              <span class="talent-sub">${t.summary}</span>
            </span>
          </label>
        `;
      }).join("");
      talEl.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <span style="font-size:12px;font-weight:800;color:#ffd166;">本能調整選單（已開啟 ${enabledCnt}/${talents.length}）</span>
          <div style="display:flex;gap:4px;">
            <button type="button" class="preset-btn" style="padding:2px 8px;font-size:11px;" onclick="setAllUnitTalents('${item.key}', true)">全開</button>
            <button type="button" class="preset-btn" style="padding:2px 8px;font-size:11px;" onclick="setAllUnitTalents('${item.key}', false)">全關</button>
          </div>
        </div>
        <div class="talent-list">${rowsHtml}</div>
      `;
    }
  }
}

function applyDeckPreset(mode) {
  const allItems = getAllSelectableUnits();
  const byKey = new Map(allItems.map(i => [i.key, i]));
  let keys = [];
  if (mode === "balanced" || mode === "all_star") {
    keys = [
      "cat_000_s", "cat_001_s", "cat_018_s", "cat_037_s", "cat_041_s",
      "cat_032_s", "cat_031_s", "cat_024_s", "cat_025_s", "cat_455_s",
    ];
  } else if (mode === "normal_ex") {
    keys = [
      "cat_000_s", "cat_001_s", "cat_002_s", "cat_003_s", "cat_004_s",
      "cat_005_s", "cat_006_s", "cat_007_s", "cat_008_s", "cat_025_s",
    ];
  } else if (mode === "uber_legend") {
    keys = [
      "cat_000_s", "cat_001_s", "cat_196_u", "cat_417_u", "cat_044_s",
      "cat_733_s", "cat_059_u", "cat_073_u", "cat_448_s", "cat_455_s",
    ];
  } else if (mode === "rush") {
    keys = [
      "cat_000_s", "cat_004_s", "cat_018_s", "cat_021_s", "cat_036_s",
      "cat_024_s", "cat_025_s", "cat_044_s", "cat_075_u", "cat_455_s",
    ];
  }
  SELECTED_DECK = keys.filter(k => byKey.has(k)).slice(0, 10);
  renderDeckSelector();
  syncDeckIfInRoom();
  showToast("已套用推薦編隊並同步至房間！");
}

function sortDeckByCost() {
  const allItems = getAllSelectableUnits();
  const costMap = new Map(allItems.map(i => [i.key, i.cost]));
  SELECTED_DECK.sort((a, b) => (costMap.get(a) || 0) - (costMap.get(b) || 0));
  renderDeckSelector();
  syncDeckIfInRoom();
  showToast("已依生產費用由低到高排序出陣編隊！");
}

function clearDeckSelection() {
  if (SELECTED_DECK.length > 1) {
    SELECTED_DECK = [SELECTED_DECK[0]];
    renderDeckSelector();
    syncDeckIfInRoom();
    showToast("已清空編隊（保留第 1 格基本角色）");
  }
}

function renderDeckSelector() {
  const allItems = getAllSelectableUnits();
  const byKey = new Map(allItems.map(i => [i.key, i]));
  if (BUILTIN_UNIT && !byKey.has(BUILTIN_UNIT.key)) {
    const allowedDeck = deckForPlayer("");
    if (allowedDeck !== SELECTED_DECK) {
      SELECTED_DECK = allowedDeck;
      if (SELECTED_DECK.length === 0 && allItems.length > 0) SELECTED_DECK.push(allItems[0].key);
    }
    if (selectedRarityFilter === "builtin") selectedRarityFilter = "ALL";
  }
  if ((!inspectedItem || !byKey.has(inspectedItem.key)) && allItems.length > 0) {
    inspectCat(byKey.get(SELECTED_DECK[0]) || allItems[0]);
  }

  // 1. Render 10-Slot 2-Row Visual Formation Tray (#formationTray10)
  const trayEl = document.getElementById("formationTray10");
  if (trayEl) {
    trayEl.innerHTML = "";
    const hotkeys = ["1", "2", "3", "4", "5", "Q", "W", "E", "R", "T"];
    for (let i = 0; i < 10; i++) {
      const k = SELECTED_DECK[i];
      const item = k ? byKey.get(k) : null;
      const slotDiv = document.createElement("div");
      if (item) {
        slotDiv.className = "formation-slot-card filled";
        const iconUrl = unitIconUrl(item);
        const iconHtml = iconUrl
          ? `<img class="slot-icon-img${item.portrait ? " builtin-portrait" : ""}" src="${iconUrl}" alt="${item.name}">`
          : `<div style="height:48px;display:flex;align-items:center;justify-content:center;color:#ff9ff3;font-weight:900;font-size:13px;">★ 自製</div>`;
        slotDiv.innerHTML = `
          <span class="slot-hotkey">${hotkeys[i]}</span>
          <span class="slot-remove" title="點擊從編隊移除">✕</span>
          ${iconHtml}
          <div class="slot-name"><span class="rarity-badge rarity-${item.rarity}" style="font-size:9px;padding:0 4px;margin-right:3px;">${item.rarity_label}</span>${item.name}</div>
          <div class="slot-cost">${item.cost} 元</div>
        `;
        slotDiv.onclick = () => {
          inspectCat(item);
          if (SELECTED_DECK.length <= 1) {
            showToast("出陣牌組至少需保留 1 隻角色！");
            return;
          }
          SELECTED_DECK.splice(i, 1);
          renderDeckSelector();
          syncDeckIfInRoom();
        };
      } else {
        slotDiv.className = "formation-slot-card";
        slotDiv.innerHTML = `
          <span class="slot-hotkey">${hotkeys[i]}</span>
          <div style="color:#657794;font-size:12px;font-weight:700;text-align:center;">空位 Slot ${i + 1}<br><span style="font-size:10px;color:#4a5a75;">點擊下方角色加入</span></div>
        `;
      }
      trayEl.appendChild(slotDiv);
    }
  }

  // 2. Render Rarity Filter Tabs (#rarityTabsContainer)
  const tabsEl = document.getElementById("rarityTabsContainer");
  if (tabsEl) {
    const rarityDefs = [
      { id: "ALL", label: "全部角色" },
      { id: "normal", label: "基本" },
      { id: "ex", label: "EX" },
      { id: "rare", label: "稀有" },
      { id: "super_rare", label: "激稀有" },
      { id: "uber_rare", label: "超激稀有" },
      { id: "legend_rare", label: "傳說稀有" },
      { id: "builtin", label: "內建角色" },
      { id: "custom", label: "自製角色" },
    ];
    tabsEl.innerHTML = "";
    rarityDefs.forEach(rt => {
      const cnt = rt.id === "ALL" ? allItems.length : allItems.filter(x => x.rarity === rt.id).length;
      if ((rt.id === "custom" || rt.id === "builtin") && cnt === 0) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rarity-tab" + (selectedRarityFilter === rt.id ? " active" : "");
      btn.textContent = `${rt.label} (${cnt})`;
      btn.onclick = () => {
        selectedRarityFilter = rt.id;
        renderDeckSelector();
      };
      tabsEl.appendChild(btn);
    });
  }

  // 3. Render Filtered Multi-Rarity Cat Roster Grid (#deckChipsContainer)
  const container = document.getElementById("deckChipsContainer");
  if (container) {
    container.innerHTML = "";
    let filtered = selectedRarityFilter === "ALL"
      ? allItems
      : allItems.filter(x => x.rarity === selectedRarityFilter);

    if (onlyTalentedFilter) {
      filtered = filtered.filter(item => Array.isArray(item.talents) && item.talents.length > 0);
    }

    if (rosterSearchQuery) {
      const q = rosterSearchQuery;
      filtered = filtered.filter(item => {
        const talNames = (item.talents || []).map(t => t.name).join(" ");
        const hay = `${item.name} ${item.key} ${item.rig} ${item.rarity_label} ${item.form_label || ""} ${talNames} ${getAbilityTags(item).join(" ")}`.toLowerCase();
        return hay.includes(q);
      });
    }

    filtered.forEach(item => {
      const card = document.createElement("div");
      const slotIdx = SELECTED_DECK.indexOf(item.key);
      const isSel = slotIdx >= 0;
      card.className = "unit-chip" + (isSel ? " selected" : "");
      const iconUrl = unitIconUrl(item);
      const iconHtml = iconUrl
        ? `<img loading="lazy" class="chip-icon${item.portrait ? " builtin-portrait" : ""}" src="${iconUrl}" alt="${item.name}">`
        : `<div class="chip-icon" style="display:flex;align-items:center;justify-content:center;color:#ff9ff3;font-weight:900;font-size:12px;">自製</div>`;
      const abTags = getAbilityTags(item).slice(0, 2).join("・");
      const lvStr = formatMaxLevelLabel(item);
      const formTag = item.custom ? "" : `<span style="font-size:10px;color:#ffd166;font-weight:800;">${item.form_label || ""}</span>`;
      const talCnt = Array.isArray(item.talents) ? item.talents.length : 0;
      const talTag = talCnt > 0 ? `<span style="font-size:9px;background:#1e3a2f;color:#7bed9f;border:1px solid #2e7d52;padding:0 4px;border-radius:4px;font-weight:800;">本能 ${item.enabled_talent_count ?? talCnt}/${talCnt}</span>` : "";
      card.innerHTML = `
        ${iconHtml}
        <div class="chip-info">
          <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;">
            <span class="rarity-badge rarity-${item.rarity}">${item.rarity_label}</span>
            ${formTag}
            ${talTag}
            <span class="chip-title">${item.name}</span>
          </div>
          <div class="chip-meta">費用 <strong style="color:#ffe066;">${item.cost}元</strong> ｜ ${lvStr} ｜ 射程 ${item.range}</div>
          <div class="chip-meta" style="color:#7bed9f;">HP ${item.hp.toLocaleString()} / AP ${item.ap.toLocaleString()}</div>
          <div class="chip-meta" style="color:#8ec5ff;font-size:10px;">${abTags}</div>
        </div>
      `;
      card.onclick = () => {
        inspectCat(item);
        const idx = SELECTED_DECK.indexOf(item.key);
        if (idx >= 0) {
          if (SELECTED_DECK.length > 1) {
            SELECTED_DECK.splice(idx, 1);
          } else {
            showToast("出陣牌組至少需保留 1 隻角色！");
          }
        } else {
          if (SELECTED_DECK.length >= 10) {
            showToast("牌組已滿 10 隻！請先點擊上方編隊格或已選角色取消勾選。");
            return;
          }
          SELECTED_DECK.push(item.key);
        }
        renderDeckSelector();
        syncDeckIfInRoom();
      };
      container.appendChild(card);
    });
  }

  const lbl = document.getElementById("deckCountLabel");
  if (lbl) lbl.textContent = `已選 ${SELECTED_DECK.length} / 10`;

  refreshActiveCombos();

  // Also update compact 10-slot summary strip on the clean Entry Hub tab (#lobbyDeckPreview)
  const summaryEl = document.getElementById("lobbyDeckPreview");
  if (summaryEl) {
    summaryEl.innerHTML = "";
    SELECTED_DECK.slice(0, 10).forEach(k => {
      const it = byKey.get(k);
      if (!it) return;
      const chip = document.createElement("div");
      chip.className = "lobby-deck-mini";
      chip.title = `${it.name} (${formatMaxLevelLabel(it)})`;
      const iconUrl = unitIconUrl(it);
      chip.innerHTML = iconUrl
        ? `<img class="${it.portrait ? "builtin-portrait" : ""}" src="${iconUrl}" alt="${it.name}"><span>${it.name}</span>`
        : `<div class="mini-custom">★</div><span>${it.name}</span>`;
      chip.onclick = () => {
        inspectCat(it);
        switchTab("formation");
      };
      summaryEl.appendChild(chip);
    });
  }
}

function renderFormationPreview() {
  const pCanvas = document.getElementById("formationPreviewCanvas");
  if (!pCanvas || !inspectedItem) return;
  const ctx = pCanvas.getContext("2d");
  ctx.clearRect(0, 0, pCanvas.width, pCanvas.height);

  // Subtle ground line
  ctx.strokeStyle = "rgba(255, 204, 51, 0.35)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(20, 152);
  ctx.lineTo(pCanvas.width - 20, 152);
  ctx.stroke();

  if (inspectedItem.portrait) {
    drawPortrait(ctx, inspectedItem.portrait, pCanvas.width / 2, 150, 190, 140);
    return;
  }

  const rigCode = inspectedItem.rig || "000_f";
  const rigObj = RIGS[rigCode] || RIGS["000_f"];
  const isEnemyRig = rigObj ? !!rigObj.is_enemy : rigCode.includes("_e");
  const baseScale = inspectedItem.scale || UNIT_RENDER_SCALE;
  const previewScale = Math.min(0.95, baseScale * 0.72);
  renderSkeletalRig(ctx, rigCode, formationPreviewAnim, previewFrame, pCanvas.width * 0.52, 150, previewScale, isEnemyRig);
}

function renderCustomUnitLibrary() {
  const listEl = document.getElementById("customUnitLibraryList");
  if (!listEl) return;
  listEl.innerHTML = "";
  CUSTOM_UNITS.forEach(cu => {
    const row = document.createElement("div");
    row.className = "room-item";
    row.style.cursor = "pointer";
    row.innerHTML = `
      <div>
        <strong style="color:var(--gold-bright);">${cu.name}</strong>
        <span style="font-size:12px;color:#9fb1cc;margin-left:8px;">[${cu.base_rig || "004_f"}] HP:${cu.hp} AP:${cu.ap} 射程:${cu.range} 費用:${cu.cost}元</span>
      </div>
      <button type="button">載入編輯</button>
    `;
    row.onclick = () => {
      document.getElementById("cuName").value = cu.name || "";
      document.getElementById("cuId").value = cu.key || cu.id || "";
      document.getElementById("cuRig").value = cu.base_rig || "004_f";
      document.getElementById("cuScale").value = cu.scale || 0.7;
      document.getElementById("cuArea").value = cu.is_area_attack !== false ? "true" : "false";
      document.getElementById("cuHp").value = cu.hp || 10000;
      document.getElementById("cuAp").value = cu.ap || 2000;
      document.getElementById("cuRange").value = cu.range || 250;
      document.getElementById("cuSpeed").value = cu.speed || 20;
      document.getElementById("cuCost").value = cu.cost || 400;
      document.getElementById("cuCd").value = cu.cd_frames || 60;
      const ab = cu.abilities || {};
      document.getElementById("cuWaveProb").value = ab.wave_prob || 0;
      document.getElementById("cuWaveLvl").value = ab.wave_lvl || 2;
      document.getElementById("cuCritProb").value = ab.crit_prob || 0;
      document.getElementById("cuFreezeProb").value = ab.freeze_prob || 0;
      document.getElementById("cuFreezeFrames").value = ab.freeze_frames || 60;
    };
    listEl.appendChild(row);
  });
}

async function saveCustomUnit() {
  const baseRig = document.getElementById("cuRig").value;
  const rigObj = RIGS[baseRig];
  const rigAtkMax = (rigObj && rigObj.anims && rigObj.anims.attack && rigObj.anims.attack.max_frame) || 24;
  const preAtkFrames = Math.min(10, Math.max(2, rigAtkMax - 2));
  const spec = {
    id: document.getElementById("cuId").value.trim() || `custom_${Date.now()}`,
    name: document.getElementById("cuName").value.trim() || "自製英雄貓",
    base_rig: baseRig,
    scale: parseFloat(document.getElementById("cuScale").value) || UNIT_RENDER_SCALE,
    is_area_attack: document.getElementById("cuArea").value === "true",
    hp: parseInt(document.getElementById("cuHp").value, 10) || 10000,
    ap: parseInt(document.getElementById("cuAp").value, 10) || 2000,
    range: parseInt(document.getElementById("cuRange").value, 10) || 260,
    speed: parseInt(document.getElementById("cuSpeed").value, 10) || 24,
    cost: parseInt(document.getElementById("cuCost").value, 10) || 400,
    cd_frames: parseInt(document.getElementById("cuCd").value, 10) || 60,
    kb_count: 3,
    pre_atk_frames: preAtkFrames,
    tba_frames: 20,
    attack_anim_frames: Math.max(preAtkFrames + 2, rigAtkMax),
    abilities: {
      wave_prob: parseInt(document.getElementById("cuWaveProb").value, 10) || 0,
      wave_lvl: parseInt(document.getElementById("cuWaveLvl").value, 10) || 2,
      crit_prob: parseInt(document.getElementById("cuCritProb").value, 10) || 0,
      freeze_prob: parseInt(document.getElementById("cuFreezeProb").value, 10) || 0,
      freeze_frames: parseInt(document.getElementById("cuFreezeFrames").value, 10) || 60,
    },
  };

  try {
    const res = await fetch("/api/custom_unit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec }),
    });
    const data = await res.json();
    if (data.ok) {
      CUSTOM_UNITS = data.custom_units || [];
      if (!SELECTED_DECK.includes(data.key)) {
        if (SELECTED_DECK.length >= 10) SELECTED_DECK.pop();
        SELECTED_DECK.push(data.key);
      }
      renderDeckSelector();
      renderCustomUnitLibrary();
      showToast(`已儲存自製角色「${spec.name}」並加入出陣牌組！`);
    } else {
      showToast(data.error || "儲存失敗");
    }
  } catch (err) {
    showToast("儲存發生錯誤: " + err.message);
  }
}

function updateStudioPreview() {
  previewFrame = 0;
}

// ============================================================================
// 2. ROOM CODE MATCHMAKING (CREATE / JOIN BY ROOM CODE)
// ============================================================================

async function refreshRoomList() {
  try {
    const res = await fetch("/api/room/list");
    const data = await res.json();
    renderRoomListUI(data.rooms || []);
    if (data.public_url) {
      const urlEl = document.getElementById("shareUrlBox");
      if (urlEl) urlEl.textContent = data.public_url;
    }
  } catch (_) {}
}

function renderRoomListUI(rooms) {
  const el = document.getElementById("activeRoomList");
  if (!el) return;
  if (!rooms || rooms.length === 0) {
    el.innerHTML = `<div style="color:#7788a3;font-size:13px;padding:6px 0;">目前尚無房間，請點擊右側「建立對戰房間」取得 4 位數房號！</div>`;
    return;
  }
  el.innerHTML = "";
  rooms.forEach(r => {
    const div = document.createElement("div");
    div.className = "room-item";
    const statusLabel = r.vs_ai
      ? "單人 vs AI 進行中"
      : r.status === "WAITING"
      ? "等待對手輸入房號加入..."
      : `對戰中 (${r.host_name} vs ${r.guest_name})`;
    div.innerHTML = `
      <div>
        <span class="code-pill">房號 ${r.room_code}</span>
        <span style="font-size:13px;color:#c5d4eb;margin-left:10px;">房主: ${r.host_name} ｜ ${statusLabel}</span>
      </div>
      <button onclick="joinRoomDirect('${r.room_code}')">加入此房號</button>
    `;
    el.appendChild(div);
  });
}

async function createRoom(vsAi = false) {
  const playerName = document.getElementById("hostPlayerName").value.trim() || "P1 房主";
  const customCode = document.getElementById("customRoomCodeInput").value.trim();
  try {
    const res = await fetch("/api/room/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        player_name: playerName,
        room_code: customCode,
        vs_ai: vsAi,
        deck: deckForPlayer(playerName),
        cat_level: selectedCatLevel,
        talent_overrides: TALENT_OVERRIDES,
        custom_units: CUSTOM_UNITS,
      }),
    });
    const data = await res.json();
    if (data.ok) {
      enterBattleRoom(data.room_code, TEAM_CAT, data.state, data.player_token);
      showToast(vsAi ? `已開啟單人 AI 練習房（房號 ${data.room_code}）！` : `房間建立成功！請把房號【${data.room_code}】告訴朋友！`);
    }
  } catch (err) {
    showToast("建立房間失敗: " + err.message);
  }
}

async function joinRoomByCode() {
  const code = document.getElementById("joinRoomCodeInput").value.trim();
  if (!code) {
    showToast("請先輸入 4 位數房號！");
    return;
  }
  await joinRoomDirect(code);
}

async function joinRoomDirect(code) {
  const playerName = document.getElementById("joinPlayerName").value.trim() || "P2 挑戰者";
  try {
    const res = await fetch("/api/room/join", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        room_code: code,
        player_name: playerName,
        deck: deckForPlayer(playerName),
        talent_overrides: TALENT_OVERRIDES,
        custom_units: CUSTOM_UNITS,
      }),
    });
    const data = await res.json();
    if (!data.ok) {
      showToast(data.error || `找不到房號 ${code}`);
      return;
    }
    enterBattleRoom(data.room_code, TEAM_ENEMY, data.state, data.player_token);
    showToast(`成功加入房號【${data.room_code}】！你操作左側主堡 (P2)！`);
  } catch (err) {
    showToast("加入房間失敗: " + err.message);
  }
}

function enterBattleRoom(roomCode, team, initialState, token = "") {
  currentRoomCode = roomCode;
  playerToken = token;
  myTeam = team;
  roomState = initialState;
  fxHitSparks = [];
  fxDeathSouls = [];
  fxDust = [];
  castleShake = { 0: 0, 1: 0 };
  fxCannonBeams = [];
  fxFloatingTexts = [];
  fxWaves = [];
  fxSurges = [];
  fxExplosions = [];
  fxWaveBlocks = [];
  fxBossShockwaves = [];
  fxToxic = [];
  fxCurse = [];
  fxWarp = [];
  fxShieldShatters = [];
  cameraShake = { timer: 0, intensity: 0, dx: 0, dy: 0 };

  // Position camera near our own castle initially at native 2-row 0.8897 zoom
  const stageLen = (initialState && initialState.length) || 5200;
  battleZoom = BATTLE_MAX_ZOOM_2ROW;
  const stageLenPx = stageLen * (BATTLE_WORLD_SCALE * battleZoom);
  cameraX = stageLenPx < SCREEN_W
    ? (stageLenPx - SCREEN_W) * 0.5
    : (team === TEAM_CAT ? Math.max(0, stageLenPx - SCREEN_W) : 0);

  // Pre-fetch all deck unit rigs so first deployment has zero loading blank frames
  const deckRigs = [];
  if (initialState && initialState.teams) {
    Object.values(initialState.teams).forEach(t => {
      if (t && Array.isArray(t.cards)) {
        t.cards.forEach(c => { if (c && c.rig) deckRigs.push(c.rig); });
      }
    });
  }
  if (deckRigs.length > 0) ensureRigsLoaded(deckRigs);

  document.getElementById("currentRoomCodeDisplay").textContent = roomCode;
  switchTab("battle");
  AUDIO.playBGM("battle");

  // Connect to SSE stream for 30 FPS updates
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  if (new URLSearchParams(window.location.search).get("oneshot") === "1") {
    onReceiveRoomState(initialState);
    return;
  }

  try {
    eventSource = new EventSource(`/api/room/stream?room=${encodeURIComponent(roomCode)}`);
    eventSource.onmessage = ev => {
      try {
        const st = JSON.parse(ev.data);
        onReceiveRoomState(st);
      } catch (_) {}
    };
  } catch (e) {
    // Fallback to fast polling
    let lastEvId = 0;
    pollTimer = setInterval(async () => {
      if (!currentRoomCode) return;
      try {
        const r = await fetch(`/api/room/state?room=${encodeURIComponent(currentRoomCode)}&since=${lastEvId}`);
        if (r.ok) {
          const st = await r.json();
          lastEvId = st.last_event_id || lastEvId;
          onReceiveRoomState(st);
        }
      } catch (_) {}
    }, 35);
  }
}

function onReceiveRoomState(st) {
  roomState = st;

  // Synchronize 60/120 FPS client-side unit visual interpolators
  const activeIds = new Set();
  const missingRigs = [];
  if (st && Array.isArray(st.units)) {
    st.units.forEach(u => {
      activeIds.add(u.id);
      if (!u.portrait && u.rig && !RIGS[u.rig]) missingRigs.push(u.rig);
      const isOneshot = new URLSearchParams(window.location.search).get("oneshot") === "1";
      let vis = unitVisualState.get(u.id);
      const atkId = u.atk_id || 0;
      if (!vis || vis.key !== u.key) {
        vis = {
          key: u.key,
          renderX: u.x,
          targetX: u.x,
          renderY: u.y || 0,
          targetY: u.y || 0,
          localAf: u.af,
          serverAf: u.af,
          anim: u.anim,
          atkId: atkId,
        };
        unitVisualState.set(u.id, vis);
      } else {
        vis.targetX = u.x;
        vis.targetY = u.y || 0;
        const prevServerAf = vis.serverAf !== undefined ? vis.serverAf : u.af;
        vis.serverAf = u.af;
        if (vis.anim !== u.anim || (u.anim === "attack" && vis.atkId !== atkId)) {
          vis.anim = u.anim;
          vis.atkId = atkId;
          vis.localAf = u.af;
        } else if (isOneshot) {
          vis.localAf = u.af;
        } else if (u.af < prevServerAf || (u.af - vis.localAf) > 1.5) {
          // Looping frame wrap (walk/idle/kb) or >1.5f lag catch-up; never snap backwards mid-attack
          vis.localAf = u.af;
        }
      }
    });
  }
  for (const k of unitVisualState.keys()) {
    if (!activeIds.has(k)) unitVisualState.delete(k);
  }
  if (missingRigs.length > 0) ensureRigsLoaded(missingRigs);

  const statusEl = document.getElementById("battleStatusText");
  if (statusEl) {
    const roleStr = myTeam === TEAM_CAT ? "你操作：右側貓咪主堡 (P1)" : "你操作：左側主堡 (P2)";
    if (st.status === "WAITING") {
      statusEl.textContent = `等待朋友輸入房號【${st.room_code}】加入中... (${roleStr})`;
      statusEl.style.color = "#ffe680";
    } else if (st.status === "PLAYING") {
      statusEl.textContent = `${st.host_name} vs ${st.guest_name} ｜ ${roleStr}`;
      statusEl.style.color = "#82e0aa";
    } else if (st.status === "FINISHED") {
      const winText = st.winner === myTeam ? "勝利 VICTORY!" : "敗北 DEFEAT!";
      statusEl.textContent = `對戰結束：${winText}（可點擊右側「重新開局」）`;
      statusEl.style.color = "#ffcc33";
    }
  }

  // Process newly arrived combat visual events
  if (Array.isArray(st.events)) {
    st.events.forEach(item => {
      const ev = item.ev;
      if (!ev) return;
      if (ev.type === "damage") {
        if (ev.target_is_castle) {
          castleShake[ev.team] = 6;
          if (fxDust.length < 10) {
            fxDust.push({ x: ev.x, y: GROUND_Y - 30, age: 0, maxLife: 12, isCastle: true });
          }
          AUDIO.playSFX("castle_hit");
        }
        // Authentic PONOS Battle Cats: NO floating damage text pops up on attack;
        // only the official 000_a.png hit spark and critical hit star appear.
        const rad = ev.radius || 20;
        const jy = (Math.random() - 0.5) * 8;
        const hitX = ev.target_is_castle
          ? (ev.team === TEAM_CAT ? ev.x - 35 : ev.x + 35)
          : (ev.team === TEAM_CAT ? ev.x - rad * 0.8 : ev.x + rad * 0.8);
        const targetY = Math.max(GROUND_Y - 120, GROUND_Y - rad - 6 + jy);
        if (fxHitSparks.length < 16) {
          fxHitSparks.push({
            x: hitX + (Math.random() - 0.5) * 6,
            y: targetY,
            isCrit: !!ev.is_crit,
            age: 0,
          });
        }
        if (ev.is_crit) {
          AUDIO.playSFX("crit");
          cameraShake = { timer: 12, intensity: 6.0, dx: 0, dy: 0 };
        } else if (!ev.target_is_castle) {
          AUDIO.playSFX("hit");
        }
      } else if (ev.type === "knockback") {
        const rad = ev.radius || 20;
        const targetY = Math.max(GROUND_Y - 120, GROUND_Y - rad - 4);
        const sparkX = ev.team === TEAM_CAT ? ev.x - rad * 0.8 : ev.x + rad * 0.8;
        if (fxHitSparks.length < 16) {
          fxHitSparks.push({
            x: sparkX,
            y: targetY,
            isCrit: false,
            age: 0,
          });
        }
        if (fxDust.length < 10) {
          fxDust.push({
            x: sparkX,
            y: GROUND_Y - 4,
            age: 0,
            maxLife: 10,
            isCastle: false
          });
        }
        AUDIO.playSFX("knockback");
      } else if (ev.type === "death") {
        fxDeathSouls.push({
          x: ev.x,
          y: GROUND_Y - 30,
          age: 0,
        });
      } else if (ev.type === "cannon_beam") {
        fxCannonBeams.push({
          startX: ev.start_x,
          endX: ev.end_x,
          castleX: ev.castle_x || ev.start_x,
          team: ev.team || 0,
          age: 0,
          maxLife: 28,
        });
      } else if (ev.type === "wave_proc" || ev.type === "wave_attack") {
        const dir = ev.direction || (ev.end_x !== undefined && ev.start_x !== undefined ? (ev.end_x < ev.start_x ? -1 : 1) : -1);
        fxWaves.push({
          startX: ev.start_x,
          dir: dir,
          lvl: ev.level || 2,
          team: ev.team || 0,
          isMini: !!ev.is_mini,
          seg0: ev.seg0 !== undefined ? ev.seg0 : 67.5,
          prog: ev.prog || 200,
          segFrames: ev.seg_frames || 3,
          age: 0,
        });
      } else if (ev.type === "surge") {
        const lvl = ev.level || 1;
        const delay = ev.delay !== undefined ? ev.delay : 15;
        const totalLife = ev.total_duration || (delay + (ev.duration || 20 * lvl) + 10);
        fxSurges.push({
          x: ev.x,
          team: ev.team || 0,
          lvl: lvl,
          isMini: !!ev.is_mini,
          isCounter: !!ev.is_counter,
          delay: delay,
          maxLife: totalLife,
          radius: 187.5,
          age: 0,
        });
      } else if (ev.type === "explosion") {
        fxExplosions.push({
          x: ev.x,
          team: ev.team || 0,
          delay: ev.delay !== undefined ? ev.delay : 15,
          maxLife: (ev.delay || 15) + (ev.duration || 65),
          age: 0,
        });
      } else if (ev.type === "wave_block" || ev.type === "warp_block") {
        fxWaveBlocks.push({
          x: ev.x,
          team: ev.team || 0,
          age: 0,
          maxLife: 20,
        });
      } else if (ev.type === "boss_shockwave") {
        fxBossShockwaves.push({
          x: ev.x,
          bossName: ev.boss_name || "Boss",
          age: 0,
          maxLife: 30,
        });
        cameraShake.timer = 20;
        cameraShake.intensity = 12;
        AUDIO.playBGM("boss");
        AUDIO.playSFX("boss_shockwave");
        showToast(`⚠️ 頭目來襲！【${ev.boss_name || "BOSS"}】登場！`);
      } else if (ev.type === "boss_death") {
        fxBossShockwaves.push({
          x: ev.x,
          bossName: `擊敗 ${ev.boss_name || "BOSS"}!`,
          age: 0,
          maxLife: 45,
        });
        cameraShake.timer = 30;
        cameraShake.intensity = 16;
        AUDIO.playSFX("boss_death");
        showToast(`🏆 擊敗首領【${ev.boss_name || "BOSS"}】！`, 3500);
      } else if (ev.type === "thunderbolt") {
        cameraShake.timer = 16;
        cameraShake.intensity = 10;
        AUDIO.playSFX("thunderbolt");
      } else if (ev.type === "iron_wall") {
        showToast("🛡️ 貓咪鐵壁砲已就位！");
        AUDIO.playSFX("deploy");
      } else if (ev.type === "toxic") {
        fxToxic.push({
          x: ev.x,
          age: 0,
          maxLife: 20,
        });
      } else if (ev.type === "curse") {
        fxCurse.push({
          x: ev.x,
          age: 0,
          maxLife: 25,
        });
      } else if (ev.type === "warp_enter" || ev.type === "warp_exit") {
        fxWarp.push({
          x: ev.x,
          isExit: ev.type === "warp_exit",
          age: 0,
          maxLife: 20,
        });
      } else if (ev.type === "shield_break" || ev.type === "shield_pierce") {
        fxShieldShatters.push({
          x: ev.x,
          color: "#d946ef",
          age: 0,
          maxLife: 16,
        });
        AUDIO.playSFX("hit");
      } else if (ev.type === "barrier_break") {
        fxShieldShatters.push({
          x: ev.x,
          color: "#38bdf8",
          age: 0,
          maxLife: 16,
        });
        AUDIO.playSFX("hit");
      } else if (ev.type === "game_over") {
        AUDIO.stopBGM();
        if (ev.winner === myTeam) AUDIO.playSFX("victory");
        else AUDIO.playSFX("defeat");
      } else if (ev.type === "nyancombo_activate") {
        const combos = ev.combos || [];
        combos.forEach(c => {
          showToast(`🌟 聯組發動：${c.name} [${c.size}]`);
        });
      }
    });
  }
}

async function sendRoomAction(action, payload = null) {
  if (!currentRoomCode) return;
  try {
    const res = await fetch("/api/room/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        room_code: currentRoomCode,
        team: myTeam,
        player_token: playerToken,
        action,
        payload,
      }),
    });
    const data = await res.json();
    if (data && data.ok) {
      if (action === "SPAWN_UNIT") AUDIO.playSFX("deploy");
      else if (action === "UPGRADE_WORKER") AUDIO.playSFX("wallet_up");
      else if (action === "FIRE_CANNON") AUDIO.playSFX("cannon_fire");
      else AUDIO.playSFX("click");
    }
  } catch (_) {}
}

function triggerRematch() {
  sendRoomAction("REMATCH");
}

function changeBattleSpeed(val) {
  sendRoomAction("SET_SPEED", parseFloat(val) || 1.0);
  showToast(`已切換對戰速度為 ${val}x`);
}

function copyRoomCode() {
  if (!currentRoomCode) return;
  navigator.clipboard.writeText(currentRoomCode);
  showToast(`已複製房號：${currentRoomCode}`);
}

// ============================================================================
// 3. AUTHENTIC PONOS 2D SKELETAL ANIMATION EVALUATOR (.imgcut + .mamodel + .maanim)
// ============================================================================

function evalPolynomialEase(kfs, kfIdx, localFrame) {
  let high = kfIdx;
  let low = kfIdx;
  for (let j = kfIdx - 1; j >= 0; j--) {
    if (kfs[j][2] === 3) low = j;
    else break;
  }
  for (let j = kfIdx + 1; j < kfs.length; j++) {
    high = j;
    if (kfs[j][2] !== 3) break;
  }
  let sum = 0;
  const lf = Math.trunc(localFrame);
  for (let j = low; j <= high; j++) {
    let val = kfs[j][1] * 4096;
    for (let k = low; k <= high; k++) {
      if (k !== j) {
        const denom = kfs[j][0] - kfs[k][0];
        if (denom !== 0) {
          val = Math.trunc((val * (lf - kfs[k][0])) / denom);
        }
      }
    }
    sum += val;
  }
  return Math.trunc(sum / 4096);
}

function evalTrackValue(track, frame) {
  const kfs = track.kfs;
  if (!kfs || kfs.length === 0) return null;
  const intFrame = Math.floor(frame);
  const startF = kfs[0][0];
  const endF = kfs[kfs.length - 1][0];
  if (intFrame < startF) return null;

  const span = endF - startF;
  let localF = intFrame;
  if (intFrame < endF || span <= 0) {
    localF = intFrame;
  } else if (track.loop === -1) {
    localF = ((intFrame - startF) % span) + startF;
  } else if (track.loop >= 1) {
    if ((intFrame - startF) / span < track.loop) {
      localF = ((intFrame - startF) % span) + startF;
    } else {
      localF = endF;
    }
  } else {
    localF = endF;
  }

  if (startF === endF) return kfs[0][1];
  if (localF >= endF) return kfs[kfs.length - 1][1];

  for (let i = 0; i < kfs.length - 1; i++) {
    const kf1 = kfs[i];
    const kf2 = kfs[i + 1];
    if (kf1[0] <= localF && localF < kf2[0]) {
      const fSpan = kf2[0] - kf1[0];
      const easeMode = kf1[2];
      const easePower = kf1[3];
      if (fSpan <= 0 || easeMode === 1) return kf1[1];
      const t = (localF - kf1[0]) / fSpan;
      let rawVal;
      if (easeMode === 0) {
        return kf1[1] + Math.trunc(((kf2[1] - kf1[1]) * (localF - kf1[0])) / fSpan);
      } else if (easeMode === 2) {
        let factor;
        if (easePower >= 0) {
          factor = 1.0 - Math.sqrt(Math.max(0.0, 1.0 - Math.pow(t, easePower)));
        } else {
          factor = Math.sqrt(Math.max(0.0, 1.0 - Math.pow(1.0 - t, -easePower)));
        }
        rawVal = kf1[1] + factor * (kf2[1] - kf1[1]);
      } else if (easeMode === 3) {
        rawVal = evalPolynomialEase(kfs, i, localF);
      } else if (easeMode === 4) {
        const factor = (1.0 - Math.cos((t * Math.PI) / 2.0)) / 2.0;
        rawVal = kf1[1] + factor * (kf2[1] - kf1[1]);
      } else {
        rawVal = kf1[1] + t * (kf2[1] - kf1[1]);
      }
      return Math.trunc(rawVal);
    }
  }
  return kfs[kfs.length - 1][1];
}

function evaluateNativePose(rig, animName, intFrame, flipH = false) {
  const model = rig.model;
  const su = model.scale_unit || 1000;
  const au = model.angle_unit || 3600;
  const alu = model.alpha_unit || 1000;
  const rawParts = model.parts;
  const numParts = rawParts.length;

  // 1. Reset runtime delta channels and overrideable slots (libnative-lib.so 0x340a88..0x340ac0)
  const parts = new Array(numParts);
  for (let i = 0; i < numParts; i++) {
    const p = rawParts[i];
    parts[i] = {
      index: p[0],
      parent: p[1],
      unit: p[2],
      cut: p[3],
      z: p[4],
      baseX: p[5],
      baseY: p[6],
      basePx: p[7],
      basePy: p[8],
      baseSx: p[9],
      baseSy: p[10],
      baseRot: p[11],
      baseAlpha: p[12],
      glow: p[13],
      animX: 0,
      animY: 0,
      animPx: 0,
      animPy: 0,
      animSx: su,
      animSy: su,
      animRot: 0,
      animAlpha: alu,
      ownHFlip: false,
      ownVFlip: false,
    };
  }

  // 2. Apply tracks in file order with OVERWRITE semantics (0x340c84..0x340d90)
  const anim = (rig.anims && (rig.anims[animName] || rig.anims.walk)) || null;
  if (anim && anim.tracks) {
    for (let t = 0; t < anim.tracks.length; t++) {
      const tr = anim.tracks[t];
      const pid = tr.part;
      if (pid >= 0 && pid < numParts) {
        const val = evalTrackValue(tr, intFrame);
        if (val !== null) {
          const pt = parts[pid];
          switch (tr.mod) {
            case 0: pt.parent = val; break;
            case 1: pt.unit = val; break;
            case 2: pt.cut = val; break;
            case 3: pt.z = val; break;
            case 4: pt.animX = val; break;
            case 5: pt.animY = val; break;
            case 6: pt.animPx = val; break;
            case 7: pt.animPy = val; break;
            case 8: pt.animSx = val; pt.animSy = val; break;
            case 9: pt.animSx = val; break;
            case 10: pt.animSy = val; break;
            case 11: pt.animRot = val; break;
            case 12: pt.animAlpha = val; break;
            case 13: pt.ownHFlip = !!val; break;
            case 14: pt.ownVFlip = !!val; break;
          }
        }
      }
    }
  }

  // 3. Recursive parent-first evaluation matching 0x33f0d8..0x33f63c
  const states = new Array(numParts).fill(null);
  const visiting = new Set();

  function evalPart(idx) {
    if (states[idx]) return states[idx];
    const p = parts[idx];
    const pid = p.parent;
    const hasParent = pid >= 0 && pid < numParts && pid !== idx && !visiting.has(pid) && !visiting.has(idx);

    let ps = null;
    let realSx, realSy, realAlpha, effHFlip, effVFlip;

    if (hasParent) {
      visiting.add(idx);
      ps = evalPart(pid);
      visiting.delete(idx);

      realSx = Math.trunc(Math.trunc((p.baseSx * p.animSx * ps.realSx) / su) / su);
      realSy = Math.trunc(Math.trunc((p.baseSy * p.animSy * ps.realSy) / su) / su);
      realAlpha = Math.trunc(Math.trunc((p.baseAlpha * p.animAlpha * ps.realAlpha) / alu) / alu);
      effHFlip = (p.ownHFlip ? 1 : 0) ^ ps.effHFlip;
      effVFlip = (p.ownVFlip ? 1 : 0) ^ ps.effVFlip;
    } else {
      realSx = Math.trunc((p.baseSx * p.animSx) / su);
      if (flipH) realSx = -realSx;
      realSy = Math.trunc((p.baseSy * p.animSy) / su);
      realAlpha = Math.trunc((p.baseAlpha * p.animAlpha) / alu);
      effHFlip = p.ownHFlip ? 1 : 0;
      effVFlip = p.ownVFlip ? 1 : 0;
    }

    // 0x33f208..0x33f21c
    if (p.ownHFlip) realSx = -realSx;
    if (p.ownVFlip) realSy = -realSy;

    let m0, m1, m2, m3, m4, m5;
    if (ps) {
      const pm = ps.mat;
      m0 = pm[0]; m1 = pm[1]; m2 = pm[2];
      m3 = pm[3]; m4 = pm[4]; m5 = pm[5];
      const tx = Math.trunc((ps.realSx * (p.baseX + p.animX)) / su);
      const ty = Math.trunc((ps.realSy * (p.baseY + p.animY)) / su);
      m2 += m0 * tx + m1 * ty;
      m5 += m3 * tx + m4 * ty;
    } else {
      m0 = 1.0; m1 = 0.0; m2 = p.animX;
      m3 = 0.0; m4 = 1.0; m5 = p.animY;
    }

    // 0x33f58c..0x33f5d8: local rotation with flip_h and (effHFlip != effVFlip) negation
    const totalRot = p.baseRot + p.animRot;
    if (totalRot !== 0) {
      let deg = (totalRot * 360.0) / au;
      if (flipH) deg = -deg;
      if (effHFlip !== effVFlip) deg = -deg;
      const rad = (deg * Math.PI) / 180.0;
      const sinR = Math.sin(rad);
      const cosR = Math.cos(rad);
      const f0 = m0 * cosR + m1 * sinR;
      const f1 = m1 * cosR - m0 * sinR;
      const f3 = m3 * cosR + m4 * sinR;
      const f4 = m4 * cosR - m3 * sinR;
      m0 = f0; m1 = f1; m3 = f3; m4 = f4;
    }

    const st = {
      realSx,
      realSy,
      realAlpha,
      effHFlip,
      effVFlip,
      effPx: p.basePx + p.animPx,
      effPy: p.basePy + p.animPy,
      mat: [m0, m1, m2, m3, m4, m5],
    };
    states[idx] = st;
    return st;
  }

  for (let i = 0; i < numParts; i++) {
    evalPart(i);
  }

  // 4. ints[0] anchor subtraction (0x33f9e0)
  let anchorX = 0;
  let anchorY = 0;
  if (model.ints && model.ints.length > 0 && model.ints[0].length >= 4 && numParts > 0) {
    const row0 = model.ints[0];
    const apId = row0[0];
    if (apId >= 0 && apId < numParts) {
      const aps = states[apId];
      const lx = Math.trunc((aps.realSx * (row0[2] - aps.effPx)) / su);
      const ly = Math.trunc((aps.realSy * (row0[3] - aps.effPy)) / su);
      const am = aps.mat;
      anchorX = Math.trunc(am[2] + am[0] * lx + am[1] * ly);
      anchorY = Math.trunc(am[5] + am[3] * lx + am[4] * ly);
    }
  }

  return { parts, states, anchorX, anchorY, su, alu, numParts };
}

function renderSkeletalRig(ctx, rigCode, animName, frame, rootX, rootY, scale = UNIT_RENDER_SCALE, flipH = false) {
  let rig = RIGS[rigCode];
  if (!rig) {
    if (rigCode) ensureRigLoaded(rigCode);
    return;
  }
  const sheet = loadImage(`rig_${rig.code}`, rig.png);
  if (!sheet || !sheet.complete || sheet.naturalWidth === 0) return;

  const intFrame = Math.floor(frame);
  const pose = evaluateNativePose(rig, animName, intFrame, flipH);
  const { parts, states, anchorX, anchorY, su, alu, numParts } = pose;

  const order = new Array(numParts);
  for (let i = 0; i < numParts; i++) order[i] = i;
  order.sort((a, b) => parts[a].z - parts[b].z || a - b);

  // 4. Draw sorted parts with native signed affine matrix and ints[0] anchor offset
  ctx.save();
  ctx.translate(rootX, rootY);

  for (let k = 0; k < order.length; k++) {
    const i = order[k];
    const p = parts[i];
    if (p.unit < 0 || p.cut < 0) continue;
    const cut = rig.cuts[p.cut];
    if (!cut || cut[2] <= 0 || cut[3] <= 0) continue;

    const st = states[i];
    const alpha = Math.max(0.0, Math.min(1.0, st.realAlpha / alu));
    if (alpha <= 0.01) continue;

    const sx = (st.realSx / su) * scale;
    const sy = (st.realSy / su) * scale;
    if (Math.abs(sx) < 1e-4 || Math.abs(sy) < 1e-4) continue;

    const m = st.mat;
    ctx.save();
    ctx.transform(
      m[0] * sx,
      m[3] * sx,
      m[1] * sy,
      m[4] * sy,
      (m[2] - anchorX) * scale,
      (m[5] - anchorY) * scale
    );
    ctx.globalAlpha = alpha;
    if (p.glow === 1 || p.glow === 2 || p.glow === 3 || p.glow === -1) {
      ctx.globalCompositeOperation = "lighter";
    }
    ctx.drawImage(
      sheet,
      cut[0],
      cut[1],
      cut[2],
      cut[3],
      -st.effPx,
      -st.effPy,
      cut[2],
      cut[3]
    );
    ctx.restore();
  }

  ctx.restore();
}

// ============================================================================
// 4. BATTLEFIELD & OFFICIAL UI RENDERER (1280x720)
// ============================================================================

function drawSubImage(ctx, img, sx, sy, sw, sh, dx, dy, dw, dh, flipH = false) {
  if (!img || !img.complete || img.naturalWidth === 0) return;
  ctx.save();
  if (flipH) {
    ctx.translate(dx + dw, dy);
    ctx.scale(-1, 1);
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, dw, dh);
  } else {
    ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
  }
  ctx.restore();
}

function renderBattleScene() {
  const canvas = document.getElementById("battleCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, SCREEN_W, SCREEN_H);

  const st = roomState;
  const stageLen = (st && st.length) || 5200;
  const { minZoom, maxZoom } = getBattleZoomBounds(stageLen);
  battleZoom = Math.max(minZoom, Math.min(maxZoom, battleZoom));
  const worldScale = BATTLE_WORLD_SCALE * battleZoom;
  const unitScale = UNIT_RENDER_SCALE * battleZoom;
  const stageLenPx = stageLen * worldScale;
  const minCamX = stageLenPx < SCREEN_W ? (stageLenPx - SCREEN_W) * 0.5 : -40;
  const maxCamX = stageLenPx < SCREEN_W ? (stageLenPx - SCREEN_W) * 0.5 : (stageLenPx - SCREEN_W) + 40;

  // Auto-follow frontline if player isn't manually dragging camera
  if (manualCameraHold > 0) {
    manualCameraHold--;
  } else if (st && st.units && st.units.length > 0 && !isDraggingCam) {
    const myUnits = st.units.filter(u => u.team === myTeam);
    if (myUnits.length > 0) {
      const leadX = myTeam === TEAM_CAT
        ? Math.min(...myUnits.map(u => u.x))
        : Math.max(...myUnits.map(u => u.x));
      const leadPx = leadX * worldScale;
      const targetCam = Math.max(minCamX, Math.min(maxCamX, leadPx - SCREEN_W * 0.5));
      cameraX += (targetCam - cameraX) * 0.08;
    }
  }
  cameraX = Math.max(minCamX, Math.min(maxCamX, cameraX));
  const worldToScreenX = wx => wx * worldScale - (cameraX + cameraShake.dx);

  // 1. Seamless Tiled Official Korea Stage Background (bg000.png: native 0x4b7cf8 960x640 * zoom * 9/8, aligned so grass strip sits on GROUND_Y)
  const bg = IMAGES.bg000;
  if (bg && bg.complete && bg.naturalWidth > 0) {
    const bgScale = (640.0 / 510.0) * unitScale;
    const tileW = Math.round(768 * bgScale);
    const tileH = Math.round(510 * bgScale);
    const bgTopY = Math.round(GROUND_Y - 408 * bgScale);
    if (bgTopY > 0) {
      ctx.fillStyle = "#72c0f9";
      ctx.fillRect(0, 0, SCREEN_W, bgTopY + 2);
    }
    const rawOff = Math.floor(cameraX * 0.7) % tileW;
    const offset = (rawOff + tileW) % tileW;
    for (let x = -offset - tileW; x < SCREEN_W + tileW; x += tileW) {
      ctx.drawImage(bg, 1, 1, 768, 510, x, bgTopY, tileW, tileH);
      if (bgTopY + tileH < SCREEN_H) {
        ctx.drawImage(bg, 1, 450, 768, 55, x, bgTopY + tileH - 2, tileW, SCREEN_H - (bgTopY + tileH) + 2);
      }
    }
  } else {
    ctx.fillStyle = "#87ceeb";
    ctx.fillRect(0, 0, SCREEN_W, GROUND_Y);
    ctx.fillStyle = "#7ec850";
    ctx.fillRect(0, GROUND_Y, SCREEN_W, 44);
    ctx.fillStyle = "#916b46";
    ctx.fillRect(0, GROUND_Y + 44, SCREEN_W, SCREEN_H - GROUND_Y - 44);
  }

  // 2. Castles & Modular Cat Cannon Turret
  const team0 = st && st.teams && st.teams["0"];
  const team1 = st && st.teams && st.teams["1"];
  const rShake = castleShake[TEAM_CAT] > 0 ? (Math.random() - 0.5) * 8 : 0;
  const lShake = castleShake[TEAM_ENEMY] > 0 ? (Math.random() - 0.5) * 8 : 0;
  const rightCastleX = worldToScreenX((team0 && team0.castle_x) || stageLen - 700) + rShake;
  const leftCastleX = worldToScreenX((team1 && team1.castle_x) || 700) + lShake;

  const ratio1 = team1 ? Math.min(1, team1.cannon_charge / Math.max(1, team1.cannon_max)) : 0;
  const ratio0 = team0 ? Math.min(1, team0.cannon_charge / Math.max(1, team0.cannon_max)) : 0;
  const topY1 = drawModularCatCastle(ctx, leftCastleX, true, team1 ? team1.cannon_anim : 0, ratio1, unitScale);
  const topY0 = drawModularCatCastle(ctx, rightCastleX, false, team0 ? team0.cannon_anim : 0, ratio0, unitScale);

  // Official Castle HP Digits (img001_tw.png cuts, NO synthetic green health bar; skip when castle is off-screen)
  if (team1 && leftCastleX > -120 && leftCastleX < SCREEN_W + 120) {
    drawCastleHp(ctx, leftCastleX, Math.max(82, topY1 - 24), team1.castle_hp, team1.castle_max_hp);
  }
  if (team0 && rightCastleX > -120 && rightCastleX < SCREEN_W + 120) {
    drawCastleHp(ctx, rightCastleX, Math.max(82, topY0 - 24), team0.castle_hp, team0.castle_max_hp);
  }

  // 3. Cat Cannon Range Line & Icon on Battlefield when Local Player's Cannon is 100% Ready
  const myHud = st && st.teams && st.teams[String(myTeam)];
  if (myHud && myHud.cannon_charge >= myHud.cannon_max && IMAGES.img002 && IMAGES.img002.complete) {
    const cRange = (myHud && myHud.cannon_range) || (st && st.cannon_range) || 588.75;
    const rangeWorldX = myTeam === TEAM_CAT
      ? Math.max(0, myHud.castle_x - 67.5 - cRange)
      : Math.min(stageLen, myHud.castle_x + 67.5 + cRange);
    const rSx = worldToScreenX(rangeWorldX);
    drawSubImage(ctx, IMAGES.img002, 441, 260, 9, 98, rSx - 4 * battleZoom, GROUND_Y - 120 * battleZoom, 9 * battleZoom, 120 * battleZoom);
    drawSubImage(ctx, IMAGES.img002, 442, 421, 69, 85, rSx - 28 * battleZoom, GROUND_Y - 178 * battleZoom, 56 * battleZoom, 68 * battleZoom, myTeam === TEAM_ENEMY);
  }

  // 4. Units (Sorted by slot for authentic depth layering, using 60/120 FPS smooth interpolation
  //    and PONOS's native .mamodel shadow part without synthetic ellipse or shadow-lifting kbY)
  if (st && Array.isArray(st.units)) {
    const sortedUnits = [...st.units].sort((a, b) => a.slot - b.slot);
    sortedUnits.forEach(u => {
      const vis = unitVisualState.get(u.id);
      const drawWorldX = vis ? vis.renderX : u.x;
      const drawWorldY = vis ? vis.renderY : (u.y || 0);
      const drawAf = vis ? vis.localAf : u.af;
      // [REA-AUTHENTIC-ALIGNMENT: Removed synthetic +/- 48px offset; unit root aligns 1:1 with pos_x and hitboxes]
      const visualWorldX = drawWorldX;
      const sx = worldToScreenX(visualWorldX);
      if (sx < -240 || sx > SCREEN_W + 240) return;

      const zOff = (u.slot % 5) * 2 - 4;

      // 1. Starred Alien Warp
      if (u.warped) return;

      // 2. Zombie Burrow
      if (u.burrow) {
        ctx.save();
        ctx.fillStyle = "#5c3317";
        ctx.beginPath();
        ctx.ellipse(sx, GROUND_Y - 8 + zOff, 28, 9, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#8b4513";
        ctx.beginPath();
        ctx.ellipse(sx, GROUND_Y - 10 + zOff, 22, 6, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
        return;
      }

      // 3. Zombie Corpse
      if (u.corpse) {
        ctx.save();
        ctx.fillStyle = "#e5e5e5";
        ctx.beginPath();
        ctx.ellipse(sx, GROUND_Y - 6 + zOff, 20, 4, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(sx - 10, GROUND_Y - 8 + zOff, 4, 0, Math.PI * 2);
        ctx.arc(sx + 10, GROUND_Y - 8 + zOff, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(168, 85, 247, 0.4)";
        ctx.beginPath();
        ctx.arc(sx, GROUND_Y - 16 + zOff, 8, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
        return;
      }

      const rigObj = RIGS[u.rig] || RIGS["000_f"];
      const isEnemyRig = rigObj ? !!rigObj.is_enemy : u.rig.includes("_e");
      const flipH = isEnemyRig ? u.team === TEAM_CAT : u.team === TEAM_ENEMY;
      // Authentic PONOS knockback: horizontal slide + native State 3/5/7 KB vertical bounce arc
      const drawY = GROUND_Y + 4 + zOff + drawWorldY * worldScale;
      const uScale = (u.scale ? (u.scale / UNIT_RENDER_SCALE) : 1.0) * unitScale;
      if (u.portrait) {
        drawPortrait(ctx, u.portrait, sx, drawY, 85 * uScale, 110 * uScale);
      } else {
        renderSkeletalRig(ctx, u.rig, u.anim, drawAf, sx, drawY, uScale, flipH);
      }

      // Starred Alien Barrier: Shimmering spherical plasma forcefield
      if (!u.is_dying && u.barrier > 0) {
        ctx.save();
        const pulse = 0.82 + 0.18 * Math.sin(clientTick * 0.35);
        const cy = GROUND_Y - 38 + zOff;
        const grad = ctx.createRadialGradient(sx, cy, 4, sx, cy, 30);
        grad.addColorStop(0, "rgba(56, 189, 248, 0.05)");
        grad.addColorStop(0.7, `rgba(56, 189, 248, ${0.22 * pulse})`);
        grad.addColorStop(1, `rgba(186, 230, 253, ${0.85 * pulse})`);
        ctx.fillStyle = grad;
        ctx.strokeStyle = `rgba(186, 230, 253, ${0.9 * pulse})`;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(sx, cy, 28, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }

      // Authentic Aku Shield: Official PONOS Aku Crest (img002_tw cut 54: 337, 532, 60, 67)
      if (!u.is_dying && u.shield > 0) {
        ctx.save();
        const shX = u.team === TEAM_CAT ? sx + 12 : sx - 34;
        if (IMAGES.img002 && IMAGES.img002.complete) {
          const pulse = 0.85 + 0.15 * Math.sin(clientTick * 0.25);
          ctx.globalAlpha = 0.95 * pulse;
          drawSubImage(ctx, IMAGES.img002, 337, 532, 60, 67, shX, GROUND_Y - 62 + zOff, 36, 40);
        } else {
          ctx.fillStyle = "rgba(217, 70, 239, 0.6)";
          ctx.fillRect(shX, GROUND_Y - 50 + zOff, 20, 30);
        }
        ctx.restore();
      }

      // Curse status
      if (!u.is_dying && u.curse > 0) {
        ctx.save();
        ctx.fillStyle = "rgba(126, 34, 206, 0.9)";
        ctx.beginPath();
        ctx.arc(sx, GROUND_Y - 68 + zOff, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.beginPath();
        ctx.arc(sx - 2, GROUND_Y - 69 + zOff, 1.2, 0, Math.PI * 2);
        ctx.arc(sx + 2, GROUND_Y - 69 + zOff, 1.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }

      // Authentic Freeze Bind & Slow Status Icons (bg00.imgcut cuts 17, 18, 13)
      if (!u.is_dying && u.frz > 0 && IMAGES.bg000 && IMAGES.bg000.complete) {
        const fCut = Math.floor(clientTick / 4) % 2 === 0 ? [933, 248, 60, 60] : [871, 248, 60, 60];
        drawSubImage(ctx, IMAGES.bg000, fCut[0], fCut[1], fCut[2], fCut[3], sx - 36, GROUND_Y - 68 + zOff, 72, 72);
      } else if (!u.is_dying && u.slw > 0 && IMAGES.bg000 && IMAGES.bg000.complete) {
        drawSubImage(ctx, IMAGES.bg000, 772, 248, 45, 50, sx - 22, GROUND_Y - 76 + zOff, 45, 50);
      }
    });
  }

  // 5. Combat FX (Wave Pillars, Cat Cannon Laser & Shockwave Pillars, Hit Sparks, Death Souls)
  renderCombatFX(ctx, worldToScreenX, worldScale, unitScale);

  // 6. Bottom Deploy Bar UI & Top Wallet HUD
  renderDeployBar(ctx, myHud);
  renderTopHud(ctx, st);
}

function drawModularCatCastle(ctx, sx, isLeftTeam, cannonAnim, cannonRatio = 0, unitScale = UNIT_RENDER_SCALE * battleZoom) {
  const baseImg = IMAGES.castle_base;
  const turretImg = IMAGES.castle_turret;
  const plaqueImg = IMAGES.castle_plaque;
  const beamImg = IMAGES.castle_beam;

  // Native 0x4ae8dc..0x4aea14: Cat Castle draws at 1.27x of uniform unitScale,
  // anchored so the castle's front wall sits right at castle.pos_x (sx) instead of sticking 200 DU into the battlefield.
  const cs = unitScale * CASTLE_NATIVE_SCALE;
  const cw = Math.round(128 * cs);
  const ch = Math.round(256 * cs);
  const bw = Math.round(128 * cs);
  const bh = Math.round(128 * cs);
  const csRel = cs / UNIT_RENDER_SCALE; // multiplier relative to legacy 1.125 coords
  const castleCenterX = isLeftTeam ? Math.round(sx - cw * 0.36) : Math.round(sx + cw * 0.36);
  const fcX = Math.round(castleCenterX - cw / 2);
  const fcY = GROUND_Y - ch;

  if (baseImg && baseImg.complete && turretImg && turretImg.complete) {
    let recoilX = 0;
    let liftOffset = 0;
    if (cannonAnim > 0) {
      recoilX = cannonAnim > 24 && cannonAnim % 2 === 0 ? (isLeftTeam ? 4 : -4) : 0;
    } else if (cannonRatio >= 1.0) {
      liftOffset = Math.round(Math.sin(clientTick * 0.2) * 1.5);
    }

    // 1. Full Turret Head + Radar Dish (nyankoCastle_000_00.png is 128x256, aligned with fc000.png)
    drawSubImage(ctx, turretImg, 0, 0, 128, 256, fcX + recoilX, fcY + liftOffset, cw, ch, isLeftTeam);
    // 2. Full Lower Castle Base (nyankoCastle_003_00.png is 128x128 at GROUND_Y - bh)
    drawSubImage(ctx, baseImg, 0, 0, 128, 128, Math.round(castleCenterX - bw / 2), GROUND_Y - bh, bw, bh, isLeftTeam);
    // 3. Full Hanging Cat Paw Plaque (nyankoCastle_002_00.png is 128x128 at exact same origin GROUND_Y - bh)
    if (plaqueImg && plaqueImg.complete) {
      drawSubImage(ctx, plaqueImg, 0, 0, 128, 128, Math.round(castleCenterX - bw / 2), GROUND_Y - bh, bw, bh, isLeftTeam);
    }
    // 4. Muzzle flash ONLY while actively firing (cannonAnim > 16)
    if (cannonAnim > 16 && beamImg && beamImg.complete) {
      const mx = isLeftTeam ? castleCenterX + Math.round(38 * csRel) : castleCenterX - Math.round(38 * csRel);
      const my = GROUND_Y - Math.round(169 * csRel) + liftOffset;
      const mHalf = Math.round(55 * csRel);
      drawSubImage(ctx, beamImg, 154, 157, 84, 84, mx - mHalf, my - mHalf, mHalf * 2, mHalf * 2);
    }
    return fcY;
  } else if (IMAGES.fc000 && IMAGES.fc000.complete) {
    drawSubImage(ctx, IMAGES.fc000, 0, 0, 128, 256, fcX, fcY, cw, ch, isLeftTeam);
    return fcY;
  }
  return fcY;
}

function drawCastleHp(ctx, sx, y, hp, maxHp) {
  const hpStr = `${hp}/${maxHp}`;
  const im1 = IMAGES.img001;
  if (im1 && im1.complete && im1.naturalWidth > 0) {
    const charW = 15;
    const totalW = hpStr.length * charW;
    // Clamp horizontal position so HP digits are never clipped off the left/right screen edge
    let bx = Math.max(10, Math.min(SCREEN_W - totalW - 10, Math.round(sx - totalW / 2)));
    for (let i = 0; i < hpStr.length; i++) {
      const ch = hpStr[i];
      if (ch === "/") {
        // Official Castle HP '/' slash in img001_tw.imgcut cut 126: (474, 89, 14, 18)
        drawSubImage(ctx, im1, 474, 89, 14, 18, bx, y, 15, 20);
      } else {
        const d = ch.charCodeAt(0) - 48;
        if (d >= 0 && d <= 9) {
          drawSubImage(ctx, im1, 255 + d * 16, 89, 14, 18, bx, y, 15, 20);
        }
      }
      bx += charW;
    }
  } else {
    ctx.font = "900 16px Impact, Arial";
    ctx.textAlign = "center";
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 3;
    ctx.strokeText(hpStr, sx, y + 16);
    ctx.fillStyle = "#fff";
    ctx.fillText(hpStr, sx, y + 16);
  }
}

function advanceClientFX30Fps() {
  for (let i = fxWaves.length - 1; i >= 0; i--) {
    fxWaves[i].age++;
    const segFrames = fxWaves[i].segFrames || 3;
    const maxLife = (fxWaves[i].lvl - 1) * segFrames + 14;
    if (fxWaves[i].age > maxLife) fxWaves.splice(i, 1);
  }
  for (let i = fxCannonBeams.length - 1; i >= 0; i--) {
    fxCannonBeams[i].age++;
    if (fxCannonBeams[i].age >= fxCannonBeams[i].maxLife) fxCannonBeams.splice(i, 1);
  }
  for (let i = fxHitSparks.length - 1; i >= 0; i--) {
    fxHitSparks[i].age++;
    if (fxHitSparks[i].age >= 11) fxHitSparks.splice(i, 1);
  }
  for (let i = fxDust.length - 1; i >= 0; i--) {
    fxDust[i].age++;
    if (fxDust[i].age >= fxDust[i].maxLife) fxDust.splice(i, 1);
  }
  if (castleShake[0] > 0) castleShake[0]--;
  if (castleShake[1] > 0) castleShake[1]--;
  for (let i = fxDeathSouls.length - 1; i >= 0; i--) {
    fxDeathSouls[i].age++;
    if (fxDeathSouls[i].age >= 45) fxDeathSouls.splice(i, 1);
  }
  for (let i = fxFloatingTexts.length - 1; i >= 0; i--) {
    fxFloatingTexts[i].age++;
    if (fxFloatingTexts[i].age >= 26) fxFloatingTexts.splice(i, 1);
  }
  for (let i = fxSurges.length - 1; i >= 0; i--) {
    fxSurges[i].age++;
    if (fxSurges[i].age >= fxSurges[i].maxLife) fxSurges.splice(i, 1);
  }
  for (let i = fxExplosions.length - 1; i >= 0; i--) {
    fxExplosions[i].age++;
    if (fxExplosions[i].age >= fxExplosions[i].maxLife) fxExplosions.splice(i, 1);
  }
  for (let i = fxWaveBlocks.length - 1; i >= 0; i--) {
    fxWaveBlocks[i].age++;
    if (fxWaveBlocks[i].age >= fxWaveBlocks[i].maxLife) fxWaveBlocks.splice(i, 1);
  }
  for (let i = fxBossShockwaves.length - 1; i >= 0; i--) {
    fxBossShockwaves[i].age++;
    if (fxBossShockwaves[i].age >= fxBossShockwaves[i].maxLife) fxBossShockwaves.splice(i, 1);
  }
  for (let i = fxToxic.length - 1; i >= 0; i--) {
    fxToxic[i].age++;
    if (fxToxic[i].age >= fxToxic[i].maxLife) fxToxic.splice(i, 1);
  }
  for (let i = fxCurse.length - 1; i >= 0; i--) {
    fxCurse[i].age++;
    if (fxCurse[i].age >= fxCurse[i].maxLife) fxCurse.splice(i, 1);
  }
  for (let i = fxWarp.length - 1; i >= 0; i--) {
    fxWarp[i].age++;
    if (fxWarp[i].age >= fxWarp[i].maxLife) fxWarp.splice(i, 1);
  }
  for (let i = fxShieldShatters.length - 1; i >= 0; i--) {
    fxShieldShatters[i].age++;
    if (fxShieldShatters[i].age >= fxShieldShatters[i].maxLife) fxShieldShatters.splice(i, 1);
  }
  if (cameraShake.timer > 0) {
    cameraShake.timer--;
    const decay = cameraShake.timer / 20.0;
    cameraShake.dx = (Math.random() - 0.5) * 2 * cameraShake.intensity * decay;
    cameraShake.dy = (Math.random() - 0.5) * 2 * cameraShake.intensity * decay;
  } else {
    cameraShake.dx = 0;
    cameraShake.dy = 0;
  }
}

function renderCombatFX(ctx, worldToScreenX, worldScale = BATTLE_WORLD_SCALE, unitScale = UNIT_RENDER_SCALE) {
  const zScale = unitScale / UNIT_RENDER_SCALE;
  // 1. Wave & Mini-Wave Attack Pillars (Official PONOS skill_wave_attack / skill_smallwave_attack rigs)
  const blueWaveCuts = [
    [888, 83, 56, 80],
    [946, 83, 56, 80],
    [772, 165, 56, 80],
    [830, 165, 56, 80],
    [888, 165, 56, 80],
    [946, 165, 56, 80],
  ];
  const pinkWaveCuts = [
    [772, 1, 56, 80],
    [830, 1, 56, 80],
    [888, 1, 56, 80],
    [946, 1, 56, 80],
    [772, 83, 56, 80],
    [830, 83, 56, 80],
  ];
  for (let i = fxWaves.length - 1; i >= 0; i--) {
    const w = fxWaves[i];
    const rigCode = w.isMini
      ? (w.team === TEAM_ENEMY ? "fx_miniwave_e" : "fx_miniwave")
      : (w.team === TEAM_ENEMY ? "fx_wave_e" : "fx_wave");
    const hasOfficialRig = !!RIGS[rigCode];
    const waveCuts = w.team === TEAM_ENEMY ? pinkWaveCuts : blueWaveCuts;
    const segFrames = w.segFrames || 3;
    const seg0 = w.seg0 !== undefined ? w.seg0 : 67.5;
    const prog = w.prog || 200;
    for (let lv = 0; lv < w.lvl; lv++) {
      const trig = lv * segFrames;
      const localAge = w.age - trig;
      if (localAge >= 0 && localAge <= 12) {
        const px = worldToScreenX(w.startX + w.dir * (seg0 + lv * prog));
        if (px < -180 || px > SCREEN_W + 180) continue;
        if (hasOfficialRig) {
          renderSkeletalRig(ctx, rigCode, "play", localAge, px, GROUND_Y + 4 * zScale, unitScale, false);
        } else if (IMAGES.bg000 && IMAGES.bg000.complete) {
          const cIdx = Math.min(waveCuts.length - 1, Math.floor(localAge / 2));
          const wc = waveCuts[cIdx];
          drawSubImage(ctx, IMAGES.bg000, wc[0], wc[1], wc[2], wc[3], px - 45 * zScale, GROUND_Y - 132 * zScale, 90 * zScale, 138 * zScale, w.dir > 0);
        }
      }
    }
  }

  // 2. Cat Cannon Laser Beam & Sweeping Shockwave Explosion Pillars (nyankoCastle_001_00_00.png)
  const pillarCuts = [
    [78, 156, 74, 41, 96, 54],
    [1, 1, 74, 111, 96, 144],
    [154, 23, 74, 131, 96, 170],
    [2, 114, 74, 131, 96, 170],
    [78, 23, 74, 131, 96, 170],
  ];
  for (let i = fxCannonBeams.length - 1; i >= 0; i--) {
    const cb = fxCannonBeams[i];
    const sSx = worldToScreenX(cb.startX);
    const cSx = worldToScreenX(cb.castleX);
    const isLeft = cb.team === TEAM_ENEMY;
    const cs = unitScale * CASTLE_NATIVE_SCALE;
    const cw = Math.round(128 * cs);
    const cRel = CASTLE_NATIVE_SCALE * zScale;
    const castleCenterX = isLeft ? (cSx - cw * 0.36) : (cSx + cw * 0.36);

    if (cb.age <= 12 && IMAGES.castle_beam && IMAGES.castle_beam.complete) {
      const muzzleX = isLeft ? (castleCenterX + 38 * cRel) : (castleCenterX - 38 * cRel);
      const muzzleY = GROUND_Y - 169 * cRel;
      const mHalf = 55 * cRel;
      const hitY = GROUND_Y - 10 * zScale;
      const dx = sSx - muzzleX;
      const dy = hitY - muzzleY;
      const beamLen = Math.max(20, Math.hypot(dx, dy));
      const beamAngle = Math.atan2(dy, dx);
      const beamFade = cb.age > 8 ? Math.max(0.2, 1.0 - (cb.age - 8) / 5.0) : 1.0;

      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = beamFade;
      // Diagonal laser beam from turret muzzle straight to ground impact point
      ctx.translate(muzzleX, muzzleY);
      ctx.rotate(beamAngle);
      ctx.drawImage(IMAGES.castle_beam, 145, 249, 110, 6, 0, -11 * zScale, beamLen, 22 * zScale);
      ctx.restore();

      ctx.save();
      ctx.globalAlpha = beamFade;
      drawSubImage(ctx, IMAGES.castle_beam, 154, 157, 84, 84, muzzleX - mHalf, muzzleY - mHalf, mHalf * 2, mHalf * 2);
      ctx.restore();
    }
    const dist = Math.abs(cb.endX - cb.startX);
    const dir = cb.endX < cb.startX ? -1 : 1;
    const stepDu = 95.0; // Tightly-spaced authentic shockwave pillars matching 14f wavefront
    const count = Math.max(1, Math.floor(dist / stepDu) + 1);
    for (let p = 0; p < count; p++) {
      const trig = Math.floor((p / Math.max(1, count - 1)) * 14);
      const lAge = cb.age - trig;
      if (lAge >= 0 && lAge < 12 && IMAGES.castle_beam && IMAGES.castle_beam.complete) {
        const psx = worldToScreenX(cb.startX + dir * Math.min(dist, p * stepDu));
        const cIdx = Math.min(pillarCuts.length - 1, Math.floor(lAge / 2));
        const pc = pillarCuts[cIdx];
        const pw = pc[4] * zScale;
        const ph = pc[5] * zScale;
        drawSubImage(ctx, IMAGES.castle_beam, pc[0], pc[1], pc[2], pc[3], psx - pw / 2, GROUND_Y - ph + 8 * zScale, pw, ph);
      }
    }
  }

  // 2.5 Dust Puffs (Feet knockback & Castle impacts)
  for (let i = fxDust.length - 1; i >= 0; i--) {
    const d = fxDust[i];
    const sx = worldToScreenX(d.x);
    const progress = d.age / d.maxLife;
    const alpha = Math.max(0, 1.0 - progress);
    if (d.isCastle) {
      ctx.save();
      ctx.fillStyle = `rgba(200, 190, 175, ${0.7 * alpha})`;
      for (let k = 0; k < 4; k++) {
        const cx = sx + (Math.sin(k + d.age) * 20);
        const cy = d.y - progress * 25 + k * 8;
        ctx.beginPath();
        ctx.arc(cx, cy, 5 + progress * 8, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    } else {
      const r = 5 + progress * 14;
      ctx.save();
      ctx.fillStyle = `rgba(225, 220, 205, ${0.65 * alpha})`;
      ctx.beginPath();
      ctx.arc(sx - progress * 10, d.y - progress * 4, r, 0, Math.PI * 2);
      ctx.arc(sx + progress * 8, d.y - progress * 3, r * 0.8, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  // 3. Hit Sparks & Critical Hit Stars (000_a.png)
  const hitCuts = [
    [1, 1, 55, 47],
    [1, 50, 55, 47],
    [1, 99, 55, 47],
    [1, 148, 55, 47],
    [1, 197, 55, 47],
  ];
  for (let i = fxHitSparks.length - 1; i >= 0; i--) {
    const hs = fxHitSparks[i];
    const sx = worldToScreenX(hs.x);
    if (IMAGES.hit_a && IMAGES.hit_a.complete) {
      const idx = Math.min(hitCuts.length - 1, Math.floor(hs.age / 2));
      const c = hitCuts[idx];
      drawSubImage(ctx, IMAGES.hit_a, c[0], c[1], c[2], c[3], sx - 33 * zScale, hs.y - 28 * zScale, 66 * zScale, 56 * zScale);
      if (hs.isCrit) {
        drawSubImage(ctx, IMAGES.hit_a, 58, 133, 50, 46, sx - 31 * zScale, hs.y - 38 * zScale, 62 * zScale, 58 * zScale);
      }
    }
  }

  // 4. Ascending Winged Halo Death Souls (battle_soul_000.png vertical cuts 1, 1 + i*58, 48, 56)
  const soulCuts = [
    [1, 1, 48, 56],
    [1, 59, 48, 56],
    [1, 117, 48, 56],
    [1, 175, 48, 56],
  ];
  for (let i = fxDeathSouls.length - 1; i >= 0; i--) {
    const ds = fxDeathSouls[i];
    const sx = worldToScreenX(ds.x) + Math.sin((ds.age * Math.PI) / 8.0) * 10 * zScale;
    const sy = ds.y - ds.age * 2.5 * zScale;
    if (IMAGES.soul && IMAGES.soul.complete) {
      const c = soulCuts[Math.floor(ds.age / 3) % 4];
      ctx.save();
      if (ds.age > 30) ctx.globalAlpha = Math.max(0, 1 - (ds.age - 30) / 15.0);
      drawSubImage(ctx, IMAGES.soul, c[0], c[1], c[2], c[3], sx - 29 * zScale, sy - 34 * zScale, 58 * zScale, 68 * zScale);
      ctx.restore();
    }
  }

  // 5. Surge & Mini-Surge Attack Pillars (Official PONOS skill_volcano / skill_smallvolcano 3-phase skeletal rigs)
  for (let i = fxSurges.length - 1; i >= 0; i--) {
    const s = fxSurges[i];
    const sx = worldToScreenX(s.x);
    if (sx < -260 || sx > SCREEN_W + 260) continue;
    const delay = s.delay || 15;
    const endDur = 10;
    const rigCode = s.isMini
      ? (s.team === TEAM_ENEMY ? "fx_minisurge_e" : "fx_minisurge")
      : (s.team === TEAM_ENEMY ? "fx_surge_e" : "fx_surge");
    if (RIGS[rigCode]) {
      let animName = "loop";
      let animFrame = 0;
      if (s.age < delay) {
        animName = "start";
        animFrame = s.age;
      } else if (s.age < s.maxLife - endDur) {
        animName = "loop";
        animFrame = s.age - delay;
      } else {
        animName = "end";
        animFrame = Math.max(0, s.age - (s.maxLife - endDur));
      }
      renderSkeletalRig(ctx, rigCode, animName, animFrame, sx, GROUND_Y + 4 * zScale, unitScale, false);
    }
  }

  // 5b. Explosion 3-Ring Blasts (Official PONOS skill_explosion / skill_explosion_e skeletal rigs)
  for (let i = fxExplosions.length - 1; i >= 0; i--) {
    const ex = fxExplosions[i];
    const sx = worldToScreenX(ex.x);
    if (sx < -320 || sx > SCREEN_W + 320) continue;
    const delay = ex.delay || 15;
    const rigCode = ex.team === TEAM_ENEMY ? "fx_explosion_e" : "fx_explosion";
    if (RIGS[rigCode]) {
      const expScale = unitScale;
      const offsetX = (ex.team === TEAM_ENEMY ? -30 : 30) * expScale;
      if (ex.age < delay) {
        const startFrame = Math.min(11, ex.age * (11.0 / Math.max(1, delay)));
        renderSkeletalRig(ctx, rigCode, "start", startFrame, sx + offsetX, GROUND_Y + 4 * zScale, expScale, false);
      } else if (ex.age < delay + 34) {
        const blastFrame = ex.age - delay;
        renderSkeletalRig(ctx, rigCode, "blast", blastFrame, sx + offsetX, GROUND_Y + 4 * zScale, expScale, false);
      }
    }
  }

  // 6. Wave & Warp Block Barrier Ripples (Official PONOS skill_wave_stop / skill_wave_stop_e skeletal rigs)
  for (let i = fxWaveBlocks.length - 1; i >= 0; i--) {
    const wb = fxWaveBlocks[i];
    const sx = worldToScreenX(wb.x);
    if (sx < -200 || sx > SCREEN_W + 200) continue;
    const rigCode = wb.team === TEAM_ENEMY ? "fx_wavestop_e" : "fx_wavestop";
    if (RIGS[rigCode]) {
      renderSkeletalRig(ctx, rigCode, "play", wb.age, sx, GROUND_Y - 40 * zScale, unitScale * 1.2, false);
    }
  }

  // 7. Boss Appearance Shockwaves (from 000_a.png cut 25 or circular fallback)
  for (let i = fxBossShockwaves.length - 1; i >= 0; i--) {
    const bs = fxBossShockwaves[i];
    const sx = worldToScreenX(bs.x);
    const prog = bs.age / bs.maxLife;
    const r = 40 + prog * 700;
    ctx.save();
    ctx.globalAlpha = Math.max(0, 1 - prog);
    if (IMAGES.hit_a && IMAGES.hit_a.complete) {
      drawSubImage(ctx, IMAGES.hit_a, 58, 1, 88, 88, sx - r, GROUND_Y - 30 - r * 0.4, r * 2, r * 0.8);
    } else {
      ctx.strokeStyle = "#fef08a";
      ctx.lineWidth = 6;
      ctx.beginPath();
      ctx.ellipse(sx, GROUND_Y - 20, r, r * 0.4, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  // 8. Toxic Splash Drops
  for (let i = fxToxic.length - 1; i >= 0; i--) {
    const tx = fxToxic[i];
    const sx = worldToScreenX(tx.x);
    const prog = tx.age / tx.maxLife;
    ctx.save();
    ctx.globalAlpha = Math.max(0, 1 - prog);
    ctx.fillStyle = "#a855f7";
    for (let k = 0; k < 6; k++) {
      const px = sx + Math.sin(k * 1.5 + tx.age * 0.2) * (15 + k * 3);
      const py = GROUND_Y - 30 - tx.age * 2 + Math.cos(k) * 10;
      ctx.beginPath();
      ctx.arc(px, py, Math.max(2, 5 * (1 - prog)), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // 9. Curse Swirl
  for (let i = fxCurse.length - 1; i >= 0; i--) {
    const cx = fxCurse[i];
    const sx = worldToScreenX(cx.x);
    const prog = cx.age / cx.maxLife;
    ctx.save();
    ctx.globalAlpha = Math.max(0, 0.8 * (1 - prog));
    ctx.fillStyle = "#7e22ce";
    for (let k = 0; k < 5; k++) {
      const ang = cx.age * 0.2 + k * (Math.PI * 2 / 5);
      const px = sx + Math.cos(ang) * (18 + k * 2);
      const py = GROUND_Y - 40 - cx.age * 1.5 + Math.sin(ang) * 8;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // 10. Starred Alien Warp Vortex
  for (let i = fxWarp.length - 1; i >= 0; i--) {
    const wx = fxWarp[i];
    const sx = worldToScreenX(wx.x);
    const prog = wx.age / wx.maxLife;
    const sc = wx.isExit ? 1 - prog : prog;
    const r = Math.max(6, 40 * sc);
    ctx.save();
    ctx.globalAlpha = Math.max(0, 1 - prog);
    ctx.strokeStyle = "#c084fc";
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.ellipse(sx, GROUND_Y - 35, r, r * 0.6, wx.age * 0.2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.ellipse(sx, GROUND_Y - 35, Math.max(2, r - 8), Math.max(2, (r - 8) * 0.6), -wx.age * 0.3, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  // 11. Aku Shield & Alien Barrier Shatter
  for (let i = fxShieldShatters.length - 1; i >= 0; i--) {
    const ss = fxShieldShatters[i];
    const sx = worldToScreenX(ss.x);
    const prog = ss.age / ss.maxLife;
    ctx.save();
    ctx.globalAlpha = Math.max(0, 1 - prog);
    ctx.fillStyle = ss.color || "#d946ef";
    for (let k = 0; k < 8; k++) {
      const ang = k * (Math.PI * 2 / 8);
      const dist = ss.age * 5;
      const px = sx + Math.cos(ang) * dist;
      const py = GROUND_Y - 40 + Math.sin(ang) * dist + 0.2 * (ss.age * ss.age);
      ctx.fillRect(px - 3, py - 3, 6, 6);
    }
    ctx.restore();
  }
}

function drawWorkerCostLeft(ctx, costVal, startX, y, isBright) {
  const im1 = IMAGES.img001;
  if (!im1 || !im1.complete || im1.naturalWidth === 0) return;
  const str = String(costVal);
  const s = UNIT_RENDER_SCALE; // 1.125 (640p -> 720p)
  const dw = Math.round(18 * s);
  const dh = Math.round(26 * s);
  const step = 15 * s; // native 0x4c1b8c..0x4c1d40: 18px glyph width with -3px kerning
  let curX = startX;
  for (let i = 0; i < str.length; i++) {
    const d = str.charCodeAt(i) - 48;
    if (d >= 0 && d <= 9) {
      if (isBright) {
        drawSubImage(ctx, im1, 41 + d * 20, 148, 18, 26, Math.round(curX), y, dw, dh);
      } else {
        drawSubImage(ctx, im1, 25 + d * 20, 176, 18, 26, Math.round(curX), y, dw, dh);
      }
      curX += step;
    }
  }
  const yw = Math.round(22 * s);
  if (isBright) {
    drawSubImage(ctx, im1, 1, 176, 22, 26, Math.round(curX), y, yw, dh);
  } else {
    drawSubImage(ctx, im1, 241, 148, 22, 26, Math.round(curX), y, yw, dh);
  }
}

function drawSmallCostDigits(ctx, costVal, rightX, y, isBright) {
  const im1 = IMAGES.img001;
  if (!im1 || !im1.complete || im1.naturalWidth === 0) return;
  const str = String(costVal);
  // Draw "元" on the right
  if (isBright) {
    drawSubImage(ctx, im1, 1, 176, 22, 26, rightX - 14, y, 14, 18);
  } else {
    drawSubImage(ctx, im1, 241, 148, 22, 26, rightX - 14, y, 14, 18);
  }
  let curX = rightX - 15;
  for (let i = str.length - 1; i >= 0; i--) {
    const d = str.charCodeAt(i) - 48;
    if (d >= 0 && d <= 9) {
      curX -= 12;
      if (isBright) {
        drawSubImage(ctx, im1, 41 + d * 20, 148, 18, 26, curX, y, 12, 18);
      } else {
        drawSubImage(ctx, im1, 25 + d * 20, 176, 18, 26, curX, y, 12, 18);
      }
    }
  }
}

function drawBigWalletDigits(ctx, curMoney, maxMoney, startX, y, alignRight = true) {
  const im1 = IMAGES.img001;
  if (!im1 || !im1.complete || im1.naturalWidth === 0) return;
  const digitCuts = {
    "0": [1, 1, 30, 42],
    "1": [33, 1, 30, 42],
    "2": [65, 1, 30, 42],
    "3": [97, 1, 30, 42],
    "4": [129, 1, 30, 42],
    "5": [161, 1, 30, 42],
    "6": [193, 1, 30, 42],
    "7": [225, 1, 30, 42],
    "8": [1, 45, 30, 42],
    "9": [33, 45, 30, 42],
    "/": [66, 45, 29, 42],
    unit: [102, 45, 30, 42],
  };

  const curStr = String(curMoney);
  const maxStr = String(maxMoney);
  if (alignRight) {
    let curX = startX;
    // '元'
    const uRec = digitCuts.unit;
    curX -= uRec[2];
    drawSubImage(ctx, im1, uRec[0], uRec[1], uRec[2], uRec[3], curX, y, uRec[2], uRec[3]);
    curX -= 4;
    for (let i = maxStr.length - 1; i >= 0; i--) {
      const rec = digitCuts[maxStr[i]];
      if (rec) {
        curX -= rec[2] + 2;
        drawSubImage(ctx, im1, rec[0], rec[1], rec[2], rec[3], curX, y, rec[2], rec[3]);
      }
    }
    const sRec = digitCuts["/"];
    curX -= sRec[2] + 4;
    drawSubImage(ctx, im1, sRec[0], sRec[1], sRec[2], sRec[3], curX, y, sRec[2], sRec[3]);
    for (let i = curStr.length - 1; i >= 0; i--) {
      const rec = digitCuts[curStr[i]];
      if (rec) {
        curX -= rec[2] + 2;
        drawSubImage(ctx, im1, rec[0], rec[1], rec[2], rec[3], curX, y, rec[2], rec[3]);
      }
    }
  } else {
    let lx = startX;
    for (let i = 0; i < curStr.length; i++) {
      const rec = digitCuts[curStr[i]];
      if (rec) {
        drawSubImage(ctx, im1, rec[0], rec[1], rec[2], rec[3], lx, y, rec[2], rec[3]);
        lx += rec[2] + 2;
      }
    }
    const sRec = digitCuts["/"];
    drawSubImage(ctx, im1, sRec[0], sRec[1], sRec[2], sRec[3], lx, y, sRec[2], sRec[3]);
    lx += sRec[2] + 4;
    for (let i = 0; i < maxStr.length; i++) {
      const rec = digitCuts[maxStr[i]];
      if (rec) {
        drawSubImage(ctx, im1, rec[0], rec[1], rec[2], rec[3], lx, y, rec[2], rec[3]);
        lx += rec[2] + 2;
      }
    }
    const uRec = digitCuts.unit;
    drawSubImage(ctx, im1, uRec[0], uRec[1], uRec[2], uRec[3], lx + 2, y, uRec[2], uRec[3]);
  }
}

function renderDeployBar(ctx, myHud) {
  if (!myHud) return;
  const im1 = IMAGES.img001;
  const im2 = IMAGES.img002;
  const blinkPhase = Math.floor(clientTick / 6) % 2 === 1;

  // 1. Left: Official Worker Cat Upgrade Button (native 0x4c1990 + img002[5,6,24] 194x121 at 1.125x)
  const wBtnX = 8;
  const wBtnY = 584;
  const wBtnW = Math.round(194 * UNIT_RENDER_SCALE); // 218
  const wBtnH = Math.round(121 * UNIT_RENDER_SCALE); // 136
  const wMax = myHud.worker_lv >= 8 || myHud.worker_cost === null || myHud.worker_cost <= 0;
  const wCan = !wMax && myHud.money >= myHud.worker_cost;
  if (im2 && im2.complete) {
    if (wCan) {
      drawSubImage(ctx, im2, blinkPhase ? 197 : 1, 1, 194, 121, wBtnX, wBtnY, wBtnW, wBtnH);
    } else if (wMax) {
      drawSubImage(ctx, im2, 1, 1, 194, 121, wBtnX, wBtnY, wBtnW, wBtnH);
    } else {
      drawSubImage(ctx, im2, 393, 1, 194, 121, wBtnX, wBtnY, wBtnW, wBtnH);
    }
  }
  if (im1 && im1.complete) {
    const useBrightLvl = wCan || wMax;
    const lvX = wBtnX + Math.round(4 * UNIT_RENDER_SCALE);  // 13
    const lvY = Math.round(510 * UNIT_RENDER_SCALE);         // 574
    const lvW = Math.round(68 * UNIT_RENDER_SCALE);          // 77
    const lvH = Math.round(27 * UNIT_RENDER_SCALE);          // 30
    const dgtX = wBtnX + Math.round(73 * UNIT_RENDER_SCALE); // 90
    const dgtW = Math.round(18 * UNIT_RENDER_SCALE);         // 20
    const lv = Math.min(8, Math.max(1, myHud.worker_lv));
    if (useBrightLvl) {
      drawSubImage(ctx, im1, 1, 89, 68, 27, lvX, lvY, lvW, lvH);
      drawSubImage(ctx, im1, 75 + lv * 20, 89, 18, 27, dgtX, lvY, dgtW, lvH);
    } else {
      drawSubImage(ctx, im1, 21, 118, 68, 27, lvX, lvY, lvW, lvH);
      if (lv === 8) {
        drawSubImage(ctx, im1, 1, 147, 18, 27, dgtX, lvY, dgtW, lvH);
      } else {
        drawSubImage(ctx, im1, 95 + lv * 20, 118, 18, 27, dgtX, lvY, dgtW, lvH);
      }
    }
    const costY = wBtnY + Math.round(92 * UNIT_RENDER_SCALE); // 688
    if (wMax) {
      drawSubImage(ctx, im1, 449, 45, 62, 26, lvX, costY, Math.round(62 * UNIT_RENDER_SCALE), Math.round(26 * UNIT_RENDER_SCALE));
    } else {
      drawWorkerCostLeft(ctx, myHud.worker_cost, lvX, costY, wCan);
    }
  }

  // 2. Center: Authentic PONOS 2-Row ("2段表示") 10-Card Formation Grid (5 columns x 2 rows)
  const cards = myHud.cards || [];
  const cw = 110;
  const ch = 82;
  const startX = 329;
  const stepX = 128;

  for (let row = 0; row < 2; row++) {
    const cy = row === 0 ? 528 : 620;
    for (let col = 0; col < 5; col++) {
      const slotIdx = row * 5 + col;
      const card = cards[slotIdx];
      const cx = startX + col * stepX;

      if (card) {
        const canBuy = myHud.money >= card.cost && card.cd <= 0;

        // IMPORTANT: Draw img002_tw cut 49 (134, 410, 126, 101) BEFORE the unit portrait
        // because cut 49 has a solid white center with a golden outer glow!
        if (canBuy && im2 && im2.complete) {
          drawSubImage(ctx, im2, 134, 410, 126, 101, cx - 7, cy - 7, cw + 14, ch + 14);
        }

        let drawnPortrait = card.portrait
          ? drawPortrait(ctx, card.portrait, cx + cw / 2, cy + ch, cw, ch)
          : false;
        if (!card.portrait && card.icon && !card.custom) {
          const iconImg = loadImage(`icon_${card.icon}`, `/assets/${card.icon}.png`);
          if (iconImg && iconImg.complete && iconImg.naturalWidth > 0) {
            drawSubImage(ctx, iconImg, 9, 21, 110, 85, cx, cy, cw, ch);
            drawnPortrait = true;
          }
        }
        if (!drawnPortrait) {
          const baseIdx = ["cat", "tank_cat", "axe_cat", "gross_cat", "cow_cat", "bird_cat", "fish_cat", "lizard_cat", "titan_cat"].indexOf(card.key);
          if (baseIdx >= 0 && IMAGES[`uni_${baseIdx}`] && IMAGES[`uni_${baseIdx}`].complete) {
            // Crop exact (9, 21, 110, 85) from 128x128 uni00*_f00.png to remove transparent padding
            drawSubImage(ctx, IMAGES[`uni_${baseIdx}`], 9, 21, 110, 85, cx, cy, cw, ch);
            drawnPortrait = true;
          } else if (card.rig && card.rig.includes("_")) {
            const numPart = parseInt(card.rig.split("_")[0], 10);
            const isCatRig = !card.rig.includes("_e");
            if (isCatRig && !isNaN(numPart) && numPart >= 0 && numPart < 9 && IMAGES[`uni_${numPart}`] && IMAGES[`uni_${numPart}`].complete) {
              drawSubImage(ctx, IMAGES[`uni_${numPart}`], 9, 21, 110, 85, cx, cy, cw, ch);
              drawnPortrait = true;
            }
          }
        }

        if (!drawnPortrait) {
          // Authentic PONOS-styled card frame for custom units using Boss/Enemy rigs (e.g. 018_e, 015_e)
          ctx.fillStyle = "#e6dec3";
          ctx.fillRect(cx, cy, cw, ch);
          ctx.strokeStyle = "#111111";
          ctx.lineWidth = 3;
          ctx.strokeRect(cx + 1.5, cy + 1.5, cw - 3, ch - 3);
          ctx.save();
          ctx.beginPath();
          ctx.rect(cx + 3, cy + 3, cw - 6, ch - 6);
          ctx.clip();
          const rigObj = RIGS[card.rig] || RIGS["000_f"];
          const isEnemyRig = rigObj ? !!rigObj.is_enemy : (card.rig || "").includes("_e");
          renderSkeletalRig(ctx, card.rig || "000_f", "idle", 0, cx + cw * 0.45, cy + ch - 8, 0.34, isEnemyRig);
          ctx.restore();
          // Golden price tag box in bottom-right matching uni00*_f00.png
          ctx.fillStyle = "#f5b915";
          ctx.fillRect(cx + cw - 56, cy + ch - 23, 53, 20);
          ctx.strokeStyle = "#111";
          ctx.lineWidth = 1.5;
          ctx.strokeRect(cx + cw - 56, cy + ch - 23, 53, 20);
        }

        // Dim card when on cooldown or insufficient money (portrait remains clearly visible)
        if (!canBuy) {
          ctx.fillStyle = "rgba(0, 0, 0, 0.50)";
          ctx.fillRect(cx + 2, cy + 2, cw - 4, ch - 4);
        }

        if (card.custom) {
          ctx.fillStyle = "rgba(22, 16, 38, 0.86)";
          ctx.fillRect(cx + 3, cy + 3, cw - 6, 16);
          ctx.font = "700 11px Microsoft JhengHei, sans-serif";
          ctx.textAlign = "center";
          ctx.fillStyle = "#ff9ff3";
          ctx.fillText(card.name.slice(0, 8), cx + cw / 2, cy + 14);
        }

        if (card.cd > 0) {
          const ratio = Math.max(0, Math.min(1, 1 - card.cd / Math.max(1, card.max_cd)));
          ctx.fillStyle = "#141820";
          ctx.fillRect(cx + 10, cy + ch / 2 - 6, cw - 20, 12);
          ctx.fillStyle = "#48dbfb";
          ctx.fillRect(cx + 12, cy + ch / 2 - 4, Math.round((cw - 24) * ratio), 8);
        }

        drawSmallCostDigits(ctx, card.cost, cx + cw - 5, cy + ch - 21, myHud.money >= card.cost);
      } else {
        // Empty Formation Slot (authentic dark translucent slot box)
        ctx.fillStyle = "rgba(18, 16, 14, 0.55)";
        ctx.fillRect(cx, cy, cw, ch);
        ctx.strokeStyle = "rgba(10, 10, 10, 0.85)";
        ctx.lineWidth = 3;
        ctx.strokeRect(cx + 1.5, cy + 1.5, cw - 3, ch - 3);
      }
    }
  }

  // 3. Right: Official Cat Cannon Button (1096, 586, 168, 112)
  const cReady = myHud.cannon_charge >= myHud.cannon_max;
  if (im2 && im2.complete) {
    if (cReady) {
      drawSubImage(ctx, im2, blinkPhase ? 197 : 1, 124, 194, 130, 1096, 586, 168, 112);
      drawSubImage(ctx, im2, blinkPhase ? 312 : 185, 260, 125, 55, 1124, 644, 112, 48);
    } else {
      drawSubImage(ctx, im2, 393, 124, 194, 130, 1096, 586, 168, 112);
      const cRatio = Math.max(0, Math.min(1, myHud.cannon_charge / Math.max(1, myHud.cannon_max)));
      if (cRatio > 0.01) {
        const srcH = Math.max(1, Math.round(130 * cRatio));
        const dstH = Math.max(1, Math.round(112 * cRatio));
        drawSubImage(ctx, im2, 1, 124 + (130 - srcH), 194, srcH, 1096, 586 + (112 - dstH), 168, dstH);
      }
    }
  }
}

function renderTopHud(ctx, st) {
  if (!st || !st.teams) return;
  const myHud = st.teams[String(myTeam)];
  const oppTeam = myTeam === TEAM_CAT ? TEAM_ENEMY : TEAM_CAT;
  const oppHud = st.teams[String(oppTeam)];

  // 1. Official Top-Left Pause Button (img002_tw.png 519, 256, 58, 58)
  if (IMAGES.img002 && IMAGES.img002.complete) {
    drawSubImage(ctx, IMAGES.img002, 519, 256, 58, 58, 16, 14, 58, 58);
  }

  // 2. Top-Right: Local Player's Authentic Gold Bitmap Wallet
  if (myHud) {
    drawBigWalletDigits(ctx, myHud.money, myHud.max_money, SCREEN_W - 18, 16, true);
  }

  // 3. Compact PvP Room & Opponent Status Pill next to Top-Left Pause Button
  const pillTxt = `【房號 ${st.room_code}】${myTeam === TEAM_CAT ? "P1 右側主堡" : "P2 左側主堡"} ｜ 對手錢包 Lv.${oppHud ? oppHud.worker_lv : 1} (${oppHud ? oppHud.money : 0}元)`;
  ctx.font = "700 13px Microsoft JhengHei, sans-serif";
  const tw = ctx.measureText(pillTxt).width;
  ctx.fillStyle = "rgba(18, 22, 32, 0.78)";
  ctx.fillRect(84, 22, tw + 20, 26);
  ctx.strokeStyle = "rgba(255, 204, 51, 0.55)";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(84, 22, tw + 20, 26);
  ctx.fillStyle = "#7bed9f";
  ctx.textAlign = "left";
  ctx.fillText(pillTxt, 94, 39);

  // Waiting / Victory Banner Overlay on Canvas
  if (st.status === "WAITING") {
    ctx.fillStyle = "rgba(10, 14, 24, 0.72)";
    ctx.fillRect(SCREEN_W / 2 - 290, 160, 580, 130);
    ctx.strokeStyle = "#ffcc33";
    ctx.lineWidth = 3;
    ctx.strokeRect(SCREEN_W / 2 - 290, 160, 580, 130);
    ctx.textAlign = "center";
    ctx.font = "900 28px Microsoft JhengHei, sans-serif";
    ctx.fillStyle = "#ffcc33";
    ctx.fillText(`房間已建立！房號：${st.room_code}`, SCREEN_W / 2, 210);
    ctx.font = "700 16px Microsoft JhengHei, sans-serif";
    ctx.fillStyle = "#fff";
    ctx.fillText("請讓朋友打開此網頁並輸入上方 4 位數房號即可立即開戰！", SCREEN_W / 2, 255);
  } else if (st.game_over) {
    ctx.fillStyle = "rgba(10, 14, 24, 0.78)";
    ctx.fillRect(SCREEN_W / 2 - 240, 170, 480, 120);
    ctx.strokeStyle = "#ffcc33";
    ctx.lineWidth = 3;
    ctx.strokeRect(SCREEN_W / 2 - 240, 170, 480, 120);
    ctx.textAlign = "center";
    ctx.font = "900 36px Microsoft JhengHei, sans-serif";
    ctx.fillStyle = st.winner === myTeam ? "#2ecc71" : "#ff4757";
    ctx.fillText(st.winner === myTeam ? "勝利 VICTORY!" : "敗北 DEFEAT!", SCREEN_W / 2, 228);
    ctx.font = "700 15px Microsoft JhengHei, sans-serif";
    ctx.fillStyle = "#fff";
    ctx.fillText("點擊上方「重新開局 (Rematch)」可立即再戰一場", SCREEN_W / 2, 266);
  }
}

// ============================================================================
// 5. STUDIO PREVIEW & INPUT EVENT HANDLERS
// ============================================================================

function renderStudioPreview() {
  const pCanvas = document.getElementById("previewCanvas");
  if (!pCanvas) return;
  const ctx = pCanvas.getContext("2d");
  ctx.clearRect(0, 0, pCanvas.width, pCanvas.height);

  const rigCode = document.getElementById("cuRig").value || "004_f";
  const scale = parseFloat(document.getElementById("cuScale").value) || 0.75;
  const animName = document.getElementById("previewAnimSelect").value || "walk";

  renderSkeletalRig(ctx, rigCode, animName, previewFrame, 240, 235, scale, false);
}

function setupBattleControls() {
  const canvas = document.getElementById("battleCanvas");
  if (!canvas) return;

  canvas.addEventListener("mousedown", e => {
    const rect = canvas.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * SCREEN_W;
    const my = ((e.clientY - rect.top) / rect.height) * SCREEN_H;

    if (my >= 522) {
      // Deploy Bar Clicks (Worker Cat, 10-Card 2-Row Formation Grid, Cat Cannon)
      if (mx >= 8 && mx <= 226 && my >= 584 && my <= 720) {
        sendRoomAction("UPGRADE_WORKER");
      } else if (mx >= 1096 && mx <= 1264 && my >= 586 && my <= 698) {
        sendRoomAction("FIRE_CANNON");
      } else {
        const cw = 110;
        const ch = 82;
        const startX = 329;
        const stepX = 128;
        for (let row = 0; row < 2; row++) {
          const cy = row === 0 ? 528 : 620;
          for (let col = 0; col < 5; col++) {
            const cx = startX + col * stepX;
            if (mx >= cx && mx <= cx + cw && my >= cy && my <= cy + ch) {
              sendRoomAction("SPAWN_UNIT", row * 5 + col);
              return;
            }
          }
        }
      }
    } else {
      isDraggingCam = true;
      dragStartX = mx;
      dragStartCamX = cameraX;
      manualCameraHold = 120;
    }
  });

  window.addEventListener("mousemove", e => {
    if (!isDraggingCam) return;
    const rect = canvas.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * SCREEN_W;
    cameraX = dragStartCamX - (mx - dragStartX) * 1.4;
    manualCameraHold = 120;
  });

  window.addEventListener("mouseup", () => {
    isDraggingCam = false;
  });

  canvas.addEventListener("wheel", e => {
    if (!roomState || e.deltaY === 0) return;
    e.preventDefault();
    const stageLen = roomState.length || 3200;
    const { minZoom, maxZoom } = getBattleZoomBounds(stageLen);
    const oldWorldScale = BATTLE_WORLD_SCALE * battleZoom;
    const centerWorldX = (cameraX + SCREEN_W * 0.5) / oldWorldScale;
    const step = e.deltaY > 0 ? -0.025 : 0.025;
    battleZoom = Math.max(minZoom, Math.min(maxZoom, battleZoom + step));
    const newWorldScale = BATTLE_WORLD_SCALE * battleZoom;
    cameraX = centerWorldX * newWorldScale - SCREEN_W * 0.5;
    manualCameraHold = 90;
  }, { passive: false });

  window.addEventListener("keydown", e => {
    if (document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      return;
    }
    const row1Keys = ["1", "2", "3", "4", "5"];
    const row2NumKeys = ["6", "7", "8", "9", "0"];
    const row2LetKeys = ["q", "w", "e", "r", "t"];
    const kLower = e.key.toLowerCase();
    if (row1Keys.includes(e.key)) {
      sendRoomAction("SPAWN_UNIT", row1Keys.indexOf(e.key));
    } else if (row2NumKeys.includes(e.key)) {
      sendRoomAction("SPAWN_UNIT", 5 + row2NumKeys.indexOf(e.key));
    } else if (row2LetKeys.includes(kLower)) {
      sendRoomAction("SPAWN_UNIT", 5 + row2LetKeys.indexOf(kLower));
    } else if (kLower === "z") {
      sendRoomAction("UPGRADE_WORKER");
    } else if (e.key === " ") {
      e.preventDefault();
      sendRoomAction("FIRE_CANNON");
    } else if (e.key === "ArrowLeft" || kLower === "a") {
      cameraX -= 45;
      manualCameraHold = 90;
    } else if (e.key === "ArrowRight" || kLower === "d") {
      cameraX += 45;
      manualCameraHold = 90;
    }
  });
}

let lastTickTime = performance.now();
let lastRenderTime = performance.now();
const FRAME_MS_30FPS = 1000.0 / 30.0;

function renderLoop(now = performance.now()) {
  const dt = Math.min(0.1, Math.max(0.0, (now - lastRenderTime) / 1000.0));
  lastRenderTime = now;

  // Advance 60/120 FPS smooth unit positions & continuous animation clocks
  const speedMult = (roomState && roomState.speed_mult) || 1.0;
  const df = dt * 30.0 * speedMult;
  previewFrame += dt * 30.0;

  const isOneshot = new URLSearchParams(window.location.search).get("oneshot") === "1";
  if (!isOneshot && roomState && roomState.status === "PLAYING" && !roomState.game_over && Array.isArray(roomState.units)) {
    roomState.units.forEach(u => {
      const vis = unitVisualState.get(u.id);
      if (!vis) return;
      if (!(u.frz > 0 && u.anim !== "kb")) {
        const serverAf = vis.serverAf !== undefined ? vis.serverAf : u.af;
        let maxSubAf = serverAf + 0.95;
        if (u.anim === "attack") {
          const rigObj = RIGS[u.rig];
          const maxAtkF = (rigObj && rigObj.anims && rigObj.anims.attack && rigObj.anims.attack.max_frame) || 30;
          maxSubAf = Math.min(maxAtkF, maxSubAf);
        }
        vis.localAf = Math.min(maxSubAf, Math.max(serverAf, vis.localAf + df));
      }
      if (u.anim === "kb") {
        // Zero lag during knockback: follow server's exact horizontal & vertical bounce immediately
        vis.renderX = vis.targetX;
        vis.renderY = vis.targetY;
      } else {
        const errX = vis.targetX - vis.renderX;
        const followFactor = 1.0 - Math.exp(-35.0 * dt);
        vis.renderX += errX * followFactor;
        if (Math.abs(errX) > 120.0) {
          vis.renderX = vis.targetX;
        }
        vis.renderY = vis.targetY;
      }
    });
  }

  const elapsed = now - lastTickTime;
  if (elapsed >= FRAME_MS_30FPS) {
    const ticks = Math.min(4, Math.floor(elapsed / FRAME_MS_30FPS));
    lastTickTime += ticks * FRAME_MS_30FPS;
    for (let t = 0; t < ticks; t++) {
      clientTick++;
      advanceClientFX30Fps();
    }
  }

  const formSec = document.getElementById("view-formation");
  if (formSec && formSec.classList.contains("active")) {
    renderFormationPreview();
  }
  const battleSec = document.getElementById("view-battle");
  if (battleSec && battleSec.classList.contains("active")) {
    renderBattleScene();
  }
  const studioSec = document.getElementById("view-studio");
  if (studioSec && studioSec.classList.contains("active")) {
    renderStudioPreview();
  }

  requestAnimationFrame(renderLoop);
}

window.addEventListener("DOMContentLoaded", initApp);
