import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from data import parse_xlsx, parse_doublette, normalize_spe, SLOTS, N_SLOTS

XLSX_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "TERMINALES-rentrée2026.xlsx")


def test_slots_count():
    assert N_SLOTS == 9


def test_normalize_spe():
    assert normalize_spe("M") == "Maths"
    assert normalize_spe("HG") == "HGGSP"
    assert normalize_spe("SPC") == "SPC"
    assert normalize_spe("Inconnu") == "Inconnu"


def test_parse_doublette_standard():
    assert parse_doublette("8) Maths - SPC") == ["Maths", "SPC"]
    assert parse_doublette("3) HGGSP - SES") == ["HGGSP", "SES"]
    assert parse_doublette("12) SPC - SVT") == ["SPC", "SVT"]


def test_parse_doublette_triplette():
    result = parse_doublette("1) Maths - SPC - SVT")
    assert len(result) == 3
    assert "Maths" in result


def test_parse_xlsx_student_count():
    result = parse_xlsx(XLSX_PATH)
    # 151 lignes - 1 header - 1 changement établissement - 1 hors lycée = 149
    assert len(result.students) == 149


def test_parse_xlsx_niveau():
    result = parse_xlsx(XLSX_PATH)
    assert result.niveau == "Terminale"


def test_parse_xlsx_doublettes():
    result = parse_xlsx(XLSX_PATH)
    # Maths-SPC est la doublette la plus fréquente
    most_common = max(result.doublette_counts, key=lambda k: result.doublette_counts[k])
    assert "Maths" in most_common and "SPC" in most_common


def test_parse_xlsx_spe_counts():
    result = parse_xlsx(XLSX_PATH)
    # Maths est la spé la plus fréquente
    assert result.spe_counts["Maths"] == 76
    assert result.spe_counts["SPC"] >= 70  # 74 ou 75 selon version
    assert result.spe_counts["HLP"] == 8


def test_no_matex_without_maths():
    result = parse_xlsx(XLSX_PATH)
    for s in result.students:
        if "Maths expertes" in s.options:
            assert "Maths" in s.specialites, f"{s.nom}: Maths expertes sans Maths spé"


def test_spc_svt_no_matex():
    result = parse_xlsx(XLSX_PATH)
    for s in result.students:
        if s.is_spc_svt:
            assert "Maths expertes" not in s.options, f"{s.nom}: SPC-SVT avec Maths expertes"


def test_warnings_for_excluded_students():
    result = parse_xlsx(XLSX_PATH)
    # On doit avoir des warnings pour les élèves exclus
    warning_text = " ".join(result.warnings).lower()
    assert "changement" in warning_text or "don bosco" in warning_text
