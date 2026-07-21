"""
Interface Streamlit pour Bluestar — Comité de sélection FX.

Ce fichier ne contient AUCUNE logique métier : il ne fait qu'appeler les
fonctions déjà présentes dans le package `bluestar` (extract / decide /
render), exactement comme le fait `cli.py`, mais via une UI web au lieu
d'arguments en ligne de commande.

Lancement local :
    pip install -e ".[dev]"
    pip install streamlit
    streamlit run streamlit_app.py

Déploiement sur Streamlit Community Cloud :
    - Fichier principal : streamlit_app.py
    - Dépendances : requirements.txt (à la racine, voir ce fichier)
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from bluestar import __version__
from bluestar.decide.selection_grid import decide_all
from bluestar.errors import DeskDocumentError, MacroDocumentError, RenderError
from bluestar.extract.desk_parser import parse_desk
from bluestar.extract.macro_parser import parse_macro
from bluestar.render.html_report import render_report

MAX_INPUT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 Mo — même plafond que le CLI

st.set_page_config(page_title="Bluestar — Comité de sélection", page_icon="⭐", layout="wide")

st.title("⭐ Bluestar — Comité de sélection FX")
st.caption(f"version {__version__} · croisement macro × technique, zéro-bruit")

with st.sidebar:
    st.header("Entrées")
    macro_file = st.file_uploader("Briefing macro (HTML)", type=["html", "htm"], key="macro")
    desk_file = st.file_uploader("Rapport desk technique (HTML)", type=["html", "htm"], key="desk")
    allow_advisories = st.checkbox(
        "Autoriser les advisories sans blocage",
        value=True,
        help="Équivalent du flag --allow-advisories du CLI. Décoché, une "
             "advisory affiche un avertissement mais n'empêche pas la "
             "génération du rapport dans cette UI (contrairement au code de "
             "sortie 6 du CLI, qui n'a pas de sens dans une interface web).",
    )
    run_button = st.button("Générer le rapport", type="primary", use_container_width=True)


def _read_upload(uploaded_file, label: str) -> str | None:
    if uploaded_file is None:
        return None
    raw = uploaded_file.getvalue()
    if len(raw) > MAX_INPUT_FILE_SIZE_BYTES:
        st.error(
            f"Fichier {label} : {len(raw) / (1024 * 1024):.2f} Mo, dépasse le "
            f"plafond de {MAX_INPUT_FILE_SIZE_BYTES / (1024 * 1024):.0f} Mo."
        )
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        st.error(f"Fichier {label} : encodage non UTF-8, impossible à lire.")
        return None


if run_button:
    if macro_file is None or desk_file is None:
        st.warning("Charge les deux documents (macro et desk) avant de générer le rapport.")
        st.stop()

    macro_html = _read_upload(macro_file, "macro")
    desk_html = _read_upload(desk_file, "desk")
    if macro_html is None or desk_html is None:
        st.stop()

    try:
        macro = parse_macro(macro_html)
    except MacroDocumentError as exc:
        st.error(f"Échec d'extraction du document macro : {exc}")
        st.stop()

    try:
        desk = parse_desk(desk_html)
    except DeskDocumentError as exc:
        st.error(f"Échec d'extraction du document desk : {exc}")
        st.stop()

    if not desk.setups:
        st.info("Aucun setup validé dans le document desk — rien à signaler aujourd'hui.")
        st.stop()

    decisions = decide_all(desk, macro)

    try:
        report_html = render_report(desk, macro, decisions, generated_at=datetime.now(timezone.utc))
    except RenderError as exc:
        st.error(f"Échec de génération du rapport : {exc}")
        st.stop()

    advisory_count = sum(len(d.advisories) for d in decisions)
    if advisory_count and not allow_advisories:
        st.warning(
            f"{advisory_count} advisory(ies) non bloquante(s) détectée(s) sur les "
            f"paires : {', '.join(sorted({d.pair for d in decisions if d.advisories}))}. "
            "Le rapport est généré quand même — décoche 'Autoriser les advisories "
            "sans blocage' pour changer ce comportement d'affichage."
        )

    st.download_button(
        "📥 Télécharger le rapport HTML",
        data=report_html,
        file_name=f"rapport_bluestar_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.html",
        mime="text/html",
        use_container_width=True,
    )

    st.components.v1.html(report_html, height=1400, scrolling=True)

else:
    st.info(
        "Charge un briefing macro et un rapport desk (deux fichiers HTML) dans "
        "la barre latérale, puis clique sur **Générer le rapport**."
    )
    st.markdown(
        "> **ELIGIBLE ≠ EXÉCUTER.** Ce rapport sort du moteur d'éligibilité "
        "seul — toute exécution réelle nécessite un moteur de portefeuille "
        "en aval, hors périmètre de cet outil."
    )
