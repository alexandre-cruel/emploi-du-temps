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
    AUTRE_SLOTS,
    DOUBLE_SLOT_PAIRS,
    JEUDI_SLOTS,
    LUNDI_SLOTS,
    SAME_DAY_PAIRS,
    SLOT_MATCO,
    SLOT_MATEX,
    SLOTS,
    SPE_4_SLOTS,
    VALID_TP_PAIRS,
    ParseResult,
    Student,
)


def student_visible_slots(student: Student, group: "GroupResult") -> list[int]:
    """Retourne les slots vraiment vus par cet élève dans ce groupe.
    Pour SPC/SVT : cours communs + slot TP de son sous-groupe (A ou B) uniquement.
    Pour les autres : tous les slots du groupe.
    """
    if group.specialite not in SPE_4_SLOTS or not group.subgroups:
        return list(group.slots)
    is_a = any(s.nom == student.nom and s.prenom == student.prenom
               for s in group.subgroups.get("A", []))
    visible = list(group.cours_slots)
    for (slot_a, slot_b) in group.tp_assignments:
        visible.append(slot_a if is_a else slot_b)
    return visible

N_SLOTS = len(SLOTS)


@dataclass
class SolverConfig:
    nb_groups: dict[str, int] = field(default_factory=dict)
    slot_availability: dict[str, list[bool]] = field(default_factory=dict)

    constraint_lce_no_early: bool = True
    constraint_hlp_philo_days: bool = True
    constraint_maths_common_slot: bool = True
    maths_common_slot_idx: int = SLOT_MATCO  # Cr4 Mercredi — disponible pour Maths

    timeout_seconds: int = 300
    niveau: str = "Terminale"
    deterministic_mode: bool = False  # True = 1 worker (reproductible), False = num_workers (rapide)
    num_workers: int = 8
    interleave_search: bool = False
    linearization_level: int = 1  # 0=désactivé, 1=défaut CP-SAT, 2=LP complète


@dataclass
class Violation:
    """Violation d'une contrainte dure détectée après édition manuelle."""
    code: str        # ex. "HLP_MISSING_DAY", "SAME_DAY", "MATHS_COMMON", "NB_SLOTS"
    spe: str
    groupe_id: int   # -1 si global
    message: str

    @property
    def group_label(self) -> str:
        return f"{self.spe} {self.groupe_id + 1}" if self.groupe_id >= 0 else self.spe


@dataclass
class GroupResult:
    specialite: str
    groupe_id: int
    students: list[Student]
    slots: list[int]
    subgroups: dict[str, list[Student]] | None = None  # {"A": [...], "B": [...]} pour SPC/SVT
    tp_pairs: list[tuple[int, int]] = field(default_factory=list)  # paires TP (same-day) — 1 ou 2 pour SPC/SVT
    tp_assignments: list[tuple[int, int]] = field(default_factory=list)  # [(slot_A, slot_B), ...] par jour TP
    cours_slots: list[int] = field(default_factory=list)  # slots de cours (groupe entier) pour SPC/SVT

    @property
    def label(self) -> str:
        return f"{self.specialite} {self.groupe_id + 1}"

    @property
    def effectif(self) -> int:
        return len(self.students)

    @property
    def tp_pair(self) -> tuple[int, int] | None:
        """Compat rétro : renvoie la première paire TP (ou None)."""
        return self.tp_pairs[0] if self.tp_pairs else None


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


def _hints_compatible(
    initial: "SolverResult",
    config: SolverConfig,
    specialites: list[str],
) -> bool:
    """Vérifie que la solution initiale est compatible avec la config courante."""
    if not initial.groups:
        return False
    prev_spes = {g.specialite for g in initial.groups}
    if prev_spes != set(specialites):
        return False
    for spe in specialites:
        prev_g = len([g for g in initial.groups if g.specialite == spe])
        if prev_g != config.nb_groups.get(spe, 1):
            return False
    return True


def _apply_hints(
    model: cp_model.CpModel,
    initial: "SolverResult",
    slot_var: dict,
    groupe_var: dict,
    in_group: dict,
    cours_var: dict,
    tp_day: dict,
    tp_swap: dict,
    sub_var: dict,
    spe_to_students: dict,
) -> None:
    """Ajoute des hints CP-SAT depuis une solution existante pour warm-start."""
    # Lookup: (nom, prenom, spe) → groupe_id
    student_group: dict[tuple[str, str, str], int] = {}
    for g in initial.groups:
        for st in g.students:
            student_group[(st.nom, st.prenom, g.specialite)] = g.groupe_id

    # Lookup: (spe, groupe_id) → GroupResult
    group_by_key: dict[tuple[str, int], "GroupResult"] = {
        (g.specialite, g.groupe_id): g for g in initial.groups
    }

    for key, var in slot_var.items():
        spe, g_id, c = key
        prev = group_by_key.get((spe, g_id))
        if prev is not None:
            model.add_hint(var, 1 if c in prev.slots else 0)

    for (idx, spe), var in groupe_var.items():
        st_obj = next((s for (i, s) in spe_to_students.get(spe, []) if i == idx), None)
        if st_obj is not None:
            hint_g = student_group.get((st_obj.nom, st_obj.prenom, spe))
            if hint_g is not None:
                model.add_hint(var, hint_g)

    for (idx, spe, g_id), var in in_group.items():
        st_obj = next((s for (i, s) in spe_to_students.get(spe, []) if i == idx), None)
        if st_obj is not None:
            hint_g = student_group.get((st_obj.nom, st_obj.prenom, spe))
            model.add_hint(var, 1 if hint_g == g_id else 0)

    for key, var in cours_var.items():
        spe, g_id, c = key
        prev = group_by_key.get((spe, g_id))
        if prev is not None:
            model.add_hint(var, 1 if c in prev.cours_slots else 0)

    for key, var in tp_day.items():
        spe, g_id, pi = key
        prev = group_by_key.get((spe, g_id))
        if prev is not None:
            c_a, c_b = VALID_TP_PAIRS[pi]
            model.add_hint(var, 1 if (c_a, c_b) in prev.tp_pairs else 0)

    for key, var in tp_swap.items():
        spe, g_id, pi = key
        prev = group_by_key.get((spe, g_id))
        if prev is not None:
            c_a, c_b = VALID_TP_PAIRS[pi]
            if (c_a, c_b) in prev.tp_pairs:
                assign_idx = prev.tp_pairs.index((c_a, c_b))
                slot_a, _ = prev.tp_assignments[assign_idx]
                model.add_hint(var, 1 if slot_a == c_b else 0)
            else:
                model.add_hint(var, 0)

    for key, var in sub_var.items():
        idx, spe = key
        student_objs = spe_to_students.get(spe, [])
        # sub_var est indexé par idx de spe_to_students, pas l'idx global
        for i, (global_idx, st) in enumerate(student_objs):
            if global_idx == idx:
                prev = next((g for g in initial.groups
                             if g.specialite == spe and st in g.students), None)
                if prev and prev.subgroups:
                    is_b = any(s.nom == st.nom and s.prenom == st.prenom
                               for s in prev.subgroups.get("B", []))
                    model.add_hint(var, 1 if is_b else 0)
                break


def solve(parse_result: ParseResult, config: SolverConfig, initial_solution: "SolverResult | None" = None) -> SolverResult:
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
    # Nombre exact de créneaux par groupe (non-SPC/SVT)
    # SPC/SVT : occupent 4+n_tp_days slots (5 ou 6), géré séparément plus bas
    # -----------------------------------------------------------------------
    for spe in specialites:
        if spe in SPE_4_SLOTS:
            continue
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
    # SPC/SVT : sous-groupes A/B intégrés + 1 ou 2 paires TP
    # -----------------------------------------------------------------------
    # Décomposition par (spe, g, c) : chaque slot est soit cours (groupe entier),
    # soit TP_A (sous-groupe A uniquement), soit TP_B, soit inutilisé.
    # Mode 1 : 3 cours + 1 paire TP  → 5 slots occupés au total, 4 vus/élève
    # Mode 2 : 2 cours + 2 paires TP → 6 slots occupés au total, 4 vus/élève
    cours_var: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    tp_a_slot: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    tp_b_slot: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    tp_day: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    tp_swap: dict[tuple[str, int, int], cp_model.BoolVar] = {}
    sub_var: dict[tuple[int, str], cp_model.BoolVar] = {}  # 0 = A, 1 = B

    _valid_tp_sets = {frozenset(p) for p in VALID_TP_PAIRS}
    _pairs_containing_slot: dict[int, list[int]] = {c: [] for c in range(N_SLOTS)}
    for pi, (c_a, c_b) in enumerate(VALID_TP_PAIRS):
        _pairs_containing_slot[c_a].append(pi)
        _pairs_containing_slot[c_b].append(pi)

    for spe in SPE_4_SLOTS:
        if spe not in specialites:
            continue
        G = config.nb_groups.get(spe, 1)
        avail = config.slot_availability.get(spe, [True] * N_SLOTS)
        for g in range(G):
            # Cours vars
            for c in range(N_SLOTS):
                cv = model.new_bool_var(f"cours_{spe}_{g}_{c}")
                cours_var[(spe, g, c)] = cv
                if not avail[c]:
                    model.add(cv == 0)
                # HLP-like: pas de 2 cours same-day
                # (fait plus loin globalement)

            # TP pair vars (par paire valide)
            for pi, (c_a, c_b) in enumerate(VALID_TP_PAIRS):
                td = model.new_bool_var(f"tpday_{spe}_{g}_{pi}")
                sw = model.new_bool_var(f"tpswap_{spe}_{g}_{pi}")
                tp_day[(spe, g, pi)] = td
                tp_swap[(spe, g, pi)] = sw
                if not avail[c_a] or not avail[c_b]:
                    model.add(td == 0)

            # tp_a_slot / tp_b_slot dérivés
            for c in range(N_SLOTS):
                ta = model.new_bool_var(f"tpa_{spe}_{g}_{c}")
                tb = model.new_bool_var(f"tpb_{spe}_{g}_{c}")
                tp_a_slot[(spe, g, c)] = ta
                tp_b_slot[(spe, g, c)] = tb
                if not avail[c]:
                    model.add(ta == 0)
                    model.add(tb == 0)

            # Lier tp_a_slot / tp_b_slot à tp_day et tp_swap
            # Pour chaque paire (c_a, c_b) : si td=1 ET swap=0 → A sur c_a, B sur c_b
            #                                si td=1 ET swap=1 → A sur c_b, B sur c_a
            # a_at_ca[pi] = td[pi] AND NOT swap[pi]
            # a_at_cb[pi] = td[pi] AND swap[pi]
            a_at_ca: dict[int, cp_model.BoolVar] = {}
            a_at_cb: dict[int, cp_model.BoolVar] = {}
            for pi, (c_a, c_b) in enumerate(VALID_TP_PAIRS):
                td = tp_day[(spe, g, pi)]
                sw = tp_swap[(spe, g, pi)]
                aa = model.new_bool_var(f"a_at_ca_{spe}_{g}_{pi}")
                ab = model.new_bool_var(f"a_at_cb_{spe}_{g}_{pi}")
                # aa = td AND NOT sw
                model.add_bool_and([td, sw.negated()]).only_enforce_if(aa)
                model.add_bool_or([td.negated(), sw]).only_enforce_if(aa.negated())
                # ab = td AND sw
                model.add_bool_and([td, sw]).only_enforce_if(ab)
                model.add_bool_or([td.negated(), sw.negated()]).only_enforce_if(ab.negated())
                a_at_ca[pi] = aa
                a_at_cb[pi] = ab

            # tp_a_slot[c] = sum over pairs p starting at c of a_at_ca[p]
            #              + sum over pairs p ending at c of a_at_cb[p]
            # tp_b_slot[c] = sum a_at_cb (starting) + sum a_at_ca (ending)
            for c in range(N_SLOTS):
                contrib_a = []
                contrib_b = []
                for pi, (c_a, c_b) in enumerate(VALID_TP_PAIRS):
                    if c == c_a:
                        contrib_a.append(a_at_ca[pi])
                        contrib_b.append(a_at_cb[pi])
                    elif c == c_b:
                        contrib_a.append(a_at_cb[pi])
                        contrib_b.append(a_at_ca[pi])
                if contrib_a:
                    model.add(tp_a_slot[(spe, g, c)] == sum(contrib_a))
                else:
                    model.add(tp_a_slot[(spe, g, c)] == 0)
                if contrib_b:
                    model.add(tp_b_slot[(spe, g, c)] == sum(contrib_b))
                else:
                    model.add(tp_b_slot[(spe, g, c)] == 0)

            # Exclusivité par slot : au plus un rôle (cours, tp_a, tp_b)
            for c in range(N_SLOTS):
                model.add(
                    cours_var[(spe, g, c)] + tp_a_slot[(spe, g, c)] + tp_b_slot[(spe, g, c)] <= 1
                )

            # slot_var (déjà défini plus haut) = cours + tp_a + tp_b
            for c in range(N_SLOTS):
                model.add(
                    slot_var[(spe, g, c)]
                    == cours_var[(spe, g, c)] + tp_a_slot[(spe, g, c)] + tp_b_slot[(spe, g, c)]
                )

            # 1 ou 2 jours TP
            n_tp_days = model.new_int_var(1, 2, f"n_tp_days_{spe}_{g}")
            model.add(n_tp_days == sum(tp_day[(spe, g, pi)] for pi in range(len(VALID_TP_PAIRS))))
            # Nb cours = 4 - n_tp_days (chaque élève voit 4 slots)
            model.add(sum(cours_var[(spe, g, c)] for c in range(N_SLOTS)) == 4 - n_tp_days)
            # Redéfinir la contrainte "nb slots totaux vus par le groupe" :
            # groupe occupe (n_cours + 2*n_tp_days) slots = (4-n_tp_days) + 2*n_tp_days = 4 + n_tp_days
            # Déjà encodé via slot_var, mais on avait défini plus haut slot_var == 4 (n_slots_required).
            # → il faut redéfinir cette contrainte pour SPC/SVT (elle sera supprimée / réécrite)

            # Cours : pas 2 sur le même jour
            for c_a, c_b in SAME_DAY_PAIRS:
                model.add(cours_var[(spe, g, c_a)] + cours_var[(spe, g, c_b)] <= 1)

            # Au plus un pair-TP par slot (empêche slot 1 d'être TP via (9,1) ET (0,1) en même temps)
            for c in range(N_SLOTS):
                pairs = _pairs_containing_slot[c]
                if len(pairs) > 1:
                    model.add(sum(tp_day[(spe, g, pi)] for pi in pairs) <= 1)

    # -----------------------------------------------------------------------
    # Sous-groupe A/B par élève (uniquement SPC/SVT)
    # -----------------------------------------------------------------------
    for spe in SPE_4_SLOTS:
        if spe not in specialites:
            continue
        for (idx, _) in spe_to_students[spe]:
            sub_var[(idx, spe)] = model.new_bool_var(f"sub_{idx}_{spe}")

    # Équilibrage A/B par groupe (|A| == |B| ou ±1)
    for spe in SPE_4_SLOTS:
        if spe not in specialites:
            continue
        G = config.nb_groups.get(spe, 1)
        for g in range(G):
            # sum over students of (in_group AND sub==B) is size of B
            in_group_b_vars: list[cp_model.BoolVar] = []
            in_group_a_vars: list[cp_model.BoolVar] = []
            for (idx, _) in spe_to_students[spe]:
                ig = in_group[(idx, spe, g)]
                sub = sub_var[(idx, spe)]
                # inB = ig AND sub
                inB = model.new_bool_var(f"inB_{idx}_{spe}_{g}")
                model.add_bool_and([ig, sub]).only_enforce_if(inB)
                model.add_bool_or([ig.negated(), sub.negated()]).only_enforce_if(inB.negated())
                # inA = ig AND NOT sub
                inA = model.new_bool_var(f"inA_{idx}_{spe}_{g}")
                model.add_bool_and([ig, sub.negated()]).only_enforce_if(inA)
                model.add_bool_or([ig.negated(), sub]).only_enforce_if(inA.negated())
                in_group_b_vars.append(inB)
                in_group_a_vars.append(inA)
            n_total = len(spe_to_students[spe])
            sz_a = model.new_int_var(0, n_total, f"szA_{spe}_{g}")
            sz_b = model.new_int_var(0, n_total, f"szB_{spe}_{g}")
            model.add(sz_a == sum(in_group_a_vars))
            model.add(sz_b == sum(in_group_b_vars))
            # |A - B| <= 1
            diff_ab = model.new_int_var(-n_total, n_total, f"diffAB_{spe}_{g}")
            model.add(diff_ab == sz_a - sz_b)
            model.add(diff_ab >= -1)
            model.add(diff_ab <= 1)

    # -----------------------------------------------------------------------
    # Anti-conflit inter-spé (avec support A/B pour SPC/SVT)
    # -----------------------------------------------------------------------
    # visible_slot[idx, spe, g, c] : cet élève voit-il ce slot ?
    #   - non-SPC/SVT : slot_var[spe, g, c]
    #   - SPC/SVT : cours_var[c] OR (sub==A AND tp_a_slot[c]) OR (sub==B AND tp_b_slot[c])
    visible: dict[tuple[int, str, int, int], cp_model.BoolVar] = {}

    def get_visible(idx: int, spe: str, g: int, c: int) -> cp_model.BoolVar:
        key = (idx, spe, g, c)
        if key in visible:
            return visible[key]
        if spe not in SPE_4_SLOTS:
            visible[key] = slot_var[(spe, g, c)]
            return visible[key]
        # SPC/SVT: crée un booléen dérivé
        v = model.new_bool_var(f"vis_{idx}_{spe}_{g}_{c}")
        visible[key] = v
        cv = cours_var[(spe, g, c)]
        ta = tp_a_slot[(spe, g, c)]
        tb = tp_b_slot[(spe, g, c)]
        sub = sub_var[(idx, spe)]
        # v = cv OR (NOT sub AND ta) OR (sub AND tb)
        # Décomposition par cas :
        # Si sub = 0 (A) : v = cv OR ta
        model.add(v == cv + ta).only_enforce_if(sub.negated())
        # Si sub = 1 (B) : v = cv OR tb
        model.add(v == cv + tb).only_enforce_if(sub)
        # Note : cv, ta, tb sont mutuellement exclusifs (contrainte plus haut),
        # donc cv+ta et cv+tb sont ∈ {0,1}.
        return v

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
                            v1 = get_visible(idx, s1, g1, c)
                            v2 = get_visible(idx, s2, g2, c)
                            model.add_bool_or([
                                in_group[(idx, s1, g1)].negated(),
                                in_group[(idx, s2, g2)].negated(),
                                v1.negated(),
                                v2.negated(),
                            ])

    # -----------------------------------------------------------------------
    # Un groupe ne peut pas avoir les deux créneaux d'un même jour (non-SPC/SVT)
    # -----------------------------------------------------------------------
    for spe in specialites:
        if spe in SPE_4_SLOTS:
            continue
        G = config.nb_groups.get(spe, 1)
        for g in range(G):
            for c_a, c_b in SAME_DAY_PAIRS:
                model.add(slot_var[(spe, g, c_a)] + slot_var[(spe, g, c_b)] <= 1)

    # -----------------------------------------------------------------------
    # HLP : 1 slot Lundi + 1 slot Jeudi + 1 slot autre jour (Mardi/Merc/Vend)
    # Prof philo (Lundi+Jeudi), prof littérature (tous jours)
    # -----------------------------------------------------------------------
    if config.constraint_hlp_philo_days and "HLP" in specialites:
        G = config.nb_groups.get("HLP", 1)
        for g in range(G):
            avail = config.slot_availability.get("HLP", [True] * N_SLOTS)
            lundi_avail = [c for c in LUNDI_SLOTS if avail[c]]
            jeudi_avail = [c for c in JEUDI_SLOTS if avail[c]]
            autre_avail = [c for c in AUTRE_SLOTS if avail[c]]
            # Bloquer les créneaux hors des 3 jours autorisés
            for c in range(N_SLOTS):
                if c not in LUNDI_SLOTS | JEUDI_SLOTS | AUTRE_SLOTS:
                    model.add(slot_var[("HLP", g, c)] == 0)
            # Exactement 1 slot par groupe de jour (si des créneaux sont disponibles)
            if lundi_avail:
                model.add(sum(slot_var[("HLP", g, c)] for c in lundi_avail) == 1)
            if jeudi_avail:
                model.add(sum(slot_var[("HLP", g, c)] for c in jeudi_avail) == 1)
            if autre_avail:
                model.add(sum(slot_var[("HLP", g, c)] for c in autre_avail) == 1)

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

    # Permanences : pénalité par élève et par paire same-day (métrique proviseur)
    # busy_a[idx, c_a] = 1 ssi l'élève idx a cours au slot c_a (via l'une de ses spés)
    # perm = busy_a XOR busy_b sur chaque DOUBLE_SLOT_PAIR
    for idx, st in enumerate(students):
        spes = [s for s in st.specialites if s in specialites]
        if not spes:
            continue
        for c_a, c_b in DOUBLE_SLOT_PAIRS:
            terms_a = []
            terms_b = []
            for spe in spes:
                G = config.nb_groups.get(spe, 1)
                for g in range(G):
                    ig = in_group[(idx, spe, g)]
                    va = get_visible(idx, spe, g, c_a)
                    vb = get_visible(idx, spe, g, c_b)
                    ca = model.new_bool_var(f"at_{idx}_{spe}_{g}_{c_a}")
                    cb = model.new_bool_var(f"at_{idx}_{spe}_{g}_{c_b}")
                    model.add_bool_and([ig, va]).only_enforce_if(ca)
                    model.add_bool_or([ig.negated(), va.negated()]).only_enforce_if(ca.negated())
                    model.add_bool_and([ig, vb]).only_enforce_if(cb)
                    model.add_bool_or([ig.negated(), vb.negated()]).only_enforce_if(cb.negated())
                    terms_a.append(ca)
                    terms_b.append(cb)
            busy_a = model.new_bool_var(f"busyA_{idx}_{c_a}")
            busy_b = model.new_bool_var(f"busyB_{idx}_{c_b}")
            model.add(busy_a == sum(terms_a))
            model.add(busy_b == sum(terms_b))
            perm = model.new_bool_var(f"perm_{idx}_{c_a}_{c_b}")
            model.add(busy_a + busy_b == 1).only_enforce_if(perm)
            model.add(busy_a + busy_b != 1).only_enforce_if(perm.negated())
            obj_terms.append(perm * 5)

    if obj_terms:
        model.minimize(sum(obj_terms))

    # Warm-start : hints depuis une solution précédente
    if initial_solution is not None and initial_solution.status in ("OPTIMAL", "FEASIBLE"):
        if _hints_compatible(initial_solution, config, specialites):
            _apply_hints(
                model, initial_solution,
                slot_var, groupe_var, in_group,
                cours_var, tp_day, tp_swap, sub_var,
                spe_to_students,
            )

    # -----------------------------------------------------------------------
    # Résolution
    # -----------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.timeout_seconds
    solver.parameters.log_search_progress = False
    if config.deterministic_mode:
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 42
    else:
        solver.parameters.num_workers = max(1, int(config.num_workers))
        solver.parameters.interleave_search = config.interleave_search
        solver.parameters.linearization_level = config.linearization_level

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
            gr = GroupResult(specialite=spe, groupe_id=g, students=members, slots=assigned_slots)
            if spe in SPE_4_SLOTS:
                # Extraire cours_slots, tp_pairs, tp_assignments, subgroups
                gr.cours_slots = sorted(
                    c for c in range(N_SLOTS) if solver.value(cours_var[(spe, g, c)]) == 1
                )
                tp_pairs_list: list[tuple[int, int]] = []
                tp_assign_list: list[tuple[int, int]] = []
                for pi, (c_a, c_b) in enumerate(VALID_TP_PAIRS):
                    if solver.value(tp_day[(spe, g, pi)]) == 1:
                        tp_pairs_list.append((c_a, c_b))
                        # swap=0 → A sur c_a, B sur c_b ; swap=1 → A sur c_b, B sur c_a
                        if solver.value(tp_swap[(spe, g, pi)]) == 1:
                            tp_assign_list.append((c_b, c_a))
                        else:
                            tp_assign_list.append((c_a, c_b))
                gr.tp_pairs = tp_pairs_list
                gr.tp_assignments = tp_assign_list
                # Sous-groupes A/B depuis sub_var
                a_members: list[Student] = []
                b_members: list[Student] = []
                for (idx, st_obj) in spe_to_students[spe]:
                    if solver.value(groupe_var[(idx, spe)]) != g:
                        continue
                    if solver.value(sub_var[(idx, spe)]) == 0:
                        a_members.append(st_obj)
                    else:
                        b_members.append(st_obj)
                gr.subgroups = {"A": a_members, "B": b_members}
            groups.append(gr)

    n_permanences_students = _count_permanences(groups)
    n_permanences_slots = _count_permanences_slots(groups)
    stats = {
        "n_students": len(students),
        "n_conflicts": _count_conflicts(groups),
        "n_permanences": n_permanences_students,        # rétro-compat
        "n_permanences_students": n_permanences_students,
        "n_permanences_slots": n_permanences_slots,     # métrique proviseur
        "objective": solver.objective_value,
        "wall_time": round(solver.wall_time, 2),
    }
    return SolverResult(status=status, groups=groups, stats=stats, infeasibility_hints=[])


def split_lab_groups(groups: list[GroupResult]) -> list[GroupResult]:
    """Fallback : crée les sous-groupes A/B (split alphabétique) pour SPC/SVT
    quand ils ne sont pas déjà remplis par le solveur. Conservé pour compat.
    """
    for g in groups:
        if g.specialite in SPE_4_SLOTS and not g.subgroups:
            sorted_students = sorted(g.students, key=lambda s: (s.nom, s.prenom))
            mid = (len(sorted_students) + 1) // 2
            g.subgroups = {
                "A": sorted_students[:mid],
                "B": sorted_students[mid:],
            }
            if not g.tp_pairs:
                slots_set = set(g.slots)
                for c_a, c_b in VALID_TP_PAIRS:
                    if c_a in slots_set and c_b in slots_set:
                        g.tp_pairs = [(c_a, c_b)]
                        g.tp_assignments = [(c_a, c_b)]
                        break
                # cours_slots = slots restants
                tp_slots = {c for pair in g.tp_pairs for c in pair}
                g.cours_slots = sorted(c for c in g.slots if c not in tp_slots)
    return groups


def _count_conflicts(groups: list[GroupResult]) -> int:
    """Compte les collisions de créneaux vus par chaque élève.
    Pour SPC/SVT : ne considère que les slots vus par le sous-groupe de l'élève.
    """
    student_slots: dict[str, set[int]] = {}
    conflicts = 0
    for g in groups:
        for st in g.students:
            key = f"{st.nom} {st.prenom}"
            existing = student_slots.get(key, set())
            visible = set(student_visible_slots(st, g))
            overlap = existing & visible
            if overlap:
                conflicts += len(overlap)
            student_slots[key] = existing | visible
    return conflicts


def _count_permanences(groups: list[GroupResult]) -> int:
    """Nombre d'élèves ayant AU MOINS une permanence (métrique historique)."""
    student_slots = _collect_student_slots(groups)
    count = 0
    for _, slots in student_slots.items():
        for c_a, c_b in DOUBLE_SLOT_PAIRS:
            if (c_a in slots) != (c_b in slots):
                count += 1
                break
    return count


def _count_permanences_slots(groups: list[GroupResult]) -> int:
    """Somme des créneaux-permanence sur tous les élèves (métrique proviseur).
    Un élève avec Mardi 8h sans Mardi 10h compte 1. S'il a aussi une perm Jeudi, compte 2.
    """
    student_slots = _collect_student_slots(groups)
    count = 0
    for _, slots in student_slots.items():
        for c_a, c_b in DOUBLE_SLOT_PAIRS:
            if (c_a in slots) != (c_b in slots):
                count += 1
    return count


def _collect_student_slots(groups: list[GroupResult]) -> dict[str, set[int]]:
    """Retourne les slots vus par chaque élève (agrège toutes ses spés).
    Pour SPC/SVT : slots vus dépendent du sous-groupe A/B.
    """
    student_slots: dict[str, set[int]] = {}
    for g in groups:
        for st in g.students:
            key = f"{st.nom} {st.prenom}"
            student_slots.setdefault(key, set()).update(student_visible_slots(st, g))
    return student_slots


def check_hard_constraints(
    groups: list[GroupResult],
    config: SolverConfig,
    parse_result: ParseResult | None = None,
) -> list[Violation]:
    """Vérifie les contraintes dures d'une solution (potentiellement éditée manuellement).

    Utilisé par la vue DnD pour signaler en temps réel les violations introduites
    par un déplacement de groupe. Ne recompute PAS l'affectation des élèves — attend
    des GroupResult déjà peuplés (typiquement issus du solveur, avec slots modifiés).
    """
    violations: list[Violation] = []
    niveau = config.niveau

    def nb_slots_required(spe: str) -> int:
        if niveau == "Terminale":
            return 4 if spe in SPE_4_SLOTS else 3
        return 2

    for g in groups:
        n_req = nb_slots_required(g.specialite)
        slots = list(g.slots)

        # 1. Nombre de créneaux
        if len(slots) != n_req:
            violations.append(Violation(
                code="NB_SLOTS",
                spe=g.specialite,
                groupe_id=g.groupe_id,
                message=f"{g.label} : {len(slots)} créneau(x) affecté(s), {n_req} requis.",
            ))

        # 2. Taille max ≤ 38
        if g.effectif > 38:
            violations.append(Violation(
                code="MAX_SIZE",
                spe=g.specialite,
                groupe_id=g.groupe_id,
                message=f"{g.label} : {g.effectif} élèves (> 38).",
            ))

        # 3. Disponibilité par spé
        avail = config.slot_availability.get(g.specialite, [True] * N_SLOTS)
        for c in slots:
            if 0 <= c < N_SLOTS and not avail[c]:
                violations.append(Violation(
                    code="SLOT_UNAVAILABLE",
                    spe=g.specialite,
                    groupe_id=g.groupe_id,
                    message=(
                        f"{g.label} : créneau Cr{c+1} ({SLOTS[c][1]} {SLOTS[c][2]}) "
                        f"marqué comme indisponible."
                    ),
                ))

        # 4. Deux créneaux même jour (non-SPC/SVT : sur `slots` ; SPC/SVT : sur `cours_slots`)
        if g.specialite in SPE_4_SLOTS:
            cours = set(g.cours_slots)
            for c_a, c_b in SAME_DAY_PAIRS:
                if c_a in cours and c_b in cours:
                    violations.append(Violation(
                        code="SAME_DAY",
                        spe=g.specialite,
                        groupe_id=g.groupe_id,
                        message=(
                            f"{g.label} : deux cours le même jour "
                            f"(Cr{c_a+1} & Cr{c_b+1})."
                        ),
                    ))
            # Paires TP valides
            valid_tp = {frozenset(p) for p in VALID_TP_PAIRS}
            for pair in g.tp_pairs:
                if frozenset(pair) not in valid_tp:
                    violations.append(Violation(
                        code="INVALID_TP_PAIR",
                        spe=g.specialite,
                        groupe_id=g.groupe_id,
                        message=f"{g.label} : paire TP {pair} non valide.",
                    ))
        else:
            slot_set = set(slots)
            for c_a, c_b in SAME_DAY_PAIRS:
                if c_a in slot_set and c_b in slot_set:
                    violations.append(Violation(
                        code="SAME_DAY",
                        spe=g.specialite,
                        groupe_id=g.groupe_id,
                        message=(
                            f"{g.label} : deux créneaux le même jour "
                            f"(Cr{c_a+1} & Cr{c_b+1})."
                        ),
                    ))

    # 5. HLP : 1 Lundi + 1 Jeudi + 1 Autre
    if config.constraint_hlp_philo_days:
        for g in groups:
            if g.specialite != "HLP":
                continue
            slot_set = set(g.slots)
            n_lundi = len(slot_set & LUNDI_SLOTS)
            n_jeudi = len(slot_set & JEUDI_SLOTS)
            n_autre = len(slot_set & AUTRE_SLOTS)
            if n_lundi != 1:
                violations.append(Violation(
                    code="HLP_LUNDI",
                    spe="HLP",
                    groupe_id=g.groupe_id,
                    message=f"{g.label} : {n_lundi} créneau(x) Lundi (attendu 1).",
                ))
            if n_jeudi != 1:
                violations.append(Violation(
                    code="HLP_JEUDI",
                    spe="HLP",
                    groupe_id=g.groupe_id,
                    message=f"{g.label} : {n_jeudi} créneau(x) Jeudi (attendu 1).",
                ))
            if n_autre != 1:
                violations.append(Violation(
                    code="HLP_AUTRE",
                    spe="HLP",
                    groupe_id=g.groupe_id,
                    message=f"{g.label} : {n_autre} créneau(x) hors Lundi/Jeudi (attendu 1).",
                ))

    # 6. Maths créneau commun
    if config.constraint_maths_common_slot:
        maths_groups = [g for g in groups if g.specialite == "Maths"]
        if len(maths_groups) > 1:
            common = set(maths_groups[0].slots)
            for mg in maths_groups[1:]:
                common &= set(mg.slots)
            if config.maths_common_slot_idx not in common:
                violations.append(Violation(
                    code="MATHS_COMMON",
                    spe="Maths",
                    groupe_id=-1,
                    message=(
                        f"Maths : le créneau commun Cr{config.maths_common_slot_idx+1} "
                        f"n'est pas partagé par tous les groupes."
                    ),
                ))

    return violations


def rebuild_from_slot_assignment(
    original: SolverResult,
    new_slots_by_group: dict[str, list[int]],
    config: SolverConfig,
    parse_result: ParseResult | None = None,
) -> SolverResult:
    """Reconstruit un SolverResult après édition manuelle des slots via DnD.

    `new_slots_by_group` : mapping {group.label: [slot_indices]}. Les groupes non
    listés conservent leurs slots d'origine.
    Pour SPC/SVT : les slots ne sont PAS éditables via DnD (v1) — on préserve
    cours_slots/tp_pairs/tp_assignments/subgroups tels quels.
    """
    import copy as _copy

    new_groups: list[GroupResult] = []
    for g in original.groups:
        if g.specialite in SPE_4_SLOTS:
            new_groups.append(_copy.deepcopy(g))
            continue
        new_slots = new_slots_by_group.get(g.label, list(g.slots))
        ng = GroupResult(
            specialite=g.specialite,
            groupe_id=g.groupe_id,
            students=list(g.students),
            slots=sorted(new_slots),
            subgroups=g.subgroups,
            tp_pairs=list(g.tp_pairs),
            tp_assignments=list(g.tp_assignments),
            cours_slots=list(g.cours_slots),
        )
        new_groups.append(ng)

    n_conflicts = _count_conflicts(new_groups)
    n_perm_slots = _count_permanences_slots(new_groups)
    n_perm_students = _count_permanences(new_groups)
    new_stats = dict(original.stats)
    new_stats.update({
        "n_conflicts": n_conflicts,
        "n_permanences": n_perm_students,
        "n_permanences_students": n_perm_students,
        "n_permanences_slots": n_perm_slots,
    })

    return SolverResult(
        status="MANUAL",
        groups=new_groups,
        stats=new_stats,
        infeasibility_hints=[],
    )


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
