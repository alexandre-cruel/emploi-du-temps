from __future__ import annotations

"""
Application Streamlit — Emploi du temps lycée
5 étapes séquentielles : Import → Config → Résolution → Ajustements → Export
"""

import copy
from io import BytesIO

import pandas as pd
import streamlit as st

import data as _data
import solver as _solver
import export as _export

N_SLOTS = len(_data.SLOTS)

st.set_page_config(
    page_title="Emploi du temps lycée",
    page_icon="📅",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Helpers session_state
# ---------------------------------------------------------------------------

def _init_state() -> None:
    defaults = {
        "step": 1,
        "parse_result": None,
        "config": None,
        "solver_result": None,
        "manual_groups": None,   # dict[str, list[str]] : spe → liste des noms dans chaque groupe
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _go_step(n: int) -> None:
    st.session_state["step"] = n


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _step_indicator(current: int) -> None:
    steps = ["1 Import", "2 Configuration", "3 Résolution", "4 Ajustements", "5 Export"]
    cols = st.columns(len(steps))
    for i, (col, label) in enumerate(zip(cols, steps), start=1):
        with col:
            if i < current:
                st.success(f"✓ {label}")
            elif i == current:
                st.info(f"▶ {label}")
            else:
                st.markdown(f"<div style='color:#aaa;text-align:center'>{label}</div>", unsafe_allow_html=True)
    st.divider()


# ---------------------------------------------------------------------------
# Étape 1 — Import
# ---------------------------------------------------------------------------

def step_import() -> None:
    st.title("📂 Import du fichier élèves")
    st.markdown(
        "Déposez le fichier Excel contenant les vœux des élèves "
        "(onglet **'Voeux élèves'** avec colonnes : Nom, Prénom, Classe, Q1, Q2 (doublette), Q4, Q5)."
    )

    uploaded = st.file_uploader("Choisir un fichier .xlsx", type=["xlsx"])

    if uploaded is not None:
        with st.spinner("Lecture du fichier..."):
            try:
                buf = BytesIO(uploaded.read())
                result = _data.parse_xlsx(buf)
                st.session_state["parse_result"] = result
            except Exception as e:
                st.error(f"Erreur lors de la lecture : {e}")
                return

        # Avertissements
        if result.warnings:
            with st.expander(f"⚠️ {len(result.warnings)} avertissement(s)", expanded=False):
                for w in result.warnings:
                    st.warning(w)

        # Résumé
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Élèves", len(result.students))
        with col2:
            st.metric("Doublettes uniques", len(result.doublette_counts))
        with col3:
            st.metric("Niveau détecté", result.niveau)

        # Doublettes
        st.subheader("Répartition par doublette")
        df_doublettes = pd.DataFrame(
            list(result.doublette_counts.items()),
            columns=["Doublette", "Effectif"],
        ).sort_values("Effectif", ascending=False)
        st.dataframe(df_doublettes, use_container_width=True, hide_index=True)

        # Spécialités
        st.subheader("Effectifs par spécialité")
        df_spes = pd.DataFrame(
            list(result.spe_counts.items()),
            columns=["Spécialité", "Effectif"],
        ).sort_values("Effectif", ascending=False)
        st.dataframe(df_spes, use_container_width=True, hide_index=True)

        # Aperçu élèves
        with st.expander("Aperçu des élèves importés"):
            rows = [
                {
                    "Nom": s.nom,
                    "Prénom": s.prenom,
                    "Classe": s.classe_origine,
                    "Doublette": " – ".join(s.specialites),
                    "Options": ", ".join(s.options),
                }
                for s in result.students
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.button(
            "Suivant : Configuration →",
            on_click=_go_step, args=(2,),
            type="primary",
        )


# ---------------------------------------------------------------------------
# Étape 2 — Configuration
# ---------------------------------------------------------------------------

def step_config() -> None:
    st.title("⚙️ Configuration")
    result = st.session_state["parse_result"]
    if result is None:
        st.error("Revenez à l'étape 1.")
        return

    specialites = result.all_specialites
    spe_counts = result.spe_counts

    # Récupère config existante ou construit par défaut
    existing_config: _solver.SolverConfig = (
        st.session_state["config"]
        or _solver.build_default_config(result)
    )

    st.subheader("2a. Nombre de groupes par spécialité")
    st.caption("Taille max recommandée : 38 élèves par groupe.")

    nb_groups_input: dict[str, int] = {}
    slot_avail_input: dict[str, list[bool]] = {}

    cols = st.columns(4)
    for i, spe in enumerate(specialites):
        total = spe_counts.get(spe, 0)
        default_n = existing_config.nb_groups.get(spe, max(1, -(-total // 38)))
        with cols[i % 4]:
            n = st.number_input(
                f"{spe} ({total} élèves)",
                min_value=1,
                max_value=10,
                value=default_n,
                step=1,
                key=f"nb_groups_{spe}",
            )
            nb_groups_input[spe] = n
            effective = total / n if n > 0 else 0
            color = "🔴" if effective > 38 else "🟡" if effective > 32 else "🟢"
            st.caption(f"{color} ~{effective:.1f} élèves/groupe")

    st.divider()
    st.subheader("2b. Créneaux disponibles par spécialité")
    st.caption(
        "Cochez les créneaux utilisables pour chaque spécialité. "
        "Cr1 (Lundi 15h50) est réservé aux options Maths expertes/DGEMC. "
        "Cr4 (Mercredi 8h10) est réservé à Maths complémentaires."
    )

    slot_labels_short = [
        f"Cr{c}: {_data.SLOTS[c][1][:2]} {_data.SLOTS[c][2]}"
        for c in range(N_SLOTS)
    ]

    # Tableau de disponibilité
    avail_tab_cols = st.columns([2] + [1] * N_SLOTS)
    with avail_tab_cols[0]:
        st.markdown("**Spécialité**")
    for c, lbl in enumerate(slot_labels_short):
        with avail_tab_cols[c + 1]:
            st.markdown(f"<small>{lbl}</small>", unsafe_allow_html=True)

    for spe in specialites:
        row_cols = st.columns([2] + [1] * N_SLOTS)
        with row_cols[0]:
            st.markdown(f"**{spe}**")
        default_avail = existing_config.slot_availability.get(
            spe, _solver.default_slot_availability(spe)
        )
        avail_row: list[bool] = []
        for c in range(N_SLOTS):
            with row_cols[c + 1]:
                val = st.checkbox(
                    "",
                    value=bool(default_avail[c]),
                    key=f"avail_{spe}_{c}",
                    label_visibility="collapsed",
                )
                avail_row.append(val)
        slot_avail_input[spe] = avail_row

    st.divider()
    st.subheader("2c. Contraintes spéciales")

    col_c1, col_c2 = st.columns(2)
    with col_c1:
        lce_no_early = st.checkbox(
            "LCE : éviter les créneaux 8h (Mardi/Jeudi/Vendredi)",
            value=existing_config.constraint_lce_no_early,
            help="Préférence du prof de LCE : ne pas commencer à 8h les jours de semaine.",
        )
        hlp_philo = st.checkbox(
            "HLP-Philo : seulement Lundi et Jeudi",
            value=existing_config.constraint_hlp_philo_days,
            help="Le prof de philo HLP n'est présent que le lundi et le jeudi.",
        )
    with col_c2:
        maths_common = st.checkbox(
            "Maths : imposer un créneau commun à tous les groupes",
            value=existing_config.constraint_maths_common_slot,
            help="Permet aux profs de Maths d'organiser des évaluations communes.",
        )
        if maths_common:
            common_options = [
                f"Cr{c}: {_data.SLOTS[c][1]} {_data.SLOTS[c][2]}"
                for c in range(N_SLOTS)
                if slot_avail_input.get("Maths", _solver.default_slot_availability("Maths"))[c]
            ]
            if common_options:
                default_cr = f"Cr{existing_config.maths_common_slot_idx}: " \
                             f"{_data.SLOTS[existing_config.maths_common_slot_idx][1]} " \
                             f"{_data.SLOTS[existing_config.maths_common_slot_idx][2]}"
                default_idx = common_options.index(default_cr) if default_cr in common_options else 0
                selected = st.selectbox(
                    "Créneau commun Maths",
                    common_options,
                    index=default_idx,
                    key="maths_common_slot",
                )
                maths_common_idx = int(selected.split(":")[0].replace("Cr", ""))
            else:
                maths_common_idx = existing_config.maths_common_slot_idx
        else:
            maths_common_idx = existing_config.maths_common_slot_idx

    timeout = st.slider("Timeout solveur (secondes)", 10, 300, existing_config.timeout_seconds, step=10)

    st.divider()
    if st.button("Suivant : Résoudre →", type="primary"):
        new_config = _solver.SolverConfig(
            nb_groups=nb_groups_input,
            slot_availability=slot_avail_input,
            constraint_lce_no_early=lce_no_early,
            constraint_hlp_philo_days=hlp_philo,
            constraint_maths_common_slot=maths_common,
            maths_common_slot_idx=maths_common_idx,
            timeout_seconds=timeout,
            niveau=result.niveau,
        )
        st.session_state["config"] = new_config
        st.session_state["solver_result"] = None  # reset
        _go_step(3)
        st.rerun()

    if st.button("← Retour Import"):
        _go_step(1)
        st.rerun()


# ---------------------------------------------------------------------------
# Étape 3 — Résolution
# ---------------------------------------------------------------------------

def step_solve() -> None:
    st.title("⚡ Résolution")
    result = st.session_state["parse_result"]
    config = st.session_state["config"]

    if result is None or config is None:
        st.error("Revenez à l'étape 1 ou 2.")
        return

    solver_result = st.session_state.get("solver_result")

    if solver_result is None:
        with st.spinner(f"Résolution en cours (timeout : {config.timeout_seconds}s)..."):
            solver_result = _solver.solve(result, config)
            st.session_state["solver_result"] = solver_result
            st.session_state["manual_groups"] = None

    # Statut
    status = solver_result.status
    if status == "OPTIMAL":
        st.success("✅ Solution optimale trouvée !")
    elif status == "FEASIBLE":
        st.warning("⚠️ Solution trouvée (non optimale — timeout atteint).")
    else:
        st.error(f"❌ Aucune solution trouvée ({status}).")
        if solver_result.infeasibility_hints:
            st.subheader("Causes possibles :")
            for h in solver_result.infeasibility_hints:
                st.markdown(f"- {h}")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("← Modifier la configuration"):
                _go_step(2)
                st.rerun()
        with col_b:
            if st.button("🔄 Relancer"):
                st.session_state["solver_result"] = None
                st.rerun()
        return

    # Métriques
    stats = solver_result.stats
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Élèves", stats.get("n_students", ""))
    col2.metric("Conflits", stats.get("n_conflicts", 0), delta_color="inverse")
    col3.metric("Permanences", stats.get("n_permanences", 0), delta_color="inverse")
    col4.metric("Temps (s)", stats.get("wall_time", ""))

    st.divider()
    st.subheader("Grille emploi du temps")

    # Tableau grille : créneaux × groupes
    groups = sorted(solver_result.groups, key=lambda g: (g.specialite, g.groupe_id))
    slot_rows = []
    for slot_idx, (_, day, start, end) in enumerate(_data.SLOTS):
        row: dict = {"Jour": day, "Créneau": f"{start}–{end}"}
        for g in groups:
            row[g.label] = "✓" if slot_idx in g.slots else ""
        slot_rows.append(row)
    df_grid = pd.DataFrame(slot_rows)
    st.dataframe(df_grid, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Effectifs par groupe")
    eff_rows = [
        {"Groupe": g.label, "Effectif": g.effectif,
         "Créneaux": ", ".join(f"Cr{c}" for c in g.slots)}
        for g in groups
    ]
    df_eff = pd.DataFrame(eff_rows)
    st.dataframe(df_eff, use_container_width=True, hide_index=True)

    st.divider()
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if st.button("← Modifier la configuration"):
            _go_step(2)
            st.rerun()
    with col_b:
        if st.button("🔄 Relancer la résolution"):
            st.session_state["solver_result"] = None
            st.rerun()
    with col_c:
        if st.button("Suivant : Ajustements →", type="primary"):
            _go_step(4)
            st.rerun()


# ---------------------------------------------------------------------------
# Étape 4 — Ajustements manuels
# ---------------------------------------------------------------------------

def step_adjustments() -> None:
    st.title("✏️ Ajustements manuels")
    parse_result = st.session_state["parse_result"]
    config = st.session_state["config"]
    solver_result = st.session_state["solver_result"]

    if solver_result is None or not solver_result.groups:
        st.error("Retournez à l'étape 3.")
        return

    # Initialise l'état des groupes manuels si besoin
    if st.session_state["manual_groups"] is None:
        # Structure : { spe: { groupe_id: [student_full_name, ...] } }
        mg: dict[str, dict[int, list[str]]] = {}
        for g in solver_result.groups:
            mg.setdefault(g.specialite, {})[g.groupe_id] = [
                f"{s.nom} {s.prenom}" for s in g.students
            ]
        st.session_state["manual_groups"] = mg

    mg = st.session_state["manual_groups"]
    student_map = {f"{s.nom} {s.prenom}": s for s in parse_result.students}

    st.info(
        "Sélectionnez un élève pour voir son planning et le déplacer dans un autre groupe. "
        "Les conflits potentiels sont détectés en temps réel."
    )

    # Sélection de l'élève
    all_names = sorted(student_map.keys())
    selected_name = st.selectbox("Choisir un élève", ["— sélectionner —"] + all_names)

    if selected_name != "— sélectionner —":
        st.student = student_map[selected_name]
        st.subheader(f"Planning de {selected_name}")

        # Trouver les groupes actuels de cet élève
        current_groups: dict[str, int] = {}
        for spe, groups_dict in mg.items():
            for gid, members in groups_dict.items():
                if selected_name in members:
                    current_groups[spe] = gid

        # Afficher son emploi du temps actuel
        slots_per_spe: dict[str, list[int]] = {}
        for g in solver_result.groups:
            spe = g.specialite
            gid = current_groups.get(spe)
            if gid is not None and g.groupe_id == gid:
                slots_per_spe[spe] = g.slots

        all_slots: set[int] = set()
        for sl in slots_per_spe.values():
            all_slots.update(sl)

        planning_rows = []
        for c, (_, day, start, end) in enumerate(_data.SLOTS):
            spe_in_slot = next(
                (spe for spe, slots in slots_per_spe.items() if c in slots), ""
            )
            planning_rows.append({
                "Créneau": f"Cr{c}",
                "Jour": day,
                "Horaire": f"{start}–{end}",
                "Cours": spe_in_slot,
            })
        st.dataframe(pd.DataFrame(planning_rows), use_container_width=True, hide_index=True)

        # Déplacement vers un autre groupe
        st.subheader("Déplacer vers un autre groupe")
        for spe, gid_current in current_groups.items():
            G = config.nb_groups.get(spe, 1)
            if G < 2:
                st.caption(f"{spe} : un seul groupe, pas de déplacement possible.")
                continue

            options = [f"Groupe {g+1} ({len(mg[spe][g])} élèves)" for g in range(G)]
            new_gid = st.selectbox(
                f"Groupe pour {spe}",
                options,
                index=gid_current,
                key=f"move_{selected_name}_{spe}",
            )
            target_g = options.index(new_gid)
            if target_g != gid_current:
                # Détecte les conflits
                target_slots = next(
                    (g.slots for g in solver_result.groups
                     if g.specialite == spe and g.groupe_id == target_g),
                    []
                )
                other_slots: set[int] = set()
                for s2, gid2 in current_groups.items():
                    if s2 != spe:
                        other_slots.update(
                            g.slots for g in solver_result.groups
                            if g.specialite == s2 and g.groupe_id == gid2
                            for _ in [None]  # flatten
                        )
                # Flatten
                other_slots_flat: set[int] = set()
                for s2, gid2 in current_groups.items():
                    if s2 != spe:
                        for g in solver_result.groups:
                            if g.specialite == s2 and g.groupe_id == gid2:
                                other_slots_flat.update(g.slots)

                conflicts = set(target_slots) & other_slots_flat
                if conflicts:
                    conflict_labels = [
                        f"{_data.SLOTS[c][1]} {_data.SLOTS[c][2]}" for c in conflicts
                    ]
                    st.error(f"⚠️ Conflit détecté pour {spe} → Groupe {target_g+1} : "
                             f"créneaux {', '.join(conflict_labels)} en collision.")
                else:
                    if st.button(f"Confirmer déplacement {spe} → Groupe {target_g+1}", key=f"confirm_{selected_name}_{spe}"):
                        mg[spe][gid_current].remove(selected_name)
                        mg[spe][target_g].append(selected_name)
                        st.session_state["manual_groups"] = mg
                        st.success(f"{selected_name} déplacé vers {spe} Groupe {target_g+1}.")
                        st.rerun()

    st.divider()
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if st.button("← Retour résolution"):
            _go_step(3)
            st.rerun()
    with col_b:
        if st.button("🔄 Relancer la résolution automatique"):
            st.session_state["solver_result"] = None
            st.session_state["manual_groups"] = None
            _go_step(3)
            st.rerun()
    with col_c:
        if st.button("Suivant : Export →", type="primary"):
            _go_step(5)
            st.rerun()


# ---------------------------------------------------------------------------
# Étape 5 — Export
# ---------------------------------------------------------------------------

def step_export() -> None:
    st.title("📥 Export")
    parse_result = st.session_state["parse_result"]
    solver_result = st.session_state["solver_result"]

    if solver_result is None or not solver_result.groups:
        st.error("Retournez à l'étape 3.")
        return

    # Si des ajustements manuels ont été faits, reconstruire les groupes
    mg = st.session_state.get("manual_groups")
    final_result = solver_result
    if mg is not None:
        student_map = {f"{s.nom} {s.prenom}": s for s in parse_result.students}
        new_groups: list[_solver.GroupResult] = []
        for g in solver_result.groups:
            members_names = mg.get(g.specialite, {}).get(g.groupe_id, [])
            members = [student_map[n] for n in members_names if n in student_map]
            new_groups.append(_solver.GroupResult(
                specialite=g.specialite,
                groupe_id=g.groupe_id,
                students=members,
                slots=g.slots,
            ))
        # Recalcule les stats
        n_conflicts = _solver._count_conflicts(new_groups)
        n_perm = _solver._count_permanences(new_groups)
        new_stats = dict(solver_result.stats)
        new_stats.update({"n_conflicts": n_conflicts, "n_permanences": n_perm})
        final_result = _solver.SolverResult(
            status=solver_result.status,
            groups=new_groups,
            stats=new_stats,
            infeasibility_hints=[],
        )

    # Résumé final
    stats = final_result.stats
    col1, col2, col3 = st.columns(3)
    col1.metric("Conflits", stats.get("n_conflicts", 0))
    col2.metric("Permanences", stats.get("n_permanences", 0))
    col3.metric("Statut", final_result.status)

    if stats.get("n_conflicts", 0) > 0:
        st.error("⚠️ Des conflits de créneaux subsistent — vérifiez les ajustements.")

    st.divider()
    st.subheader("Prévisualisation — Effectifs par groupe")
    groups = sorted(final_result.groups, key=lambda g: (g.specialite, g.groupe_id))
    eff_rows = [
        {"Groupe": g.label, "Effectif": g.effectif,
         "Créneaux": ", ".join(f"{_data.SLOTS[c][1]} {_data.SLOTS[c][2]}" for c in g.slots)}
        for g in groups
    ]
    st.dataframe(pd.DataFrame(eff_rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Téléchargement")
    with st.spinner("Génération du fichier Excel..."):
        buf = _export.generate_xlsx(final_result, parse_result)

    st.download_button(
        label="📥 Télécharger l'emploi du temps (Excel)",
        data=buf,
        file_name="emploi_du_temps.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

    st.divider()
    if st.button("← Retour ajustements"):
        _go_step(4)
        st.rerun()
    if st.button("🔁 Recommencer (nouveau fichier)"):
        for k in ["step", "parse_result", "config", "solver_result", "manual_groups"]:
            st.session_state[k] = None
        st.session_state["step"] = 1
        st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()

    st.sidebar.title("📅 Emploi du temps")
    st.sidebar.markdown("---")
    step_names = {1: "Import", 2: "Configuration", 3: "Résolution", 4: "Ajustements", 5: "Export"}
    for s, name in step_names.items():
        icon = "✓" if s < st.session_state["step"] else ("▶" if s == st.session_state["step"] else "○")
        disabled = s > st.session_state["step"]
        if st.sidebar.button(f"{icon} {s}. {name}", disabled=disabled, key=f"nav_{s}"):
            _go_step(s)
            st.rerun()

    _step_indicator(st.session_state["step"])

    step = st.session_state["step"]
    if step == 1:
        step_import()
    elif step == 2:
        step_config()
    elif step == 3:
        step_solve()
    elif step == 4:
        step_adjustments()
    elif step == 5:
        step_export()


if __name__ == "__main__":
    main()
