import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from data import Student, ParseResult, SLOTS, SAME_DAY_PAIRS, VALID_TP_PAIRS, SPE_4_SLOTS, LUNDI_SLOTS, JEUDI_SLOTS, AUTRE_SLOTS
from solver import (
    SolverConfig,
    GroupResult,
    SolverResult,
    Violation,
    solve,
    build_default_config,
    default_slot_availability,
    split_lab_groups,
    check_hard_constraints,
    rebuild_from_slot_assignment,
    _count_conflicts,
    SLOT_MATCO,
    SLOT_MATEX,
    N_SLOTS,
)

XLSX_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "TERMINALES-rentrée2026.xlsx")


def _make_student(nom: str, spes: list[str], options: list[str] = None) -> Student:
    return Student(
        nom=nom, prenom="Test", classe_origine="Terminale A",
        specialites=spes, options=options or [], niveau="Terminale",
    )


def _make_parse_result(students: list[Student]) -> ParseResult:
    spe_counts: dict[str, int] = {}
    doublette_counts: dict[str, int] = {}
    for s in students:
        key = " – ".join(sorted(s.specialites))
        doublette_counts[key] = doublette_counts.get(key, 0) + 1
        for spe in s.specialites:
            spe_counts[spe] = spe_counts.get(spe, 0) + 1
    return ParseResult(
        students=students,
        niveau="Terminale",
        doublette_counts=doublette_counts,
        spe_counts=spe_counts,
        warnings=[],
    )


# ---------------------------------------------------------------------------
# Tests slot availability defaults — tous True après correctif 1
# ---------------------------------------------------------------------------

def test_default_avail_all_true_maths():
    avail = default_slot_availability("Maths")
    assert all(avail), "Maths : tous les créneaux doivent être disponibles par défaut"


def test_default_avail_all_true_spc():
    avail = default_slot_availability("SPC")
    assert all(avail), "SPC : tous les créneaux doivent être disponibles par défaut"


def test_default_avail_all_true_svt():
    avail = default_slot_availability("SVT")
    assert all(avail), "SVT : tous les créneaux doivent être disponibles par défaut"


def test_default_avail_length():
    for spe in ("Maths", "SPC", "SVT", "SES", "HLP", "LCE", "NSI", "HGGSP"):
        avail = default_slot_availability(spe)
        assert len(avail) == N_SLOTS


# ---------------------------------------------------------------------------
# Test simple : 6 élèves, 2 spés, 1 groupe chacun
# ---------------------------------------------------------------------------

def test_simple_no_conflict():
    students = [_make_student(f"E{i}", ["Maths", "SES"]) for i in range(6)]
    pr = _make_parse_result(students)
    config = SolverConfig(
        nb_groups={"Maths": 1, "SES": 1},
        slot_availability={
            "Maths": [True, False, True, True, True, True, True, True, True, True],
            "SES": [True, False, True, True, True, True, True, True, True, True],
        },
        constraint_maths_common_slot=False,
        constraint_hlp_philo_days=False,
        constraint_lce_no_early=False,
        timeout_seconds=30,
        niveau="Terminale",
    )
    result = solve(pr, config)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert _count_conflicts(result.groups) == 0
    # Maths et SES ne partagent aucun créneau
    maths_slots = set(result.groups[0].slots if result.groups[0].specialite == "Maths"
                      else result.groups[1].slots)
    ses_slots = set(result.groups[1].slots if result.groups[1].specialite == "SES"
                    else result.groups[0].slots)
    assert len(maths_slots & ses_slots) == 0


# ---------------------------------------------------------------------------
# Test sur données réelles
# ---------------------------------------------------------------------------

def test_real_data_feasible():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    assert result.status in ("OPTIMAL", "FEASIBLE"), f"Status: {result.status}, hints: {result.infeasibility_hints}"


def test_real_data_no_conflicts():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    assert _count_conflicts(result.groups) == 0


def test_real_data_group_sizes():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    for g in result.groups:
        assert g.effectif <= 38, f"{g.label} a {g.effectif} élèves (> 38)"
        assert g.effectif > 0, f"{g.label} est vide"


def test_real_data_all_students_placed():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    # Chaque élève doit être dans exactement un groupe par spécialité
    placed: dict[str, set[str]] = {}
    for g in result.groups:
        for s in g.students:
            key = f"{s.nom} {s.prenom}"
            placed.setdefault(g.specialite, set()).add(key)
    for s in pr.students:
        for spe in s.specialites:
            key = f"{s.nom} {s.prenom}"
            assert key in placed.get(spe, set()), f"{key} non placé en {spe}"


def test_real_data_hlp_on_allowed_days():
    """HLP : exactement 1 slot Lundi, 1 slot Jeudi, 1 slot autre jour (jamais 2 le même jour)."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    for g in result.groups:
        if g.specialite == "HLP":
            slots = set(g.slots)
            n_lundi = len(slots & LUNDI_SLOTS)
            n_jeudi = len(slots & JEUDI_SLOTS)
            n_autre = len(slots & AUTRE_SLOTS)
            assert n_lundi == 1, f"HLP groupe {g.groupe_id+1} : {n_lundi} slot(s) Lundi (attendu 1)"
            assert n_jeudi == 1, f"HLP groupe {g.groupe_id+1} : {n_jeudi} slot(s) Jeudi (attendu 1)"
            assert n_autre == 1, f"HLP groupe {g.groupe_id+1} : {n_autre} slot(s) autre jour (attendu 1)"


def test_real_data_maths_common_slot():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    config.constraint_maths_common_slot = True
    result = solve(pr, config)
    maths_groups = [g for g in result.groups if g.specialite == "Maths"]
    if len(maths_groups) > 1:
        common = set(maths_groups[0].slots)
        for g in maths_groups[1:]:
            common &= set(g.slots)
        assert len(common) >= 1, "Les groupes Maths n'ont aucun créneau commun"


# ---------------------------------------------------------------------------
# Test correctif 2 — contrainte "pas 2 créneaux le même jour"
# ---------------------------------------------------------------------------

def test_no_double_slot_same_day():
    """Aucun groupe (hors SPC/SVT) ne doit avoir 2 créneaux le même jour.
    SPC/SVT ont 1 ou 2 paires TP valides (VALID_TP_PAIRS)."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    for g in result.groups:
        if g.specialite in SPE_4_SLOTS:
            # Doit avoir 1 ou 2 paires TP valides (sans chevauchement)
            assert 1 <= len(g.tp_pairs) <= 2, f"{g.label} : {len(g.tp_pairs)} paire(s) TP (attendu 1 ou 2)"
            valid_sets = {frozenset(p) for p in VALID_TP_PAIRS}
            for c_a, c_b in g.tp_pairs:
                assert frozenset((c_a, c_b)) in valid_sets, f"{g.label} : paire {(c_a, c_b)} invalide"
            # Cours slots : 1 max par jour
            for c_a, c_b in SAME_DAY_PAIRS:
                assert not (c_a in g.cours_slots and c_b in g.cours_slots), (
                    f"{g.label} a 2 cours même jour ({c_a}, {c_b})"
                )
        else:
            for c_a, c_b in SAME_DAY_PAIRS:
                assert not (c_a in g.slots and c_b in g.slots), (
                    f"{g.label} utilise Cr{c_a+1} ET Cr{c_b+1} (même jour)"
                )


# ---------------------------------------------------------------------------
# Test correctif 3 — taille min garantie
# ---------------------------------------------------------------------------

def test_min_group_size():
    """Chaque groupe doit avoir au moins floor(N/G) élèves."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    spe_students: dict[str, int] = {}
    spe_groups: dict[str, list[int]] = {}
    for g in result.groups:
        spe_groups.setdefault(g.specialite, []).append(g.effectif)
        spe_students[g.specialite] = spe_students.get(g.specialite, 0) + g.effectif
    for spe, effectifs in spe_groups.items():
        G = len(effectifs)
        if G < 2:
            continue
        min_size = spe_students[spe] // G
        for e in effectifs:
            assert e >= min_size, f"{spe} : groupe de {e} < min_size={min_size}"


# ---------------------------------------------------------------------------
# Test correctif 4 — sous-groupes SPC/SVT
# ---------------------------------------------------------------------------

def test_split_lab_groups_creates_subgroups():
    """split_lab_groups() (fallback) crée les sous-groupes A/B alphabétiques."""
    students_a = [
        Student(nom=f"Z{i}", prenom="T", classe_origine="", specialites=["SPC", "SVT"],
                options=[], niveau="Terminale")
        for i in range(10)
    ]
    g = GroupResult(specialite="SPC", groupe_id=0, students=students_a, slots=[2, 3, 5, 6])
    split_lab_groups([g])
    assert g.subgroups is not None
    assert "A" in g.subgroups and "B" in g.subgroups
    total = len(g.subgroups["A"]) + len(g.subgroups["B"])
    assert total == 10
    assert len(g.subgroups["A"]) >= len(g.subgroups["B"])


def test_split_lab_groups_alphabetic():
    """Le split de secours est alphabétique par nom (quand solveur n'a rien rempli)."""
    students_a = [
        Student(nom=name, prenom="T", classe_origine="", specialites=["SVT", "SES"],
                options=[], niveau="Terminale")
        for name in ["Martin", "Dupont", "Bernard", "Adam"]
    ]
    g = GroupResult(specialite="SVT", groupe_id=0, students=students_a, slots=[0, 2, 5, 7])
    split_lab_groups([g])
    assert g.subgroups["A"][0].nom == "Adam"
    assert g.subgroups["B"][-1].nom == "Martin"


def test_split_lab_groups_no_subgroups_for_others():
    """Les spés non SPC/SVT ne doivent pas avoir de sous-groupes."""
    students_a = [
        Student(nom=f"E{i}", prenom="T", classe_origine="", specialites=["Maths", "SES"],
                options=[], niveau="Terminale")
        for i in range(5)
    ]
    g = GroupResult(specialite="Maths", groupe_id=0, students=students_a, slots=[2, 5, 7])
    split_lab_groups([g])
    assert g.subgroups is None


def test_real_data_subgroups_created():
    """Les groupes SPC/SVT ont des sous-groupes A/B après solve()."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    for g in result.groups:
        if g.specialite in SPE_4_SLOTS:
            assert g.subgroups is not None, f"{g.label} : sous-groupes manquants"
            assert len(g.subgroups.get("A", [])) > 0
            assert len(g.subgroups.get("B", [])) > 0


def test_spc_svt_has_tp_pairs():
    """Chaque groupe SPC/SVT a 1 ou 2 paires TP identifiées (tp_pairs) parmi VALID_TP_PAIRS."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    valid_tp_sets = {frozenset(p) for p in VALID_TP_PAIRS}
    for g in result.groups:
        if g.specialite in SPE_4_SLOTS:
            assert 1 <= len(g.tp_pairs) <= 2, f"{g.label} : {len(g.tp_pairs)} paires (attendu 1 ou 2)"
            for c_a, c_b in g.tp_pairs:
                assert c_a in g.slots and c_b in g.slots, f"{g.label} : paire {(c_a, c_b)} hors slots {g.slots}"
                assert frozenset((c_a, c_b)) in valid_tp_sets, f"{g.label} : paire {(c_a, c_b)} invalide"
            # tp_assignments cohérent
            assert len(g.tp_assignments) == len(g.tp_pairs)
            for (sa, sb), pair in zip(g.tp_assignments, g.tp_pairs):
                assert {sa, sb} == set(pair), f"{g.label} : assignment {(sa, sb)} ne matche pas paire {pair}"


def test_spc_svt_subgroups_from_solver():
    """Les sous-groupes A/B sont remplis par le solveur (pas par split_lab_groups)."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    for g in result.groups:
        if g.specialite in SPE_4_SLOTS:
            assert g.subgroups is not None
            n_a = len(g.subgroups.get("A", []))
            n_b = len(g.subgroups.get("B", []))
            assert abs(n_a - n_b) <= 1, f"{g.label} : |A|={n_a} |B|={n_b} déséquilibré"
            assert n_a + n_b == g.effectif
            assert len(g.cours_slots) == 4 - len(g.tp_pairs)


def test_permanences_slots_metric():
    """La métrique n_permanences_slots compte la somme des créneaux-permanence."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 30
    result = solve(pr, config)
    assert "n_permanences_slots" in result.stats
    assert "n_permanences_students" in result.stats
    # somme des créneaux ≥ nb élèves concernés
    assert result.stats["n_permanences_slots"] >= result.stats["n_permanences_students"]


# ---------------------------------------------------------------------------
# Tests check_hard_constraints + rebuild_from_slot_assignment
# ---------------------------------------------------------------------------

def _default_config_all_avail(niveau: str = "Terminale") -> SolverConfig:
    return SolverConfig(
        nb_groups={},
        slot_availability={},
        constraint_lce_no_early=False,
        constraint_hlp_philo_days=True,
        constraint_maths_common_slot=True,
        maths_common_slot_idx=SLOT_MATCO,
        niveau=niveau,
    )


def _hlp_valid_slots() -> list[int]:
    # 1 Lundi (Cr0), 1 Jeudi (Cr5), 1 Autre (Cr2)
    return [0, 2, 5]


def test_check_valid_no_violations():
    g = GroupResult(specialite="SES", groupe_id=0, students=[], slots=[2, 5, 7])
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    violations = check_hard_constraints([g], config)
    assert violations == []


def test_check_nb_slots_mismatch():
    g = GroupResult(specialite="SES", groupe_id=0, students=[], slots=[2, 5])  # 2 au lieu de 3
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    violations = check_hard_constraints([g], config)
    codes = [v.code for v in violations]
    assert "NB_SLOTS" in codes


def test_check_same_day():
    g = GroupResult(specialite="SES", groupe_id=0, students=[], slots=[2, 3, 5])  # Mardi 8h + Mardi 10h
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    violations = check_hard_constraints([g], config)
    codes = [v.code for v in violations]
    assert "SAME_DAY" in codes


def test_check_hlp_missing_lundi():
    g = GroupResult(specialite="HLP", groupe_id=0, students=[], slots=[2, 5, 7])  # pas de Lundi
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    violations = check_hard_constraints([g], config)
    codes = [v.code for v in violations]
    assert "HLP_LUNDI" in codes


def test_check_hlp_valid():
    g = GroupResult(specialite="HLP", groupe_id=0, students=[], slots=_hlp_valid_slots())
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    violations = check_hard_constraints([g], config)
    assert [v for v in violations if v.code.startswith("HLP")] == []


def test_check_maths_common_violated():
    g1 = GroupResult(specialite="Maths", groupe_id=0, students=[], slots=[2, 5, 7])  # pas Cr4
    g2 = GroupResult(specialite="Maths", groupe_id=1, students=[], slots=[2, 5, 8])
    config = _default_config_all_avail()
    config.maths_common_slot_idx = 4  # Cr4
    violations = check_hard_constraints([g1, g2], config)
    codes = [v.code for v in violations]
    assert "MATHS_COMMON" in codes


def test_check_maths_common_ok():
    g1 = GroupResult(specialite="Maths", groupe_id=0, students=[], slots=[2, 4, 5])
    g2 = GroupResult(specialite="Maths", groupe_id=1, students=[], slots=[0, 4, 7])
    config = _default_config_all_avail()
    config.maths_common_slot_idx = 4
    violations = check_hard_constraints([g1, g2], config)
    assert [v for v in violations if v.code == "MATHS_COMMON"] == []


def test_check_slot_unavailable():
    g = GroupResult(specialite="SES", groupe_id=0, students=[], slots=[2, 5, 7])
    config = _default_config_all_avail()
    config.constraint_maths_common_slot = False
    config.slot_availability = {"SES": [True]*N_SLOTS}
    config.slot_availability["SES"][2] = False  # Cr3 (index 2) indisponible
    violations = check_hard_constraints([g], config)
    codes = [v.code for v in violations]
    assert "SLOT_UNAVAILABLE" in codes


def test_rebuild_updates_slots_and_stats():
    students = [_make_student(f"E{i}", ["SES", "HGGSP"]) for i in range(5)]
    g_ses = GroupResult(specialite="SES", groupe_id=0, students=students, slots=[2, 5, 7])
    g_hg = GroupResult(specialite="HGGSP", groupe_id=0, students=students, slots=[0, 3, 6])
    original = SolverResult(
        status="OPTIMAL",
        groups=[g_ses, g_hg],
        stats={"n_conflicts": 0, "n_permanences_slots": 0, "n_permanences_students": 0},
        infeasibility_hints=[],
    )
    config = _default_config_all_avail()
    # Déplace SES sur les mêmes créneaux que HGGSP → conflits
    rebuilt = rebuild_from_slot_assignment(
        original,
        {"SES 1": [0, 3, 6]},
        config,
    )
    assert rebuilt.status == "MANUAL"
    ses_new = next(g for g in rebuilt.groups if g.specialite == "SES")
    assert set(ses_new.slots) == {0, 3, 6}
    # Conflit détecté sur 3 créneaux × 5 élèves
    assert rebuilt.stats["n_conflicts"] > 0


def test_rebuild_preserves_spc_svt_slots():
    students = [_make_student(f"E{i}", ["SPC", "SVT"]) for i in range(4)]
    g = GroupResult(
        specialite="SPC", groupe_id=0, students=students,
        slots=[0, 2, 3, 5],
        cours_slots=[0, 5], tp_pairs=[(2, 3)], tp_assignments=[(2, 3)],
        subgroups={"A": students[:2], "B": students[2:]},
    )
    original = SolverResult(
        status="OPTIMAL", groups=[g],
        stats={}, infeasibility_hints=[],
    )
    config = _default_config_all_avail()
    # Même si on tente de bouger SPC, ses slots sont préservés
    rebuilt = rebuild_from_slot_assignment(original, {"SPC 1": [7, 8]}, config)
    spc_new = rebuilt.groups[0]
    assert sorted(spc_new.slots) == [0, 2, 3, 5]
    assert spc_new.tp_pairs == [(2, 3)]


def test_determinism():
    """Deux runs en mode déterministe donnent exactement les mêmes slots."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 15
    config.deterministic_mode = True
    r1 = solve(pr, config)
    r2 = solve(pr, config)
    assert r1.status == r2.status
    slots1 = {g.label: sorted(g.slots) for g in r1.groups}
    slots2 = {g.label: sorted(g.slots) for g in r2.groups}
    assert slots1 == slots2, "Les deux runs donnent des slots différents (non-déterministe)"


# ---------------------------------------------------------------------------
# Test warm-start
# ---------------------------------------------------------------------------

def test_warm_start_no_worse_conflicts():
    """Un run avec warm-start ne doit pas produire plus de conflits que le run initial."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 15
    r1 = solve(pr, config)
    assert r1.status in ("OPTIMAL", "FEASIBLE")
    r2 = solve(pr, config, initial_solution=r1)
    assert r2.status in ("OPTIMAL", "FEASIBLE")
    assert _count_conflicts(r2.groups) <= _count_conflicts(r1.groups)


def test_warm_start_with_none_is_identical_signature():
    """Passer initial_solution=None doit donner le même comportement qu'avant."""
    students = [_make_student(f"E{i}", ["Maths", "SES"]) for i in range(6)]
    pr = _make_parse_result(students)
    config = SolverConfig(
        nb_groups={"Maths": 1, "SES": 1},
        slot_availability={
            "Maths": [True, False, True, True, True, True, True, True, True, True],
            "SES": [True, False, True, True, True, True, True, True, True, True],
        },
        constraint_maths_common_slot=False,
        constraint_hlp_philo_days=False,
        constraint_lce_no_early=False,
        timeout_seconds=15,
        niveau="Terminale",
    )
    result = solve(pr, config, initial_solution=None)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert _count_conflicts(result.groups) == 0


def test_solver_config_new_fields():
    """SolverConfig accepte les nouveaux champs avec leurs valeurs par défaut."""
    config = SolverConfig(nb_groups={}, slot_availability={})
    assert config.interleave_search is False
    assert config.linearization_level == 1


def test_linearization_level_produces_valid_result():
    """linearization_level=2 doit toujours produire une solution valide."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.timeout_seconds = 15
    config.linearization_level = 2
    result = solve(pr, config)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert _count_conflicts(result.groups) == 0
