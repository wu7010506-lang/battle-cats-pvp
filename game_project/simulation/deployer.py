"""
Cat Deployment and Cooldown Controller - Authentic Ponos Battle Cats Edition.
Manages player 10-slot battle deck, cooldown timers, production authorization,
and Level-scaled combat specifications from SaveManager.
"""

from typing import List, Dict, Any, Optional, Union
from ..core.unit_data import CAT_REGISTRY
from ..core.official_data_parser import OfficialDataParser, compute_ponos_level_stat
from ..core.talents import apply_talents_to_spec
from ..core.damage_engine import NativeDamageEngine

_CAT_IDX_TO_KEY = {
    0: "cat", 1: "tank_cat", 2: "axe_cat", 3: "gross_cat", 4: "cow_cat",
    5: "bird_cat", 6: "fish_cat", 7: "lizard_cat", 8: "titan_cat"
}

DEFAULT_CORE_DECK = [0, 1, 2, 3, 4, 5, 6, 7, 8, -1]

class CatDeployer:
    def __init__(self, deck_keys: Optional[List[Union[str, int]]] = None, cat_levels: Optional[Dict[str, int]] = None, registry: Optional[Dict[Union[str, int], Dict[str, Any]]] = None):
        if deck_keys is None:
            deck_keys = list(DEFAULT_CORE_DECK)
        self.registry = registry if registry is not None else CAT_REGISTRY
        self.deck_keys = [_CAT_IDX_TO_KEY.get(k, k) if isinstance(k, int) and k in _CAT_IDX_TO_KEY and self.registry is CAT_REGISTRY else k for k in deck_keys]
        self.cat_levels = cat_levels or {}
        self.talent_overrides: Dict[str, List[int]] = {}  # key -> list of disabled talent indices

        # Track cooldown frames for each deck slot
        self.cooldowns = [0] * len(self.deck_keys)
        self.base_max_cooldowns = []
        self.max_cooldowns = []

        for k in self.deck_keys:
            spec = self._lookup_spec(k)
            if spec is not None:
                base_cd = int(spec.get("cooldown_frames", 150))
                self.base_max_cooldowns.append(base_cd)
                self.max_cooldowns.append(base_cd)
            else:
                self.base_max_cooldowns.append(999999)
                self.max_cooldowns.append(999999)

    def set_deck(self, deck_keys: List[Union[str, int]]):
        self.deck_keys = [
            _CAT_IDX_TO_KEY.get(k, k) if isinstance(k, int) and k in _CAT_IDX_TO_KEY and self.registry is CAT_REGISTRY else k
            for k in deck_keys[:10]
        ]
        self.cooldowns = [0] * len(self.deck_keys)
        self.base_max_cooldowns = []
        self.max_cooldowns = []
        for k in self.deck_keys:
            spec = self._lookup_spec(k)
            if spec is not None:
                base_cd = int(spec.get("cooldown_frames", 150))
                self.base_max_cooldowns.append(base_cd)
                self.max_cooldowns.append(base_cd)
            else:
                self.base_max_cooldowns.append(999999)
                self.max_cooldowns.append(999999)

    def apply_cooldown_reduction(self, reduction_frames: int = 0):
        """Applies EoC treasure and Nyancombo reduction to deck slot max cooldowns,
        strictly respecting the PONOS native engine hard minimum of 60 frames (2.0s)."""
        for idx in range(len(self.deck_keys)):
            base_cd = self.base_max_cooldowns[idx] if idx < len(self.base_max_cooldowns) else 150
            eff_cd = self._effective_cooldown(idx, base_cd, reduction_frames)
            self.max_cooldowns[idx] = eff_cd
            if self.cooldowns[idx] > eff_cd:
                self.cooldowns[idx] = eff_cd

    def _lookup_spec(self, key: Any) -> Optional[Dict[str, Any]]:
        if key is None or key == -1:
            return None
        if isinstance(key, dict):
            return key
        if key in self.registry:
            return self.registry[key]
        if key in CAT_REGISTRY:
            return CAT_REGISTRY[key]
        return None

    def update(self):
        """Decrements cooldown timers by 1 frame."""
        for i in range(len(self.cooldowns)):
            if self.cooldowns[i] > 0:
                self.cooldowns[i] -= 1

    def get_slot_info(self, idx: int) -> Optional[Dict[str, Any]]:
        if not (0 <= idx < len(self.deck_keys)):
            return None
        key = self.deck_keys[idx]
        base_spec = self._lookup_spec(key)
        if base_spec is None:
            return None

        # Authentic PONOS unitlevel.csv growth curve matching ARM64 0x9632d8 / 0x9645a8
        cat_id_str = str(base_spec.get("id", key))
        if self.cat_levels.get("__max__"):
            level = max(1, int(base_spec.get("max_level", 50)))
        else:
            default_lv = self.cat_levels.get("__default__", 1)
            level = max(1, int(self.cat_levels.get(cat_id_str, self.cat_levels.get(str(key), default_lv))))
        brackets = base_spec.get("level_brackets")
        if not brackets:
            is_crazed = "crazed" in str(key).lower() or "狂亂" in base_spec.get("name", "")
            if is_crazed:
                brackets = [20, 20, 10, 10, 10, 10, 10, 10, 10, 10]
            else:
                brackets = OfficialDataParser.get_instance().get_unit_level_brackets(base_spec.get("id"))

        scaled_spec = dict(base_spec)
        scaled_spec["level"] = level
        scaled_spec["hp"] = compute_ponos_level_stat(base_spec["hp"], level, brackets)
        scaled_hits = []
        for h in base_spec.get("attack_hits") or []:
            hc = dict(h)
            hc["damage"] = compute_ponos_level_stat(h.get("damage", 1), level, brackets)
            scaled_hits.append(hc)
        if scaled_hits:
            scaled_spec["attack_hits"] = scaled_hits
            scaled_spec["attack_power"] = sum(h["damage"] for h in scaled_hits)
        else:
            scaled_spec["attack_power"] = compute_ponos_level_stat(base_spec["attack_power"], level, brackets)

        # Apply all enabled official PONOS Talents + Standard 1.5x PvP/Ch.2+ Cost when in Web PvP MAX mode
        if self.cat_levels.get("__max__"):
            if scaled_spec.get("talents"):
                disabled_list = self.talent_overrides.get(str(key)) or self.talent_overrides.get(cat_id_str) or []
                scaled_spec = apply_talents_to_spec(scaled_spec, set(int(i) for i in disabled_list))
                if idx < len(self.max_cooldowns):
                    self.max_cooldowns[idx] = max(60, int(scaled_spec.get("cooldown_frames", self.max_cooldowns[idx])))
            if not scaled_spec.get("is_custom", False):
                scaled_spec["cost"] = max(15, int(round(scaled_spec.get("cost", 50) * 1.5)))

        cost = scaled_spec.get("cost", max(50, scaled_spec.get("money_drop", 100)))

        return {
            "key": key if not isinstance(key, dict) else base_spec.get("id", "custom"),
            "id": base_spec.get("id", key),
            "name": base_spec.get("name", str(key)),
            "level": level,
            "cost": cost,
            "current_cd": self.cooldowns[idx],
            "max_cd": self.max_cooldowns[idx],
            "cd_ratio": self.cooldowns[idx] / max(1, self.max_cooldowns[idx]),
            "texture_code": base_spec.get("texture_code", "000_f"),
            "icon_code": base_spec.get("icon_code", "uni000_f00"),
            "rarity": base_spec.get("rarity", 0),
            "rarity_label": base_spec.get("rarity_label", "基本"),
            "is_custom": bool(base_spec.get("is_custom", False)),
            "tint_rgb": base_spec.get("tint_rgb"),
            "spec": scaled_spec
        }

    def can_deploy(self, idx: int, current_money: int, has_free_slot: bool) -> bool:
        if not (0 <= idx < len(self.deck_keys)):
            return False
        if not has_free_slot:
            return False
        if self.cooldowns[idx] > 0:
            return False
        base_spec = self._lookup_spec(self.deck_keys[idx])
        if base_spec is None:
            return False
        cost = base_spec.get("cost", max(50, base_spec.get("money_drop", 100)))
        return current_money >= cost

    def trigger_cooldown(self, idx: int, reduction_frames: int = 0):
        if 0 <= idx < len(self.cooldowns):
            base_cd = self.base_max_cooldowns[idx] if idx < len(self.base_max_cooldowns) else self.max_cooldowns[idx]
            eff_cd = self._effective_cooldown(idx, base_cd, reduction_frames)
            self.max_cooldowns[idx] = eff_cd
            self.cooldowns[idx] = eff_cd

    def _effective_cooldown(self, idx: int, base_cd: int, reduction_frames: int) -> int:
        spec = self._lookup_spec(self.deck_keys[idx]) or {}
        if spec.get("fixed_stats"):
            return base_cd
        return max(60, base_cd - reduction_frames)
