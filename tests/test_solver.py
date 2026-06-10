import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from data import Student, ParseResult, SLOTS
from solver import (
    SolverConfig,
    GroupResult,
    solve,
    build_default_config,
    default_slot_availability,
    _count_conflicts,
    SLOT_MATCO,
    SLOT_MATEX,
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
# Tests slot availability defaults
# ---------------------------------------------------------------------------

def test_maths_avail_blocks_matex():
    avail = default_slot_availability("Maths")
    assert avail[SLOT_MATEX] is False
    assert avail[SLOT_MATCO] is True  # Maths peut utiliser Cr4


def test_spc_avail_blocks_matco():
    avail = default_slot_availability("SPC")
    assert avail[SLOT_MATCO] is False
    assert avail[SLOT_MATEX] is True  # SPC peut utiliser Cr1


def test_svt_avail_blocks_matco():
    avail = default_slot_availability("SVT")
    assert avail[SLOT_MATCO] is False


# ---------------------------------------------------------------------------
# Test simple : 6 élèves, 2 spés, 1 groupe chacun
# ---------------------------------------------------------------------------

def test_simple_no_conflict():
    students = [_make_student(f"E{i}", ["Maths", "SES"]) for i in range(6)]
    pr = _make_parse_result(students)
    config = SolverConfig(
        nb_groups={"Maths": 1, "SES": 1},
        slot_availability={
            "Maths": [True, False, True, True, True, True, True, True, True],
            "SES": [True, False, True, True, True, True, True, True, True],
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
    result = solve(pr, config)
    assert result.status in ("OPTIMAL", "FEASIBLE"), f"Status: {result.status}, hints: {result.infeasibility_hints}"


def test_real_data_no_conflicts():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    result = solve(pr, config)
    assert _count_conflicts(result.groups) == 0


def test_real_data_group_sizes():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    result = solve(pr, config)
    for g in result.groups:
        assert g.effectif <= 38, f"{g.label} a {g.effectif} élèves (> 38)"
        assert g.effectif > 0, f"{g.label} est vide"


def test_real_data_all_students_placed():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
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
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    result = solve(pr, config)
    ALLOWED_HLP = {0, 5, 6}  # Lundi, Jeudi
    for g in result.groups:
        if g.specialite == "HLP":
            for c in g.slots:
                assert c in ALLOWED_HLP, f"HLP slot Cr{c} interdit (doit être Lundi ou Jeudi)"


def test_real_data_maths_common_slot():
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    config.constraint_maths_common_slot = True
    result = solve(pr, config)
    maths_groups = [g for g in result.groups if g.specialite == "Maths"]
    if len(maths_groups) > 1:
        common = set(maths_groups[0].slots)
        for g in maths_groups[1:]:
            common &= set(g.slots)
        assert len(common) >= 1, "Les groupes Maths n'ont aucun créneau commun"


def test_spc_svt_matco_free():
    """Les élèves SPC-SVT doivent avoir Cr4 (matco) libre."""
    from data import parse_xlsx
    pr = parse_xlsx(XLSX_PATH)
    config = build_default_config(pr)
    result = solve(pr, config)
    spc_slots: dict[int, list[int]] = {}
    svt_slots: dict[int, list[int]] = {}
    for g in result.groups:
        if g.specialite == "SPC":
            spc_slots[g.groupe_id] = g.slots
        elif g.specialite == "SVT":
            svt_slots[g.groupe_id] = g.slots
    # Vérifier qu'aucun groupe SPC ou SVT n'utilise Cr4
    for gid, slots in spc_slots.items():
        assert SLOT_MATCO not in slots, f"SPC groupe {gid} utilise Cr4 (matco)"
    for gid, slots in svt_slots.items():
        assert SLOT_MATCO not in slots, f"SVT groupe {gid} utilise Cr4 (matco)"
