"""Système isolé de guerre de divisions pour bot Discord (persistant SQLite).

Ce module est volontairement autonome :
- aucune dépendance à discord.py
- toutes les données sont persistées en base SQLite
- API simple à importer depuis le bot principal

Si vous voulez retirer ce système plus tard, vous pouvez supprimer ce fichier
et retirer les imports/appels côté bot principal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable
import math
import random
import sqlite3
import time


NumberFormula = Callable[[int], int]


@dataclass(slots=True)
class DivisionWarConfig:
    """Configuration globale et formules du système."""

    # Anti-spam XP
    min_message_length: int = 5
    xp_per_valid_message: int = 12
    min_seconds_between_xp: float = 20.0

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
    """Représentation d'un membre (alignée avec la table DB)."""

    user_id: int
    division_id: int | None = None
    xp: int = 0
    level: int = 0
    hp: int = 100
    atk: int = 10
    member_power: float = 120.0
    last_message_timestamp: float = 0.0


@dataclass(slots=True)
class DivisionProfile:
    """Représentation d'une division (alignée avec la table DB)."""

    division_id: int
    name: str
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
    """Point d'entrée principal du système de guerre de divisions.

    Architecture interne (dans ce seul fichier) :
    - Logique DB : création tables + CRUD membres/divisions
    - Logique XP/stats : attribution XP, recalcul niveau/statistiques
    - Logique combat : duels 1v1 et guerre de division
    """

    def __init__(
        self,
        config: DivisionWarConfig | None = None,
        rng: random.Random | None = None,
        db_path: str = "division_war.sqlite3",
    ) -> None:
        self.config = config or DivisionWarConfig()
        self._rng = rng or random.Random()
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables_if_needed()

    # ------------------------------------------------------------------
    # Logique DB (persistante)
    # ------------------------------------------------------------------
    def _create_tables_if_needed(self) -> None:
        """Crée automatiquement les tables si elles n'existent pas."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS divisions (
                    division_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY,
                    division_id INTEGER NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 0,
                    hp INTEGER NOT NULL DEFAULT 100,
                    atk INTEGER NOT NULL DEFAULT 10,
                    member_power REAL NOT NULL DEFAULT 120.0,
                    last_message_timestamp REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY (division_id) REFERENCES divisions(division_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_division_id ON members(division_id)"
            )

    def close(self) -> None:
        """Ferme explicitement la connexion DB."""
        self._conn.close()

    def register_division_name(self, division_id: int, name: str) -> None:
        """Crée ou met à jour le nom d'une division."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO divisions (division_id, name)
                VALUES (?, ?)
                ON CONFLICT(division_id) DO UPDATE SET name=excluded.name
                """,
                (division_id, name),
            )

    def get_division(self, division_id: int) -> DivisionProfile:
        row = self._conn.execute(
            "SELECT division_id, name FROM divisions WHERE division_id = ?",
            (division_id,),
        ).fetchone()
        if row is None:
            default_name = f"Division {division_id}"
            self.register_division_name(division_id, default_name)
            return DivisionProfile(division_id=division_id, name=default_name)
        return DivisionProfile(division_id=row["division_id"], name=row["name"])

    def _row_to_member(self, row: sqlite3.Row) -> MemberProfile:
        return MemberProfile(
            user_id=row["user_id"],
            division_id=row["division_id"],
            xp=row["xp"],
            level=row["level"],
            hp=row["hp"],
            atk=row["atk"],
            member_power=row["member_power"],
            last_message_timestamp=row["last_message_timestamp"],
        )

    def get_member(self, user_id: int) -> MemberProfile | None:
        row = self._conn.execute(
            """
            SELECT user_id, division_id, xp, level, hp, atk, member_power, last_message_timestamp
            FROM members
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return self._row_to_member(row) if row is not None else None

    def get_or_create_member(self, user_id: int, division_id: int | None = None) -> MemberProfile:
        """Crée/récupère un membre, puis renvoie l'état actuel depuis la DB."""
        member = self.get_member(user_id)
        if member is None:
            level = 0
            hp = self.compute_hp(level)
            atk = self.compute_atk(level)
            member_power = self.compute_member_power(hp, atk)
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO members (
                        user_id, division_id, xp, level, hp, atk, member_power, last_message_timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, division_id, 0, level, hp, atk, member_power, 0.0),
                )
            member = self.get_member(user_id)

        if division_id is not None and member is not None and member.division_id != division_id:
            with self._conn:
                self._conn.execute(
                    "UPDATE members SET division_id = ? WHERE user_id = ?",
                    (division_id, user_id),
                )
            member = self.get_member(user_id)

        # mypy-like safety; en pratique member est non-None ici.
        if member is None:
            raise RuntimeError("Impossible de créer/récupérer le membre")
        return member

    def save_member(self, member: MemberProfile) -> None:
        """Persiste l'état complet d'un membre."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE members
                SET division_id = ?, xp = ?, level = ?, hp = ?, atk = ?, member_power = ?, last_message_timestamp = ?
                WHERE user_id = ?
                """,
                (
                    member.division_id,
                    member.xp,
                    member.level,
                    member.hp,
                    member.atk,
                    member.member_power,
                    member.last_message_timestamp,
                    member.user_id,
                ),
            )

    def iter_members(self) -> Iterable[MemberProfile]:
        rows = self._conn.execute(
            "SELECT user_id, division_id, xp, level, hp, atk, member_power, last_message_timestamp FROM members"
        ).fetchall()
        for row in rows:
            yield self._row_to_member(row)

    def get_members_by_division(self, division_id: int) -> list[MemberProfile]:
        """Récupère tous les membres d'une division (sans filtre d'activité)."""
        rows = self._conn.execute(
            """
            SELECT user_id, division_id, xp, level, hp, atk, member_power, last_message_timestamp
            FROM members
            WHERE division_id = ?
            ORDER BY user_id ASC
            """,
            (division_id,),
        ).fetchall()
        return [self._row_to_member(row) for row in rows]

    # ------------------------------------------------------------------
    # Logique XP / niveau / stats
    # ------------------------------------------------------------------
    def level_from_xp(self, xp: int) -> int:
        return max(0, int(self.config.level_formula(max(0, xp))))

    def compute_hp(self, level: int) -> int:
        return max(1, int(self.config.hp_formula(level)))

    def compute_atk(self, level: int) -> int:
        return max(1, int(self.config.atk_formula(level)))

    def compute_member_power(self, hp: int, atk: int) -> float:
        return float(hp + atk * 2)

    def recalculate_member_level_and_stats(self, member: MemberProfile) -> MemberProfile:
        """Recalcule niveau + stats à partir de l'XP, puis persiste."""
        member.level = self.level_from_xp(member.xp)
        member.hp = self.compute_hp(member.level)
        member.atk = self.compute_atk(member.level)
        member.member_power = self.compute_member_power(member.hp, member.atk)
        self.save_member(member)
        return member

    def update_member_xp(self, user_id: int, xp_delta: int) -> MemberProfile:
        """Met à jour l'XP d'un membre, recalcule et persiste ses stats."""
        member = self.get_or_create_member(user_id)
        member.xp = max(0, member.xp + xp_delta)
        return self.recalculate_member_level_and_stats(member)

    def handle_message(
        self,
        *,
        user_id: int,
        division_id: int | None,
        content: str,
        timestamp: float | None = None,
    ) -> XPUpdateResult:
        """Traite un message utilisateur et attribue éventuellement de l'XP.

        Anti-spam conservé :
        - longueur minimale
        - cooldown entre gains XP via last_message_timestamp

        IMPORTANT:
        - aucune logique "membre actif" n'est utilisée.
        - tout est persisté en base.
        """
        now_ts = timestamp if timestamp is not None else time.time()
        member = self.get_or_create_member(user_id=user_id, division_id=division_id)

        cleaned_content = content.strip()
        if len(cleaned_content) < self.config.min_message_length:
            member.last_message_timestamp = now_ts
            self.save_member(member)
            return XPUpdateResult(awarded=False, reason="message_too_short")

        if (now_ts - member.last_message_timestamp) < self.config.min_seconds_between_xp:
            member.last_message_timestamp = now_ts
            self.save_member(member)
            return XPUpdateResult(awarded=False, reason="cooldown")

        old_level = member.level
        member.xp += self.config.xp_per_valid_message
        member.last_message_timestamp = now_ts
        self.recalculate_member_level_and_stats(member)

        return XPUpdateResult(
            awarded=True,
            reason="xp_awarded",
            xp_gained=self.config.xp_per_valid_message,
            leveled_up=member.level > old_level,
        )

    # ------------------------------------------------------------------
    # Puissance de division
    # ------------------------------------------------------------------
    def build_division_profile(self, division_id: int, name: str | None = None) -> DivisionProfile:
        """Construit un profil division en incluant TOUS les membres."""
        if name is not None:
            self.register_division_name(division_id, name)

        division = self.get_division(division_id)
        members = self.get_members_by_division(division_id)
        division.division_power = self.compute_division_power(members)
        return division

    def compute_division_power(self, members: list[MemberProfile]) -> float:
        """Puissance de division calculée sur tous les membres."""
        if not members:
            return 0.0
        total_power = sum(member.member_power for member in members)
        count = len(members)
        # Rendement décroissant: la taille aide, mais moins que la qualité.
        return total_power / math.sqrt(count)

    # ------------------------------------------------------------------
    # Logique combat
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
        """Duel de divisions (sans notion d'activité): tous les membres combattent."""
        members_1 = self.get_members_by_division(team1.division_id)
        members_2 = self.get_members_by_division(team2.division_id)

        queue_1 = [self._build_fighter_state(member) for member in members_1]
        queue_2 = [self._build_fighter_state(member) for member in members_2]

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
                idx_2 += 1
                log.append(f" -> Gagnant: {fighter_1.user_id} (A) avec {fighter_1.hp} HP restants")
            else:
                fighter_2.hp = winner_remaining_hp
                idx_1 += 1
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


# ----------------------------------------------------------------------
# Exemple minimal d'utilisation (importable depuis discord.py)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    system = DivisionWarSystem(db_path="division_war_demo.sqlite3")

    # Simule quelques messages
    system.handle_message(user_id=1, division_id=10, content="Bonjour à tous, on attaque !")
    system.handle_message(user_id=2, division_id=20, content="Prêts pour la guerre de divisions ?")

    div_a = system.build_division_profile(10, "Audacieux")
    div_b = system.build_division_profile(20, "Radieux")

    print(f"Puissance {div_a.name}: {div_a.division_power:.2f}")
    print(f"Puissance {div_b.name}: {div_b.division_power:.2f}")

    duel = system.duel_divisions(div_a, div_b)
    print(f"Gagnant division: {duel.winner_division_id}, rounds: {duel.rounds}")

    system.close()
