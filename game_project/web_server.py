"""
Web Edition & Room-Code Multiplayer Server (`web_server.py`).
Turns The Battle Cats PvP + Custom Character Studio into a browser-accessible Web App
with 4-digit Room Code (`房號`) matchmaking:
- Open `http://<host>:8080` in any browser (PC, Mac, or mobile).
- Player 1 clicks "建立房間" (or types a custom 4-digit code like `1234`) -> gets a 4-digit Room Code (`房號`).
- Player 2 opens the website, enters the 4-digit Room Code (`房號`), and clicks "加入房間" -> instant PvP match!
- Also includes an in-browser Custom Character Creator ("自製角色工坊") that saves to `custom_units/*.json`
  and automatically syncs custom characters between players in the room.
- Renders 100% authentic PONOS `.imgcut` + `.mamodel` + `.maanim` 2D skeletal animations and official UI
  atlases directly on HTML5 `<canvas>`.
"""

import os
import sys
import json
import gzip
import time
import random
import socket
import secrets
import urllib.parse
import threading
import subprocess
import re
import ctypes
import shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Any, Optional, Union

try:
    if os.name == "nt":
        ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

from .core.constants import TEAM_CAT, TEAM_ENEMY, SLOT_CASTLE, KNOCKBACK_FRAMES_DEFAULT, UNIT_RENDER_SCALE
from .core.unit_data import CAT_REGISTRY, ENEMY_REGISTRY, OFFICIAL_CAT_KEYS, FINAL_CAT_KEYS
from .core.official_data_parser import compute_ponos_level_stat
from .core.custom_unit_loader import CustomUnitLoader
from .simulation.battle_sim import BattleSimulation, SimUnit
from .simulation.nyancombo import NyancomboManager
from .anim_engine.ponos_anim_player import PonosImgcut, PonosMamodel, PonosMaanim
from .network.pvp_net import get_local_ip


ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "official_extracted")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ENEMY_186_DIR = ASSET_DIR if os.path.isfile(os.path.join(ASSET_DIR, "186_e.png")) else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "phase23_26", "enemy_186")

BUILTIN_PLAYER_NAME = "lingogoogol"
BUILTIN_UNIT_KEY = "builtin_lingogoogol"
BUILTIN_UNIT = {
    "key": BUILTIN_UNIT_KEY,
    "id": BUILTIN_UNIT_KEY,
    "name": "宇宙無敵大帥哥",
    "portrait": "/web/builtin_lingogoogol.png",
    "hp": 9_999_999_999,
    "attack_power": 9_999,
    "cost": 1,
    "attack_range": 1,
    "speed": 2,
    "attack_interval": 3,
    "attack_windup": 1,
    "attack_anim_len": 3,
    "cooldown_frames": 1,
    "knockback_count": 1,
    "target_type": "area",
    "rarity": "builtin",
    "rarity_label": "內建",
    "is_custom": True,
    "fixed_stats": True,
    "max_level": 1,
}
DEFAULT_PVP_DECK = (
    "cat_000_s", "cat_001_s", "cat_018_s", "cat_037_s", "cat_041_s",
    "cat_032_s", "cat_031_s", "cat_024_s", "cat_025_s", "cat_455_s",
)


def filter_room_deck(deck: Optional[List[Union[str, int]]], player_name: str) -> List[Union[str, int]]:
    if not isinstance(deck, list):
        return []
    return [
        key for key in deck[:10]
        if isinstance(key, (str, int)) and key != ""
        and (key != BUILTIN_UNIT_KEY or player_name == BUILTIN_PLAYER_NAME)
    ]


def resolve_room_deck(deck: List[Union[str, int]]) -> List[Any]:
    return [BUILTIN_UNIT if key == BUILTIN_UNIT_KEY else key for key in deck]


def serialize_rig_to_dict(base_dir: str, code: str, url_prefix: str = "/assets/") -> Optional[Dict[str, Any]]:
    """Serializes a PONOS `.imgcut` + `.mamodel` + `.maanim` skeletal rig into compact JSON for HTML5 Canvas."""
    png_p = os.path.join(base_dir, f"{code}.png")
    cut_p = os.path.join(base_dir, f"{code}_tw.imgcut")
    if not os.path.isfile(cut_p):
        cut_p = os.path.join(base_dir, f"{code}.imgcut")
    model_p = os.path.join(base_dir, f"{code}.mamodel")
    if not (os.path.isfile(png_p) and os.path.isfile(cut_p) and os.path.isfile(model_p)):
        return None

    try:
        imgcut = PonosImgcut(cut_p)
        mamodel = PonosMamodel(model_p)
        cuts_list = []
        max_cut_id = max(imgcut.cuts.keys()) if imgcut.cuts else -1
        for cid in range(max_cut_id + 1):
            c = imgcut.cuts.get(cid)
            if c:
                cuts_list.append([c.x, c.y, c.w, c.h])
            else:
                cuts_list.append([0, 0, 0, 0])

        parts_list = []
        for p in mamodel.parts:
            parts_list.append([
                p.index, p.parent_id, p.unit_id, p.cut_id, p.z_depth,
                p.x, p.y, p.pivot_x, p.pivot_y, p.scale_x, p.scale_y,
                p.rotation, p.alpha, p.glow
            ])

        anims_dict = {}
        for anim_name, suffix in [("walk", "00"), ("idle", "01"), ("attack", "02"), ("kb", "03")]:
            ap = os.path.join(base_dir, f"{code}{suffix}.maanim")
            if os.path.isfile(ap):
                ma = PonosMaanim(ap)
                tracks = []
                for tr in ma.tracks:
                    kfs = [[kf.frame, kf.value, kf.ease_mode, kf.ease_power] for kf in tr.keyframes]
                    if kfs:
                        tracks.append({
                            "part": tr.part_id,
                            "mod": tr.mod_type,
                            "loop": tr.loop,
                            "kfs": kfs,
                        })
                anims_dict[anim_name] = {
                    "max_frame": ma.max_frame,
                    "tracks": tracks,
                }

        return {
            "code": code,
            "png": f"{url_prefix}{code}.png",
            "is_enemy": ("_e" in code.lower()),
            "cuts": cuts_list,
            "model": {
                "scale_unit": mamodel.scale_unit,
                "angle_unit": mamodel.angle_unit,
                "alpha_unit": mamodel.alpha_unit,
                "ints": mamodel.ints,
                "parts": parts_list,
            },
            "anims": anims_dict,
        }
    except Exception as e:
        print(f"[!] Failed to serialize rig {code}: {e}")
        return None


def serialize_skill_rig_to_dict(
    base_dir: str,
    code: str,
    model_file: str,
    cut_file: str,
    png_file: str,
    anim_files: Dict[str, str],
    url_prefix: str = "/assets/",
    strip_ints: bool = True,
) -> Optional[Dict[str, Any]]:
    """Serializes an official PONOS skill effect (.imgcut + .mamodel + .maanim) into JSON."""
    imgcut_p = os.path.join(base_dir, cut_file)
    mamodel_p = os.path.join(base_dir, model_file)
    png_p = os.path.join(base_dir, png_file)
    if not (os.path.isfile(imgcut_p) and os.path.isfile(mamodel_p) and os.path.isfile(png_p)):
        return None
    try:
        imgcut = PonosImgcut(imgcut_p)
        mamodel = PonosMamodel(mamodel_p)
        max_cid = max(imgcut.cuts.keys()) if imgcut.cuts else -1
        cuts_list = []
        for cid in range(max_cid + 1):
            c = imgcut.cuts.get(cid)
            cuts_list.append([c.x, c.y, c.w, c.h] if c else [0, 0, 0, 0])
        parts_list = [
            [
                p.index, p.parent_id, p.unit_id, p.cut_id, p.z_depth,
                p.x, p.y, p.pivot_x, p.pivot_y, p.scale_x, p.scale_y,
                p.rotation, p.alpha, p.glow,
            ]
            for p in mamodel.parts
        ]
        anims_dict = {}
        for anim_name, anim_fn in anim_files.items():
            mp = os.path.join(base_dir, anim_fn)
            if os.path.isfile(mp):
                ma = PonosMaanim(mp)
                tracks = []
                for tr in ma.tracks:
                    kfs = [[kf.frame, kf.value, kf.ease_mode, kf.ease_power] for kf in tr.keyframes]
                    if kfs:
                        tracks.append({"part": tr.part_id, "mod": tr.mod_type, "loop": tr.loop, "kfs": kfs})
                anims_dict[anim_name] = {"max_frame": ma.max_frame, "tracks": tracks}
        return {
            "code": code,
            "png": f"{url_prefix}{png_file}",
            "is_enemy": code.endswith("_e"),
            "cuts": cuts_list,
            "model": {
                "scale_unit": mamodel.scale_unit,
                "angle_unit": mamodel.angle_unit,
                "alpha_unit": mamodel.alpha_unit,
                "ints": [] if strip_ints else mamodel.ints,
                "parts": parts_list,
            },
            "anims": anims_dict,
        }
    except Exception as e:
        print(f"[!] Failed to serialize skill rig {code}: {e}")
        return None


class BattleRoom:
    """Represents an active Room identified by a Room Code (`房號`, e.g. '8492')."""

    def __init__(self, room_code: str, host_name: str = "P1 房主", vs_ai: bool = False, host_deck: Optional[List[str]] = None, cat_level: int = 30, host_talent_overrides: Optional[Dict[str, List[int]]] = None):
        self.room_code = room_code
        self.host_name = host_name
        self.guest_name = "AI 電腦對手" if vs_ai else ""
        self.host_token = secrets.token_urlsafe(24)
        self.guest_token = ""
        self.vs_ai = vs_ai
        self.cat_level = max(1, min(50, int(cat_level or 30)))
        self.created_at = time.time()
        self.last_active = time.time()
        self.lock = threading.Lock()
        self.frame_cond = threading.Condition(self.lock)

        self.p1_deck = filter_room_deck(host_deck, self.host_name) or list(DEFAULT_PVP_DECK)
        self.p2_deck = self._build_diverse_ai_deck() if vs_ai else list(DEFAULT_PVP_DECK)
        self.p1_talent_overrides: Dict[str, List[int]] = dict(host_talent_overrides or {})
        self.p2_talent_overrides: Dict[str, List[int]] = {}
        self.status = "PLAYING" if vs_ai else "WAITING"  # WAITING -> PLAYING -> FINISHED
        self.speed_mult: float = 1.0  # 1.0 = authentic 30 FPS speed (supports 0.5, 0.75, 1.0, 1.5, 2.0)
        self._frame_accum: float = 0.0
        self.sim: Optional[BattleSimulation] = None
        self.event_log_id = 0
        self.recent_events: List[Dict[str, Any]] = []  # each item: {"id": int, "frame": int, "ev": dict}

        if vs_ai:
            self._init_simulation()

    @staticmethod
    def _build_diverse_ai_deck() -> List[str]:
        """Builds a balanced, varied 10-cat final-form AI deck from all 779 official cats."""
        meatshields = [k for k in FINAL_CAT_KEYS if CAT_REGISTRY[k].get("cost", 999) <= 200]
        mids = [k for k in FINAL_CAT_KEYS if 200 < CAT_REGISTRY[k].get("cost", 0) <= 1000 and CAT_REGISTRY[k].get("rarity") in ("normal", "ex", "rare", "super_rare")]
        ubers = [k for k in FINAL_CAT_KEYS if CAT_REGISTRY[k].get("rarity") == "uber_rare"]
        legends = [k for k in FINAL_CAT_KEYS if CAT_REGISTRY[k].get("rarity") == "legend_rare"]
        deck: List[str] = ["cat_000_s", "cat_001_s"]
        for pool, cnt in ((meatshields, 1), (mids, 3), (ubers, 3), (legends, 1)):
            cands = [k for k in pool if k not in deck]
            random.shuffle(cands)
            deck.extend(cands[:cnt])
        return deck[:10]

    def _init_simulation(self):
        stg = {
            "name": f"房號 {self.room_code} 聯機對戰",
            "length": 5200,
            "waves": [],
        }
        max_base_levels = {
            "castle_hp": 30,
            "cannon_power": 30,
            "cannon_range": 10,
            "cannon_charge": 30,
            "worker_cat": 30,
            "wallet": 30,
            "research": 30,
            "accounting": 30,
        }
        treasures_cfg = {
            "eoc_chapters": 3.0,
            "itf_chapters": 3.0,
            "trait_fruits": {"red": True, "floating": True, "black": True, "angel": True, "alien": True, "zombie": True}
        }
        self.sim = BattleSimulation(
            stage_config=stg,
            deck=resolve_room_deck(self.p1_deck),
            pvp_mode=True,
            p2_deck=resolve_room_deck(self.p2_deck),
            treasures=treasures_cfg,
            p2_treasures=treasures_cfg,
            base_levels=max_base_levels,
            p2_base_levels=dict(max_base_levels),
            rng_seed=int(time.time() * 1000) & 0x7FFFFFFF,
        )
        # Fixed authentic per-unit MAX level from unitbuy.csv (base_max + plus_max) + official Talents
        self.sim.deployer.cat_levels["__max__"] = True
        self.sim.p2_deployer.cat_levels["__max__"] = True
        self.sim.deployer.talent_overrides = dict(self.p1_talent_overrides)
        self.sim.p2_deployer.talent_overrides = dict(self.p2_talent_overrides)
        # Standard authentic starting wallet (100 money)
        self.sim.wallet.money = 100
        self.sim.p2_wallet.money = 100
        self._frame_accum = 0.0
        self.status = "PLAYING"

    def join_guest(self, guest_name: str = "P2 挑戰者", guest_deck: Optional[List[str]] = None, guest_talent_overrides: Optional[Dict[str, List[int]]] = None) -> str:
        with self.lock:
            self.guest_name = guest_name or "P2 挑戰者"
            self.guest_token = secrets.token_urlsafe(24)
            self.p2_deck = filter_room_deck(guest_deck or self.p2_deck, self.guest_name) or list(DEFAULT_PVP_DECK)
            if isinstance(guest_talent_overrides, dict):
                self.p2_talent_overrides = dict(guest_talent_overrides)
            self.vs_ai = False
            self._init_simulation()
            self.last_active = time.time()
            self.frame_cond.notify_all()
            return self.guest_token

    def apply_action(self, team: int, action: str, payload: Any = None, player_token: str = "") -> bool:
        with self.lock:
            self.last_active = time.time()
            team_id = TEAM_CAT if int(team) == TEAM_CAT else TEAM_ENEMY
            player_name, token, deck = (
                (self.host_name, self.host_token, self.p1_deck)
                if team_id == TEAM_CAT else (self.guest_name, self.guest_token, self.p2_deck)
            )
            can_use_builtin = player_name == BUILTIN_PLAYER_NAME and bool(token) and player_token == token
            if action == "REMATCH":
                if self.vs_ai:
                    self.p2_deck = self._build_diverse_ai_deck()
                self._init_simulation()
                self.frame_cond.notify_all()
                return True
            if action == "UPDATE_DECK":
                if isinstance(payload, list) and len(payload) > 0:
                    if not can_use_builtin and (BUILTIN_UNIT_KEY in payload or BUILTIN_UNIT_KEY in deck):
                        return False
                    clean_deck = filter_room_deck(payload, player_name)
                    if not clean_deck:
                        return False
                    if team_id == TEAM_CAT:
                        self.p1_deck = clean_deck
                    else:
                        self.p2_deck = clean_deck
                    if self.sim is not None and team_id in self.sim.deployers:
                        self.sim.deployers[team_id].set_deck(resolve_room_deck(clean_deck))
                    self.frame_cond.notify_all()
                    return True
                return False
            if action == "UPDATE_TALENTS":
                if isinstance(payload, dict):
                    clean_ov = {str(k): [int(i) for i in v] for k, v in payload.items() if isinstance(v, list)}
                    if team_id == TEAM_CAT:
                        self.p1_talent_overrides = clean_ov
                    else:
                        self.p2_talent_overrides = clean_ov
                    if self.sim is not None and team_id in self.sim.deployers:
                        self.sim.deployers[team_id].talent_overrides = dict(clean_ov)
                    self.frame_cond.notify_all()
                    return True
                return False
            if action == "SET_SPEED":
                try:
                    val = float(payload)
                    if val in (0.5, 0.75, 1.0, 1.5, 2.0):
                        self.speed_mult = val
                        self.frame_cond.notify_all()
                        return True
                except Exception:
                    pass
                return False
            if action == "SET_LEVEL":
                # Web PvP locks all official units to their authentic per-unit MAX level
                if self.sim is not None:
                    self.sim.deployer.cat_levels["__max__"] = True
                    self.sim.p2_deployer.cat_levels["__max__"] = True
                self.frame_cond.notify_all()
                return True
            if self.sim is None or self.sim.game_over:
                return False
            res = False
            if action == "SPAWN_UNIT":
                slot_idx = int(payload)
                if 0 <= slot_idx < len(deck) and deck[slot_idx] == BUILTIN_UNIT_KEY and not can_use_builtin:
                    return False
                res = self.sim.deploy_unit(team_id, slot_idx)
            elif action == "UPGRADE_WORKER":
                res = self.sim.wallets[team_id].upgrade_worker()
            elif action == "FIRE_CANNON":
                res = self.sim.fire_cannon(team_id)
            elif action == "SPAWN_CUSTOM":
                if isinstance(payload, str) and payload != BUILTIN_UNIT_KEY:
                    res = self.sim.spawn_custom_unit(payload, team=team_id) is not None
            if res:
                self.frame_cond.notify_all()
            return res

    def _update_ai(self):
        """Tactical Battle Cats PvP AI with frontline distance and threat assessment."""
        w2 = self.sim.p2_wallet
        d2 = self.sim.deployers[TEAM_ENEMY]
        my_castle_x = self.sim.get_castle(TEAM_ENEMY).pos_x  # 120

        # 1. Frontline threat assessment
        opp_units = [u for u in self.sim.units[TEAM_CAT] if u is not None and u.is_alive and not getattr(u, 'is_burrowing', False)]
        closest_opp_x = min([u.pos_x for u in opp_units]) if opp_units else self.sim.battlefield_length
        threat_dist = closest_opp_x - my_castle_x  # Distance of closest enemy from our castle (120)

        # 2. Strategic Cat Cannon: fire when charged and enemies are within range
        if self.sim.p2_cannon_charge >= self.sim.cannon_max_charge:
            c_range = self.sim.cannon_ranges.get(TEAM_ENEMY, self.sim.cannon_range)
            enemies_in_cannon_range = [u for u in opp_units if u.pos_x <= (my_castle_x + c_range)]
            if enemies_in_cannon_range:
                # Fire if enemies threaten castle (< 700 DU), or cluster >= 2, or every 120 frames
                if threat_dist < 700 or len(enemies_in_cannon_range) >= 2 or self.sim.current_frame % 120 == 0:
                    self.sim.fire_cannon(TEAM_ENEMY)

        # 3. Worker Cat Economy investment: upgrade if frontline is safe or economy is strong
        if w2.level < 8 and w2.upgrade_cost is not None and w2.money >= w2.upgrade_cost:
            if threat_dist > 1000 or w2.money > (w2.upgrade_cost + 400):
                if self.sim.current_frame % 30 == 0:
                    w2.upgrade_worker()

        # 4. Tactical Deployment: Meatshields vs Heavy Attackers
        # Evaluates every 10 frames (~0.33s reaction window)
        if self.sim.current_frame % 10 == 0:
            affordable = [i for i in range(len(d2.deck_keys)) if d2.can_deploy(i, w2.money, has_free_slot=True)]
            if affordable:
                slot_costs = {i: (d2.get_slot_info(i) or {}).get("cost", 100) for i in affordable}
                meatshields = [i for i in affordable if slot_costs[i] <= 150]
                attackers = [i for i in affordable if slot_costs[i] > 150]

                if threat_dist < 800 and meatshields:
                    # Under pressure: prioritize cheap meatshield frontline
                    self.sim.deploy_unit(TEAM_ENEMY, random.choice(meatshields))
                elif attackers and (w2.money > 800 or threat_dist >= 800):
                    # Safe / well-funded: deploy highest-cost heavy attacker
                    attackers.sort(key=lambda i: slot_costs[i], reverse=True)
                    self.sim.deploy_unit(TEAM_ENEMY, attackers[0])
                elif meatshields:
                    self.sim.deploy_unit(TEAM_ENEMY, random.choice(meatshields))
                else:
                    self.sim.deploy_unit(TEAM_ENEMY, random.choice(affordable))

    def step_frame(self):
        with self.lock:
            if self.status != "PLAYING" or self.sim is None or self.sim.game_over:
                return
            self._frame_accum += self.speed_mult
            steps_to_run = int(self._frame_accum)
            if steps_to_run <= 0:
                return
            self._frame_accum -= steps_to_run

            for _ in range(steps_to_run):
                if self.sim.game_over:
                    break
                # If vs_ai, let AI control Team 1 (Left Castle) with frontline threat assessment
                if self.vs_ai:
                    self._update_ai()

                self.sim.step()
                evs = self.sim.consume_visual_events()
                for ev in evs:
                    self.event_log_id += 1
                    self.recent_events.append({
                        "id": self.event_log_id,
                        "frame": self.sim.current_frame,
                        "ev": ev,
                    })
            if len(self.recent_events) > 120:
                self.recent_events = self.recent_events[-120:]
            if self.sim.game_over:
                self.status = "FINISHED"
            self.frame_cond.notify_all()

    def serialize_state(self, since_event_id: int = 0) -> Dict[str, Any]:
        with self.lock:
            if self.sim is None:
                return {
                    "room_code": self.room_code,
                    "status": self.status,
                    "host_name": self.host_name,
                    "guest_name": self.guest_name,
                    "vs_ai": self.vs_ai,
                    "frame": 0,
                }

            sim = self.sim
            units_out = []
            for team_id in (TEAM_CAT, TEAM_ENEMY):
                for slot_idx in range(1, len(sim.units[team_id])):
                    u = sim.units[team_id][slot_idx]
                    if u is not None and (u.is_alive or getattr(u, "is_dying", False) or getattr(u, "is_corpse", False)):
                        # Determine rig code & anim state
                        rig_code = u.texture_code or u.spec.get("texture_code", "")
                        if not rig_code:
                            if team_id == TEAM_CAT:
                                cid = u.spec.get("id", 0)
                                rig_code = f"{int(cid):03d}_f" if isinstance(cid, int) else "000_f"
                            else:
                                eid = u.spec.get("enemy_code", 0)
                                rig_code = f"{int(eid):03d}_e" if isinstance(eid, int) else "000_e"

                        if getattr(u, "is_corpse", False):
                            anim_state = "corpse"
                        elif getattr(u, "is_burrowing", False):
                            anim_state = "burrow"
                        elif getattr(u, "is_warped", False):
                            anim_state = "warp"
                        elif u.is_in_knockback():
                            anim_state = "kb"
                        elif u.is_in_attack_anim():
                            anim_state = "attack"
                        elif not u.is_moving:
                            anim_state = "idle"
                        else:
                            anim_state = "walk"

                        units_out.append({
                            "id": f"{team_id}_{slot_idx}",
                            "team": team_id,
                            "slot": slot_idx,
                            "key": u.key,
                            "name": u.name,
                            "rig": rig_code,
                            "x": round(u.pos_x, 1),
                            "y": round(float(getattr(u, "kb_y", 0.0)), 1),
                            "hp": int(u.hp),
                            "max_hp": int(u.max_hp),
                            "anim": anim_state,
                            "af": round(float(getattr(u, "anim_frame", sim.current_frame)), 1),
                            "atk_id": int(getattr(u, "attack_cycle_id", 0)),
                            "spd": float(u.speed),
                            "kb_t": int(u.knockback_timer),
                            "kb_total": int(getattr(u, "kb_total_frames", 24)),
                            "frz": int(u.freeze_timer),
                            "slw": int(u.slow_timer),
                            "scale": float(u.spec.get("render_scale", UNIT_RENDER_SCALE)),
                            "tint": u.spec.get("tint_rgb"),
                            "custom": bool(u.spec.get("is_custom", False)),
                            "is_dying": bool(getattr(u, "is_dying", False)),
                            "burrow": bool(getattr(u, "is_burrowing", False)),
                            "corpse": bool(getattr(u, "is_corpse", False)),
                            "warped": bool(getattr(u, "is_warped", False)),
                            "shield": int(getattr(u, "aku_shield", 0)),
                            "barrier": int(getattr(u, "barrier", 0)),
                            "curse": int(getattr(u, "curse_timer", 0)),
                        })
                        if u.spec.get("portrait"):
                            units_out[-1]["portrait"] = u.spec["portrait"]

            def serialize_team_hud(t_id: int) -> Dict[str, Any]:
                w = sim.wallets[t_id]
                d = sim.deployers[t_id]
                c_charge = sim.cannon_charge if t_id == TEAM_CAT else sim.p2_cannon_charge
                c_anim = sim.cannon_anim_timer.get(t_id, 0)
                cards = []
                for idx, k in enumerate(d.deck_keys):
                    raw_sp = d._lookup_spec(k) or {}
                    sp = d.get_slot_info(idx) or raw_sp
                    max_cd = max(1, int(d.max_cooldowns[idx] if idx < len(d.max_cooldowns) else raw_sp.get("cooldown_frames", 60)))
                    cur_cd = int(d.cooldowns[idx] if idx < len(d.cooldowns) else 0)
                    cards.append({
                        "slot": idx,
                        "key": str(raw_sp["key"]) if isinstance(k, dict) else str(k),
                        "name": raw_sp.get("name", sp.get("name", str(k))),
                        "cost": int(sp.get("cost", raw_sp.get("cost", 50))),
                        "cd": cur_cd,
                        "max_cd": max_cd,
                        "rig": raw_sp.get("texture_code", sp.get("texture_code", "000_f")),
                        "icon": raw_sp.get("icon_code", sp.get("icon_code", "uni000_f00")),
                        "rarity": str(raw_sp.get("rarity", sp.get("rarity", "normal"))),
                        "rarity_label": raw_sp.get("rarity_label", sp.get("rarity_label", "基本")),
                        "scale": float(raw_sp.get("render_scale", UNIT_RENDER_SCALE)),
                        "tint": raw_sp.get("tint_rgb"),
                        "custom": bool(raw_sp.get("is_custom", False)),
                    })
                    if raw_sp.get("portrait"):
                        cards[-1]["portrait"] = raw_sp["portrait"]
                castle = sim.get_castle(t_id)
                return {
                    "money": int(w.money),
                    "max_money": int(w.max_money),
                    "worker_lv": int(w.level),
                    "worker_cost": int(w.upgrade_cost) if w.upgrade_cost is not None else None,
                    "cannon_charge": int(c_charge),
                    "cannon_max": int(sim.cannon_max_charge if t_id == TEAM_CAT else getattr(sim, "p2_cannon_max_charge", sim.cannon_max_charge)),
                    "cannon_anim": int(c_anim),
                    "castle_hp": int(castle.hp) if castle else 0,
                    "castle_max_hp": int(castle.max_hp) if castle else 1,
                    "castle_x": float(castle.pos_x) if castle else (700.0 if t_id == TEAM_ENEMY else float(sim.battlefield_length - 700.0)),
                    "cannon_range": float(sim.cannon_ranges.get(t_id, sim.cannon_range)),
                    "cards": cards,
                }

            new_events = [item for item in self.recent_events if item["id"] > since_event_id]

            return {
                "room_code": self.room_code,
                "status": self.status,
                "host_name": self.host_name,
                "guest_name": self.guest_name,
                "vs_ai": self.vs_ai,
                "cat_level": self.cat_level,
                "speed_mult": self.speed_mult,
                "frame": sim.current_frame,
                "length": sim.battlefield_length,
                "cannon_range": sim.cannon_range,
                "game_over": sim.game_over,
                "winner": sim.winner,
                "teams": {
                    "0": serialize_team_hud(TEAM_CAT),
                    "1": serialize_team_hud(TEAM_ENEMY),
                },
                "units": units_out,
                "events": new_events,
                "last_event_id": self.event_log_id,
            }


class RoomManager:
    """Manages all active rooms by Room Code (`房號`) and runs the 30 FPS simulation loop."""

    def __init__(self):
        self.rooms: Dict[str, BattleRoom] = {}
        self.lock = threading.Lock()
        self.rigs_cache: Dict[str, Any] = {}
        self._load_all_rigs()
        self.running = True
        self.thread = threading.Thread(target=self._tick_loop, daemon=True)
        self.thread.start()

    def get_or_load_rig(self, code: str) -> Optional[Dict[str, Any]]:
        if not code or ".." in code or "/" in code or "\\" in code:
            return None
        with self.lock:
            cached = self.rigs_cache.get(code)
        if cached is not None:
            return cached
        base_dir = ENEMY_186_DIR if code == "186_e" else ASSET_DIR
        r = serialize_rig_to_dict(base_dir, code, "/assets/")
        if r:
            with self.lock:
                self.rigs_cache[code] = r
        return r

    def _load_all_rigs(self):
        CustomUnitLoader.load_all()
        # Preload lightweight core + default deck rigs so startup & /api/bootstrap stay fast (<0.5s);
        # all other 800+ official final-form rigs are serialized & cached on demand via /api/rig.
        preload_codes = [
            "000_f", "000_s", "001_s", "002_s", "004_s",
            "010_s", "018_s", "024_s", "025_s",
            "031_s", "032_s", "033_s", "036_s", "037_s", "041_s", "042_s", "048_s",
            "455_s", "733_s",
        ]
        for code in preload_codes:
            self.get_or_load_rig(code)
        for eid in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 18, 21, 22, 186]:
            self.get_or_load_rig(f"{eid:03d}_e")
        # Official PONOS skeletal effect rigs (Wave, Mini-Wave, Surge, Mini-Surge, Explosion, Wave-Stop)
        skill_fx_specs = [
            ("fx_wave", "skill_wave_attack.mamodel", "skill004.imgcut", "skill004.png", {"play": "skill_wave_attack.maanim"}),
            ("fx_wave_e", "skill_wave_attack_e.mamodel", "skill005.imgcut", "skill005.png", {"play": "skill_wave_attack_e.maanim"}),
            ("fx_miniwave", "skill_smallwave_attack.mamodel", "skill012.imgcut", "skill012.png", {"play": "skill_smallwave_attack.maanim"}),
            ("fx_miniwave_e", "skill_smallwave_attack_e.mamodel", "skill013.imgcut", "skill013.png", {"play": "skill_smallwave_attack_e.maanim"}),
            ("fx_surge", "skill_volcano.mamodel", "skill009.imgcut", "skill009.png", {"start": "skill_volcano00.maanim", "loop": "skill_volcano01.maanim", "end": "skill_volcano02.maanim"}),
            ("fx_surge_e", "skill_volcano_e.mamodel", "skill010.imgcut", "skill010.png", {"start": "skill_volcano00.maanim", "loop": "skill_volcano01.maanim", "end": "skill_volcano02.maanim"}),
            ("fx_minisurge", "skill_smallvolcano.mamodel", "skill015.imgcut", "skill015.png", {"start": "skill_smallvolcano00.maanim", "loop": "skill_smallvolcano01.maanim", "end": "skill_smallvolcano02.maanim"}),
            ("fx_minisurge_e", "skill_smallvolcano_e.mamodel", "skill016.imgcut", "skill016.png", {"start": "skill_smallvolcano_e00.maanim", "loop": "skill_smallvolcano_e01.maanim", "end": "skill_smallvolcano_e02.maanim"}),
            ("fx_explosion", "skill_explosion.mamodel", "skill021.imgcut", "skill021.png", {"start": "skill_explosion00.maanim", "blast": "skill_explosion01.maanim", "end": "skill_explosion02.maanim"}),
            ("fx_explosion_e", "skill_explosion_e.mamodel", "skill022.imgcut", "skill022.png", {"start": "skill_explosion00.maanim", "blast": "skill_explosion01.maanim", "end": "skill_explosion02.maanim"}),
            ("fx_wavestop", "skill_wave_stop.mamodel", "skill000.imgcut", "skill000.png", {"play": "skill_wave_stop.maanim"}),
            ("fx_wavestop_e", "skill_wave_stop_e.mamodel", "skill000.imgcut", "skill000.png", {"play": "skill_wave_stop_e.maanim"}),
        ]
        for fx_code, m_fn, c_fn, p_fn, a_map in skill_fx_specs:
            r = serialize_skill_rig_to_dict(ASSET_DIR, fx_code, m_fn, c_fn, p_fn, a_map)
            if r:
                self.rigs_cache[fx_code] = r

    def create_room(self, custom_code: str = "", host_name: str = "P1 房主", vs_ai: bool = False, host_deck: Optional[List[str]] = None, cat_level: int = 30, host_talent_overrides: Optional[Dict[str, List[int]]] = None) -> BattleRoom:
        with self.lock:
            code = custom_code.strip()
            if not code:
                for _ in range(100):
                    cand = f"{random.randint(1000, 9999)}"
                    if cand not in self.rooms:
                        code = cand
                        break
                if not code:
                    code = "8888"
            room = BattleRoom(room_code=code, host_name=host_name, vs_ai=vs_ai, host_deck=host_deck, cat_level=cat_level, host_talent_overrides=host_talent_overrides)
            self.rooms[code] = room
            return room

    def get_room(self, room_code: str) -> Optional[BattleRoom]:
        with self.lock:
            return self.rooms.get(room_code.strip())

    def list_rooms(self) -> List[Dict[str, Any]]:
        with self.lock:
            out = []
            for code, rm in list(self.rooms.items()):
                out.append({
                    "room_code": code,
                    "host_name": rm.host_name,
                    "guest_name": rm.guest_name,
                    "status": rm.status,
                    "vs_ai": rm.vs_ai,
                })
            return out

    def _tick_loop(self):
        interval = 1.0 / 30.0
        next_tick = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            if now < next_tick:
                time.sleep(max(0.001, next_tick - now))
                continue
            next_tick += interval
            if now - next_tick > 0.15:
                next_tick = now + interval
            with self.lock:
                active_rooms = list(self.rooms.values())
            for rm in active_rooms:
                try:
                    rm.step_frame()
                except Exception as e:
                    print(f"[!] Room {rm.room_code} step error: {e}")


ROOM_MGR: Optional[RoomManager] = None
PUBLIC_TUNNEL_URL: str = ""


def _start_cloudflare_tunnel(port: int):
    """Automatically launches a free Cloudflare HTTPS Quick Tunnel so the site is reachable from anywhere."""
    global PUBLIC_TUNNEL_URL
    cf_bin = shutil.which("cloudflared")
    if not cf_bin:
        cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp", "cloudflared.exe")
        if os.path.isfile(cand):
            cf_bin = cand
        elif os.path.isfile(r"E:\rea\temp\cloudflared.exe"):
            cf_bin = r"E:\rea\temp\cloudflared.exe"
    if not cf_bin or not os.path.isfile(cf_bin):
        return
    try:
        proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        url_pattern = re.compile(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)")
        for line in proc.stdout:
            m = url_pattern.search(line)
            if m:
                PUBLIC_TUNNEL_URL = m.group(1)
                print(f"[+] 外網公開網址已就緒 (任何電腦/手機皆可直接點開): {PUBLIC_TUNNEL_URL}", flush=True)
    except Exception as e:
        print(f"[!] Tunnel warning: {e}", flush=True)



class BattleCatsWebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"; timeout = 15

    def log_message(self, format: str, *args):
        # Quiet standard request logs to keep terminal clean
        pass

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send_json(self, data: Any, status: int = 200):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        use_gz = len(raw) >= 1024 and self._accepts_gzip()
        if use_gz:
            raw = gzip.compress(raw, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, filepath: str, content_type: str):
        if not os.path.isfile(filepath):
            self.send_error(404, "File not found")
            return
        with open(filepath, "rb") as f:
            raw = f.read()
        is_media = content_type.startswith("image/") or content_type.startswith("audio/")
        use_gz = (not is_media) and len(raw) >= 1024 and self._accepts_gzip()
        if use_gz:
            raw = gzip.compress(raw, compresslevel=5)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(raw)))
        if is_media:
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        global ROOM_MGR
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")

        if path.startswith("/web/"):
            rel = path[len("/web/"):]
            if ".." in rel or "/" in rel or "\\" in rel:
                self.send_error(403)
                return
            ext = os.path.splitext(rel)[1].lower()
            ctype = {
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".png": "image/png",
            }.get(ext, "application/octet-stream")
            return self._send_file(os.path.join(WEB_DIR, rel), ctype)

        if path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            if ".." in rel or "/" in rel or "\\" in rel:
                self.send_error(403)
                return
            candidate = os.path.join(ASSET_DIR, rel)
            if not os.path.isfile(candidate) and rel == "186_e.png":
                candidate = os.path.join(ENEMY_186_DIR, "186_e.png")
            return self._send_file(candidate, "image/png")

        if path.startswith("/audio/"):
            rel = path[len("/audio/"):]
            if ".." in rel or "/" in rel or "\\" in rel:
                self.send_error(403)
                return
            audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "audio")
            candidate = os.path.join(audio_dir, rel)
            ctype = "audio/ogg" if rel.endswith(".ogg") else ("audio/wav" if rel.endswith(".wav") else "application/octet-stream")
            return self._send_file(candidate, ctype)

        if path == "/api/bootstrap":
            CustomUnitLoader.load_all()
            base_cats = []
            for k in FINAL_CAT_KEYS:
                sp = CAT_REGISTRY.get(k, {})
                if not sp:
                    continue
                max_lv = int(sp.get("max_level", 50))
                brackets = sp.get("level_brackets", [20, 20, 20, 20, 20, 20, 10, 10, 10, 10])
                hits = sp.get("attack_hits", [])
                max_hp = compute_ponos_level_stat(sp.get("hp", 100), max_lv, brackets)
                if hits:
                    max_ap = sum(compute_ponos_level_stat(h.get("damage", 1), max_lv, brackets) for h in hits)
                else:
                    max_ap = compute_ponos_level_stat(sp.get("attack_power", 20), max_lv, brackets)
                base_cats.append({
                    "key": k,
                    "id": sp.get("id", 0),
                    "name": sp.get("name", k),
                    "desc": sp.get("description", ""),
                    "cost": sp.get("cost", 50),
                    "hp": sp.get("hp", 100),
                    "ap": sp.get("attack_power", 20),
                    "max_hp": max_hp,
                    "max_ap": max_ap,
                    "max_level": max_lv,
                    "max_base_level": int(sp.get("max_base_level", max_lv)),
                    "max_plus_level": int(sp.get("max_plus_level", 0)),
                    "form": sp.get("form", "s"),
                    "form_label": sp.get("form_label", "三階"),
                    "attack_hits": hits,
                    "range": sp.get("attack_range", 140),
                    "speed": sp.get("speed", 10),
                    "kb_count": sp.get("knockback_count", sp.get("kb_count", 3)),
                    "level_brackets": brackets,
                    "foreswing": sp.get("attack_windup", 10),
                    "tba": sp.get("attack_interval", 30),
                    "cd": sp.get("cooldown_frames", 60),
                    "cd_frames": sp.get("cooldown_frames", 60),
                    "target_type": sp.get("target_type", "single"),
                    "is_area": sp.get("target_type", "single") == "area",
                    "rig": sp.get("texture_code", "000_s"),
                    "icon": sp.get("icon_code", "uni000_s00"),
                    "rarity": str(sp.get("rarity", "normal")),
                    "rarity_label": sp.get("rarity_label", "基本"),
                    "scale": sp.get("render_scale", UNIT_RENDER_SCALE),
                    "traits": sp.get("traits", []),
                    "target_traits": sp.get("target_traits", []),
                    "abilities": sp.get("abilities", {}),
                    "talents": sp.get("talents", []),
                    "custom": False,
                })
            return self._send_json({
                "lan_ip": get_local_ip(),
                "public_url": PUBLIC_TUNNEL_URL,
                "rigs": {
                    k: v for k, v in ROOM_MGR.rigs_cache.items()
                    if k.startswith("fx_") or k in ("000_f", "000_s", "001_s", "018_s", "037_s", "041_s", "032_s", "031_s", "024_s", "025_s", "455_s")
                },
                "base_cats": base_cats,
                "custom_units": CustomUnitLoader.export_all_specs(),
                "builtin_unit": {**BUILTIN_UNIT, "player_name": BUILTIN_PLAYER_NAME},
                "rooms": ROOM_MGR.list_rooms(),
            })

        if path == "/api/rig":
            codes = []
            if "code" in qs and qs["code"][0]:
                codes.append(qs["code"][0].strip())
            if "codes" in qs and qs["codes"][0]:
                codes.extend([c.strip() for c in qs["codes"][0].split(",") if c.strip()])
            rigs_out = {}
            for code in codes[:40]:
                rig = ROOM_MGR.get_or_load_rig(code)
                if rig:
                    rigs_out[code] = rig
            return self._send_json({"ok": True, "rigs": rigs_out})

        if path == "/api/room/list":
            return self._send_json({"rooms": ROOM_MGR.list_rooms(), "public_url": PUBLIC_TUNNEL_URL})

        if path == "/api/room/state":
            code = qs.get("room", [""])[0]
            since_id = int(qs.get("since", ["0"])[0])
            rm = ROOM_MGR.get_room(code)
            if rm is None:
                return self._send_json({"error": f"找不到房號 {code}"}, status=404)
            return self._send_json(rm.serialize_state(since_event_id=since_id))

        if path == "/api/room/stream":
            code = qs.get("room", [""])[0]
            rm = ROOM_MGR.get_room(code)
            if rm is None:
                self.send_error(404, "Room not found")
                return
            self.send_response(200)
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            last_ev_id = 0
            try:
                while True:
                    state = rm.serialize_state(since_event_id=last_ev_id)
                    last_ev_id = state.get("last_event_id", last_ev_id)
                    payload = f"data: {json.dumps(state, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    with rm.frame_cond:
                        rm.frame_cond.wait(timeout=0.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
                return

        self.send_error(404, "Not found")

    def do_POST(self):
        global ROOM_MGR
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            body = {}

        if path == "/api/room/create":
            # Sync any custom units uploaded by Host
            custom_list = body.get("custom_units", [])
            if isinstance(custom_list, list) and custom_list:
                CustomUnitLoader.sync_specs_from_list(custom_list)
            room_code = str(body.get("room_code", "")).strip()
            host_name = str(body.get("player_name", "P1 房主")).strip() or "P1 房主"
            vs_ai = bool(body.get("vs_ai", False))
            deck = body.get("deck")
            cat_level = int(body.get("cat_level", 30) or 30)
            talent_ov = body.get("talent_overrides") if isinstance(body.get("talent_overrides"), dict) else None
            rm = ROOM_MGR.create_room(custom_code=room_code, host_name=host_name, vs_ai=vs_ai, host_deck=deck, cat_level=cat_level, host_talent_overrides=talent_ov)
            return self._send_json({
                "ok": True,
                "room_code": rm.room_code,
                "team": TEAM_CAT,
                "player_token": rm.host_token,
                "state": rm.serialize_state(),
            })

        if path == "/api/room/join":
            room_code = str(body.get("room_code", "")).strip()
            rm = ROOM_MGR.get_room(room_code)
            if rm is None:
                return self._send_json({"ok": False, "error": f"找不到房號「{room_code}」，請確認房號是否正確或先建立房間！"}, status=404)
            custom_list = body.get("custom_units", [])
            if isinstance(custom_list, list) and custom_list:
                CustomUnitLoader.sync_specs_from_list(custom_list)
            guest_name = str(body.get("player_name", "P2 挑戰者")).strip() or "P2 挑戰者"
            deck = body.get("deck")
            talent_ov = body.get("talent_overrides") if isinstance(body.get("talent_overrides"), dict) else None
            player_token = rm.join_guest(guest_name=guest_name, guest_deck=deck, guest_talent_overrides=talent_ov)
            return self._send_json({
                "ok": True,
                "room_code": rm.room_code,
                "team": TEAM_ENEMY,
                "player_token": player_token,
                "state": rm.serialize_state(),
            })

        if path == "/api/check_combos":
            deck = body.get("deck", [])
            combos = []
            if isinstance(deck, list) and deck:
                row1 = []
                for k in deck[:5]:
                    sp = CAT_REGISTRY.get(str(k))
                    if sp and sp.get("unit_id") is not None:
                        row1.append((int(sp["unit_id"]), int(sp.get("form_index", 2))))
                mgr = NyancomboManager.get_instance()
                active = mgr.evaluate_deck(row1)
                combos = mgr.calculate_combo_effects(active).get("active_combos", [])
            return self._send_json({"ok": True, "combos": combos})

        if path == "/api/room/action":
            room_code = str(body.get("room_code", "")).strip()
            rm = ROOM_MGR.get_room(room_code)
            if rm is None:
                return self._send_json({"ok": False, "error": "房間不存在"}, status=404)
            team = int(body.get("team", TEAM_CAT))
            action = str(body.get("action", ""))
            payload = body.get("payload")
            applied = rm.apply_action(team, action, payload, body.get("player_token", ""))
            return self._send_json({"ok": applied})

        if path == "/api/custom_unit":
            spec = body.get("spec", body)
            if not isinstance(spec, dict) or not spec.get("name"):
                return self._send_json({"ok": False, "error": "請提供角色名稱與數值"}, status=400)
            uid = str(spec.get("id") or spec.get("key") or f"custom_{int(time.time())}").strip()
            if not uid.startswith("custom_"):
                uid = "custom_" + uid
            spec["id"] = uid
            spec["key"] = uid

            # Persist to custom_units/<uid>.json
            cu_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_units")
            os.makedirs(cu_dir, exist_ok=True)
            save_path = os.path.join(cu_dir, f"{uid}.json")
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False, indent=2)

            CustomUnitLoader.load_from_dict(spec)
            return self._send_json({
                "ok": True,
                "key": uid,
                "custom_units": CustomUnitLoader.export_all_specs(),
            })

        self.send_error(404, "Unknown API endpoint")


def run_web_server(host: str = "0.0.0.0", port: int = 8080):
    global ROOM_MGR
    if ROOM_MGR is None:
        ROOM_MGR = RoomManager()
    threading.Thread(target=_start_cloudflare_tunnel, args=(port,), daemon=True).start()
    lan_ip = get_local_ip()
    server = ThreadingHTTPServer((host, port), BattleCatsWebHandler)
    print("=" * 68, flush=True)
    print("  貓咪大戰爭 (The Battle Cats) - 網頁房號聯機對戰版 (Web PvP Server)", flush=True)
    print(f"  本機瀏覽器開啟 : http://127.0.0.1:{port}", flush=True)
    print(f"  區網瀏覽器開啟 : http://{lan_ip}:{port}", flush=True)
    print("  功能：輸入 4 位數房號對戰 / 網頁直接自製角色 / 官方骨骼動畫完整渲染", flush=True)
    print("=" * 68, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down web server...")
        server.server_close()


if __name__ == "__main__":
    port_arg = 8080
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port_arg = int(sys.argv[1])
    run_web_server(port=port_arg)
