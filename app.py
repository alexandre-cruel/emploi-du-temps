from __future__ import annotations

"""
Application Streamlit — Emploi du temps lycée
5 étapes séquentielles : Import → Config → Résolution → Ajustements → Export
"""

import copy
import queue
import threading
import time
from io import BytesIO

import pandas as pd
import streamlit as st
from streamlit_sortables import sort_items

import data as _data
import solver as _solver
import export as _export

N_SLOTS = len(_data.SLOTS)

DAY_ORDER = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi"]
DAY_SLOTS: dict[str, list[int]] = {}
for _c, (_, _day, _s, _e) in enumerate(_data.SLOTS):
    DAY_SLOTS.setdefault(_day, []).append(_c)

SPE_COLORS: dict[str, str] = {
    "Maths": "#4a90e2",
    "SES": "#27ae60",
    "HLP": "#e67e22",
    "LCE": "#8e44ad",
    "NSI": "#16a085",
    "HGGSP": "#c0392b",
    "SPC": "#2980b9",
    "SVT": "#1abc9c",
}


# Créneaux habituels pour les options connues (index 0-based interne)
_OPTION_DEFAULT_SLOT: dict[str, int] = {
    "Maths expertes": _data.SLOT_MATEX,
    "DGEMC": _data.SLOT_MATEX,
    "Maths complémentaires": _data.SLOT_MATCO,
}


def _option_panel(
    solver_result: _solver.SolverResult,
    parse_result: _data.ParseResult,
) -> None:
    """Panneau interactif de placement des options post-résolution."""
    student_busy = solver_result.get_student_slots()
    student_groups = solver_result.get_student_groups()

    option_to_students: dict[str, list[str]] = {}
    for s in parse_result.students:
        for opt in s.options:
            option_to_students.setdefault(opt, []).append(f"{s.nom} {s.prenom}")

    if not option_to_students:
        st.caption("Aucune option déclarée dans le fichier.")
        return

    slot_options = [
        f"Cr{c+1}: {_data.SLOTS[c][1]} {_data.SLOTS[c][2]}"
        for c in range(N_SLOTS)
    ]

    for opt, names in sorted(option_to_students.items()):
        default_idx = _OPTION_DEFAULT_SLOT.get(opt, 0)
        col_sel, col_stat = st.columns([3, 2])
        with col_sel:
            chosen = st.selectbox(
                f"**{opt}** ({len(names)} élèves)",
                slot_options,
                index=default_idx,
                key=f"opt_slot_{opt}",
            )
        chosen_c = int(chosen.split(":")[0].replace("Cr", "")) - 1

        compatible = [n for n in names if chosen_c not in student_busy.get(n, [])]
        conflict = [n for n in names if chosen_c in student_busy.get(n, [])]
        ratio = len(compatible) / len(names) if names else 1.0

        with col_stat:
            st.markdown("")  # alignement vertical
            if ratio == 1.0:
                st.success(f"✅ {len(compatible)}/{len(names)} compatibles")
            elif ratio >= 0.8:
                st.warning(f"⚠️ {len(compatible)}/{len(names)} compatibles")
            else:
                st.error(f"❌ {len(compatible)}/{len(names)} compatibles")

        if conflict:
            with st.expander(f"  {len(conflict)} élève(s) en conflit sur ce créneau"):
                rows = []
                for name in conflict:
                    grps = student_groups.get(name, [])
                    blocking = next(
                        (g.label for g in grps if chosen_c in g.slots), "?"
                    )
                    parts = name.split(" ", 1)
                    rows.append({
                        "Nom": parts[0],
                        "Prénom": parts[1] if len(parts) > 1 else "",
                        "Cours qui occupe le créneau": blocking,
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.caption(
        "En cas de conflit : déplacez les élèves concernés vers un autre groupe "
        "en étape 4, ou planifiez l'option sur un créneau de tronc commun."
    )

def _slot_header(c: int) -> str:
    return f"Cr{c+1} — {_data.SLOTS[c][1]} {_data.SLOTS[c][2]}"


def _build_day_containers(
    day: str,
    groups: list[_solver.GroupResult],
    slots_by_group: dict[str, list[int]],
) -> list[dict]:
    """Construit les containers sort_items pour un seul jour.
    Chaque container = 1 créneau du jour. Groupes SPC/SVT exclus (lecture seule).
    """
    containers = []
    for c in DAY_SLOTS.get(day, []):
        items = []
        for g in groups:
            if g.specialite in _data.SPE_4_SLOTS:
                continue
            if c in slots_by_group.get(g.label, g.slots):
                items.append(g.label)
        containers.append({"header": _slot_header(c), "items": items})
    return containers


def _merge_day_containers(
    day_edited: dict[str, list[dict]],
    groups: list[_solver.GroupResult],
) -> dict[str, list[int]]:
    """Fusionne les résultats des 5 sort_items en un slots_by_group cohérent."""
    result: dict[str, list[int]] = {}
    for day, containers in day_edited.items():
        for c_local, cont in enumerate(containers):
            # Retrouver l'index global du slot depuis le header
            day_slot_indices = DAY_SLOTS.get(day, [])
            if c_local < len(day_slot_indices):
                c_global = day_slot_indices[c_local]
                for label in cont.get("items", []):
                    result.setdefault(label, []).append(c_global)
    # SPC/SVT : préserver leurs slots d'origine
    for g in groups:
        if g.specialite in _data.SPE_4_SLOTS:
            result[g.label] = list(g.slots)
    return result


def _dnd_panel(
    solver_result: _solver.SolverResult,
    config: _solver.SolverConfig,
    parse_result: _data.ParseResult,
) -> None:
    """Panneau interactif : déplacer les groupes entre créneaux via drag & drop.
    Layout calendrier : métriques en haut, 5 colonnes de jours avec DnD vertical.
    """
    st.subheader("🎯 Optimiser manuellement — drag & drop")
    st.caption(
        "Glissez les cartes de groupes entre créneaux du même jour. "
        "Pour déplacer un groupe vers un autre jour, utilisez l'éditeur ci-dessous. "
        "Les métriques et violations se recalculent en temps réel. "
        "Les groupes SPC/SVT ne sont pas éditables ici."
    )

    groups = sorted(solver_result.groups, key=lambda g: (g.specialite, g.groupe_id))

    # État initial
    if "dnd_slots_by_group" not in st.session_state or st.session_state.get("dnd_result_source") != id(solver_result):
        st.session_state["dnd_slots_by_group"] = {g.label: list(g.slots) for g in groups}
        st.session_state["dnd_result_source"] = id(solver_result)

    slots_by_group = st.session_state["dnd_slots_by_group"]

    # Calcul anticipé pour les métriques en haut
    day_results: dict[str, list[dict]] = {}
    for day in DAY_ORDER:
        day_results[day] = _build_day_containers(day, groups, slots_by_group)
    new_slots_by_group = _merge_day_containers(day_results, groups)

    virtual = _solver.rebuild_from_slot_assignment(
        solver_result, new_slots_by_group, config, parse_result
    )
    violations = _solver.check_hard_constraints(virtual.groups, config, parse_result)

    # ── Métriques + violations (pleine largeur, en haut) ──
    stats = virtual.stats
    orig_stats = solver_result.stats
    n_conf = stats.get("n_conflicts", 0)
    n_perm = stats.get("n_permanences_slots", 0)
    orig_conf = orig_stats.get("n_conflicts", 0)
    orig_perm = orig_stats.get("n_permanences_slots", 0)

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric(
        "Conflits élèves",
        n_conf,
        delta=n_conf - orig_conf if n_conf != orig_conf else None,
        delta_color="inverse",
    )
    mc2.metric(
        "Permanences (créneaux)",
        n_perm,
        delta=n_perm - orig_perm if n_perm != orig_perm else None,
        delta_color="inverse",
    )
    mc3.metric("Violations contraintes dures", len(violations))

    if not violations:
        st.success("✅ Toutes les contraintes dures sont respectées.")
    else:
        grouped_v: dict[str, list[str]] = {}
        for v in violations:
            grouped_v.setdefault(v.code, []).append(v.message)
        for code, msgs in grouped_v.items():
            with st.expander(f"❌ {code} ({len(msgs)})", expanded=True):
                for m in msgs:
                    st.markdown(f"- {m}")

    st.divider()

    # ── Calendrier DnD 5 colonnes ──
    custom_style = """
    .sortable-container {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 6px;
        margin: 3px 0;
        min-height: 60px;
        background: #fafafa;
    }
    .sortable-container-header {
        font-weight: 600;
        font-size: 0.78em;
        margin-bottom: 4px;
        color: #555;
    }
    .sortable-item {
        background: #4a90e2;
        color: white;
        padding: 3px 6px;
        margin: 2px 0;
        border-radius: 4px;
        cursor: grab;
        font-size: 0.82em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    """

    day_cols = st.columns(5)
    updated_day_results: dict[str, list[dict]] = {}
    for day, col in zip(DAY_ORDER, day_cols):
        with col:
            st.markdown(f"**{day}**")
            containers = _build_day_containers(day, groups, slots_by_group)
            edited = sort_items(
                containers,
                multi_containers=True,
                direction="vertical",
                custom_style=custom_style,
                key=f"dnd_{day}",
            )
            updated_day_results[day] = edited

    new_slots_by_group = _merge_day_containers(updated_day_results, groups)
    st.session_state["dnd_slots_by_group"] = new_slots_by_group

    # ── Groupes SPC/SVT en lecture seule ──
    spc_svt = [g for g in groups if g.specialite in _data.SPE_4_SLOTS]
    if spc_svt:
        with st.expander("🔬 Groupes SPC/SVT (lecture seule)", expanded=False):
            rows = []
            for g in spc_svt:
                cours_lbl = ", ".join(f"Cr{c+1}" for c in g.cours_slots)
                tp_lbl = " / ".join(
                    f"A→Cr{sa+1} B→Cr{sb+1}" for sa, sb in g.tp_assignments
                )
                rows.append({
                    "Groupe": g.label,
                    "Cours (groupe entier)": cours_lbl,
                    "TP (A/B)": tp_lbl,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── Éditeur cross-day ──
    non_spc_groups = [g for g in groups if g.specialite not in _data.SPE_4_SLOTS]
    if non_spc_groups:
        with st.expander("↔️ Déplacer un groupe vers un autre jour", expanded=False):
            all_labels = [g.label for g in non_spc_groups]
            selected_label = st.selectbox("Groupe à déplacer", all_labels, key="xday_group")
            if selected_label:
                current_slots = new_slots_by_group.get(selected_label, [])
                current_slot_names = [_slot_header(c) for c in current_slots]
                st.caption(f"Créneaux actuels : {', '.join(current_slot_names) or '—'}")
                all_slot_options = [_slot_header(c) for c in range(N_SLOTS)]
                new_slot_names = st.multiselect(
                    "Nouveaux créneaux",
                    options=all_slot_options,
                    default=current_slot_names,
                    key="xday_slots",
                )
                if st.button("Appliquer", key="xday_apply"):
                    new_indices = [i for i, h in enumerate(all_slot_options) if h in new_slot_names]
                    new_sbg = dict(new_slots_by_group)
                    new_sbg[selected_label] = new_indices
                    st.session_state["dnd_slots_by_group"] = new_sbg
                    for day in DAY_ORDER:
                        st.session_state.pop(f"dnd_{day}", None)
                    st.rerun()

    st.divider()

    # ── Actions ──
    col_r, col_a = st.columns(2)
    with col_r:
        if st.button("🔄 Réinitialiser", use_container_width=True):
            st.session_state["dnd_slots_by_group"] = {g.label: list(g.slots) for g in solver_result.groups}
            for day in DAY_ORDER:
                st.session_state.pop(f"dnd_{day}", None)
            st.rerun()
    with col_a:
        adopt_disabled = len(violations) > 0
        if st.button(
            "✅ Adopter cette solution",
            use_container_width=True,
            type="primary",
            disabled=adopt_disabled,
            help="Impossible tant qu'il reste des violations." if adopt_disabled else None,
        ):
            st.session_state["solver_result"] = virtual
            st.session_state["manual_groups"] = None
            st.session_state["dnd_result_source"] = id(virtual)
            st.success("Nouvelle solution adoptée. Elle sera utilisée à l'export.")
            st.rerun()


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
        "Cr2 (Lundi 15h50) est traditionnellement réservé aux options Maths expertes/DGEMC. "
        "Cr5 (Mercredi 8h10) est traditionnellement réservé à Maths complémentaires. "
        "Décochez-les si une spécialité ne doit pas utiliser ces créneaux."
    )

    col_labels = [
        f"Cr{c+1}: {_data.SLOTS[c][1][:2]} {_data.SLOTS[c][2]}"
        for c in range(N_SLOTS)
    ]

    avail_data = {
        spe: existing_config.slot_availability.get(
            spe, _solver.default_slot_availability(spe)
        )
        for spe in specialites
    }
    df_avail = pd.DataFrame(avail_data, index=col_labels).T
    df_avail.index.name = "Spécialité"

    col_config = {
        col: st.column_config.CheckboxColumn(col, width="small")
        for col in col_labels
    }

    edited = st.data_editor(
        df_avail,
        column_config=col_config,
        use_container_width=True,
        key="avail_editor",
    )

    for spe in specialites:
        slot_avail_input[spe] = [bool(edited.loc[spe, col]) for col in col_labels]

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
                f"Cr{c+1}: {_data.SLOTS[c][1]} {_data.SLOTS[c][2]}"
                for c in range(N_SLOTS)
                if slot_avail_input.get("Maths", _solver.default_slot_availability("Maths"))[c]
            ]
            if common_options:
                default_cr = f"Cr{existing_config.maths_common_slot_idx + 1}: " \
                             f"{_data.SLOTS[existing_config.maths_common_slot_idx][1]} " \
                             f"{_data.SLOTS[existing_config.maths_common_slot_idx][2]}"
                default_idx = common_options.index(default_cr) if default_cr in common_options else 0
                selected = st.selectbox(
                    "Créneau commun Maths",
                    common_options,
                    index=default_idx,
                    key="maths_common_slot",
                )
                maths_common_idx = int(selected.split(":")[0].replace("Cr", "")) - 1
            else:
                maths_common_idx = existing_config.maths_common_slot_idx
        else:
            maths_common_idx = existing_config.maths_common_slot_idx

    timeout = st.slider(
        "Timeout solveur (secondes)",
        10, 3600, min(existing_config.timeout_seconds, 3600), step=30,
        help=(
            "Durée MAXIMALE allouée au solveur. Il s'arrête AUTOMATIQUEMENT plus tôt "
            "s'il prouve avoir exploré toutes les possibilités et trouvé l'optimum "
            "(statut OPTIMAL). Sinon, à l'expiration du timeout, il retourne la meilleure "
            "solution trouvée jusque-là (statut FEASIBLE — potentiellement améliorable "
            "avec un timeout plus long). Max 3600 s = 1 h. "
            "⚠️ Sur Streamlit Community Cloud, l'onglet peut se déconnecter au-delà de "
            "~10 min : préférer ≤ 900 s en ligne, plus long en local."
        ),
    )

    col_det, col_wk = st.columns([2, 1])
    with col_det:
        deterministic_mode = st.toggle(
            "Mode déterministe",
            value=getattr(existing_config, "deterministic_mode", False),
            help=(
                "Désactivé (recommandé) : plusieurs threads en parallèle — solution "
                "trouvée bien plus vite. Les créneaux peuvent légèrement varier d'un "
                "run à l'autre.\n"
                "Activé : 1 thread — résultat identique à chaque run, mais plus lent. "
                "À utiliser uniquement si vous avez besoin de reproductibilité exacte."
            ),
        )
    with col_wk:
        import os as _os
        cpu_max = max(1, (_os.cpu_count() or 8))
        num_workers = st.number_input(
            "Nb workers (CPU)",
            min_value=1,
            max_value=max(cpu_max, 16),
            value=int(getattr(existing_config, "num_workers", 8)),
            step=1,
            disabled=deterministic_mode,
            help=(
                "CP-SAT lance N stratégies de recherche en parallèle sur N cœurs. "
                "Ignoré en mode déterministe (forcé à 1)."
            ),
        )

    with st.expander("⚙️ Paramètres avancés solveur", expanded=False):
        st.caption(
            "CP-SAT est un solveur CPU multi-thread — le GPU n'est pas supporté. "
            "Avec 8+ workers, le parallélisme est déjà maximal. "
            "Ces options ajustent la stratégie de recherche interne."
        )
        col_adv1, col_adv2 = st.columns(2)
        with col_adv1:
            interleave_search = st.toggle(
                "Interleave search",
                value=getattr(existing_config, "interleave_search", False),
                disabled=deterministic_mode,
                help=(
                    "Interleave agressif des stratégies de recherche entre workers. "
                    "Peut accélérer la convergence sur des instances difficiles. "
                    "Ignoré en mode déterministe."
                ),
            )
        with col_adv2:
            linearization_level = st.select_slider(
                "Linearisation LP",
                options=[0, 1, 2],
                value=getattr(existing_config, "linearization_level", 1),
                help=(
                    "0 = LP désactivée (plus rapide par itération, moins de pruning). "
                    "1 = défaut CP-SAT. "
                    "2 = LP complète (moins de branches mais plus coûteux par nœud)."
                ),
            )

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
            deterministic_mode=deterministic_mode,
            num_workers=int(num_workers),
            interleave_search=interleave_search,
            linearization_level=int(linearization_level),
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

def _clear_solve_thread_state() -> None:
    for key in ("_solve_queue", "_solve_thread", "_solve_start", "_solving"):
        st.session_state.pop(key, None)


def _start_solve(
    parse_result: _data.ParseResult,
    config: _solver.SolverConfig,
    initial_solution: "_solver.SolverResult | None" = None,
) -> None:
    q: queue.Queue = queue.Queue()
    st.session_state["_solve_queue"] = q
    st.session_state["_solve_start"] = time.monotonic()
    st.session_state["_solving"] = True

    def _worker() -> None:
        try:
            res = _solver.solve(parse_result, config, initial_solution=initial_solution)
            q.put(("ok", res))
        except Exception as exc:  # noqa: BLE001
            q.put(("err", exc))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    st.session_state["_solve_thread"] = t


def _render_solve_progress(timeout_seconds: int, num_workers: int) -> None:
    elapsed = time.monotonic() - st.session_state.get("_solve_start", time.monotonic())
    pct = min(elapsed / max(timeout_seconds, 1), 0.99)
    with st.status("Résolution en cours...", expanded=True, state="running"):
        st.progress(pct, text=f"⏱️ {elapsed:.0f}s écoulées / {timeout_seconds}s — CP-SAT explore ({num_workers} workers)...")


def step_solve() -> None:
    st.title("⚡ Résolution")
    result = st.session_state["parse_result"]
    config = st.session_state["config"]

    if result is None or config is None:
        st.error("Revenez à l'étape 1 ou 2.")
        return

    solver_result = st.session_state.get("solver_result")

    # Récupérer résultat si thread terminé
    _q = st.session_state.get("_solve_queue")
    if _q is not None and not _q.empty():
        tag, payload = _q.get_nowait()
        _clear_solve_thread_state()
        if tag == "ok":
            st.session_state["solver_result"] = payload
            st.session_state["manual_groups"] = None
        else:
            st.error(f"Erreur solveur : {payload}")
        st.rerun()
        return

    if solver_result is None and not st.session_state.get("_solving"):
        warm_start = st.session_state.pop("_warm_start_from", None)
        _start_solve(result, config, initial_solution=warm_start)
        st.rerun()
        return

    if st.session_state.get("_solving"):
        _render_solve_progress(config.timeout_seconds, config.num_workers)
        time.sleep(0.5)
        st.rerun()
        return

    # Statut
    status = solver_result.status
    if status == "OPTIMAL":
        st.success(
            "✅ Solution OPTIMALE prouvée — le solveur a exploré toutes les "
            "possibilités et garantit qu'aucune meilleure solution n'existe. "
            "Inutile de relancer avec un timeout plus long."
        )
    elif status == "FEASIBLE":
        st.warning(
            "⚠️ Solution FEASIBLE — trouvée avant expiration du timeout, mais "
            "le solveur n'a pas pu prouver qu'elle est optimale. "
            "Augmenter le timeout en étape 2 pourrait donner une meilleure solution."
        )
        col_warm, col_info = st.columns([1, 2])
        with col_warm:
            if st.button("🔁 Améliorer (warm start)", key="warm_start_btn"):
                st.session_state["_warm_start_from"] = solver_result
                st.session_state["solver_result"] = None
                st.rerun()
        with col_info:
            st.caption(
                "Relance le solveur en repartant de cette solution feasible. "
                "Il peut trouver une meilleure solution plus rapidement en évitant "
                "de reconstruire une première solution de zéro."
            )
    elif status == "INFEASIBLE":
        st.error("❌ Aucune solution possible (INFEASIBLE).")
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
    else:
        st.error(f"❌ Temps écoulé sans trouver de solution ({status}).")
        st.info(
            "Le problème n'est pas prouvé infaisable — le solveur a simplement manqué de temps. "
            "Essayez : augmenter le timeout en étape 2, ou désactiver le mode déterministe "
            "(4 workers = ~4× plus rapide)."
        )
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
    col3.metric(
        "Permanences",
        stats.get("n_permanences_slots", stats.get("n_permanences", 0)),
        delta_color="inverse",
        help=(
            "Somme des créneaux-permanence sur tous les élèves. "
            "Un élève avec cours Mardi 8h mais pas Mardi 10h compte 1. "
            "S'il a aussi une permanence Jeudi, ça compte 2. "
            "Le solveur cherche à minimiser ce total. "
            f"(Élèves concernés : {stats.get('n_permanences_students', stats.get('n_permanences', 0))})"
        ),
    )
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
         "Créneaux": ", ".join(f"Cr{c+1}" for c in g.slots)}
        for g in groups
    ]
    df_eff = pd.DataFrame(eff_rows)
    st.dataframe(df_eff, use_container_width=True, hide_index=True)

    # Panneau options interactif
    has_options = any(s.options for s in result.students)
    if has_options:
        st.divider()
        with st.expander("🎓 Options — simuler le placement", expanded=False):
            _option_panel(solver_result, result)

    st.divider()
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if st.button("← Modifier la configuration"):
            _go_step(2)
            st.rerun()
    with col_b:
        if st.button("🔄 Relancer la résolution"):
            _clear_solve_thread_state()
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

    _dnd_panel(solver_result, config, parse_result)
    solver_result = st.session_state["solver_result"]
    st.divider()

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

    # --- Sous-groupes A/B pour SPC et SVT ---
    spc_svt_groups = [g for g in solver_result.groups if g.specialite in _data.SPE_4_SLOTS and g.subgroups]
    if spc_svt_groups:
        with st.expander("🔬 Sous-groupes TP (SPC/SVT)", expanded=False):
            st.caption(
                "Les sous-groupes A et B alternent entre cours en groupe entier et TP en demi-groupe. "
                "Modifiez les affectations ci-dessous si nécessaire."
            )
            # Clé de stockage dans session_state
            if "subgroups_override" not in st.session_state:
                st.session_state["subgroups_override"] = {}

            for g in sorted(spc_svt_groups, key=lambda x: (x.specialite, x.groupe_id)):
                st.markdown(f"**{g.label}**")
                sg_key = g.label
                sub = g.subgroups or {}
                rows = []
                for letter in ("A", "B"):
                    for st_obj in sub.get(letter, []):
                        rows.append({"Nom": st_obj.nom, "Prénom": st_obj.prenom, "Sous-groupe": letter})

                # Remplace par les overrides existants si disponibles
                override = st.session_state["subgroups_override"].get(sg_key)
                if override is not None:
                    rows = override

                df_sg = pd.DataFrame(rows)
                edited_sg = st.data_editor(
                    df_sg,
                    column_config={
                        "Sous-groupe": st.column_config.SelectboxColumn(
                            "Sous-groupe", options=["A", "B"], required=True
                        )
                    },
                    use_container_width=True,
                    hide_index=True,
                    key=f"sg_editor_{sg_key}",
                )
                st.session_state["subgroups_override"][sg_key] = edited_sg.to_dict("records")

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
                "Créneau": f"Cr{c+1}",
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
                subgroups=g.subgroups,
                tp_pairs=g.tp_pairs,
                tp_assignments=g.tp_assignments,
                cours_slots=g.cours_slots,
            ))
        # Recalcule les stats
        n_conflicts = _solver._count_conflicts(new_groups)
        n_perm_slots = _solver._count_permanences_slots(new_groups)
        n_perm_students = _solver._count_permanences(new_groups)
        new_stats = dict(solver_result.stats)
        new_stats.update({
            "n_conflicts": n_conflicts,
            "n_permanences": n_perm_students,
            "n_permanences_students": n_perm_students,
            "n_permanences_slots": n_perm_slots,
        })
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
    col2.metric(
        "Permanences",
        stats.get("n_permanences_slots", stats.get("n_permanences", 0)),
        help=(
            "Somme des créneaux-permanence sur tous les élèves. "
            "Un élève avec cours Mardi 8h mais pas Mardi 10h compte 1. "
            "S'il a aussi une permanence Jeudi, ça compte 2. "
            f"(Élèves concernés : {stats.get('n_permanences_students', stats.get('n_permanences', 0))})"
        ),
    )
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
