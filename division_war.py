"""Système isolé de guerre de divisions pour bot Discord.

Ce module est volontairement autonome :
- aucune dépendance à discord.py
- stockage en mémoire via dataclasses
- API simple à importer depuis le bot principal

Si vous voulez retirer ce système plus tard, vous pouvez supprimer ce fichier
et retirer les imports/appels côté bot principal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Iterable
import math
import random
import time


NumberFormula = Callable[[int], int]


@dataclass(slots=True)
class DivisionWarConfig:
    """Configuration globale et formules du système.

    Les formules sont injectables/modifiables facilement.
    """

    min_message_length: int = 5
    xp_per_valid_message: int = 12
    min_seconds_between_xp: float = 20.0
    repeated_message_similarity_exact: bool = True

    # Fenêtres d'activité
    recent_window_seconds: int = 72 * 3600
    recent_message_threshold: int = 5
    xp_activity_window_seconds: int = 7 * 24 * 3600

    # Combat
    damage_random_factor_min: float = 0.9
    damage_random_factor_max: float = 1.15

    # Formules
    hp_formula: NumberFormula = staticmethod(lambda level: 100 + level * 10)
    atk_formula: NumberFormula = staticmethod(lambda level: 10 + level * 2)

    # Formule niveau (par défaut: progression quadratique)
    # Exemple: level ~= sqrt(xp / 50)
    level_formula: Callable[[int], int] = staticmethod(lambda xp: int(math.sqrt(max(0, xp) / 50)))


@dataclass(slots=True)
class MemberProfile:
    user_id: int
    division_id: int | None = None
    xp: int = 0
    level: int = 0
    hp: int = 100
    atk: int = 10
    member_power: float = 120.0
    last_message_timestamp: float = 0.0
    last_message_content: str = ""
    recent_message_count: int = 0
    is_active: bool = False

    # Historique utile à la détection d'activité (interne)
    message_timestamps: deque[float] = field(default_factory=deque)
    xp_gain_timestamps: deque[float] = field(default_factory=deque)


@dataclass(slots=True)
class DivisionProfile:
    division_id: int
    name: str
    members: list[MemberProfile] = field(default_factory=list)
    active_members: list[MemberProfile] = field(default_factory=list)
    division_power: float = 0.0


@dataclass(slots=True)
class XPUpdateResult:
    awarded: bool
    reason: str
    xp_gained: int = 0
    leveled_up: bool = False


@dataclass(slots=True)
class DuelResult:
    winner_division_id: int | None
    loser_division_id: int | None
    rounds: int
    log: list[str]


class DivisionWarSystem:
    """Point d'entrée principal du système de guerre de divisions."""

    def __init__(self, config: DivisionWarConfig | None = None, rng: random.Random | None = None) -> None:
        self.config = config or DivisionWarConfig()
        self._members: dict[int, MemberProfile] = {}
        self._division_names: dict[int, str] = {}
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    # Membres / divisions
    # ------------------------------------------------------------------
    def get_or_create_member(self, user_id: int, division_id: int | None = None) -> MemberProfile:
        profile = self._members.get(user_id)
        if profile is None:
            profile = MemberProfile(user_id=user_id, division_id=division_id)
            self._members[user_id] = profile
            self._recompute_member_stats(profile)
        elif division_id is not None and profile.division_id != division_id:
            profile.division_id = division_id
        return profile

    def register_division_name(self, division_id: int, name: str) -> None:
        self._division_names[division_id] = name

    def get_member(self, user_id: int) -> MemberProfile | None:
        return self._members.get(user_id)

    def iter_members(self) -> Iterable[MemberProfile]:
        return self._members.values()

    # ------------------------------------------------------------------
    # XP / niveau / anti-spam
    # ------------------------------------------------------------------
    def handle_message(
        self,
        *,
        user_id: int,
        division_id: int | None,
        content: str,
        timestamp: float | None = None,
    ) -> XPUpdateResult:
        """Traite un message utilisateur et attribue éventuellement de l'XP."""
        now_ts = timestamp if timestamp is not None else time.time()
        profile = self.get_or_create_member(user_id=user_id, division_id=division_id)

        cleaned_content = content.strip()
        if len(cleaned_content) < self.config.min_message_length:
            self._record_message_only(profile, cleaned_content, now_ts)
            return XPUpdateResult(awarded=False, reason="message_too_short")

        if (
            self.config.repeated_message_similarity_exact
            and cleaned_content.casefold() == profile.last_message_content.casefold()
        ):
            self._record_message_only(profile, cleaned_content, now_ts)
            return XPUpdateResult(awarded=False, reason="repeated_message")

        if (now_ts - profile.last_message_timestamp) < self.config.min_seconds_between_xp:
            self._record_message_only(profile, cleaned_content, now_ts)
            return XPUpdateResult(awarded=False, reason="cooldown")

        old_level = profile.level
        profile.xp += self.config.xp_per_valid_message
        profile.level = self.level_from_xp(profile.xp)
        profile.last_message_timestamp = now_ts
        profile.last_message_content = cleaned_content
        profile.message_timestamps.append(now_ts)
        profile.xp_gain_timestamps.append(now_ts)
        self._trim_old_activity(profile, now_ts)
        profile.recent_message_count = len(profile.message_timestamps)
        profile.is_active = self.is_member_active(profile, now_ts)
        self._recompute_member_stats(profile)

        return XPUpdateResult(
            awarded=True,
            reason="xp_awarded",
            xp_gained=self.config.xp_per_valid_message,
            leveled_up=profile.level > old_level,
        )

    def level_from_xp(self, xp: int) -> int:
        return max(0, int(self.config.level_formula(max(0, xp))))

    # ------------------------------------------------------------------
    # Stats membre
    # ------------------------------------------------------------------
    def compute_hp(self, level: int) -> int:
        return max(1, int(self.config.hp_formula(level)))

    def compute_atk(self, level: int) -> int:
        return max(1, int(self.config.atk_formula(level)))

    def compute_member_power(self, hp: int, atk: int) -> float:
        return float(hp + atk * 2)

    # ------------------------------------------------------------------
    # Activité
    # ------------------------------------------------------------------
    def is_member_active(self, profile: MemberProfile, now_ts: float | None = None) -> bool:
        now = now_ts if now_ts is not None else time.time()
        self._trim_old_activity(profile, now)

        recent_message_ok = len(profile.message_timestamps) >= self.config.recent_message_threshold
        recent_xp_ok = any((now - ts) <= self.config.xp_activity_window_seconds for ts in profile.xp_gain_timestamps)
        talked_recently = profile.last_message_timestamp > 0 and (now - profile.last_message_timestamp) <= self.config.recent_window_seconds

        return recent_message_ok or recent_xp_ok or talked_recently

    # ------------------------------------------------------------------
    # Puissance de division
    # ------------------------------------------------------------------
    def build_division_profile(self, division_id: int, name: str | None = None) -> DivisionProfile:
        division_name = name or self._division_names.get(division_id, f"Division {division_id}")
        division = DivisionProfile(division_id=division_id, name=division_name)

        now = time.time()
        for member in self._members.values():
            if member.division_id != division_id:
                continue
            member.is_active = self.is_member_active(member, now)
            division.members.append(member)
            if member.is_active:
                division.active_members.append(member)

        division.division_power = self.compute_division_power(division.active_members)
        return division

    def compute_division_power(self, active_members: list[MemberProfile]) -> float:
        if not active_members:
            return 0.0

        total_power = sum(member.member_power for member in active_members)
        count = len(active_members)
        # Rendement décroissant: la taille aide, mais moins que la qualité/activité.
        return total_power / math.sqrt(count)

    # ------------------------------------------------------------------
    # Duel entre divisions
    # ------------------------------------------------------------------
    def simulate_1v1(self, fighter_a: MemberProfile, fighter_b: MemberProfile) -> tuple[int, int, list[str]]:
        """Retourne (winner_user_id, loser_user_id, combat_log)."""
        hp_a = fighter_a.hp
        hp_b = fighter_b.hp
        log: list[str] = []
        turn = 1

        while hp_a > 0 and hp_b > 0:
            dmg_a = self._roll_damage(fighter_a.atk)
            hp_b = max(0, hp_b - dmg_a)
            log.append(f"Tour {turn}: {fighter_a.user_id} inflige {dmg_a} (HP adversaire: {hp_b}).")
            if hp_b <= 0:
                break

            dmg_b = self._roll_damage(fighter_b.atk)
            hp_a = max(0, hp_a - dmg_b)
            log.append(f"Tour {turn}: {fighter_b.user_id} inflige {dmg_b} (HP adversaire: {hp_a}).")
            turn += 1

        if hp_a > 0:
            return fighter_a.user_id, fighter_b.user_id, log
        return fighter_b.user_id, fighter_a.user_id, log

    def duel_divisions(self, division_a: DivisionProfile, division_b: DivisionProfile) -> DuelResult:
        """Format duel: le gagnant reste, le perdant est remplacé par le suivant."""
        queue_a = [member for member in division_a.active_members]
        queue_b = [member for member in division_b.active_members]

        if not queue_a and not queue_b:
            return DuelResult(winner_division_id=None, loser_division_id=None, rounds=0, log=["Aucun combattant actif des deux côtés."])
        if not queue_a:
            return DuelResult(winner_division_id=division_b.division_id, loser_division_id=division_a.division_id, rounds=0, log=["Division A sans combattant actif."])
        if not queue_b:
            return DuelResult(winner_division_id=division_a.division_id, loser_division_id=division_b.division_id, rounds=0, log=["Division B sans combattant actif."])

        idx_a = 0
        idx_b = 0
        rounds = 0
        log: list[str] = []

        while idx_a < len(queue_a) and idx_b < len(queue_b):
            rounds += 1
            fighter_a = queue_a[idx_a]
            fighter_b = queue_b[idx_b]
            winner_user_id, _loser_user_id, fight_log = self.simulate_1v1(fighter_a, fighter_b)
            log.append(f"Round {rounds}: {fighter_a.user_id} (A) vs {fighter_b.user_id} (B)")
            log.extend(fight_log)

            if winner_user_id == fighter_a.user_id:
                idx_b += 1  # Le perdant B est remplacé
                log.append(f" -> Gagnant: {fighter_a.user_id} (A)")
            else:
                idx_a += 1  # Le perdant A est remplacé
                log.append(f" -> Gagnant: {fighter_b.user_id} (B)")

        if idx_a >= len(queue_a):
            winner = division_b.division_id
            loser = division_a.division_id
        else:
            winner = division_a.division_id
            loser = division_b.division_id

        return DuelResult(winner_division_id=winner, loser_division_id=loser, rounds=rounds, log=log)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------
    def _roll_damage(self, atk: int) -> int:
        factor = self._rng.uniform(self.config.damage_random_factor_min, self.config.damage_random_factor_max)
        return max(1, int(round(atk * factor)))

    def _recompute_member_stats(self, profile: MemberProfile) -> None:
        profile.hp = self.compute_hp(profile.level)
        profile.atk = self.compute_atk(profile.level)
        profile.member_power = self.compute_member_power(profile.hp, profile.atk)

    def _trim_old_activity(self, profile: MemberProfile, now_ts: float) -> None:
        message_window_limit = now_ts - self.config.recent_window_seconds
        xp_window_limit = now_ts - self.config.xp_activity_window_seconds

        while profile.message_timestamps and profile.message_timestamps[0] < message_window_limit:
            profile.message_timestamps.popleft()
        while profile.xp_gain_timestamps and profile.xp_gain_timestamps[0] < xp_window_limit:
            profile.xp_gain_timestamps.popleft()

        profile.recent_message_count = len(profile.message_timestamps)

    def _record_message_only(self, profile: MemberProfile, content: str, now_ts: float) -> None:
        profile.last_message_timestamp = now_ts
        profile.last_message_content = content
        profile.message_timestamps.append(now_ts)
        self._trim_old_activity(profile, now_ts)
        profile.is_active = self.is_member_active(profile, now_ts)


# ----------------------------------------------------------------------
# Exemple minimal d'utilisation (importable depuis discord.py)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    system = DivisionWarSystem()

    # Simule quelques messages
    system.handle_message(user_id=1, division_id=10, content="Bonjour à tous, on attaque !")
    system.handle_message(user_id=2, division_id=20, content="Prêts pour la guerre de divisions ?")

    div_a = system.build_division_profile(10, "Audacieux")
    div_b = system.build_division_profile(20, "Radieux")

    print(f"Puissance {div_a.name}: {div_a.division_power:.2f}")
    print(f"Puissance {div_b.name}: {div_b.division_power:.2f}")

    duel = system.duel_divisions(div_a, div_b)
    print(f"Gagnant division: {duel.winner_division_id}, rounds: {duel.rounds}")
