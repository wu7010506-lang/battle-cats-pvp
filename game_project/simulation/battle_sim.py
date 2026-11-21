"""
Battlefield Simulation Engine - Authentic Ponos Native AArch64 Parity & Symmetric PvP Edition.
Autonomous, headless-compatible simulation orchestrator implementing Phase 10-14 verified rules:
- Exact 51-slot symmetrical team architecture (Team 0 Right Castle marching LEFT, Team 1 Left Castle marching RIGHT).
- Authentic 4-phase combat state machine:
  * Walk (`00.maanim`): Moves when `!in_range && !is_attacking && !in_kb && !frozen`.
  * Foreswing / Windup (`02.maanim` frames `0 .. pre_atk`): Locked in place; does NOT cancel if target leaves range; only interrupted by knockback/freeze/death.
  * Backswing (`02.maanim` frames `pre_atk .. attack_anim_len`): Locked in place finishing attack animation after hit delivery.
  * TBA Cooldown (`tba_timer`): Ticks down after attack animation completes; unit stands in Idle (`01.maanim`) if enemy is in range, or advances if path is clear.
- Authentic traits & abilities (Strong, Massive Damage, Resistant, Crit, Knockback proc, Freeze proc, Slow proc, Weaken proc, Strengthen, Survive Lethal, Base Destroyer, Metal 1-damage rule, Barrier, Wave attack).
- Full symmetric PvP mode: Both Team 0 (Right) and Team 1 (Left) have independent `WorkerCatWallet`, `CatDeployer` (10-slot deck, supporting official cats, enemies, or custom characters), and `CatCannon`.
"""

import random
from typing import List, Dict, Any, Optional, Union, Tuple
from ..core.constants import (
    MAX_UNITS_PER_TEAM, SLOT_CASTLE, TEAM_CAT, TEAM_ENEMY,
    KNOCKBACK_FRAMES_DEFAULT, KNOCKBACK_SPEED_PX, KB_SPECS,
    CASTLE_EDGE_OFFSET_DU, KB_Y_ARC_STATE3_DU, KB_Y_ARC_STATE5_DU,
    WAVE_PROG, WAVE_SEG_FRAMES, WAVE_INI_CAT, WAVE_INI_ENEMY, WAVE_WIDTH_CAT, WAVE_WIDTH_ENEMY,
    SLOW_SPEED_DU_PER_FRAME, BOSS_DEATH_SLOWMO_FRAMES, DEFAULT_ZOMBIE_REVIVE_FRAMES, DEFAULT_WARP_DISTANCE_DU,
    UNIT_RENDER_SCALE
)
from ..core.unit_data import CAT_REGISTRY, ENEMY_REGISTRY, ROW_TO_ENEMY_KEY
from ..core.damage_engine import NativeDamageEngine
from .wallet import WorkerCatWallet
from .deployer import CatDeployer
from .enemy_spawner import StageWaveSpawner
from .nyancombo import NyancomboManager
from ..core.treasure_manager import TreasureManager

class SimUnit:
    """Represents an active entity occupying one of the 51 slots in a team."""
    def __init__(self, team: int, slot: int, unit_key: str, spec: Dict[str, Any], pos_x: int, magnification: int = 100):
        self.team = team
        self.slot = slot
        self.key = unit_key
        self.spec = spec
        self.name = spec.get("name", unit_key)
        self.magnification = magnification
        
        base_hp = spec.get("hp", 100)
        base_atk = spec.get("attack_power", 20)
        
        # Scale stats by magnification using native magic division permille scaling
        permille = magnification * 10
        self.max_hp = NativeDamageEngine.calculate_damage(base_hp, permille) if magnification != 100 else base_hp
        self.hp = self.max_hp
        self.attack_power = NativeDamageEngine.calculate_damage(base_atk, permille) if magnification != 100 else base_atk
        
        self.attack_range = spec.get("attack_range", 140)
        self.speed = spec.get("speed", 10)
        self.attack_interval = spec.get("attack_interval", 30)
        self.attack_windup = max(1, spec.get("attack_windup", 8))

        raw_hits = spec.get("attack_hits")
        if raw_hits and isinstance(raw_hits, list):
            self.attack_hits: List[Dict[str, Any]] = []
            for h in raw_hits:
                hdmg = h.get("damage", base_atk)
                if magnification != 100:
                    hdmg = NativeDamageEngine.calculate_damage(hdmg, permille)
                self.attack_hits.append({
                    "damage": max(1, hdmg),
                    "foreswing": max(1, int(h.get("foreswing", self.attack_windup))),
                    "use_ability": bool(h.get("use_ability", True)),
                    "ld_start": int(h.get("ld_start", spec.get("abilities", {}).get("ld_start", 0))),
                    "ld_range": int(h.get("ld_range", spec.get("abilities", {}).get("ld_range", 0))),
                })
            self.attack_power = sum(h["damage"] for h in self.attack_hits)
            self.attack_windup = self.attack_hits[0]["foreswing"]
            max_pre = max(h["foreswing"] for h in self.attack_hits)
        else:
            self.attack_hits = [{
                "damage": self.attack_power,
                "foreswing": self.attack_windup,
                "use_ability": True,
                "ld_start": int(spec.get("abilities", {}).get("ld_start", 0)),
                "ld_range": int(spec.get("abilities", {}).get("ld_range", 0)),
            }]
            max_pre = self.attack_windup

        # Total attack animation length (foreswing + backswing) from 02.maanim
        self.attack_anim_len = max(max_pre + 2, spec.get("attack_anim_len", max_pre + 7))
        self.current_hit_idx = 0
        self.knockback_max = max(1, spec.get("knockback_count", 3))
        self.color = spec.get("color", (255, 255, 255))
        self.radius = spec.get("radius", 20)
        self.money_drop = spec.get("money_drop", spec.get("cost", 50) // 2)
        self.target_type = spec.get("target_type", "single")
        self.texture_code = spec.get("texture_code", "")

        # Traits & Abilities
        self.traits: List[str] = list(spec.get("traits", ["traitless"]))
        self.target_traits: List[str] = list(spec.get("target_traits", []))
        self.abilities: Dict[str, Any] = dict(spec.get("abilities", {}))
        self.hitbox_front: int = 0 if slot == SLOT_CASTLE else int(spec.get("hitbox_front", 0))
        self.hitbox_back: int = 0 if slot == SLOT_CASTLE else int(spec.get("hitbox_back", 320))

        # Dynamic battle state
        self.pos_x = pos_x
        self.kb_y = 0.0
        self.is_alive = True
        self.is_moving = False
        self.attack_cooldown_timer = 0
        self.windup_timer = 0
        self.is_winding_up = False
        self.backswing_timer = 0
        self.anim_state = "walk"
        self.anim_frame = 0.0
        self.attack_cycle_id = 0
        self.knockback_timer = 0
        self.kb_total_frames = 0
        self.kb_elapsed = 0
        self.kb_distance = 0.0
        self.is_dying = False
        # Native 0x5cbf84 / 0x5dbc18: TBA starts on last hit frame, overlapping backswing
        self.tba_timer = 0
        self.cycle_timer = 0
        self.swing_procs: Optional[Dict[str, Any]] = None
        self.countered_surge_ids: set = set()
        self.dodge_timer = 0
        self.curse_timer = 0
        self.kb_points_taken = 0
        self.kb_threshold = max(1.0, float(self.max_hp) / float(self.knockback_max))
        self.slow_timer = 0
        self.freeze_timer = 0
        self.weaken_timer = 0
        self.weaken_pct = 50
        self.barrier = int(self.abilities.get("barrier_hp", 0))
        self.aku_shield = int(self.abilities.get("shield_hp", 0))
        self.aku_shield_max = int(self.abilities.get("shield_hp", 0))
        self.survived_lethal = False
        self.is_boss = bool(spec.get("is_boss", False))
        self.boss_death_triggered = False
        self.lifetime = spec.get("lifetime")
        # Zombie burrow and revive state
        self.burrow_left = int(self.abilities.get("burrow_count", 0))
        self.burrow_range = int(self.abilities.get("burrow_range", 0))
        self.burrow_timer = 0
        self.is_burrowing = False
        self.revive_left = int(self.abilities.get("revive_count", 0))
        self.corpse_timer = 0
        self.is_corpse = False
        self.killed_by_zombie_killer = False
        self.killed_with_extra_money = False
        # Starred Alien Warp state
        self.is_warped = False
        self.warp_timer = 0
        self.warp_dest_x = float(pos_x)

    def is_in_knockback(self) -> bool:
        return self.knockback_timer > 0

    def is_in_warp(self) -> bool:
        return self.is_warped or self.warp_timer > 0

    def is_frozen(self) -> bool:
        return self.freeze_timer > 0

    def is_slowed(self) -> bool:
        return self.slow_timer > 0

    def is_weakened(self) -> bool:
        return self.weaken_timer > 0

    def is_cursed(self) -> bool:
        return self.curse_timer > 0

    def is_in_attack_anim(self) -> bool:
        return self.is_winding_up or self.backswing_timer > 0

class BattleSimulation:
    def __init__(
        self,
        stage_config: Optional[Dict[str, Any]] = None,
        deck: Optional[List[Any]] = None,
        cat_levels: Optional[Dict[str, int]] = None,
        base_levels: Optional[Dict[str, int]] = None,
        pvp_mode: bool = False,
        p2_deck: Optional[List[Any]] = None,
        p2_levels: Optional[Dict[str, int]] = None,
        p2_base_levels: Optional[Dict[str, int]] = None,
        rng_seed: Optional[int] = None,
        treasures: Optional[Dict[str, Any]] = None,
        p2_treasures: Optional[Dict[str, Any]] = None,
        cannon_type: Optional[str] = None,
        cannon_types: Optional[Dict[int, str]] = None
    ):
        self.stage_config = stage_config or StageWaveSpawner.get_default_stage()
        self.battlefield_length = self.stage_config.get("length", 3000)
        self.current_frame = 0
        self.game_over = False
        self.winner: Optional[int] = None
        self.pvp_mode = pvp_mode
        self.rng = random.Random(rng_seed) if rng_seed is not None else random.Random(42)
        base_levels = base_levels or {}

        # Slots: list of length 51 for Team 0 (Right) and Team 1 (Left)
        self.units: List[List[Optional[SimUnit]]] = [
            [None] * MAX_UNITS_PER_TEAM,
            [None] * MAX_UNITS_PER_TEAM
        ]

        # Initialize Castles at Slot 0
        def _calc_native_castle_hp(lv: int) -> int:
            ab = max(0, int(lv) - 1)
            if ab <= 3:
                return 1000 + 1000 * ab
            if ab <= 7:
                return -2000 + 2000 * ab
            return -9000 + 3000 * ab

        t0_castle_lv = base_levels.get("castle_hp", 1)
        t1_castle_lv = (p2_base_levels or {}).get("castle_hp", t0_castle_lv)
        if "cat_castle_hp" in self.stage_config and t0_castle_lv == 1 and "castle_hp" not in base_levels:
            cat_castle_hp = int(self.stage_config["cat_castle_hp"])
        else:
            cat_castle_hp = _calc_native_castle_hp(t0_castle_lv)
        if pvp_mode:
            if p2_base_levels is not None and "castle_hp" in p2_base_levels:
                enemy_castle_hp = _calc_native_castle_hp(t1_castle_lv)
            else:
                enemy_castle_hp = cat_castle_hp
        else:
            enemy_castle_hp = self.stage_config.get("enemy_castle_hp", 500)
        
        # Authentic Battle Cats layout:
        # Authentic PONOS stage edge offset (0x8b6dec): 0xaf0 = 2800 native = 700.0 DU from stage borders
        castle_margin = min(CASTLE_EDGE_OFFSET_DU, max(120.0, self.battlefield_length * 0.25))
        # Team 1 (Enemy / Player 2) Castle is on the LEFT (pos_x = 700 DU)
        # Team 0 (Cat / Player 1) Castle is on the RIGHT (pos_x = battlefield_length - 700 DU)
        self.units[TEAM_CAT][SLOT_CASTLE] = SimUnit(
            team=TEAM_CAT, slot=SLOT_CASTLE, unit_key="cat_castle",
            spec={"name": "貓咪軍團主堡", "hp": cat_castle_hp, "attack_power": 0, "attack_range": 0, "speed": 0, "knockback_count": 1, "radius": 50, "color": (230, 230, 240), "texture_code": "fc000"},
            pos_x=self.battlefield_length - castle_margin
        )
        self.units[TEAM_ENEMY][SLOT_CASTLE] = SimUnit(
            team=TEAM_ENEMY, slot=SLOT_CASTLE, unit_key="enemy_castle",
            spec={"name": "P2 軍團主堡" if pvp_mode else "敵方汪汪大本營", "hp": enemy_castle_hp, "attack_power": 0, "attack_range": 0, "speed": 0, "knockback_count": 1, "radius": 50, "color": (200, 160, 140), "texture_code": "ec000_tw"},
            pos_x=castle_margin
        )

        # Symmetrical Economy & Deployers for Team 0 (Right) and Team 1 (Left)
        eff_lv = base_levels.get("worker_cat", 1)
        wal_lv = base_levels.get("wallet", 1)
        p2_eff_lv = (p2_base_levels or {}).get("worker_cat", eff_lv)
        p2_wal_lv = (p2_base_levels or {}).get("wallet", wal_lv)

        self.wallet = WorkerCatWallet(initial_money=0, efficiency_level=eff_lv, wallet_level=wal_lv)
        self.deployer = CatDeployer(deck_keys=deck, cat_levels=cat_levels)

        self.p2_wallet = WorkerCatWallet(initial_money=0, efficiency_level=p2_eff_lv, wallet_level=p2_wal_lv)
        merged_p2_reg = dict(CAT_REGISTRY)
        merged_p2_reg.update(ENEMY_REGISTRY)
        self.p2_deployer = CatDeployer(
            deck_keys=p2_deck if p2_deck is not None else (deck if deck is not None else None),
            cat_levels=p2_levels if p2_levels is not None else cat_levels,
            registry=merged_p2_reg
        )

        self.wallets = {TEAM_CAT: self.wallet, TEAM_ENEMY: self.p2_wallet}
        self.deployers = {TEAM_CAT: self.deployer, TEAM_ENEMY: self.p2_deployer}

        self.spawner = StageWaveSpawner(self.stage_config)

        # Cannon Foundation Types (normal, slow, iron_wall, thunderbolt, waterblast, holyblast, breakerblast, curseblast)
        if cannon_types is not None:
            self.cannon_types = dict(cannon_types)
        else:
            ct = cannon_type or "normal"
            self.cannon_types = {TEAM_CAT: ct, TEAM_ENEMY: ct}

        # Calculate & Apply Official Battle Cats Treasures
        tr_mgr = TreasureManager.get_instance()
        t0_tr = tr_mgr.calculate_battle_treasures(treasures)
        t1_tr = tr_mgr.calculate_battle_treasures(p2_treasures if p2_treasures is not None else (treasures if pvp_mode else {"eoc_chapters": 0, "trait_fruits": {}}))
        self.team_treasures = {TEAM_CAT: t0_tr, TEAM_ENEMY: t1_tr}

        # Evaluate Nyancombos before Economy/Cannon init (so 0x47a29c/0x47a3a4 & AddMoney see exact active combos)
        combo_mgr = NyancomboManager.get_instance()
        def _resolve_deck_form(k: Any) -> Optional[Tuple[int, int]]:
            if k is None:
                return None
            spec = k if isinstance(k, dict) else CAT_REGISTRY.get(k) or ENEMY_REGISTRY.get(k)
            if not spec or not isinstance(spec, dict):
                return None
            cid = spec.get("id")
            if cid is None or not isinstance(cid, int):
                return None
            tc = spec.get("texture_code", "")
            form = 1 if "_c" in tc else (2 if "_s" in tc else 0)
            return (cid, form)

        t0_deck_forms = [_resolve_deck_form(k) for k in (deck or [])[:5]]
        t0_eff = combo_mgr.calculate_combo_effects(combo_mgr.evaluate_deck(t0_deck_forms))
        t1_eff = combo_mgr.calculate_combo_effects([])
        if self.pvp_mode:
            p2_deck_list = p2_deck if p2_deck is not None else deck
            t1_deck_forms = [_resolve_deck_form(k) for k in (p2_deck_list or [])[:5]]
            t1_eff = combo_mgr.calculate_combo_effects(combo_mgr.evaluate_deck(t1_deck_forms))
        self.team_combo_effects = {TEAM_CAT: t0_eff, TEAM_ENEMY: t1_eff}

        # Symmetrical Cat Cannon Controllers (Native ARM64 0x47a29c & 0x47a3a4)
        cannon_charge_lv = base_levels.get("cannon_charge", 1)
        cannon_pwr_lv = base_levels.get("cannon_power", 1)
        c_range_lv = base_levels.get("cannon_range", 1)

        p2_charge_lv = (p2_base_levels or {}).get("cannon_charge", cannon_charge_lv)
        p2_pwr_lv = (p2_base_levels or {}).get("cannon_power", cannon_pwr_lv)
        p2_c_range_lv = (p2_base_levels or {}).get("cannon_range", c_range_lv)

        self.cannon_max_charge: int = self._calc_cannon_max_charge(
            self.cannon_types.get(TEAM_CAT, "normal"), c_range_lv, cannon_charge_lv, t0_tr,
            combo_recharge_pct=int(round(t0_eff.get("cannon_recharge_pct", 0.0))),
        )
        self.p2_cannon_max_charge: int = self._calc_cannon_max_charge(
            self.cannon_types.get(TEAM_ENEMY, "normal"), p2_c_range_lv, p2_charge_lv, t1_tr,
            combo_recharge_pct=int(round(t1_eff.get("cannon_recharge_pct", 0.0))),
        )
        self.cannon_charge: int = self._calc_cannon_init_charge(
            self.cannon_max_charge, int(round(t0_eff.get("cannon_charge_rate", 0.0) * 100))
        )
        self.p2_cannon_charge: int = (
            self._calc_cannon_init_charge(
                self.p2_cannon_max_charge, int(round(t1_eff.get("cannon_charge_rate", 0.0) * 100))
            )
            if self.pvp_mode
            else 0
        )

        # Authentic PONOS Cat Cannon Range (DU = native / 4):
        # 2355 / 4 = 588.75 DU base at Lv 1, +101.25 DU / level (+405/4), Max Lv 10 = 6000 / 4 = 1500.0 DU
        self.cannon_ranges = {
            TEAM_CAT: min(1500.0, (2355 + 405 * (c_range_lv - 1)) / 4.0),
            TEAM_ENEMY: min(1500.0, (2355 + 405 * (p2_c_range_lv - 1)) / 4.0),
        }
        self.cannon_range: float = self.cannon_ranges[TEAM_CAT]
        t0_base_pwr = 100 if (cannon_pwr_lv > 1 or "cannon_power" in base_levels) else 250
        t1_base_pwr = 100 if (p2_pwr_lv > 1 or "cannon_power" in (p2_base_levels or {})) else 250
        self.cannon_powers = {
            TEAM_CAT: int((t0_base_pwr + (cannon_pwr_lv - 1) * 50 + t0_tr["cannon_power_bonus"]) * t0_eff["cannon_power_mult"]),
            TEAM_ENEMY: int((t1_base_pwr + (p2_pwr_lv - 1) * 50 + t1_tr["cannon_power_bonus"]) * t1_eff["cannon_power_mult"]),
        }
        self.cannon_power: int = self.cannon_powers[TEAM_CAT]
        self.cannon_anim_timer: Dict[int, int] = {TEAM_CAT: 0, TEAM_ENEMY: 0}
        self.active_cannon_waves: List[Dict[str, Any]] = []
        self.active_waves: List[Dict[str, Any]] = []
        self.active_surges: List[Dict[str, Any]] = []
        self.active_explosions: List[Dict[str, Any]] = []
        self._surge_id_seq: int = 0

        # Symmetrical Accounting bounty multipliers for Team 0 and Team 1
        t0_acct = base_levels.get("accounting", 1)
        t1_acct = (p2_base_levels or {}).get("accounting", 1)
        self.accounting_level = t0_acct
        self.wallet_multipliers = {
            TEAM_CAT: (t0_tr["bounty_mult"] + 0.05 * max(0, t0_acct - 1)) * t0_eff["accounting_mult"],
            TEAM_ENEMY: (t1_tr["bounty_mult"] + 0.05 * max(0, t1_acct - 1)) * (t1_eff.get("accounting_mult", 1.0) if self.pvp_mode else 1.0),
        }
        self.wallet_multiplier = self.wallet_multipliers[TEAM_CAT]

        # Apply Treasure & Nyancombo bonuses to Castle HP & Wallet symmetrically
        teams_to_init = [TEAM_CAT, TEAM_ENEMY] if self.pvp_mode else [TEAM_CAT]
        for t_id in teams_to_init:
            tr = self.team_treasures[t_id]
            eff = self.team_combo_effects[t_id]
            castle_u = self.units[t_id][SLOT_CASTLE]
            castle_u.max_hp += tr["castle_hp_bonus"]
            if eff["castle_hp_mult"] != 1.0:
                castle_u.max_hp = int(castle_u.max_hp * eff["castle_hp_mult"])
            castle_u.hp = castle_u.max_hp

            w = self.wallets[t_id]
            w.set_treasure_bonuses(
                rate_mult=tr["worker_rate_mult"],
                cap_bonus=tr["wallet_cap_bonus"],
                tr3_centi=tr.get("worker_income_bonus_centi"),
                tr4_centi=tr.get("wallet_cap_bonus_centi"),
            )
            if eff["worker_level"] > 0:
                w.level = min(w.max_level, w.level + eff["worker_level"])
                w._update_level_stats()
            if eff["initial_money"] > 0:
                w.add_money(eff["initial_money"])

        self.slow_motion_timer: int = 0

        # Apply EoC treasure, Cat Research (native 0x47aa0c: 6*(lv-1)f), and Nyancombo cooldown reduction to deployers
        self.research_reductions = {
            TEAM_CAT: 6 * max(0, int(base_levels.get("research", 1)) - 1),
            TEAM_ENEMY: 6 * max(0, int((p2_base_levels or {}).get("research", 1)) - 1),
        }
        t0_cd_reduct = t0_tr.get("cooldown_reduction", 0) + t0_eff.get("cooldown_reduction", 0) + self.research_reductions[TEAM_CAT]
        self.deployer.apply_cooldown_reduction(t0_cd_reduct)
        if self.pvp_mode:
            t1_cd_reduct = t1_tr.get("cooldown_reduction", 0) + self.team_combo_effects[TEAM_ENEMY].get("cooldown_reduction", 0) + self.research_reductions[TEAM_ENEMY]
            self.p2_deployer.apply_cooldown_reduction(t1_cd_reduct)

        # Frame Events for Renderer (preserved across pre-step user input & step())
        self.visual_events: List[Dict[str, Any]] = []
        self._pending_visual_events: List[Dict[str, Any]] = []

        if t0_eff["active_combos"]:
            self._emit_event({"type": "nyancombo_activate", "team": TEAM_CAT, "combos": t0_eff["active_combos"]})
        if self.pvp_mode and self.team_combo_effects[TEAM_ENEMY]["active_combos"]:
            self._emit_event({"type": "nyancombo_activate", "team": TEAM_ENEMY, "combos": self.team_combo_effects[TEAM_ENEMY]["active_combos"]})

    def _emit_event(self, ev: Dict[str, Any]):
        self.visual_events.append(ev)
        if not getattr(self, "_in_step", False):
            self._pending_visual_events.append(ev)

    def consume_visual_events(self) -> List[Dict[str, Any]]:
        events = list(self.visual_events)
        self.visual_events.clear()
        self._pending_visual_events.clear()
        return events

    _CANNON_TYPE_INDICES = {
        "normal": 0,
        "slow": 1,
        "iron_wall": 2,
        "thunderbolt": 3,
        "waterblast": 4,
        "holyblast": 5,
        "breakerblast": 6,
        "curseblast": 7,
    }

    @classmethod
    def _calc_cannon_max_charge(
        cls,
        ctype: str,
        range_lv: int,
        charge_lv: int,
        treasures: Dict[str, Any],
        combo_recharge_pct: int = 0,
    ) -> int:
        """Native ARM64 0x47a29c: exact integer Cat Cannon max charge in frames."""
        c_idx = cls._CANNON_TYPE_INDICES.get(ctype, 0)
        c_bonus = c_idx * 450 if c_idx <= 2 else 1350
        lv_range = max(0, int(range_lv) - 1)
        lv_charge = max(0, int(charge_lv) - 1)
        tr_range = int(treasures.get("cannon_range_charge_reduction", 0))
        tr_charge = int(treasures.get("cannon_charge_reduction", 0))
        raw = 1500 + c_bonus + max(0, 50 * lv_range - tr_range) - 50 * lv_charge - tr_charge
        return max(950, (raw * (100 - int(combo_recharge_pct))) // 100)

    @staticmethod
    def _calc_cannon_init_charge(max_charge: int, init_pct: int) -> int:
        """Native ARM64 0x47a3a4: remaining cooldown = ((100 - pct) * max) // 100."""
        remaining = max(0, ((100 - int(init_pct)) * int(max_charge)) // 100)
        return max(0, int(max_charge) - remaining)

    def is_cannon_ready(self, team: int = TEAM_CAT) -> bool:
        charge = self.cannon_charge if team == TEAM_CAT else self.p2_cannon_charge
        max_ch = self.cannon_max_charge if team == TEAM_CAT else getattr(self, "p2_cannon_max_charge", self.cannon_max_charge)
        return charge >= max_ch

    def get_cannon_ratio(self, team: int = TEAM_CAT) -> float:
        charge = self.cannon_charge if team == TEAM_CAT else self.p2_cannon_charge
        max_ch = self.cannon_max_charge if team == TEAM_CAT else getattr(self, "p2_cannon_max_charge", self.cannon_max_charge)
        return min(1.0, float(charge) / float(max_ch))

    def fire_cannon(self, team: int = TEAM_CAT) -> bool:
        """Fires the Cat Cannon for `team` across the battlefield with authentic sub-type mechanics."""
        if not self.is_cannon_ready(team) or self.game_over:
            return False

        ctype = self.cannon_types.get(team, "normal")

        c_range = self.cannon_ranges.get(team, self.cannon_range)
        c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
        c_cat_x = self.get_castle(TEAM_CAT).pos_x
        if team == TEAM_CAT:
            self.cannon_charge = 0
            c_unit = self.get_castle(TEAM_CAT)
            # Native 0x4aecec muzzle offset: 270 native = 67.5 DU in front of castle edge
            beam_start_x = c_unit.pos_x - 67.5
            beam_end_x = max(0.0, beam_start_x - c_range)
            min_x, max_x = beam_end_x, beam_start_x
            opp_team = TEAM_ENEMY
            direction = -1
        else:
            self.p2_cannon_charge = 0
            c_unit = self.get_castle(TEAM_ENEMY)
            beam_start_x = c_unit.pos_x + 67.5
            beam_end_x = min(float(self.battlefield_length), beam_start_x + c_range)
            min_x, max_x = beam_start_x, beam_end_x
            opp_team = TEAM_CAT
            direction = 1

        self.cannon_anim_timer[team] = 32

        # Special foundation: Iron Wall Cannon
        if ctype == "iron_wall":
            front_unit = None
            for slot in range(1, MAX_UNITS_PER_TEAM):
                u = self.units[team][slot]
                if u and u.is_alive and not getattr(u, 'is_burrowing', False) and not getattr(u, 'is_corpse', False):
                    if front_unit is None:
                        front_unit = u
                    elif direction == -1 and u.pos_x < front_unit.pos_x:
                        front_unit = u
                    elif direction == 1 and u.pos_x > front_unit.pos_x:
                        front_unit = u
            wall_x = (front_unit.pos_x + direction * 40) if front_unit else (beam_start_x + direction * 150)
            c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
            c_cat_x = self.get_castle(TEAM_CAT).pos_x
            wall_x = max(c_enemy_x + 40, min(c_cat_x - 40, wall_x))

            free_slot = self.find_free_slot(team)
            if free_slot is not None:
                wall_spec = {
                    "name": "鐵壁貓",
                    "hp": 3,
                    "speed": 0,
                    "traits": ["metal"],
                    "abilities": {"is_metal": True, "kb_immune": True},
                    "texture_code": "000_f",
                    "render_scale": UNIT_RENDER_SCALE,
                    "cost": 0,
                    "money_drop": 0,
                    "attack_power": 0,
                    "attack_range": 0,
                    "attack_interval": 9999,
                    "is_custom": True,
                    "lifetime": 90,
                }
                iron_unit = SimUnit(team=team, slot=free_slot, unit_key="iron_wall_cat", spec=wall_spec, pos_x=wall_x)
                iron_unit.lifetime = 90
                self.units[team][free_slot] = iron_unit

            self._emit_event({
                "type": "cannon_beam",
                "cannon_type": "iron_wall",
                "team": team,
                "castle_x": c_unit.pos_x,
                "start_x": beam_start_x,
                "end_x": wall_x,
                "hits": 1
            })
            self._emit_event({
                "type": "iron_wall",
                "team": team,
                "x": wall_x,
                "duration": 90
            })
            return True

        # Special foundation: Thunderbolt Cannon
        if ctype == "thunderbolt":
            target_opp = None
            for slot in range(1, MAX_UNITS_PER_TEAM):
                u = self.units[opp_team][slot]
                if u and u.is_alive and not getattr(u, 'is_burrowing', False) and not getattr(u, 'is_corpse', False) and not getattr(u, 'is_warped', False):
                    if target_opp is None:
                        target_opp = u
                    elif direction == -1 and u.pos_x > target_opp.pos_x:
                        target_opp = u
                    elif direction == 1 and u.pos_x < target_opp.pos_x:
                        target_opp = u
            strike_x = target_opp.pos_x if target_opp else (beam_start_x + direction * 400)
            strike_min = strike_x - 125
            strike_max = strike_x + 125

            hit_count = 0
            opp_dir = -1 if opp_team == TEAM_CAT else 1
            for slot in range(1, MAX_UNITS_PER_TEAM):
                u = self.units[opp_team][slot]
                if u and u.is_alive and not getattr(u, 'is_burrowing', False) and not getattr(u, 'is_corpse', False) and not getattr(u, 'is_warped', False) and not u.is_in_knockback():
                    u_front = u.pos_x - opp_dir * getattr(u, "hitbox_front", 0)
                    if strike_min <= u_front < strike_max or strike_min <= u.pos_x <= strike_max:
                        self._cannon_hit(u, opp_team, ctype="thunderbolt")
                        hit_count += 1

            self._emit_event({
                "type": "cannon_beam",
                "cannon_type": "thunderbolt",
                "team": team,
                "castle_x": c_unit.pos_x,
                "start_x": strike_x,
                "end_x": strike_x,
                "hits": hit_count
            })
            self._emit_event({
                "type": "thunderbolt",
                "x": strike_x,
                "radius": 125,
                "team": team
            })
            self._update_deaths_and_castles()
            return True

        # Standard / Slow / Waterblast / Breakerblast / Curseblast sweeping beams
        # Progressive wavefront model:
        # Initial frame 0 hits units within the muzzle blast reach (native 0x4c5e0c checks target front edge)
        dist = abs(beam_end_x - beam_start_x)
        init_reach_x = beam_start_x + direction * min(dist, 120.0)
        curr_min_x = min(beam_start_x, init_reach_x)
        curr_max_x = max(beam_start_x, init_reach_x)

        hit_ids = set()
        hit_count = 0
        opp_dir = -1 if opp_team == TEAM_CAT else 1
        for slot in range(1, MAX_UNITS_PER_TEAM):
            u = self.units[opp_team][slot]
            if u and u.is_alive and not getattr(u, 'is_burrowing', False) and not getattr(u, 'is_corpse', False) and not getattr(u, 'is_warped', False) and not u.is_in_knockback():
                u_front = u.pos_x - opp_dir * getattr(u, "hitbox_front", 0)
                if curr_min_x <= u_front <= curr_max_x or curr_min_x <= u.pos_x <= curr_max_x:
                    self._cannon_hit(u, opp_team, ctype=ctype)
                    hit_ids.add(id(u))
                    hit_count += 1

        self.active_cannon_waves.append({
            "team": team,
            "opp_team": opp_team,
            "direction": direction,
            "cannon_type": ctype,
            "start_x": beam_start_x,
            "end_x": beam_end_x,
            "dist": dist,
            "min_x": min_x,
            "max_x": max_x,
            "timer": 20,
            "max_timer": 20,
            "elapsed": 0,
            "prev_reach_x": init_reach_x,
            "hit_ids": hit_ids,
        })

        self._emit_event({
            "type": "cannon_beam",
            "cannon_type": ctype,
            "team": team,
            "castle_x": c_unit.pos_x,
            "start_x": beam_start_x,
            "end_x": beam_end_x,
            "hits": hit_count
        })
        self._update_deaths_and_castles()
        return True

    def get_castle(self, team: int) -> SimUnit:
        return self.units[team][SLOT_CASTLE]

    def _knockback(self, u: SimUnit, kind: str = "kb", tr_kb_permille: int = 0) -> bool:
        """Starts an authentic PONOS interrupt knockback (KB_SPECS & native 0x5dd600 State 6).
        A unit already being knocked back is not restarted by an equal/shorter knockback."""
        if u is None or u.slot == SLOT_CASTLE:
            return False
        dist, frames = KB_SPECS.get(kind, KB_SPECS["kb"])
        if u.is_in_knockback() and frames <= u.knockback_timer:
            return False
        u.kb_kind = kind
        u.kb_tr_permille = int(tr_kb_permille)
        if kind == "kb":
            # Native 0x5dd600 State 6: 12 frames with base ((12 - f) % 12) * 10 native units (/4 -> DU)
            total_native = sum(
                (base := ((12 - f) % 12) * 10) + (base * u.kb_tr_permille) // 1000
                for f in range(1, 13)
            )
            u.kb_distance = total_native / 4.0
        else:
            u.kb_distance = float(dist)
        u.kb_total_frames = int(frames)
        u.kb_elapsed = 0
        u.kb_y = 0.0
        u.knockback_timer = int(frames)
        u.anim_state = "kb"
        u.anim_frame = 0.0
        u.is_moving = False
        u.is_winding_up = False
        u.windup_timer = 0
        u.backswing_timer = 0
        u.current_hit_idx = 0
        # Authentic Battle Cats knockback interrupt:
        # Cancels windup/backswing, and resets attack cycle timer so the unit can attack immediately
        # upon recovery once in range (rebound effect).
        u.tba_timer = 0
        u.cycle_timer = 0
        u.attack_cooldown_timer = 0
        u.swing_procs = None
        if kind == "hitback" and u.abilities.get("shield_hp", 0) > 0 and u.abilities.get("shield_regen_pct", 0) > 0 and not getattr(u, "is_dying", False):
            regen_pct = u.abilities["shield_regen_pct"]
            base_shield = u.abilities["shield_hp"]
            u.aku_shield = max(1, (base_shield * regen_pct) // 100)
            self._emit_event({"type": "shield_regen", "x": u.pos_x, "team": u.team, "shield": u.aku_shield})
        return True

    def _cannon_hit(self, u: SimUnit, opp_team: int, ctype: str = "normal"):
        # Native 0x4c6340:58 checks 0x958fe8 targetability (skip units already in KB/death/warp)
        if u is None or u.slot == SLOT_CASTLE or not u.is_alive or u.is_in_knockback() or getattr(u, "is_dying", False) or getattr(u, "is_corpse", False) or getattr(u, "is_warped", False):
            return
        dmg = 0
        if ctype == "slow":
            # Native 0x4c6340: 0 damage, 0 knockback, direct 120-frame Slow write
            if not u.abilities.get("slow_immune", False):
                u.slow_timer = 120
                self._emit_event({"type": "slow", "x": u.pos_x, "team": opp_team})

        elif ctype == "curseblast":
            # Native 0x4c6340: 0 damage, 0 knockback, direct 180-frame Curse write
            if not u.abilities.get("curse_immune", False):
                u.curse_timer = 180
                self._emit_event({"type": "curse", "x": u.pos_x, "team": opp_team})

        elif ctype == "thunderbolt":
            # Native 0x4c6340 + 0x5cbf84: direct 75-frame Freeze write; pauses attack windup without cancelling
            c_pwr = self.cannon_powers.get(1 - opp_team, self.cannon_power)
            dmg = c_pwr
            u.hp -= dmg
            if not u.abilities.get("freeze_immune", False):
                u.freeze_timer = 75
            self._emit_event({"type": "damage", "x": u.pos_x, "radius": u.radius, "damage": dmg, "team": opp_team, "target_id": f"{opp_team}_{u.slot}"})

        elif ctype == "waterblast":
            # Native 0x4c6340 (type 4): integer permille of max_hp (30% vs Metal, 5% vs Non-Metal), no KB
            if "metal" in u.traits or u.abilities.get("is_metal", False):
                dmg = max(1, (u.max_hp * 300) // 1000)
            else:
                dmg = max(1, (u.max_hp * 50) // 1000)
            u.hp -= dmg
            self._emit_event({"type": "damage", "x": u.pos_x, "radius": u.radius, "damage": dmg, "team": opp_team, "target_id": f"{opp_team}_{u.slot}"})

        elif ctype == "breakerblast":
            c_pwr = self.cannon_powers.get(1 - opp_team, self.cannon_power)
            if getattr(u, "aku_shield", 0) > 0:
                u.aku_shield = 0
                self._emit_event({"type": "shield_break", "x": u.pos_x, "team": opp_team})
            if getattr(u, "barrier", 0) > 0:
                u.barrier = 0
                self._emit_event({"type": "barrier_break", "x": u.pos_x, "team": opp_team})
            dmg = max(1, c_pwr // 2)
            u.hp -= dmg
            if not u.abilities.get("kb_immune", False):
                self._knockback(u, "cannon")
                self._emit_event({"type": "knockback", "x": u.pos_x, "radius": u.radius, "team": opp_team, "kind": "cannon"})
            self._emit_event({"type": "damage", "x": u.pos_x, "radius": u.radius, "damage": dmg, "team": opp_team, "target_id": f"{opp_team}_{u.slot}"})

        else: # "normal"
            # Authentic PONOS Default Cat Cannon: wave blast knocks back even normally immune enemies,
            # but is completely blocked/nullified by wave_immune or wave_blocker!
            if u.abilities.get("wave_immune", False) or u.abilities.get("wave_blocker", False):
                return
            c_pwr = self.cannon_powers.get(1 - opp_team, self.cannon_power)
            dmg = 1 if ("metal" in u.traits or u.abilities.get("is_metal")) else c_pwr
            u.hp -= dmg
            self._knockback(u, "cannon")
            self._emit_event({"type": "knockback", "x": u.pos_x, "radius": u.radius, "team": opp_team, "kind": "cannon"})
            self._emit_event({"type": "damage", "x": u.pos_x, "radius": u.radius, "damage": dmg, "team": opp_team, "target_id": f"{opp_team}_{u.slot}"})

        if u.hp <= 0 and u.slot != SLOT_CASTLE and not getattr(u, "is_dying", False):
            u.hp = 0
            u.is_alive = False
            u.is_dying = True
            u.freeze_timer = 0
            u.slow_timer = 0
            u.curse_timer = 0
            self._knockback(u, "hitback")
            self._emit_event({"type": "knockback", "x": u.pos_x, "radius": u.radius, "team": u.team, "kind": "hitback"})
            if opp_team in self.wallets and u.money_drop > 0 and not getattr(u, "bounty_awarded", False):
                u.bounty_awarded = True
                bounty = int(u.money_drop * self.wallet_multipliers.get(opp_team, 1.0))
                self.wallets[opp_team].add_money(bounty)
            if getattr(u, "is_boss", False) and not getattr(u, "boss_death_triggered", False):
                u.boss_death_triggered = True
                self.slow_motion_timer = BOSS_DEATH_SLOWMO_FRAMES
                self._emit_event({"type": "boss_death", "x": u.pos_x, "team": u.team, "name": u.name, "boss_name": u.name})
        elif u.hp > 0 and dmg > 0:
            damage_taken = u.max_hp - u.hp
            expected_kbs = int(damage_taken // max(1.0, u.kb_threshold))
            if expected_kbs > u.kb_points_taken:
                u.kb_points_taken = expected_kbs
                self._knockback(u, "hitback")
                self._emit_event({"type": "knockback", "x": u.pos_x, "radius": u.radius, "team": u.team, "kind": "hitback"})

    def find_free_slot(self, team: int) -> Optional[int]:
        """Finds first empty slot in [1, 50]. Slot 0 reserved for Castle."""
        for slot in range(1, MAX_UNITS_PER_TEAM):
            if self.units[team][slot] is None:
                return slot
        return None

    def deploy_unit(self, team: int, deck_idx: int) -> bool:
        """Symmetrical deployment for Team 0 (Right) or Team 1 (Left in PvP mode)."""
        if self.game_over:
            return False
        slot = self.find_free_slot(team)
        if slot is None:
            return False

        wallet = self.wallets[team]
        deployer = self.deployers[team]
        slot_info = deployer.get_slot_info(deck_idx)
        if slot_info is None:
            return False
        cost = slot_info["cost"]
        if not deployer.can_deploy(deck_idx, wallet.money, has_free_slot=True):
            return False

        wallet.spend(cost)
        combo_eff = self.team_combo_effects.get(team, {})
        team_tr = self.team_treasures.get(team, {})
        cd_reduct = combo_eff.get("cooldown_reduction", 0) + team_tr.get("cooldown_reduction", 0) + self.research_reductions.get(team, 0)
        deployer.trigger_cooldown(deck_idx, reduction_frames=cd_reduct)

        unit_key = slot_info["key"]
        spec = slot_info["spec"]
        castle_x = self.get_castle(team).pos_x
        spawn_x = (castle_x - 30) if team == TEAM_CAT else (castle_x + 30)
        new_unit = SimUnit(team=team, slot=slot, unit_key=unit_key, spec=spec, pos_x=spawn_x)

        if not spec.get("fixed_stats"):
            tr_hp_mult = team_tr.get("cat_hp_mult", 1.0)
            tr_atk_mult = team_tr.get("cat_atk_mult", 1.0)
            tot_hp_mult = combo_eff.get("hp_mult", 1.0) * tr_hp_mult
            tot_atk_mult = combo_eff.get("atk_mult", 1.0) * tr_atk_mult

            if tot_hp_mult != 1.0:
                new_unit.max_hp = max(1, int(new_unit.max_hp * tot_hp_mult))
                new_unit.hp = new_unit.max_hp
                new_unit.kb_threshold = max(1.0, float(new_unit.max_hp) / float(new_unit.knockback_max))
            if tot_atk_mult != 1.0:
                new_unit.attack_power = max(1, int(new_unit.attack_power * tot_atk_mult))
                for h in new_unit.attack_hits:
                    h["damage"] = max(1, int(h["damage"] * tot_atk_mult))
            if combo_eff.get("spd_mult", 1.0) != 1.0:
                new_unit.speed = max(1, int(new_unit.speed * combo_eff["spd_mult"]))
        self.units[team][slot] = new_unit

        self._emit_event({"type": "spawn", "x": spawn_x, "team": team, "name": spec["name"]})
        return True

    def spawn_cat(self, deck_idx: int) -> bool:
        """Player 1 requests deployment of cat unit at deck_idx on Team 0 (Right)."""
        return self.deploy_unit(TEAM_CAT, deck_idx)

    def spawn_p2_unit(self, deck_idx: int) -> bool:
        """Player 2 requests deployment of unit at deck_idx on Team 1 (Left) in PvP mode."""
        return self.deploy_unit(TEAM_ENEMY, deck_idx)

    def upgrade_p2_worker(self) -> bool:
        """Player 2 upgrades Worker Cat wallet in PvP mode."""
        return self.p2_wallet.upgrade_worker()

    def fire_p2_cannon(self) -> bool:
        """Player 2 fires Left Castle Cat Cannon in PvP mode."""
        return self.fire_cannon(TEAM_ENEMY)

    def spawn_custom_unit(
        self,
        unit_or_team: Union[int, str, Dict[str, Any]],
        spec: Optional[Dict[str, Any]] = None,
        team: int = TEAM_CAT,
        magnification: int = 100
    ) -> Optional[SimUnit]:
        """Directly spawns a custom unit onto `team` (Team 0 Right or Team 1 Left)."""
        if self.game_over:
            return None
        if isinstance(unit_or_team, int) and isinstance(spec, dict):
            actual_team = unit_or_team
            actual_spec = spec
            unit_key = str(spec.get("id", spec.get("name", "custom_unit")))
        elif isinstance(unit_or_team, dict):
            actual_team = team
            actual_spec = unit_or_team
            unit_key = str(actual_spec.get("id", actual_spec.get("name", "custom_unit")))
        else:
            actual_team = team
            unit_key = str(unit_or_team)
            actual_spec = CAT_REGISTRY.get(unit_key) or ENEMY_REGISTRY.get(unit_key)
            if actual_spec is None:
                return None

        slot = self.find_free_slot(actual_team)
        if slot is None:
            return None
        castle_x = self.get_castle(actual_team).pos_x
        spawn_x = (castle_x - 30) if actual_team == TEAM_CAT else (castle_x + 30)
        new_unit = SimUnit(team=actual_team, slot=slot, unit_key=unit_key, spec=actual_spec, pos_x=spawn_x, magnification=magnification)
        self.units[actual_team][slot] = new_unit
        self._emit_event({"type": "spawn", "x": spawn_x, "team": actual_team, "name": new_unit.name})
        return new_unit

    def spawn_enemy(self, enemy_info: Union[str, int, Dict[str, Any]], magnification: int = 100) -> bool:
        """Spawns an enemy into Team 1."""
        slot = self.find_free_slot(TEAM_ENEMY)
        if slot is None:
            return False

        if isinstance(enemy_info, dict):
            e_key = enemy_info.get("key")
            e_id = enemy_info.get("id")
            mag = enemy_info.get("mag", magnification)
            boss = enemy_info.get("boss", 0)
            custom_spec = enemy_info.get("spec")
        else:
            e_key = str(enemy_info) if isinstance(enemy_info, str) else None
            e_id = enemy_info if isinstance(enemy_info, int) else None
            mag = magnification
            boss = 0
            custom_spec = None

        spec = custom_spec
        if spec is None:
            if e_id is not None and e_id in ENEMY_REGISTRY:
                spec = ENEMY_REGISTRY[e_id]
            elif e_key is not None and e_key in ENEMY_REGISTRY:
                spec = ENEMY_REGISTRY[e_key]
            elif e_key is not None and e_key in CAT_REGISTRY:
                spec = CAT_REGISTRY[e_key]

        if spec is None:
            return False

        # Enemies spawn in front of Enemy Castle on LEFT (marching RIGHT)
        spawn_x = self.get_castle(TEAM_ENEMY).pos_x + 30
        actual_key = e_key or ROW_TO_ENEMY_KEY.get(e_id, f"enemy_{e_id}")
        new_unit = SimUnit(team=TEAM_ENEMY, slot=slot, unit_key=actual_key, spec=spec, pos_x=spawn_x, magnification=mag)
        if boss > 0:
            new_unit.is_boss = True
        self.units[TEAM_ENEMY][slot] = new_unit

        # Authentic Ponos Boss Shockwave: when a boss wave triggers, push all Cats back
        if boss > 0:
            for cat_slot in range(1, MAX_UNITS_PER_TEAM):
                cat = self.units[TEAM_CAT][cat_slot]
                if cat is not None and cat.is_alive:
                    self._knockback(cat, "boss")
            self._emit_event({"type": "boss_shockwave", "x": spawn_x, "boss_name": spec["name"], "boss_flag": boss})

        self._emit_event({"type": "spawn", "x": spawn_x, "team": TEAM_ENEMY, "name": spec["name"]})
        return True

    def step(self):
        """Advances battle by 1 simulation frame (1/30 second)."""
        if self.game_over:
            return

        self._in_step = True
        self.current_frame += 1
        self.visual_events = list(self._pending_visual_events)
        self._pending_visual_events.clear()

        if self.slow_motion_timer > 0:
            self.slow_motion_timer -= 1

        # Check temporary entity lifetimes (e.g. Iron Wall Cat)
        for t_id in (TEAM_CAT, TEAM_ENEMY):
            for s_idx in range(1, MAX_UNITS_PER_TEAM):
                un = self.units[t_id][s_idx]
                if un and un.is_alive and getattr(un, "lifetime", None) is not None:
                    un.lifetime -= 1
                    if un.lifetime <= 0:
                        un.hp = 0
                        un.is_alive = False
                        self.units[t_id][s_idx] = None
                        self._emit_event({"type": "death", "x": un.pos_x, "team": un.team, "name": un.name})

        for t_id in (TEAM_CAT, TEAM_ENEMY):
            if self.cannon_anim_timer.get(t_id, 0) > 0:
                self.cannon_anim_timer[t_id] -= 1

        # Update active sweeping Cat Cannon shockwaves with progressive wavefront
        remaining_waves = []
        for cw in self.active_cannon_waves:
            cw["timer"] -= 1
            cw["elapsed"] = cw.get("elapsed", 0) + 1
            if cw["timer"] > 0:
                opp_t = cw["opp_team"]
                opp_dir = -1 if opp_t == TEAM_CAT else 1
                # Sweeping wavefront advances across 14 frames matching visual pillars
                progress = min(1.0, cw["elapsed"] / 14.0)
                current_reach_x = cw["start_x"] + cw["direction"] * (cw["dist"] * progress)
                span_min_x = min(cw["start_x"], current_reach_x)
                span_max_x = max(cw["start_x"], current_reach_x)
                for slot in range(1, MAX_UNITS_PER_TEAM):
                    u = self.units[opp_t][slot]
                    if u and u.is_alive and not getattr(u, 'is_burrowing', False) and not getattr(u, 'is_corpse', False) and not getattr(u, 'is_warped', False) and not u.is_in_knockback() and id(u) not in cw["hit_ids"]:
                        u_front = u.pos_x - opp_dir * getattr(u, "hitbox_front", 0)
                        if span_min_x <= u_front <= span_max_x or span_min_x <= u.pos_x <= span_max_x:
                            self._cannon_hit(u, opp_t, ctype=cw.get("cannon_type", "normal"))
                            cw["hit_ids"].add(id(u))
                cw["prev_reach_x"] = current_reach_x
                remaining_waves.append(cw)
        self.active_cannon_waves = remaining_waves

        # Advance travelling wave attacks (one segment every WAVE_SEG_FRAMES frames)
        self._update_waves()

        # Advance surge attacks (15f spawn delay, ticks every 20f, 10f linger)
        self._update_surges()

        # Advance 3-ring explosion attacks (native 0x4c3f24)
        self._update_explosions()

        # 1. Update Team 0 wallet, cooldowns & cannon charging
        self.wallet.update()
        self.deployer.update()
        if self.cannon_charge < self.cannon_max_charge:
            self.cannon_charge += 1

        # 2. Update Team 1: PvP Player 2 economy OR Stage Wave Spawner
        if self.pvp_mode:
            self.p2_wallet.update()
            self.p2_deployer.update()
            if self.p2_cannon_charge < self.p2_cannon_max_charge:
                self.p2_cannon_charge += 1
        else:
            enemy_castle = self.get_castle(TEAM_ENEMY)
            active_enemy_count = len([u for u in self.units[TEAM_ENEMY] if u is not None and u.slot != SLOT_CASTLE and u.is_alive])
            enemies_to_spawn = self.spawner.update(
                self.current_frame, enemy_castle.hp, enemy_castle.max_hp, current_active_enemies=active_enemy_count
            )
            for spawn_req in enemies_to_spawn:
                self.spawn_enemy(spawn_req)

        # 3. Outer movement loop [1..50]
        self._update_movement()

        # 4. Attack, windup, backswing & damage resolution
        self._update_attacks()

        # 5. Death & victory checking
        self._update_deaths_and_castles()
        self._in_step = False

    def _can_unit_target_opponent(self, attacker: SimUnit, opp: SimUnit) -> bool:
        """Checks Attacks Only ability restriction (col 32), Soulstrike (col 98), and un-targetable states."""
        if opp.slot == SLOT_CASTLE:
            return True
        if getattr(opp, "is_corpse", False):
            # Native Soulstrike (col 98): allows targeting un-revived Zombie corpses
            return bool(not attacker.is_cursed() and attacker.abilities.get("soul_strike", False))
        if getattr(opp, "is_burrowing", False) or getattr(opp, "is_dying", False) or getattr(opp, "is_warped", False) or opp.is_in_knockback():
            return False
        if not attacker.is_cursed() and attacker.abilities.get("attacks_only", False) and attacker.target_traits:
            return any(t in opp.traits for t in attacker.target_traits)
        return True

    def _get_target_interval(self, opp: SimUnit) -> Tuple[float, float]:
        """Returns target [t_min, t_max] hurtbox bounds matching native CheckRange (0x5ce084)."""
        if opp.slot == SLOT_CASTLE:
            return float(opp.pos_x), float(opp.pos_x)
        opp_dir = -1 if opp.team == TEAM_CAT else 1
        edge_front = opp.pos_x - opp_dir * getattr(opp, "hitbox_front", 0)
        edge_back = opp.pos_x - opp_dir * getattr(opp, "hitbox_back", 320)
        return min(edge_front, edge_back), max(edge_front, edge_back)

    def _is_opp_in_standing_range(self, attacker: SimUnit, opp: SimUnit, direction: int) -> Tuple[bool, float]:
        """Native CheckRange (0x5ce084, state != atk/LD): checks standing attack span vs target hurtbox."""
        t_min, t_max = self._get_target_interval(opp)
        e1 = attacker.pos_x
        e2 = attacker.pos_x + direction * attacker.attack_range
        atk_min, atk_max = min(e1, e2), max(e1, e2)
        dist = (opp.pos_x - attacker.pos_x) * direction
        return (atk_min <= t_max and atk_max >= t_min), dist

    def _is_opp_in_attack_box(self, attacker: SimUnit, opp: SimUnit, direction: int, hit_spec: Optional[dict] = None) -> Tuple[bool, float]:
        """Native CheckRange (0x5ce084): Normal attack checks target hurtbox; LD/Omni checks target center pos_x."""
        dist = (opp.pos_x - attacker.pos_x) * direction
        ld_start = attacker.abilities.get("ld_start", 0)
        ld_range = attacker.abilities.get("ld_range", 0)
        if hit_spec is not None and hit_spec.get("ld_range", 0) != 0:
            ld_start = hit_spec.get("ld_start", ld_start)
            ld_range = hit_spec.get("ld_range", ld_range)
        if ld_range != 0:
            p1 = attacker.pos_x + direction * ld_start
            p2 = attacker.pos_x + direction * (ld_start + ld_range)
            atk_min, atk_max = min(p1, p2), max(p1, p2)
            if opp.slot == SLOT_CASTLE:
                # Castle extends behind its front line so LD/Omni units standing near the castle still hit it
                c_min, c_max = (-2000.0, float(opp.pos_x)) if opp.team == TEAM_ENEMY else (float(opp.pos_x), float(self.battlefield_length) + 2000.0)
                return (atk_min <= c_max and atk_max >= c_min), dist
            # Native 0x5ce084 L144-166: LD/Omni checks opp center pos_x (+0x83914)
            return (atk_min <= opp.pos_x <= atk_max), dist
        t_min, t_max = self._get_target_interval(opp)
        e1 = attacker.pos_x
        e2 = attacker.pos_x + direction * attacker.attack_range
        atk_min, atk_max = min(e1, e2), max(e1, e2)
        return (atk_min <= t_max and atk_max >= t_min), dist

    def _update_movement(self):
        """
        Moves units that are not in knockback, not frozen, not in attack animation (foreswing/backswing),
        and not within standing range of a valid opponent.
        Matches 0x005c8ad4 (Outer Loop 1..50), 0x005c8b5c (Attack state lock), and 0x0095a494 (CanMove).
        """
        c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
        c_cat_x = self.get_castle(TEAM_CAT).pos_x
        for team in (TEAM_CAT, TEAM_ENEMY):
            opp_team = 1 - team
            # Team 0 (Right Castle) marches LEFT (direction = -1).
            # Team 1 (Left Castle) marches RIGHT (direction = +1).
            direction = -1 if team == TEAM_CAT else 1

            opp_units = [u for u in self.units[opp_team] if u is not None and u.is_alive]

            for slot in range(1, MAX_UNITS_PER_TEAM):
                unit = self.units[team][slot]
                if unit is None or (not unit.is_alive and not unit.is_dying):
                    continue

                # Knockback handling: native ARM64 KB trajectories in Battle Cats DU
                if unit.is_in_knockback():
                    unit.is_moving = False
                    unit.is_winding_up = False
                    unit.windup_timer = 0
                    unit.backswing_timer = 0
                    total = max(1, unit.kb_total_frames or KNOCKBACK_FRAMES_DEFAULT)
                    dist = unit.kb_distance or KB_SPECS["kb"][0]
                    unit.kb_elapsed += 1
                    unit.knockback_timer -= 1
                    kind = getattr(unit, "kb_kind", "")
                    if kind == "kb":
                        # Native 0x5dd600 State 6: decelerating ((12 - elapsed) % 12) * 10 native units (/4 -> DU), horizontal only
                        base_native = ((12 - unit.kb_elapsed) % 12) * 10
                        tr_permille = getattr(unit, "kb_tr_permille", 0)
                        dx = ((base_native * tr_permille) // 1000 + base_native) / 4.0
                        unit.kb_y = 0.0
                    elif unit.kb_elapsed < total:
                        # Native 0x8b95b4 / 0x5dbc18: States 3 (24f), 5 (12f), 7 (48f) move on ticks 1..total-1 and exit on final tick when frame wraps to 0
                        dx = dist / float(max(1, total - 1))
                        if kind == "hitback":
                            arc_idx = min(len(KB_Y_ARC_STATE3_DU) - 1, unit.kb_elapsed)
                            unit.kb_y += KB_Y_ARC_STATE3_DU[arc_idx]
                        elif kind == "boss":
                            arc_idx = min(len(KB_Y_ARC_STATE3_DU) - 1, unit.kb_elapsed >> 1)
                            unit.kb_y += KB_Y_ARC_STATE3_DU[arc_idx]
                        elif kind == "cannon":
                            arc_idx = min(len(KB_Y_ARC_STATE5_DU) - 1, unit.kb_elapsed)
                            unit.kb_y += KB_Y_ARC_STATE5_DU[arc_idx]
                        else:
                            unit.kb_y = 0.0
                    else:
                        dx = 0.0
                        unit.kb_y = 0.0
                    unit.pos_x -= direction * dx
                    unit.pos_x = max(c_enemy_x, min(c_cat_x, unit.pos_x))
                    if unit.knockback_timer <= 0:
                        unit.kb_y = 0.0
                    if unit.anim_state != "kb":
                        unit.anim_state = "kb"
                        unit.anim_frame = 0.0
                    else:
                        unit.anim_frame += 1.0
                    continue
                unit.kb_y = 0.0

                if unit.is_dying or getattr(unit, "is_corpse", False):
                    continue

                # Starred Alien warp handling
                if getattr(unit, "is_warped", False):
                    unit.warp_timer -= 1
                    unit.is_moving = False
                    if unit.warp_timer <= 0:
                        unit.is_warped = False
                        unit.pos_x = max(c_enemy_x, min(c_cat_x, unit.warp_dest_x))
                        unit.anim_state = "walk"
                        unit.anim_frame = 0.0
                        self._emit_event({"type": "warp_exit", "x": unit.pos_x, "team": unit.team})
                    continue

                # Status timers update
                if unit.weaken_timer > 0:
                    unit.weaken_timer -= 1
                if unit.dodge_timer > 0:
                    unit.dodge_timer -= 1
                if unit.curse_timer > 0:
                    unit.curse_timer -= 1

                # Zombie burrowing logic
                if getattr(unit, "is_burrowing", False):
                    unit.burrow_timer -= 1
                    unit.pos_x += direction * (unit.speed * 0.5)
                    unit.pos_x = max(c_enemy_x, min(c_cat_x, unit.pos_x))
                    if unit.burrow_timer <= 0:
                        unit.is_burrowing = False
                        unit.anim_state = "walk"
                        unit.anim_frame = 0.0
                        self._emit_event({"type": "burrow_up", "x": unit.pos_x, "team": unit.team})
                    continue

                if unit.freeze_timer > 0:
                    unit.is_moving = False
                    continue

                if unit.slow_timer > 0:
                    unit.slow_timer -= 1

                # Authentic Ponos rule (0x005c8b5c): Unit cannot move while in foreswing or backswing!
                if unit.is_in_attack_anim():
                    unit.is_moving = False
                    continue

                # Find closest valid target ahead within standing attack_range (native 0x5ce084)
                closest_dist = 999999
                target_ahead = False
                in_standing_range = False
                for opp in opp_units:
                    if not self._can_unit_target_opponent(unit, opp):
                        continue
                    in_stand, dist = self._is_opp_in_standing_range(unit, opp, direction)
                    if in_stand:
                        in_standing_range = True
                    if dist >= 0:
                        target_ahead = True
                        if dist < closest_dist:
                            closest_dist = dist

                # Trigger burrow when encountering enemies in front
                if getattr(unit, "burrow_left", 0) > 0 and (in_standing_range or (target_ahead and closest_dist <= (unit.attack_range + 50))):
                    unit.is_burrowing = True
                    unit.burrow_left -= 1
                    unit.burrow_timer = 50
                    unit.anim_state = "burrow"
                    unit.anim_frame = 0.0
                    self._emit_event({"type": "burrow_down", "x": unit.pos_x, "team": unit.team})
                    continue

                # If any valid target hurtbox overlaps standing attack_range, stop and prepare/wait for attack
                if in_standing_range:
                    unit.is_moving = False
                    if unit.anim_state != "idle":
                        unit.anim_state = "idle"
                        unit.anim_frame = 0.0
                    else:
                        unit.anim_frame += 1.0
                else:
                    # Free to advance [VERIFIED 0x0095a494 return 1]
                    # In PONOS Battle Cats CSV, speed is in half-pixels per frame (speed * 0.5 at 30 FPS)
                    unit.is_moving = True
                    effective_speed = SLOW_SPEED_DU_PER_FRAME if unit.is_slowed() else max(0.5, unit.speed * 0.5)
                    unit.pos_x += direction * effective_speed
                    unit.pos_x = max(c_enemy_x, min(c_cat_x, unit.pos_x))
                    if unit.anim_state != "walk":
                        unit.anim_state = "walk"
                        unit.anim_frame = 0.0
                    else:
                        unit.anim_frame += 1.0

    def _roll_swing_procs(self, attacker: SimUnit) -> Dict[str, bool]:
        """Native ARM64 0x5da618: pre-rolls all proc abilities ONCE per attack hit execution
        and shares those booleans across every target, Wave segment, Surge tick, and Explosion ring."""
        ab = attacker.abilities
        def _roll(key: str) -> bool:
            prob = ab.get(key, 0)
            return bool(prob > 0 and self.rng.randint(1, 100) <= prob)
        bb_prob = ab.get("barrier_break_prob", ab.get("barrier_breaker_prob", 0))
        return {
            "kb": _roll("kb_prob"),
            "freeze": _roll("freeze_prob") if ab.get("freeze_dur", 0) > 0 else False,
            "slow": _roll("slow_prob") if ab.get("slow_dur", 0) > 0 else False,
            "crit": _roll("crit_prob"),
            "weaken": _roll("weaken_prob") if ab.get("weaken_dur", 0) > 0 else False,
            "savage": _roll("savage_blow_prob"),
            "warp": _roll("warp_prob"),
            "barrier_break": bool(bb_prob > 0 and self.rng.randint(1, 100) <= bb_prob),
            "curse": _roll("curse_prob") if ab.get("curse_dur", 0) > 0 else False,
            "toxic": _roll("toxic_prob") if ab.get("toxic_hp_pct", 0) > 0 else False,
            "shield_pierce": _roll("shield_pierce_prob"),
        }

    def _update_attacks(self):
        """
        Native PONOS Attack State Machine (0x5cbf84, 0x5dbc18, 0x5d9d6c, 0x5da618):
        1. If frozen, pause attack windup/backswing and TBA without cancelling windup.
        2. When not winding up, decrement TBA (`tba_timer`) and finish any active backswing.
        3. On hit frame (`windup_timer == 0`): pre-roll `0x5da618` procs once, apply hits, roll Wave/Surge/Explosion once;
           on last hit (`atk_idx == hit_count - 1`), set `tba_timer` overlapping `backswing_timer`.
        4. Initiate next windup once `backswing_timer == 0 and tba_timer == 0` and target is in standing range.
        """
        for team in (TEAM_CAT, TEAM_ENEMY):
            opp_team = 1 - team
            direction = -1 if team == TEAM_CAT else 1

            for slot in range(1, MAX_UNITS_PER_TEAM):
                unit = self.units[team][slot]
                if unit is None or not unit.is_alive or unit.is_in_knockback():
                    continue
                if unit.freeze_timer > 0:
                    unit.freeze_timer -= 1
                    continue

                # Native 0x5dbc18: TBA starts on last hit frame and decrements while not in windup
                if not unit.is_winding_up and unit.tba_timer > 0:
                    unit.tba_timer -= 1

                # Phase A: Backswing completion
                if unit.backswing_timer > 0:
                    unit.backswing_timer -= 1
                    if unit.backswing_timer > 0:
                        unit.anim_frame += 1.0
                        unit.cycle_timer = max(unit.backswing_timer, unit.tba_timer)
                        unit.attack_cooldown_timer = unit.cycle_timer
                        continue
                    # Native 0x5c8ae0: exit attack anim cleanly on final backswing tick
                    # and fall through to Phase C so tba_timer==0 units re-attack immediately
                    # without emitting a 1-frame idle(alen) glitch.
                    unit.anim_state = "idle"
                    unit.anim_frame = 0.0
                    unit.cycle_timer = unit.tba_timer
                    unit.attack_cooldown_timer = unit.tba_timer

                # Phase B: Foreswing (Windup) execution - committed once started (paused by Freeze, cancelled only by KB/Warp/Death)
                if unit.is_winding_up:
                    unit.windup_timer -= 1
                    unit.anim_frame += 1.0
                    if unit.windup_timer <= 0:
                        hit_idx = min(unit.current_hit_idx, len(unit.attack_hits) - 1)
                        hit_spec = unit.attack_hits[hit_idx]
                        hit_damage = unit.attack_power if len(unit.attack_hits) == 1 else int(hit_spec.get("damage", unit.attack_power))
                        use_ability = bool(hit_spec.get("use_ability", True))

                        # Collect targets currently inside this hit's attack hitbox (0x5ce084)
                        targets_in_hitbox = []
                        for opp in self.units[opp_team]:
                            if opp is not None and opp.is_alive and self._can_unit_target_opponent(unit, opp):
                                in_box, dist = self._is_opp_in_attack_box(unit, opp, direction, hit_spec=hit_spec)
                                if in_box:
                                    targets_in_hitbox.append((dist, opp))

                        targets_in_hitbox.sort(key=lambda t: t[0])

                        if targets_in_hitbox:
                            # Native 0x5da618: pre-roll proc booleans ONCE for this hit execution
                            swing_procs = self._roll_swing_procs(unit) if use_ability else None

                            if unit.target_type == "area":
                                for _, target_entity in targets_in_hitbox:
                                    self._apply_damage(
                                        attacker=unit,
                                        target=target_entity,
                                        direction=direction,
                                        hit_damage=hit_damage,
                                        use_ability=use_ability,
                                        swing_procs=swing_procs,
                                    )
                            else:
                                # Native ARM64 0x5c9158..0x5c92e4: random tie-break among equidistant closest targets
                                min_dist = targets_in_hitbox[0][0]
                                tied_targets = [t[1] for t in targets_in_hitbox if abs(t[0] - min_dist) < 1e-6]
                                primary_target: SimUnit = (
                                    self.rng.choice(tied_targets) if len(tied_targets) > 1 else tied_targets[0]
                                )
                                self._apply_damage(
                                    attacker=unit,
                                    target=primary_target,
                                    direction=direction,
                                    hit_damage=hit_damage,
                                    use_ability=use_ability,
                                    swing_procs=swing_procs,
                                )

                            # Wave, Surge & Explosion attack proc once per swing on ability-enabled hits
                            if use_ability:
                                wave_prob = unit.abilities.get("wave_prob", 0)
                                wave_lvl = unit.abilities.get("wave_lvl", 0)
                                if wave_prob > 0 and wave_lvl > 0 and self.rng.randint(1, 100) <= wave_prob:
                                    self._trigger_wave_attack(unit, opp_team, direction, wave_lvl, hit_damage=hit_damage, swing_procs=swing_procs)

                                surge_prob = unit.abilities.get("surge_prob", 0)
                                surge_lvl = unit.abilities.get("surge_lvl", 0)
                                if surge_prob > 0 and surge_lvl > 0 and self.rng.randint(1, 100) <= surge_prob:
                                    self._trigger_surge_attack(unit, opp_team, direction, surge_lvl, hit_damage=hit_damage, swing_procs=swing_procs)

                                exp_prob = unit.abilities.get("explosion_prob", 0)
                                if exp_prob > 0 and self.rng.randint(1, 100) <= exp_prob:
                                    self._trigger_explosion_attack(unit, opp_team, direction, hit_damage=hit_damage, swing_procs=swing_procs)

                        # Advance to next hit in multi-hit combo or transition to backswing + start TBA
                        if unit.current_hit_idx + 1 < len(unit.attack_hits):
                            next_hit = unit.attack_hits[unit.current_hit_idx + 1]
                            cur_pre = int(hit_spec.get("foreswing", unit.attack_windup))
                            next_pre = int(next_hit.get("foreswing", cur_pre + 1))
                            unit.current_hit_idx += 1
                            unit.windup_timer = max(1, next_pre - cur_pre)
                        else:
                            last_pre = int(hit_spec.get("foreswing", unit.attack_windup))
                            unit.is_winding_up = False
                            unit.current_hit_idx = 0
                            unit.backswing_timer = max(1, unit.attack_anim_len - last_pre)
                            unit.tba_timer = max(0, int(unit.attack_interval) - 1)
                            unit.cycle_timer = max(unit.backswing_timer, unit.tba_timer)
                            unit.attack_cooldown_timer = unit.cycle_timer
                            # Native Kamikaze (col 55=1, col 58=2): self-destruct after completing attack (e.g. Stone/Glass/Doron)
                            if unit.abilities.get("kamikaze", False):
                                unit.hp = 0
                                unit.is_alive = False
                                unit.is_dying = True
                                unit.backswing_timer = 0
                                self._emit_event({"type": "death", "x": unit.pos_x, "team": unit.team, "name": unit.name})
                    continue

                unit.cycle_timer = unit.tba_timer
                unit.attack_cooldown_timer = unit.tba_timer

                # Phase C: Idle / Walking & Attack Initiation (once backswing & TBA have elapsed)
                has_target_in_standing_range = False
                for opp in self.units[opp_team]:
                    if opp is not None and opp.is_alive and self._can_unit_target_opponent(unit, opp):
                        in_stand, _ = self._is_opp_in_standing_range(unit, opp, direction)
                        if in_stand:
                            has_target_in_standing_range = True
                            break

                if has_target_in_standing_range and unit.tba_timer <= 0:
                    unit.is_moving = False
                    unit.is_winding_up = True
                    unit.current_hit_idx = 0
                    first_pre = int(unit.attack_hits[0].get("foreswing", unit.attack_windup)) if unit.attack_hits else unit.attack_windup
                    unit.windup_timer = max(1, first_pre)
                    unit.anim_state = "attack"
                    unit.anim_frame = 0.0
                    unit.attack_cycle_id += 1
                    self._emit_event({
                        "type": "windup", "x": unit.pos_x, "team": unit.team
                    })

    def _trigger_wave_attack(
        self,
        attacker: SimUnit,
        opp_team: int,
        direction: int,
        wave_lvl: int,
        hit_damage: Optional[int] = None,
        swing_procs: Optional[Dict[str, bool]] = None,
    ):
        """Spawns an authentic travelling PONOS wave: `wave_lvl` segments, each WAVE_PROG further,
        appearing every WAVE_SEG_FRAMES frames; each target can only be hit once per wave."""
        is_enemy_spec = attacker.key in ENEMY_REGISTRY and attacker.key not in CAT_REGISTRY
        ini = WAVE_INI_ENEMY if is_enemy_spec else WAVE_INI_CAT
        width = WAVE_WIDTH_ENEMY if is_enemy_spec else WAVE_WIDTH_CAT
        origin = float(attacker.pos_x)
        is_mini_wave = attacker.abilities.get("mini_wave", False)
        base_dmg = attacker.attack_power if hit_damage is None else hit_damage
        w_dmg = max(1, base_dmg // 5) if is_mini_wave else base_dmg
        w_seg_frames = max(1, WAVE_SEG_FRAMES // 2) if is_mini_wave else WAVE_SEG_FRAMES
        wave = {
            "attacker": attacker,
            "opp_team": opp_team,
            "dir": direction,
            "origin": origin,
            "ini": ini,
            "width": width,
            "lvl": int(wave_lvl),
            "seg": 0,
            "timer": 0,
            "dmg": w_dmg,
            "hit": set(),
            "seg_frames": w_seg_frames,
            "is_mini": is_mini_wave,
            "swing_procs": swing_procs,
        }
        self._emit_event({
            "type": "wave_attack",
            "x": origin,
            "start_x": origin,
            "end_x": origin + direction * (ini + WAVE_PROG / 2 + WAVE_PROG * (wave_lvl - 1)),
            "direction": direction,
            "seg0": ini + WAVE_PROG / 2,
            "prog": WAVE_PROG,
            "seg_frames": w_seg_frames,
            "team": attacker.team,
            "level": wave_lvl,
            "is_mini": is_mini_wave,
        })
        # First segment lands on the same frame as the hit
        self._advance_wave(wave)
        if wave["seg"] < wave["lvl"]:
            self.active_waves.append(wave)

    def _advance_wave(self, wave: Dict[str, Any]):
        center = wave["origin"] + wave["dir"] * (wave["ini"] + WAVE_PROG / 2 + WAVE_PROG * wave["seg"])
        half = wave["width"] / 2.0
        w_min, w_max = center - half, center + half
        blocked = False
        procs = wave.get("swing_procs")
        for opp in self.units[wave["opp_team"]]:
            if opp is None or not opp.is_alive or opp.slot == SLOT_CASTLE or id(opp) in wave["hit"] or opp.is_in_knockback() or getattr(opp, 'is_burrowing', False) or getattr(opp, 'is_corpse', False) or getattr(opp, 'is_warped', False):
                continue
            t_min, t_max = self._get_target_interval(opp)
            if w_min <= t_max and w_max >= t_min:
                if opp.abilities.get("wave_blocker", False):
                    blocked = True
                    self._emit_event({
                        "type": "wave_block",
                        "x": opp.pos_x,
                        "team": opp.team,
                    })
                    # Authentic PONOS: Wave Blocker intercepts and deletes wave, but takes the wave damage unless wave_immune
                    if not opp.abilities.get("wave_immune", False):
                        wave["hit"].add(id(opp))
                        self._apply_damage(attacker=wave["attacker"], target=opp, direction=wave["dir"], is_wave=True, hit_damage=wave["dmg"], swing_procs=procs)
                    break
                if opp.abilities.get("wave_immune", False):
                    continue
                wave["hit"].add(id(opp))
                self._apply_damage(attacker=wave["attacker"], target=opp, direction=wave["dir"], is_wave=True, hit_damage=wave["dmg"], swing_procs=procs)
        if blocked:
            wave["seg"] = wave["lvl"]  # Extinguishes wave immediately upon hitting Wave Blocker
        else:
            wave["seg"] += 1
            wave["timer"] = wave.get("seg_frames", WAVE_SEG_FRAMES)

    def _update_waves(self):
        remaining = []
        for wave in self.active_waves:
            wave["timer"] -= 1
            if wave["timer"] <= 0:
                self._advance_wave(wave)
            if wave["seg"] < wave["lvl"]:
                remaining.append(wave)
        self.active_waves = remaining

    def _trigger_surge_attack(
        self,
        attacker: SimUnit,
        opp_team: int,
        direction: int,
        surge_lvl: int,
        hit_damage: Optional[int] = None,
        swing_procs: Optional[Dict[str, bool]] = None,
    ):
        """Spawns an authentic PONOS Surge Attack (0x4c32c4): 15f delay, 20f interval x L ticks,
        10f linger, width 375 DU (half=187.5 DU), -62.5 DU hitbox offset."""
        s_start = int(attacker.abilities.get("surge_start", 150))
        s_range = int(attacker.abilities.get("surge_range", 300))
        rnd_offset = self.rng.randint(0, max(0, s_range))
        surge_x = attacker.pos_x + direction * (s_start + rnd_offset)
        c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
        c_cat_x = self.get_castle(TEAM_CAT).pos_x
        surge_x = max(c_enemy_x, min(c_cat_x, surge_x))

        is_mini_surge = attacker.abilities.get("mini_surge", False)
        base_dmg = attacker.attack_power if hit_damage is None else hit_damage
        dmg = max(1, base_dmg // 5) if is_mini_surge else base_dmg
        self._surge_id_seq += 1
        lvl = max(1, int(surge_lvl))
        total_frames = 15 + 20 * lvl + 10
        surge = {
            "id": self._surge_id_seq,
            "attacker": attacker,
            "proc_attacker": attacker,
            "opp_team": opp_team,
            "dir": direction,
            "x": surge_x,
            "lvl": lvl,
            "s_start": s_start,
            "s_range": s_range,
            "elapsed": 0,
            "timer": total_frames,
            "dmg": dmg,
            "radius": 187.5,
            "is_mini": is_mini_surge,
            "swing_procs": swing_procs,
        }
        self.active_surges.append(surge)
        self._emit_event({
            "type": "surge",
            "x": surge_x,
            "team": attacker.team,
            "level": lvl,
            "delay": 15,
            "duration": 20 * lvl,
            "total_duration": total_frames,
            "is_mini": is_mini_surge,
        })

    def _apply_surge_hit(self, surge: Dict[str, Any]):
        sx = surge["x"]
        r = surge["radius"]  # 187.5 DU (half of 375 DU)
        procs = surge.get("swing_procs")
        dmg_attacker = surge.get("proc_attacker", surge["attacker"])
        c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
        c_cat_x = self.get_castle(TEAM_CAT).pos_x
        for opp in self.units[surge["opp_team"]]:
            if opp is None or not opp.is_alive or opp.slot == SLOT_CASTLE or opp.is_in_knockback() or getattr(opp, 'is_burrowing', False) or getattr(opp, 'is_corpse', False) or getattr(opp, 'is_warped', False):
                continue
            if opp.abilities.get("surge_immune", False):
                continue
            # Native 0x4c32c4 L188-212: target front edge shifted by -62.5 DU along target's facing
            opp_dir = -1 if opp.team == TEAM_CAT else 1
            opp_front = opp.pos_x - opp_dir * getattr(opp, "hitbox_front", 0)
            edge_x = opp_front - opp_dir * 62.5
            if (sx - r) <= edge_x < (sx + r):
                self._apply_damage(
                    attacker=dmg_attacker,
                    target=opp,
                    direction=surge["dir"],
                    is_surge=True,
                    hit_damage=surge["dmg"],
                    use_ability=True,
                    swing_procs=procs,
                )
                # Native 0x5db2c0 L375-447: Counter-Surge reflects original surge attributes ONCE per surge ID
                if (
                    opp.is_alive
                    and opp.abilities.get("surge_counter", False)
                    and not opp.is_cursed()
                    and not surge.get("is_counter", False)
                    and surge["id"] not in opp.countered_surge_ids
                ):
                    opp.countered_surge_ids.add(surge["id"])
                    c_lvl = surge["lvl"]
                    opp_opp_team = 1 - opp.team
                    opp_dir_out = -surge["dir"]
                    orig_start = int(surge.get("s_start", 150))
                    orig_range = int(surge.get("s_range", 300))
                    rnd_off = self.rng.randint(0, max(0, orig_range))
                    counter_x = max(c_enemy_x, min(c_cat_x, opp.pos_x + opp_dir_out * (orig_start + rnd_off)))
                    self._surge_id_seq += 1
                    total_frames = 15 + 20 * c_lvl + 10
                    self._emit_event({"type": "surge_counter", "x": opp.pos_x, "team": opp.team})
                    counter_surge = {
                        "id": self._surge_id_seq,
                        "attacker": opp,
                        "proc_attacker": dmg_attacker,
                        "opp_team": opp_opp_team,
                        "dir": opp_dir_out,
                        "x": counter_x,
                        "lvl": c_lvl,
                        "s_start": orig_start,
                        "s_range": orig_range,
                        "elapsed": 0,
                        "timer": total_frames,
                        "dmg": surge["dmg"],
                        "radius": 187.5,
                        "is_counter": True,
                        "swing_procs": procs,
                    }
                    self.active_surges.append(counter_surge)
                    self._emit_event({
                        "type": "surge",
                        "x": counter_surge["x"],
                        "team": opp.team,
                        "level": c_lvl,
                        "delay": 15,
                        "duration": 20 * c_lvl,
                        "total_duration": total_frames,
                        "is_mini": bool(surge.get("is_mini", False)),
                        "is_counter": True,
                    })

    def _update_surges(self):
        remaining = []
        for s in self.active_surges:
            s["elapsed"] += 1
            s["timer"] -= 1
            e = s["elapsed"]
            if e >= 15 and (e - 15) % 20 == 0:
                tick_idx = (e - 15) // 20
                if tick_idx < s["lvl"]:
                    self._apply_surge_hit(s)
            if s["timer"] > 0:
                remaining.append(s)
        self.active_surges = remaining

    def _trigger_explosion_attack(
        self,
        attacker: SimUnit,
        opp_team: int,
        direction: int,
        hit_damage: Optional[int] = None,
        swing_procs: Optional[Dict[str, bool]] = None,
    ):
        """Spawns an authentic PONOS Explosion (0x4c3f24): 3 expanding rings spaced by 15f,
        widths (200, 125, 100) DU and damage (100%, 60%, 20%), hitting each target once across all 3 rings."""
        e_start = int(attacker.abilities.get("explosion_start", 0))
        e_width = int(attacker.abilities.get("explosion_width", 0))
        rnd = (self.rng.randint(0, e_width) - (e_width // 2)) if e_width > 0 else 0
        c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
        c_cat_x = self.get_castle(TEAM_CAT).pos_x
        origin_x = max(c_enemy_x, min(c_cat_x, float(attacker.pos_x + direction * e_start + rnd)))
        base_dmg = attacker.attack_power if hit_damage is None else hit_damage
        exp = {
            "attacker": attacker,
            "opp_team": opp_team,
            "dir": direction,
            "x": origin_x,
            "tick": 0,
            "dmg": base_dmg,
            "hit": set(),
            "swing_procs": swing_procs,
        }
        self.active_explosions.append(exp)
        self._emit_event({
            "type": "explosion",
            "x": origin_x,
            "team": attacker.team,
            "delay": 15,
            "duration": 65,
        })

    def _update_explosions(self):
        """Native ARM64 0x4c3f24: rings 0..2 activate every 15f (n_frame4=15), active [15, 35) age window."""
        widths_du = (200.0, 125.0, 100.0)
        dmg_pcts = (100, 60, 20)
        ring_offsets = (0.0, 162.5, 275.0)  # (w1+w2)/2=162.5 DU, (w1+2*w2+w3)/2=275.0 DU
        remaining = []
        for exp in self.active_explosions:
            tick = exp["tick"]
            ox = exp["x"]
            max_ring = min(3, 1 + tick // 15)
            procs = exp.get("swing_procs")
            for r in range(max_ring):
                age = tick - 15 * r
                if 15 <= age < 35:
                    half = widths_du[r] / 2.0
                    centers = (ox,) if r == 0 else (ox - ring_offsets[r], ox + ring_offsets[r])
                    ring_dmg = max(1, (exp["dmg"] * dmg_pcts[r]) // 100)
                    for opp in self.units[exp["opp_team"]]:
                        if (
                            opp is None
                            or not opp.is_alive
                            or opp.slot == SLOT_CASTLE
                            or id(opp) in exp["hit"]
                            or opp.is_in_knockback()
                            or getattr(opp, "is_burrowing", False)
                            or getattr(opp, "is_corpse", False)
                            or getattr(opp, "is_warped", False)
                            or opp.abilities.get("explosion_immune", False)
                        ):
                            continue
                        opp_dir = -1 if opp.team == TEAM_CAT else 1
                        opp_front = opp.pos_x - opp_dir * getattr(opp, "hitbox_front", 0)
                        if any((cx - half) <= opp_front < (cx + half) for cx in centers):
                            exp["hit"].add(id(opp))
                            self._apply_damage(
                                attacker=exp["attacker"],
                                target=opp,
                                direction=exp["dir"],
                                hit_damage=ring_dmg,
                                use_ability=True,
                                swing_procs=procs,
                            )
            exp["tick"] += 1
            if exp["tick"] < 80:
                remaining.append(exp)
        self.active_explosions = remaining

    def _apply_damage(
        self,
        attacker: SimUnit,
        target: SimUnit,
        direction: int = 1,
        is_wave: bool = False,
        is_surge: bool = False,
        hit_damage: Optional[int] = None,
        use_ability: bool = True,
        swing_procs: Optional[Dict[str, bool]] = None,
    ):
        """
        Applies damage using NativeDamageEngine magic division (`0x10624dd3`),
        authentic trait multipliers, once-per-swing proc rolls (`0x5da618`), Metal 1-damage rule, and status procs.
        Supports Curse, Toxic, Aku Shield, Colossus/Behemoth slayer, and Zombie mechanics.
        """
        if target.slot != SLOT_CASTLE and target.is_in_knockback():
            return

        def _proc_flag(proc_key: str, prob_key: str, default_prob: int = 0) -> bool:
            if not use_ability:
                return False
            if swing_procs is not None:
                return bool(swing_procs.get(proc_key, False))
            prob = attacker.abilities.get(prob_key, default_prob)
            return bool(prob > 0 and self.rng.randint(1, 100) <= prob)

        raw_damage = attacker.attack_power if hit_damage is None else hit_damage

        # 1. Strengthen ability when HP <= strengthen_hp_pct
        str_hp_pct = attacker.abilities.get("strengthen_hp_pct", 0)
        str_boost = attacker.abilities.get("strengthen_boost_pct", 0)
        if str_hp_pct > 0 and str_boost > 0 and attacker.hp <= (attacker.max_hp * str_hp_pct) // 100:
            raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 1000 + str_boost * 10)

        # 2. Weaken status effect on attacker
        if attacker.is_weakened():
            raw_damage = NativeDamageEngine.calculate_damage(raw_damage, attacker.weaken_pct * 10)

        # 3. Trait-targeted multipliers (disabled if attacker is cursed)
        attacker_cursed = attacker.is_cursed()
        target_cursed = target.is_cursed()

        attacker_tr = self.team_treasures.get(attacker.team, {})
        attacker_fruits = attacker_tr.get("trait_fruits", {})
        target_tr = self.team_treasures.get(target.team, {})
        target_fruits = target_tr.get("trait_fruits", {})

        trait_match = (target.slot != SLOT_CASTLE) and any(t in target.traits for t in attacker.target_traits)
        has_fruit_atk = any(attacker_fruits.get(t, False) for t in target.traits if t in attacker.target_traits) if (target.slot != SLOT_CASTLE) else False

        if target.slot == SLOT_CASTLE and attacker.abilities.get("base_destroyer", False):
            raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 4000) # 4x vs Castle
        elif trait_match and not attacker_cursed:
            if attacker.abilities.get("insane_damage", False):
                ins_permille = 6000 if has_fruit_atk else 5000
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, ins_permille) # 5x or 6x Insane Damage
            elif attacker.abilities.get("massive_damage", False):
                mas_permille = 4000 if has_fruit_atk else 3000
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, mas_permille) # 3x or 4x Massive Damage
            elif attacker.abilities.get("strong", False):
                str_permille = 1800 if has_fruit_atk else 1500
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, str_permille) # 1.5x or 1.8x Strong Against

        if target.slot != SLOT_CASTLE and not attacker_cursed:
            if "colossus" in target.traits and attacker.abilities.get("colossus_slayer", False):
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 1600)
            if "behemoth" in target.traits and attacker.abilities.get("behemoth_slayer", False):
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 2500)  # Official PONOS 2.5x damage against Behemoths

        # Target's own Insanely Tough / Resistant / Strong defense against attacker's traits (disabled if target is cursed)
        target_matches_attacker = (target.slot != SLOT_CASTLE) and any(t in attacker.traits for t in target.target_traits)
        has_fruit_def = any(target_fruits.get(t, False) for t in attacker.traits if t in target.target_traits) if (target.slot != SLOT_CASTLE) else False

        if target_matches_attacker and not target_cursed:
            if target.abilities.get("insanely_tough", False):
                div = 7 if has_fruit_def else 6
                raw_damage = max(1, raw_damage // div)
            elif target.abilities.get("resistant", False):
                div = 5 if has_fruit_def else 4
                raw_damage = max(1, raw_damage // div)
            elif target.abilities.get("strong", False):
                if has_fruit_def:
                    raw_damage = max(1, NativeDamageEngine.calculate_damage(raw_damage, 400)) # 0.4x damage taken with Trait Fruit
                else:
                    raw_damage = max(1, raw_damage // 2)  # 0.5x damage taken without Trait Fruit

        if target.slot != SLOT_CASTLE and not target_cursed:
            if "colossus" in attacker.traits and target.abilities.get("colossus_slayer", False):
                raw_damage = max(1, NativeDamageEngine.calculate_damage(raw_damage, 700))
            if "behemoth" in attacker.traits and target.abilities.get("behemoth_slayer", False):
                raw_damage = max(1, NativeDamageEngine.calculate_damage(raw_damage, 600))
            if "sage" in attacker.traits and target.abilities.get("sage_slayer", False):
                raw_damage = max(1, NativeDamageEngine.calculate_damage(raw_damage, 500))
        if target.slot != SLOT_CASTLE and not attacker_cursed:
            if "sage" in target.traits and attacker.abilities.get("sage_slayer", False):
                raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 1200)

        # 4. Critical Hit (2x damage, bypasses Metal) & Savage Blow (Col 82/83, does NOT bypass Metal)
        is_crit = _proc_flag("crit", "crit_prob")
        if is_crit:
            raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 2000)

        savage_boost = attacker.abilities.get("savage_blow_boost", 200)
        is_savage = _proc_flag("savage", "savage_blow_prob")
        if is_savage:
            raw_damage = NativeDamageEngine.calculate_damage(raw_damage, 1000 + savage_boost * 10)

        damage = NativeDamageEngine.calculate_damage(raw_damage, 1000)

        # 5. Metal trait rule: takes 1 damage unless Critical Hit or Metal Killer (col 112: X% of current HP)
        if (target.slot != SLOT_CASTLE) and ("metal" in target.traits or target.abilities.get("is_metal", False)) and not is_crit:
            mk_pct = int(attacker.abilities.get("metal_killer_pct", 0)) if (use_ability and not attacker_cursed) else 0
            if mk_pct > 0 and target.hp > 0:
                damage = max(1, (target.hp * mk_pct + 99) // 100)
            else:
                damage = 1

        # 5b. Trait-Targeted Dodge (Col 84/85 & Behemoth Slayer 5% passive dodge)
        dodged = False
        if target.dodge_timer > 0:
            dodged = True
        elif target_matches_attacker and not target_cursed:
            if target.abilities.get("dodge_prob", 0) > 0 and self.rng.randint(1, 100) <= target.abilities.get("dodge_prob", 0):
                target.dodge_timer = int(target.abilities.get("dodge_dur", 0))
                dodged = True
        elif not target_cursed and "behemoth" in attacker.traits and target.abilities.get("behemoth_slayer", False):
            if self.rng.randint(1, 100) <= 5:  # 5% innate dodge against Behemoth attacks
                dodged = True
        if dodged:
            self._emit_event({"type": "dodge", "x": target.pos_x, "team": target.team})
            return

        # 6. Aku Shield & Alien Barrier check
        shield_or_barrier_blocked = False
        if getattr(target, "aku_shield", 0) > 0:
            if _proc_flag("shield_pierce", "shield_pierce_prob"):
                target.aku_shield = 0
                self._emit_event({"type": "shield_pierce", "x": target.pos_x, "team": target.team})
            else:
                shield_or_barrier_blocked = True
                if damage >= target.aku_shield:
                    target.aku_shield = 0
                    damage = 0  # Authentic PONOS: excess damage from breaking blow is negated!
                    self._emit_event({"type": "shield_break", "x": target.pos_x, "team": target.team})
                else:
                    target.aku_shield -= damage
                    damage = 0
                    self._emit_event({"type": "shield_hit", "x": target.pos_x, "team": target.team})

        if target.barrier > 0:
            bb_prob = attacker.abilities.get("barrier_break_prob", attacker.abilities.get("barrier_breaker_prob", 0))
            bb_hit = (
                bool(swing_procs.get("barrier_break", False))
                if (use_ability and swing_procs is not None)
                else (use_ability and bb_prob > 0 and self.rng.randint(1, 100) <= bb_prob)
            )
            if damage > target.barrier or bb_hit:
                target.barrier = 0
                self._emit_event({"type": "barrier_break", "x": target.pos_x, "team": target.team})
            else:
                damage = 0
                shield_or_barrier_blocked = True

        target.hp -= damage

        # 7. Toxic % max HP true damage (blocked by active Aku shield or Alien barrier)
        toxic_hp_pct = attacker.abilities.get("toxic_hp_pct", 0)
        if target.hp > 0 and not shield_or_barrier_blocked and target.slot != SLOT_CASTLE and toxic_hp_pct > 0 and not target.abilities.get("toxic_immune", False):
            if _proc_flag("toxic", "toxic_prob"):
                toxic_damage = max(1, (target.max_hp * toxic_hp_pct) // 100)
                target.hp -= toxic_damage
                self._emit_event({"type": "toxic", "x": target.pos_x, "team": target.team, "damage": toxic_damage})

        # 8. Survive Lethal Strike check (Authentic PONOS: surviving lethal guarantees hitback to safety)
        survive_prob = target.abilities.get("survive_prob", 0)
        if target.hp <= 0 and target.slot != SLOT_CASTLE and not target.survived_lethal and survive_prob > 0:
            if self.rng.randint(1, 100) <= survive_prob:
                target.hp = 1
                target.survived_lethal = True
                self._knockback(target, "hitback")
                self._emit_event({
                    "type": "knockback", "x": target.pos_x, "radius": target.radius, "team": target.team, "kind": "hitback"
                })

        # Check lethal blow flags for money drops and zombie killer
        if target.hp <= 0:
            target.hp = 0
            if attacker.abilities.get("extra_money", False):
                target.killed_with_extra_money = True
            if attacker.abilities.get("zombie_killer", False):
                target.killed_by_zombie_killer = True

        self._emit_event({
            "type": "damage",
            "x": target.pos_x,
            "radius": target.radius,
            "damage": damage,
            "is_crit": is_crit,
            "team": target.team,
            "target_is_castle": (target.slot == SLOT_CASTLE),
            "target_id": f"{target.team}_{target.slot}",
            "attacker_id": f"{attacker.team}_{attacker.slot}",
        })

        if target.slot == SLOT_CASTLE:
            return

        # 9. Status Effect Procs (native 0x5d6da0: direct timer overwrite, Freeze pauses windup instead of cancelling)
        can_proc = use_ability and not shield_or_barrier_blocked and not attacker_cursed and ((not attacker.target_traits) or trait_match)
        if target.hp > 0 and can_proc:
            if not target.abilities.get("kb_immune", False) and _proc_flag("kb", "kb_prob"):
                tr_kb_permille = 300 if has_fruit_atk else 0
                self._knockback(target, "kb", tr_kb_permille=tr_kb_permille)
                self._emit_event({"type": "knockback", "x": target.pos_x, "radius": target.radius, "team": target.team, "kind": "kb"})

            freeze_dur = attacker.abilities.get("freeze_dur", 0)
            if freeze_dur > 0 and not target.abilities.get("freeze_immune", False) and _proc_flag("freeze", "freeze_prob"):
                target.freeze_timer = int(freeze_dur)

            slow_dur = attacker.abilities.get("slow_dur", 0)
            if slow_dur > 0 and not target.abilities.get("slow_immune", False) and _proc_flag("slow", "slow_prob"):
                target.slow_timer = int(slow_dur)

            weaken_dur = attacker.abilities.get("weaken_dur", 0)
            weaken_pct = attacker.abilities.get("weaken_pct", 50)
            if weaken_dur > 0 and not target.abilities.get("weaken_immune", False) and _proc_flag("weaken", "weaken_prob"):
                target.weaken_timer = int(weaken_dur)
                target.weaken_pct = weaken_pct

            curse_dur = attacker.abilities.get("curse_dur", 0)
            if curse_dur > 0 and not target.abilities.get("curse_immune", False) and _proc_flag("curse", "curse_prob"):
                target.curse_timer = int(curse_dur)
                self._emit_event({"type": "curse", "x": target.pos_x, "team": target.team})

            if _proc_flag("warp", "warp_prob"):
                if target.abilities.get("warp_blocker", False) or target.abilities.get("warp_immune", False):
                    self._emit_event({"type": "warp_block", "x": target.pos_x, "team": target.team})
                else:
                    warp_dur = max(10, int(attacker.abilities.get("warp_dur", 60)))
                    w_min = int(attacker.abilities.get("warp_min", 0))
                    w_max = int(attacker.abilities.get("warp_max", 0))
                    disp = self.rng.randint(min(w_min, w_max), max(w_min, w_max)) if (w_min or w_max) else DEFAULT_WARP_DISTANCE_DU
                    c_enemy_x = self.get_castle(TEAM_ENEMY).pos_x
                    c_cat_x = self.get_castle(TEAM_CAT).pos_x
                    target_dir = -1 if target.team == TEAM_CAT else 1
                    dest_x = max(c_enemy_x, min(c_cat_x, target.pos_x - target_dir * disp))
                    target.is_warped = True
                    target.warp_timer = warp_dur
                    target.warp_dest_x = dest_x
                    target.is_moving = False
                    target.is_winding_up = False
                    target.windup_timer = 0
                    target.backswing_timer = 0
                    self._emit_event({"type": "warp_enter", "x": target.pos_x, "team": target.team, "dest_x": dest_x})

        # 10. HP Threshold Knockback & Death Knockback (0x00959cf4) + Death Surge (t_unit col 89..92)
        if target.hp <= 0 and target.slot != SLOT_CASTLE:
            if getattr(target, "is_corpse", False):
                # Soulstrike killed a Zombie corpse before revival -> permanently remove
                target.hp = 0
                target.is_alive = False
                target.is_corpse = False
                target.revive_left = 0
                target.killed_by_zombie_killer = True
                self.units[target.team][target.slot] = None
                self._emit_event({"type": "death", "x": target.pos_x, "team": target.team, "name": target.name})
                return
            ds_prob = int(target.abilities.get("death_surge_prob", 0))
            ds_lvl = int(target.abilities.get("death_surge_lvl", 0))
            if ds_prob > 0 and ds_lvl > 0 and self.rng.randint(1, 100) <= ds_prob:
                t_dir = -1 if target.team == TEAM_CAT else 1
                self._trigger_surge_attack(
                    target, 1 - target.team, t_dir, ds_lvl, hit_damage=target.attack_power, swing_procs=None
                )
            target.hp = 0
            target.is_alive = False
            target.is_dying = True
            target.freeze_timer = 0
            target.slow_timer = 0
            target.curse_timer = 0
            self._knockback(target, "hitback")
            self._emit_event({
                "type": "knockback", "x": target.pos_x, "radius": target.radius, "team": target.team, "kind": "hitback"
            })
            opp_team = 1 - target.team
            if opp_team in self.wallets and target.money_drop > 0 and not getattr(target, "bounty_awarded", False):
                target.bounty_awarded = True
                bounty = int(target.money_drop * self.wallet_multipliers.get(opp_team, 1.0))
                if getattr(target, "killed_with_extra_money", False):
                    bounty *= 2
                self.wallets[opp_team].add_money(bounty)
            if getattr(target, "is_boss", False) and not getattr(target, "boss_death_triggered", False):
                target.boss_death_triggered = True
                self.slow_motion_timer = BOSS_DEATH_SLOWMO_FRAMES
                self._emit_event({
                    "type": "boss_death",
                    "x": target.pos_x,
                    "team": target.team,
                    "name": target.name,
                    "boss_name": target.name
                })
        elif target.hp > 0:
            damage_taken = target.max_hp - target.hp
            expected_kbs = int(damage_taken // max(1.0, target.kb_threshold))
            if expected_kbs > target.kb_points_taken:
                target.kb_points_taken = expected_kbs
                self._knockback(target, "hitback")
                self._emit_event({
                    "type": "knockback", "x": target.pos_x, "radius": target.radius, "team": target.team, "kind": "hitback"
                })

    def _update_deaths_and_castles(self):
        """Authentic Battle_KillUnit (0x008b6f50): a unit whose HP reaches 0 performs its final
        death knockback; once the knockback finishes, it releases a soul particle and clears the slot."""
        for team in (TEAM_CAT, TEAM_ENEMY):
            opp_team = 1 - team
            for slot in range(1, MAX_UNITS_PER_TEAM):
                unit = self.units[team][slot]
                if unit is None:
                    continue

                # Zombie corpse revive update
                if getattr(unit, "is_corpse", False):
                    unit.corpse_timer -= 1
                    if unit.corpse_timer <= 0:
                        unit.is_corpse = False
                        unit.is_alive = True
                        rev_pct = unit.abilities.get("revive_hp_pct", 50)
                        unit.hp = max(1, (unit.max_hp * rev_pct) // 100)
                        unit.kb_points_taken = 0
                        unit.anim_state = "walk"
                        unit.anim_frame = 0.0
                        self._emit_event({"type": "revive", "x": unit.pos_x, "team": unit.team, "name": unit.name})
                    continue

                # If unit is dying, wait until its death knockback completes before releasing soul
                if getattr(unit, "is_dying", False):
                    if not unit.is_in_knockback():
                        # Knockback finished! Check zombie revive or release soul
                        if getattr(unit, "revive_left", 0) > 0 and not getattr(unit, "killed_by_zombie_killer", False):
                            unit.revive_left -= 1
                            unit.is_corpse = True
                            unit.is_dying = False
                            unit.corpse_timer = max(1, int(unit.abilities.get("revive_time", DEFAULT_ZOMBIE_REVIVE_FRAMES)))
                            unit.anim_state = "corpse"
                            self._emit_event({"type": "corpse", "x": unit.pos_x, "team": unit.team, "name": unit.name})
                            continue

                        self._emit_event({
                            "type": "death", "x": unit.pos_x, "team": unit.team, "name": unit.name
                        })
                        self.units[team][slot] = None
                    continue

                # Fallback: if unit has hp <= 0 but not yet marked dying (e.g. direct assignment in test)
                if unit.is_alive and unit.hp <= 0:
                    unit.hp = 0
                    unit.is_alive = False
                    unit.is_dying = True
                    unit.freeze_timer = 0
                    unit.slow_timer = 0
                    unit.curse_timer = 0
                    self._knockback(unit, "hitback")
                    self._emit_event({
                        "type": "knockback", "x": unit.pos_x, "radius": unit.radius, "team": unit.team, "kind": "hitback"
                    })
                    if opp_team in self.wallets and unit.money_drop > 0 and not getattr(unit, "bounty_awarded", False):
                        unit.bounty_awarded = True
                        bounty = int(unit.money_drop * self.wallet_multipliers.get(opp_team, 1.0))
                        if getattr(unit, "killed_with_extra_money", False):
                            bounty *= 2
                        self.wallets[opp_team].add_money(bounty)
                    if getattr(unit, "is_boss", False) and not getattr(unit, "boss_death_triggered", False):
                        unit.boss_death_triggered = True
                        self.slow_motion_timer = BOSS_DEATH_SLOWMO_FRAMES
                        self._emit_event({
                            "type": "boss_death", "x": unit.pos_x, "team": unit.team, "name": unit.name, "boss_name": unit.name
                        })

        cat_castle = self.get_castle(TEAM_CAT)
        enemy_castle = self.get_castle(TEAM_ENEMY)

        if enemy_castle.hp <= 0:
            enemy_castle.hp = 0
            self.game_over = True
            self.winner = TEAM_CAT
            self._emit_event({"type": "game_over", "winner": TEAM_CAT})
        elif cat_castle.hp <= 0:
            cat_castle.hp = 0
            self.game_over = True
            self.winner = TEAM_ENEMY
            self._emit_event({"type": "game_over", "winner": TEAM_ENEMY})

