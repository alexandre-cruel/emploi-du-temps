from __future__ import annotations

"""
Solveur CP-SAT pour la construction de l'emploi du temps.

Les options (Maths expertes/Cr1, Matco/Cr4, DGEMC/Cr1) sont planifiées
APRÈS la résolution des spécialités — le solveur ne bloque aucun créneau
pour les options. L'utilisateur contrôle la disponibilité via la grille en
étape 2.
"""

from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from data import (
    DOUBLE_SLOT_PAIRS,
    SAME_DAY_PAIRS,
    SLOT_MATCO,
    SLOT_MATEX,
    SLOTS,
    SPE_4_SLOTS,
    ParseResult,
    Student,
)

N_SLOTS = len(SLOTS)


@dataclass
class SolverConfig:
    nb_groups: dict[str, int] = field(default_factory=dict)
    slot_availability: dict[str, list[bool]] = field(default_factory=dict)

    constraint_lce_no_early: bool = True
    constraint_hlp_philo_days: bool = True
    constraint_maths_common_slot: bool = True
    maths_common_slot_idx: int = SLOT_MATCO  # Cr4 Mercredi — disponible pour Maths

    timeout_seconds: int = 60
    niveau: str = "Terminale"


@dataclass
class GroupResult:
    specialite: str
    groupe_id: int
    students: list[Student]
    slots: list[int]
    subgroups: dict[str, list[Student]] | None = None  # {"A": [...], "B": [...]} pour SPC/SVT

    @property
    def label(self) -> str:
        return f"{self.specialite} {self.groupe_id + 1}"

    @property
    def effectif(self) -> int:
        return len(self.students)


@dataclass
class SolverResult:
    status: str  # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN"
    groups: list[GroupResult]
    stats: dict[str, Any]
    infeasibility_hints: list[str]

    def get_student_groups(self) -> dict[str, list[GroupResult]]:
        mapping: dict[str, list[GroupResult]] = {}
        for g in self.groups:
            for s in g.students:
                key = f"{s.nom} {s.prenom}"
                mapping.setdefault(key, []).append(g)
        return mapping

    def get_student_slots(self) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        for name, grps in self.get_student_groups().items():
            slots_set: set[int] = set()
            for g in grps:
                slots_set.update(g.slots)
            result[name] = sorted(slots_set)
        return result


def default_slot_availability(spe: str) -> list[bool]:  # noqa: ARG001
    """Tous les créneaux disponibles par défaut — l'utilisateur ajuste en étape 2."""
    return [True] * N_SLOTS


def build_default_config(parse_result: ParseResult) -> SolverConfig:
    nb_groups: dict[str, int] = {}
    slot_avail: dict[str, list[bool]] = {}

    for spe, count in parse_result.spe_counts.items():
        n = max(1, -(-count // 38))  # ceil(count/38)
        nb_groups[spe] = n
        slot_avail[spe] = default_slot_availability(spe)

    return SolverConfig(
        nb_groups=nb_groups,
        slot_availability=slot_avail,
        niveau=parse_result.niveau,
    )


def solve(parse_result: ParseResult, config: SolverConfig) -> SolverResult:
    students = parse_result.students
    specialites = parse_result.all_specialites
    niveau = config.niveau

    def nb_slots_required(spe: str) -> int:
        if niveau == "Terminale":
            return 4 if spe in SPE_4_SLOTS else 3
        return 2  # Première : 3 spés × 2 créneaux

    model = cp_model.CpModel()

    # Index des étudiants par spécialité
    spe_to_students: dict[str, list[tuple[int, Student]]] = {s: [] for s in specialites}
    for idx, st in enumerate(students):
        for spe in st.specialites:
            if spe in spe_to_students:
                spe_to_students[spe].append((idx, st))

    # -----------------------------------------------------------------------
    # Variables groupe_var[student_idx, spe] ∈ {0..G-1}
    # -----------------------------------------------------------------------
    groupe_var: dict[tuple[int, str], cp_model.IntVar] = {}
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        for (idx, _) in spe_to_students[spe]:
            groupe_var[(idx, spe)] = model.new_int_var(0, G - 1, f"g_{idx}_{spe}")

    # -----------------------------------------------------------------------
    # Variables slot_var[spe, g, c] ∈ {0,1}
    # -----------------------------------------------------------------------
    slot_var: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        avail = config.slot_availability.get(spe, [True] * N_SLOTS)
        for g in range(G):
            for c in range(N_SLOTS):
                v = model.new_bool_var(f"s_{spe}_{g}_{c}")
                slot_var[(spe, g, c)] = v
                if not avail[c]:
                    model.add(v == 0)

    # -----------------------------------------------------------------------
    # Variables in_group[student_idx, spe, g] ∈ {0,1} — pré-calculées une fois
    # -----------------------------------------------------------------------
    in_group: dict[tuple[int, str, int], cp_model.BoolVar] = {}
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        for (idx, _) in spe_to_students[spe]:
            for g in range(G):
                b = model.new_bool_var(f"ig_{idx}_{spe}_{g}")
                model.add(groupe_var[(idx, spe)] == g).only_enforce_if(b)
                model.add(groupe_var[(idx, spe)] != g).only_enforce_if(b.negated())
                in_group[(idx, spe, g)] = b

    # -----------------------------------------------------------------------
    # Nombre exact de créneaux par groupe
    # -----------------------------------------------------------------------
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        n_req = nb_slots_required(spe)
        for g in range(G):
            model.add(sum(slot_var[(spe, g, c)] for c in range(N_SLOTS)) == n_req)

    # -----------------------------------------------------------------------
    # Taille max (≤ 38) et taille min (≥ floor(N/G)) pour l'équilibre
    # -----------------------------------------------------------------------
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        students_in_spe = spe_to_students[spe]
        min_size = len(students_in_spe) // G
        for g in range(G):
            group_sum = sum(in_group[(idx, spe, g)] for (idx, _) in students_in_spe)
            model.add(group_sum <= 38)
            if G > 1 and min_size > 0:
                model.add(group_sum >= min_size)

    # -----------------------------------------------------------------------
    # Anti-conflit : aucun créneau partagé entre les spés d'un même élève
    # -----------------------------------------------------------------------
    for idx, st in enumerate(students):
        spes = [s for s in st.specialites if s in specialites]
        for i in range(len(spes)):
            for j in range(i + 1, len(spes)):
                s1, s2 = spes[i], spes[j]
                G1 = config.nb_groups.get(s1, 1)
                G2 = config.nb_groups.get(s2, 1)
                for c in range(N_SLOTS):
                    for g1 in range(G1):
                        for g2 in range(G2):
                            # NOT(in_g1 AND in_g2 AND slot_s1_g1_c AND slot_s2_g2_c)
                            model.add_bool_or([
                                in_group[(idx, s1, g1)].negated(),
                                in_group[(idx, s2, g2)].negated(),
                                slot_var[(s1, g1, c)].negated(),
                                slot_var[(s2, g2, c)].negated(),
                            ])

    # -----------------------------------------------------------------------
    # Un groupe ne peut pas avoir les deux créneaux d'un même jour
    # Exception : HLP — ses 3 créneaux {Cr0, Cr5, Cr6} couvrent 2 enseignants
    # (philo + littérature), avoir Cr5+Cr6 jeudi est intentionnel.
    # -----------------------------------------------------------------------
    for spe in specialites:
        if spe == "HLP":
            continue
        G = config.nb_groups.get(spe, 1)
        for g in range(G):
            for c_a, c_b in SAME_DAY_PAIRS:
                model.add(slot_var[(spe, g, c_a)] + slot_var[(spe, g, c_b)] <= 1)

    # -----------------------------------------------------------------------
    # HLP-P : seulement lundi (Cr0) et jeudi (Cr5, Cr6)
    # -----------------------------------------------------------------------
    ALLOWED_HLP = {0, 5, 6}
    if config.constraint_hlp_philo_days and "HLP" in specialites:
        G = config.nb_groups.get("HLP", 1)
        for g in range(G):
            for c in range(N_SLOTS):
                if c not in ALLOWED_HLP:
                    model.add(slot_var[("HLP", g, c)] == 0)

    # -----------------------------------------------------------------------
    # Maths créneau commun (Cr4 Mercredi par défaut)
    # -----------------------------------------------------------------------
    if config.constraint_maths_common_slot and "Maths" in specialites:
        G = config.nb_groups.get("Maths", 1)
        if G > 1:
            c_common = config.maths_common_slot_idx
            avail = config.slot_availability.get("Maths", [True] * N_SLOTS)
            if avail[c_common]:
                for g in range(G):
                    model.add(slot_var[("Maths", g, c_common)] == 1)

    # -----------------------------------------------------------------------
    # Variables de taille pour l'objectif d'équilibre
    # -----------------------------------------------------------------------
    size_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        students_in_spe = spe_to_students[spe]
        n_total = len(students_in_spe)
        for g in range(G):
            sv = model.new_int_var(0, n_total, f"size_{spe}_{g}")
            model.add(sv == sum(in_group[(idx, spe, g)] for (idx, _) in students_in_spe))
            size_vars[(spe, g)] = sv

    # -----------------------------------------------------------------------
    # Fonction objectif
    # -----------------------------------------------------------------------
    obj_terms = []

    # Équilibre des effectifs (priorité haute)
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        if G < 2:
            continue
        n_total = len(spe_to_students[spe])
        max_s = model.new_int_var(0, n_total, f"max_{spe}")
        min_s = model.new_int_var(0, n_total, f"min_{spe}")
        model.add_max_equality(max_s, [size_vars[(spe, g)] for g in range(G)])
        model.add_min_equality(min_s, [size_vars[(spe, g)] for g in range(G)])
        diff = model.new_int_var(0, n_total, f"diff_{spe}")
        model.add(diff == max_s - min_s)
        obj_terms.append(diff * 10)

    # LCE évite créneaux 8h mardi/jeudi/vendredi
    LCE_EARLY = [2, 5, 7]
    if config.constraint_lce_no_early and "LCE" in specialites:
        G = config.nb_groups.get("LCE", 1)
        avail = config.slot_availability.get("LCE", [True] * N_SLOTS)
        for g in range(G):
            for c in LCE_EARLY:
                if avail[c]:
                    obj_terms.append(slot_var[("LCE", g, c)] * 3)

    # Permanences : pénalité si créneau isolé sur un jour à 2 slots
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        avail = config.slot_availability.get(spe, [True] * N_SLOTS)
        for g in range(G):
            for c_a, c_b in DOUBLE_SLOT_PAIRS:
                if not avail[c_a] or not avail[c_b]:
                    continue
                sa, sb = slot_var[(spe, g, c_a)], slot_var[(spe, g, c_b)]
                only_a = model.new_bool_var(f"pa_{spe}_{g}_{c_a}")
                only_b = model.new_bool_var(f"pb_{spe}_{g}_{c_b}")
                # only_a = sa AND NOT sb
                model.add_bool_and([sa, sb.negated()]).only_enforce_if(only_a)
                model.add_bool_or([sa.negated(), sb]).only_enforce_if(only_a.negated())
                # only_b = sb AND NOT sa
                model.add_bool_and([sb, sa.negated()]).only_enforce_if(only_b)
                model.add_bool_or([sb.negated(), sa]).only_enforce_if(only_b.negated())
                obj_terms.extend([only_a, only_b])

    if obj_terms:
        model.minimize(sum(obj_terms))

    # -----------------------------------------------------------------------
    # Résolution
    # -----------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.timeout_seconds
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = False

    status_code = solver.solve(model)
    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
    }
    status = status_map.get(status_code, "UNKNOWN")

    if status not in ("OPTIMAL", "FEASIBLE"):
        hints = _infeasibility_hints(config, parse_result, specialites)
        return SolverResult(status=status, groups=[], stats={}, infeasibility_hints=hints)

    # -----------------------------------------------------------------------
    # Extraction
    # -----------------------------------------------------------------------
    groups: list[GroupResult] = []
    for spe in specialites:
        G = config.nb_groups.get(spe, 1)
        for g in range(G):
            assigned_slots = [c for c in range(N_SLOTS) if solver.value(slot_var[(spe, g, c)]) == 1]
            members = [
                st for (idx, st) in spe_to_students[spe]
                if solver.value(groupe_var[(idx, spe)]) == g
            ]
            groups.append(GroupResult(specialite=spe, groupe_id=g, students=members, slots=assigned_slots))

    split_lab_groups(groups)
    n_permanences = _count_permanences(groups)
    stats = {
        "n_students": len(students),
        "n_conflicts": _count_conflicts(groups),
        "n_permanences": n_permanences,
        "objective": solver.objective_value,
        "wall_time": round(solver.wall_time, 2),
    }
    return SolverResult(status=status, groups=groups, stats=stats, infeasibility_hints=[])


def split_lab_groups(groups: list[GroupResult]) -> list[GroupResult]:
    """Crée les sous-groupes A/B pour SPC et SVT (split alphabétique)."""
    for g in groups:
        if g.specialite in SPE_4_SLOTS:
            sorted_students = sorted(g.students, key=lambda s: (s.nom, s.prenom))
            mid = (len(sorted_students) + 1) // 2  # ceil → A toujours ≥ B
            g.subgroups = {
                "A": sorted_students[:mid],
                "B": sorted_students[mid:],
            }
    return groups


def _count_conflicts(groups: list[GroupResult]) -> int:
    student_slots: dict[str, set[int]] = {}
    conflicts = 0
    for g in groups:
        for st in g.students:
            key = f"{st.nom} {st.prenom}"
            existing = student_slots.get(key, set())
            overlap = existing & set(g.slots)
            if overlap:
                conflicts += len(overlap)
            student_slots[key] = existing | set(g.slots)
    return conflicts


def _count_permanences(groups: list[GroupResult]) -> int:
    student_slots: dict[str, set[int]] = {}
    for g in groups:
        for st in g.students:
            key = f"{st.nom} {st.prenom}"
            student_slots.setdefault(key, set()).update(g.slots)

    count = 0
    for _, slots in student_slots.items():
        for c_a, c_b in DOUBLE_SLOT_PAIRS:
            if (c_a in slots) != (c_b in slots):
                count += 1
                break
    return count


def _infeasibility_hints(
    config: SolverConfig,
    parse_result: ParseResult,
    specialites: list[str],
) -> list[str]:
    hints: list[str] = []
    for spe in specialites:
        avail = config.slot_availability.get(spe, [True] * N_SLOTS)
        n_avail = sum(avail)
        n_req = 4 if (spe in SPE_4_SLOTS and config.niveau == "Terminale") else (2 if config.niveau == "Première" else 3)
        if n_avail < n_req:
            hints.append(
                f"{spe} : {n_avail} créneau(x) disponible(s) mais {n_req} requis."
            )
            continue
        # Vérifier les jours disponibles (contrainte "1 créneau max par jour")
        # Un jour à 2 créneaux ne compte que pour 1 créneau disponible
        days_avail: set[int] = set()
        for c, avail_c in enumerate(avail):
            if avail_c:
                # Trouver le "jour" de ce créneau : les paires de SAME_DAY_PAIRS partagent le même jour
                day_id = c
                for c_a, c_b in SAME_DAY_PAIRS:
                    if c == c_b:
                        day_id = c_a  # normalise vers le 1er créneau du jour
                        break
                days_avail.add(day_id)
        if len(days_avail) < n_req:
            hints.append(
                f"{spe} : seulement {len(days_avail)} jour(s) disponible(s) "
                f"mais {n_req} créneaux requis (1 max par jour)."
            )
    if not hints:
        hints.append(
            "Impossible de satisfaire toutes les contraintes. "
            "Essayez d'activer plus de créneaux ou de réduire le nombre de groupes."
        )
    return hints
