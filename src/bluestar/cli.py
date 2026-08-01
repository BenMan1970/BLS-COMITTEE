"""
Interface en ligne de commande Bluestar.

Codes de sortie (convention standard pour intégration dans un pipeline CI/CD
ou un ordonnanceur) :
  0 : succès, aucune advisory
  1 : erreur d'extraction du document macro
  2 : erreur d'extraction du document desk (document structurellement
      malformé — l'extraction elle-même a échoué)
  3 : erreur de rendu du rapport
  4 : erreur d'entrée/sortie (fichier introuvable, permissions, ou fichier
      dépassant MAX_INPUT_FILE_SIZE_BYTES — cf. ce plafond ci-dessous)
  5 : erreur inattendue non catégorisée (à ne jamais voir en production stable)
  6 : au moins une décision porte une advisory non bloquante — comportement
      PAR DÉFAUT depuis la version 3.0.0 (voir note de rupture ci-dessous).
      Utiliser --allow-advisories pour revenir à l'ancien comportement
      (code 0 malgré des advisories présentes).
  7 : extraction desk réussie mais aucun setup validé (résultat métier
      valide — "rien à signaler aujourd'hui"). Distinct du code 2 : le
      document a été lu et compris correctement, ce n'est pas un échec de
      données.

CHANGEMENT DE COMPORTEMENT DÉLIBÉRÉ (v3.0.0, 20/07/2026) — décision de
gouvernance, pas un correctif de bug :
Jusqu'à la version 2.1.3, les advisories (ex. GBP/AUD : décision ELIGIBLE
mécanique contredite par une thèse macro implicite sur une autre paire)
produisaient un code de sortie 0 par défaut — le flag --strict existait pour
forcer le code 6, mais était désactivé par défaut. Relevé convergent de
plusieurs audits indépendants (Claude 4.8, GPT-5.5, Kimi K2, GLM, Codex,
Gemini Pro) : un opérateur ou un ordonnanceur qui ne lit que le code de
sortie ne voyait jamais ces signaux. Pour un système destiné à informer des
décisions impliquant des sommes réelles, un défaut qui masque un signal de
désaccord interne est le mauvais choix par défaut. Le comportement est donc
inversé : les advisories sont maintenant bloquantes (code 6) SAUF activation
explicite de --allow-advisories. C'est un changement d'API cassant pour tout
appelant qui dépendait de l'ancien défaut — d'où le passage à la version
majeure 3.0.0.

Note : la grille de décision elle-même (fichier decide/selection_grid.py)
n'est PAS modifiée par ce changement. GBP/AUD continue de ressortir ELIGIBLE
de la grille mécanique — la question de savoir si cette grille doit changer
est une question de méthodologie/calibration distincte, non résolue ici.
Ce changement rend seulement le signal visible par défaut, il ne le tranche
pas silencieusement à la place de l'opérateur.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from bluestar import __version__
from bluestar.decide.selection_grid import decide_all
from bluestar.errors import DeskDocumentError, MacroDocumentError, RenderError
from bluestar.extract.desk_parser import parse_desk, audit_document_freshness
from bluestar.extract.macro_parser import parse_macro
from bluestar.render.html_report import render_report

logger = logging.getLogger("bluestar.cli")

# Plafond de taille des fichiers d'entrée (macro.html, desk.html). Signalé
# indépendamment par deux audits (GLM : ~11s CPU sur 7,3 Mo sans plafond ni
# timeout ; Gemini Pro : "un fichier de 5 Go saturera la mémoire
# instantanément") sans jamais être corrigé entre-temps — fermé maintenant
# plutôt que de laisser un 3e audit le re-signaler. 10 Mo est très largement
# au-dessus de la taille réelle des documents observés (~50-60 Ko) ; c'est un
# garde-fou contre un fichier pathologique ou une erreur d'export, pas une
# limite dimensionnée pour un usage normal.
MAX_INPUT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 Mo


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _read_file(path: Path, label: str) -> str:
    if not path.exists():
        logger.error("Fichier %s introuvable : %s", label, path)
        raise SystemExit(4)
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("Impossible de lire les métadonnées du fichier %s (%s) : %s", label, path, exc)
        raise SystemExit(4) from exc
    if size > MAX_INPUT_FILE_SIZE_BYTES:
        logger.error(
            "Fichier %s (%s) : %.2f Mo, dépasse le plafond de %.0f Mo (MAX_INPUT_FILE_SIZE_BYTES).",
            label, path, size / (1024 * 1024), MAX_INPUT_FILE_SIZE_BYTES / (1024 * 1024),
        )
        raise SystemExit(4)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Impossible de lire le fichier %s (%s) : %s", label, path, exc)
        raise SystemExit(4) from exc


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bluestar-committee",
        description="Comité de sélection Bluestar — croisement macro × technique, zéro-bruit.",
    )
    parser.add_argument("--macro", required=True, type=Path, help="Chemin du briefing macro (HTML).")
    parser.add_argument("--desk", required=True, type=Path, help="Chemin du rapport desk technique (HTML).")
    parser.add_argument("--out", required=True, type=Path, help="Chemin du rapport HTML de sortie.")
    parser.add_argument("--verbose", action="store_true", help="Logging niveau DEBUG.")
    parser.add_argument(
        "--allow-advisories", action="store_true",
        help="Retourne le code de sortie 0 même si au moins une décision porte "
             "une advisory non bloquante. Défaut (depuis v3.0.0) : les "
             "advisories produisent le code 6 — ce flag restaure l'ancien "
             "comportement (permissif) pour compatibilité explicite, opt-in.",
    )
    parser.add_argument("--version", action="version", version=f"bluestar-committee {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)

    logger.info("bluestar_committee_start version=%s", __version__)

    try:
        macro_html = _read_file(args.macro, "macro")
        desk_html = _read_file(args.desk, "desk")

        try:
            macro = parse_macro(macro_html)
        except MacroDocumentError as exc:
            logger.error("Échec d'extraction du document macro : %s", exc)
            return 1

        try:
            desk = parse_desk(desk_html)
        except DeskDocumentError as exc:
            logger.error("Échec d'extraction du document desk : %s", exc)
            return 2

        if not desk.setups:
            logger.warning("Aucun setup validé dans le document desk — rapport non généré.")
            return 7

        # PATCH-B2/B4/F18 (audit 31/07/2026) : Activation des gardes-fous de 
        # transmission et de fraîcheur documentaire.
        now = datetime.now(timezone.utc)
        
        # B-2 / F-04 : Audit de fraîcheur du document desk
        freshness_msg = audit_document_freshness(desk, now)
        if freshness_msg:
            logger.warning("ALERTE FRAÎCHEUR DOCUMENTAIRE : %s", freshness_msg)
            
        # B-4 : Déclaration du canal macro vide
        if not macro.priority_setups:
            macro_channel_status = ("INERTE — aucun setup prioritaire macro ce cycle : le garde-fou de "
                "conflit et les advisories currency-level étaient structurellement désactivés. "
                "« Advisories : aucun » signifie absence de thèse macro, pas absence de conflit.")
            logger.warning("MACRO_CHANNEL_STATUS : %s", macro_channel_status)
        else:
            macro_channel_status = "ACTIF"

        # Passage explicite de l'horloge (now) à decide_all pour activer la 
        # rétrogradation BLOCKED_DATA en cas de péremption > 3h.
        decisions = decide_all(desk, macro, now=now)

        # F-18 : Log des cardinalités pour lever l'ambiguïté (5 + 31 = 33)
        logger.info(
            "Univers desk : %d actifs · %d franchissent les gates · %d validés · %d rejetés",
            desk.universe_total,
            desk.universe_evaluated,
            len(desk.setups),
            len(desk.rejected)
        )

        try:
            report_html = render_report(desk, macro, decisions, generated_at=now)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report_html, encoding="utf-8")
        except RenderError as exc:
            logger.error("Échec de génération du rapport : %s", exc)
            return 3
        except OSError as exc:
            logger.error("Impossible d'écrire le rapport dans %s : %s", args.out, exc)
            return 4

        logger.info("bluestar_committee_done output=%s", args.out)

        advisory_count = sum(len(d.advisories) for d in decisions)
        if advisory_count:
            logger.warning(
                "decisions_with_advisories count=%d pairs=%s",
                advisory_count,
                [d.pair for d in decisions if d.advisories],
            )
            if not args.allow_advisories:
                return 6

        return 0

    except SystemExit:
        raise  # laisser passer les sys.exit() explicites (ex. _read_file), pas notre problème
    except Exception:
        # Filet de sécurité : toute exception qui a échappé aux `except` typés
        # ci-dessus (ex. AttributeError sur un document structurellement
        # inattendu non couvert par les parseurs) est une anomalie, pas une
        # erreur de données propre. Correctif du round d'audit du 19/07/2026 :
        # ce code de sortie était documenté dans le docstring du module mais
        # aucun chemin ne le produisait réellement — trouvé indépendamment
        # par les audits Claude 4.8 et GPT-5.5.
        logger.exception("Erreur inattendue non catégorisée pendant l'exécution.")
        return 5


if __name__ == "__main__":
    sys.exit(main())
