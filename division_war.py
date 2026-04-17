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


@dataclass(slots=True)
class FighterState:
    user_id: int
    hp: int
    atk: int
    source_member: MemberProfile


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
    def simulate_fight(
        self,
        member1: MemberProfile,
        member2: MemberProfile,
        *,
        member1_starting_hp: int | None = None,
        member2_starting_hp: int | None = None,
    ) -> tuple[int, int, int, int, list[str]]:
        """Simule un combat 1v1 et retourne:
        (winner_user_id, loser_user_id, winner_remaining_hp, loser_remaining_hp, combat_log).
        """
        hp_1 = max(0, member1_starting_hp if member1_starting_hp is not None else member1.hp)
        hp_2 = max(0, member2_starting_hp if member2_starting_hp is not None else member2.hp)
        log: list[str] = []
        turn = 1

        while hp_1 > 0 and hp_2 > 0:
            dmg_1, crit_1 = self._compute_damage(member1.atk)
            hp_2 = max(0, hp_2 - dmg_1)
            crit_label_1 = " 💥CRITIQUE!" if crit_1 else ""
            log.append(f"Tour {turn}: {member1.user_id} inflige {dmg_1}{crit_label_1} (HP adversaire: {hp_2}).")
            if hp_2 <= 0:
                break

            dmg_2, crit_2 = self._compute_damage(member2.atk)
            hp_1 = max(0, hp_1 - dmg_2)
            crit_label_2 = " 💥CRITIQUE!" if crit_2 else ""
            log.append(f"Tour {turn}: {member2.user_id} inflige {dmg_2}{crit_label_2} (HP adversaire: {hp_1}).")
            turn += 1

        if hp_1 > 0:
            return member1.user_id, member2.user_id, hp_1, hp_2, log
        return member2.user_id, member1.user_id, hp_2, hp_1, log

    def simulate_division_war(self, team1: DivisionProfile, team2: DivisionProfile) -> DuelResult:
        """Format duel: le gagnant reste, le perdant est remplacé par le suivant.

        Important: pour les duels de divisions, tous les membres participent
        (actifs et inactifs).
        """
        queue_1 = [self._build_fighter_state(member) for member in team1.members]
        queue_2 = [self._build_fighter_state(member) for member in team2.members]

        if not queue_1 and not queue_2:
            return DuelResult(winner_division_id=None, loser_division_id=None, rounds=0, log=["Aucun combattant des deux côtés."])
        if not queue_1:
            return DuelResult(winner_division_id=team2.division_id, loser_division_id=team1.division_id, rounds=0, log=["Division A sans combattant."])
        if not queue_2:
            return DuelResult(winner_division_id=team1.division_id, loser_division_id=team2.division_id, rounds=0, log=["Division B sans combattant."])

        idx_1 = 0
        idx_2 = 0
        rounds = 0
        log: list[str] = []

        while idx_1 < len(queue_1) and idx_2 < len(queue_2):
            rounds += 1
            fighter_1 = queue_1[idx_1]
            fighter_2 = queue_2[idx_2]
            winner_user_id, _loser_user_id, winner_remaining_hp, _loser_remaining_hp, fight_log = self.simulate_fight(
                fighter_1.source_member,
                fighter_2.source_member,
                member1_starting_hp=fighter_1.hp,
                member2_starting_hp=fighter_2.hp,
            )
            log.append(f"Round {rounds}: {fighter_1.user_id} (A) vs {fighter_2.user_id} (B)")
            log.extend(fight_log)

            if winner_user_id == fighter_1.user_id:
                fighter_1.hp = winner_remaining_hp
                idx_2 += 1  # Le perdant B est remplacé
                log.append(f" -> Gagnant: {fighter_1.user_id} (A) avec {fighter_1.hp} HP restants")
            else:
                fighter_2.hp = winner_remaining_hp
                idx_1 += 1  # Le perdant A est remplacé
                log.append(f" -> Gagnant: {fighter_2.user_id} (B) avec {fighter_2.hp} HP restants")

        if idx_1 >= len(queue_1):
            winner = team2.division_id
            loser = team1.division_id
        else:
            winner = team1.division_id
            loser = team2.division_id

        return DuelResult(winner_division_id=winner, loser_division_id=loser, rounds=rounds, log=log)

    def duel_divisions(self, division_a: DivisionProfile, division_b: DivisionProfile) -> DuelResult:
        """Alias de compatibilité avec l'ancienne API."""
        return self.simulate_division_war(division_a, division_b)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------
    def _build_fighter_state(self, member: MemberProfile) -> FighterState:
        return FighterState(user_id=member.user_id, hp=member.hp, atk=member.atk, source_member=member)

    def _compute_damage(self, atk: int) -> tuple[int, bool]:
        is_critical = self._rng.random() < 0.02
        base_damage = max(1, atk)
        if is_critical:
            return base_damage * 2, True
        return base_damage, False

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
