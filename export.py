from __future__ import annotations

"""
Génération du fichier XLSX résultat.

Onglets :
  1. Planning groupes  — grille Jour × Spécialité/Groupe
  2. Planning par élève — une ligne par élève avec ses créneaux
  3. Effectifs          — récapitulatif doublettes, groupes, stats
"""

from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from data import SLOTS, ParseResult
from solver import GroupResult, SolverResult

# Couleurs par spécialité
SPE_COLORS: dict[str, str] = {
    "Maths":  "D6E4F0",
    "SPC":    "FAD7A0",
    "SVT":    "A9DFBF",
    "SES":    "F9E79F",
    "HGGSP":  "D2B4DE",
    "HLP":    "FADBD8",
    "LCE":    "D5F5E3",
    "NSI":    "D6EAF8",
}
DEFAULT_COLOR = "EEEEEE"

HEADER_FILL = PatternFill("solid", fgColor="2E4057")
HEADER_FONT = Font(bold=True, color="FFFFFF")
SUBHEADER_FILL = PatternFill("solid", fgColor="7F8C8D")
SUBHEADER_FONT = Font(bold=True, color="FFFFFF")


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _bold() -> Font:
    return Font(bold=True)


def _centered() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


def generate_xlsx(result: SolverResult, parse_result: ParseResult) -> BytesIO:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # type: ignore

    _sheet_planning_groupes(wb, result)
    _sheet_planning_eleves(wb, result, parse_result)
    _sheet_effectifs(wb, result, parse_result)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Onglet 1 : Planning groupes
# ---------------------------------------------------------------------------

def _sheet_planning_groupes(wb: openpyxl.Workbook, result: SolverResult) -> None:
    ws = wb.create_sheet("Planning groupes")

    # Collecte tous les groupes triés par spécialité + id
    groups = sorted(result.groups, key=lambda g: (g.specialite, g.groupe_id))

    # En-têtes colonnes : Jour | Créneau | Groupe1 | Groupe2 | ...
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 18

    headers = ["Jour", "Créneau"] + [g.label for g in groups]
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = _centered()
        ws.column_dimensions[get_column_letter(col)].width = 16

    # Pour chaque créneau, une ligne
    for row_idx, (slot_idx, day, start, end) in enumerate(SLOTS, start=2):
        ws.cell(row=row_idx, column=1, value=f"{day}").alignment = _centered()
        ws.cell(row=row_idx, column=2, value=f"{start}–{end}").alignment = _centered()

        for col_idx, g in enumerate(groups, start=3):
            if slot_idx in g.slots:
                color = SPE_COLORS.get(g.specialite, DEFAULT_COLOR)
                cell = ws.cell(row=row_idx, column=col_idx, value=g.label)
                cell.fill = _fill(color)
                cell.alignment = _centered()
                cell.font = _bold()

    # Freeze première ligne
    ws.freeze_panes = "A2"


# ---------------------------------------------------------------------------
# Onglet 2 : Planning par élève
# ---------------------------------------------------------------------------

def _sheet_planning_eleves(
    wb: openpyxl.Workbook,
    result: SolverResult,
    parse_result: ParseResult,
) -> None:
    ws = wb.create_sheet("Planning par élève")

    slot_labels = [f"{day} {start}" for _, day, start, end in SLOTS]
    headers = ["Nom", "Prénom", "Doublette", "Options"] + slot_labels + ["Nb créneaux"]

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = _centered()

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 28
    for c in range(5, 5 + len(SLOTS) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 14

    student_groups = result.get_student_groups()
    student_slots = result.get_student_slots()

    # Construit un dict nom_prenom → Student depuis parse_result
    student_map = {f"{s.nom} {s.prenom}": s for s in parse_result.students}

    row_idx = 2
    for name in sorted(student_map.keys()):
        st = student_map[name]
        grps = student_groups.get(name, [])
        slots = student_slots.get(name, [])

        ws.cell(row=row_idx, column=1, value=st.nom)
        ws.cell(row=row_idx, column=2, value=st.prenom)
        ws.cell(row=row_idx, column=3, value=" – ".join(st.specialites))
        ws.cell(row=row_idx, column=4, value=", ".join(st.options) if st.options else "")

        for slot_idx in slots:
            col = 5 + slot_idx
            # Trouver quelle spécialité est dans ce créneau pour cet élève
            spe_label = ""
            for g in grps:
                if slot_idx in g.slots:
                    spe_label = g.label
                    break
            cell = ws.cell(row=row_idx, column=col, value=spe_label)
            if spe_label:
                spe = spe_label.split()[0]
                cell.fill = _fill(SPE_COLORS.get(spe, DEFAULT_COLOR))
            cell.alignment = _centered()

        ws.cell(row=row_idx, column=5 + len(SLOTS), value=len(slots))
        row_idx += 1

    ws.freeze_panes = "A2"


# ---------------------------------------------------------------------------
# Onglet 3 : Effectifs
# ---------------------------------------------------------------------------

def _sheet_effectifs(
    wb: openpyxl.Workbook,
    result: SolverResult,
    parse_result: ParseResult,
) -> None:
    ws = wb.create_sheet("Effectifs")
    row = 1

    # ---- Résumé global ----
    ws.cell(row=row, column=1, value="RÉSUMÉ").font = HEADER_FONT
    ws.cell(row=row, column=1).fill = HEADER_FILL
    row += 1
    ws.cell(row=row, column=1, value="Élèves total").font = _bold()
    ws.cell(row=row, column=2, value=result.stats.get("n_students", ""))
    row += 1
    ws.cell(row=row, column=1, value="Conflits de créneaux").font = _bold()
    ws.cell(row=row, column=2, value=result.stats.get("n_conflicts", ""))
    row += 1
    ws.cell(row=row, column=1, value="Élèves avec permanence").font = _bold()
    ws.cell(row=row, column=2, value=result.stats.get("n_permanences", ""))
    row += 1
    ws.cell(row=row, column=1, value="Statut solveur").font = _bold()
    ws.cell(row=row, column=2, value=result.status)
    row += 2

    # ---- Effectifs par groupe ----
    ws.cell(row=row, column=1, value="EFFECTIFS PAR GROUPE").font = HEADER_FONT
    ws.cell(row=row, column=1).fill = HEADER_FILL
    row += 1
    for h, col in [("Groupe", 1), ("Effectif", 2), ("Créneaux", 3)]:
        cell = ws.cell(row=row, column=col, value=h)
        cell.fill = _fill("BDC3C7")
        cell.font = _bold()
    row += 1

    groups = sorted(result.groups, key=lambda g: (g.specialite, g.groupe_id))
    for g in groups:
        slot_str = ", ".join(
            f"{SLOTS[c][1]} {SLOTS[c][2]}" for c in g.slots
        )
        color = SPE_COLORS.get(g.specialite, DEFAULT_COLOR)
        for col, val in [(1, g.label), (2, g.effectif), (3, slot_str)]:
            cell = ws.cell(row=row, column=col, value=val)
            if col <= 2:
                cell.fill = _fill(color)
        row += 1
    row += 1

    # ---- Doublettes ----
    ws.cell(row=row, column=1, value="DOUBLETTES").font = HEADER_FONT
    ws.cell(row=row, column=1).fill = HEADER_FILL
    row += 1
    for h, col in [("Doublette", 1), ("Effectif", 2)]:
        cell = ws.cell(row=row, column=col, value=h)
        cell.fill = _fill("BDC3C7")
        cell.font = _bold()
    row += 1
    for doublette, count in parse_result.doublette_counts.items():
        ws.cell(row=row, column=1, value=doublette)
        ws.cell(row=row, column=2, value=count)
        row += 1
    row += 1

    # ---- Effectifs par spécialité ----
    ws.cell(row=row, column=1, value="PAR SPÉCIALITÉ").font = HEADER_FONT
    ws.cell(row=row, column=1).fill = HEADER_FILL
    row += 1
    for h, col in [("Spécialité", 1), ("Effectif", 2), ("Nb groupes", 3)]:
        cell = ws.cell(row=row, column=col, value=h)
        cell.fill = _fill("BDC3C7")
        cell.font = _bold()
    row += 1
    for spe, count in parse_result.spe_counts.items():
        n_grp = len([g for g in result.groups if g.specialite == spe])
        color = SPE_COLORS.get(spe, DEFAULT_COLOR)
        for col, val in [(1, spe), (2, count), (3, n_grp)]:
            cell = ws.cell(row=row, column=col, value=val)
            cell.fill = _fill(color)
        row += 1

    # Largeurs colonnes
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 40
