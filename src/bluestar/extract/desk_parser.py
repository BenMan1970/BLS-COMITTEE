"""Extraction du rapport desk technique BLUESTAR (setups validés + rejets)."""

from __future__ import annotations

import json
import logging
import re
from bs4 import BeautifulSoup, Tag

from bluestar.errors import DeskDocumentError
from bluestar.models import CorrelationSignal, DeskRejectedSetup, DeskSetup, DeskSnapshot, Direction

logger = logging.getLogger("bluestar.extract.desk")


def _safe_float(raw_text: str, *, field: str, pair: str) -> float:
    """
    Cast float() défensif — lève DeskDocumentError (code CLI 2) au lieu de
    laisser fuiter ValueError (rattrapée par le filet générique de cli.py et
    catégorisée en code 5, "erreur inattendue").

    Correction du round d'audit du 20/07/2026 (trouvé par l'audit Gemini Pro,
    confirmé par exécution : injecter "N/A" dans le champ R:R d'un setup
    provoque exactement ce comportement avant correction). C'est la
    troisième occurrence du même défaut de fond (après le .pair manquant du
    round 2.1.1) — traité ici systématiquement pour tous les champs de prix
    du bloc px-grid, pas au cas par cas.
    """
    try:
        return float(raw_text)
    except ValueError as exc:
        raise DeskDocumentError(
            f"Setup {pair!r} : valeur non numérique pour le champ {field!r} "
            f"({raw_text!r}) — document desk malformé."
        ) from exc


def _parse_report_datetime(soup: BeautifulSoup) -> tuple[str, str]:
    subbar = soup.find(class_="page-subbar")
    if subbar is None:
        raise DeskDocumentError("Bandeau de date (page-subbar) introuvable.")
    text = subbar.get_text(" ", strip=True)
    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(GMT[+-]\d+|UTC|CET|CEST)", text)
    if not m:
        raise DeskDocumentError("Date/heure du rapport desk introuvable dans le bandeau.")
    return m.group(1), m.group(2)


def _parse_universe(soup: BeautifulSoup) -> tuple[int, int]:
    text = soup.find(class_="page-subbar").get_text(" ", strip=True)
    m = re.search(r"Universe\s*(\d+)\s*/\s*(\d+)", text)
    if not m:
        raise DeskDocumentError("Libellé Universe X/Y introuvable.")
    return int(m.group(1)), int(m.group(2))


def _parse_event_risk(soup: BeautifulSoup) -> str:
    text = soup.find(class_="page-subbar").get_text(" ", strip=True)
    m = re.search(r"Event Risk\s*:\s*(\w+)", text)
    return m.group(1) if m else "non disponible dans les documents fournis"


def _parse_themes(soup: BeautifulSoup) -> tuple[str, ...]:
    """PATCH-THEMES-BLEED (round du 31/07/2026) : l'ancienne version regexait
    le texte aplati de tout le page-subbar avec une frontière
    "(?:CONFIDENTIEL|$)" -- si un badge (ex. "SR indisponible · mode ATR")
    s'intercalait entre le span Thèmes et le span CONFIDENTIEL, il était
    absorbé dans le dernier thème. Confirmé par exécution sur le rapport du
    29/07/2026 : le thème NZD devenait "NZD Bearish SR indisponible · mode
    ATR" au lieu de "NZD Bearish".

    Correctif : chaque bloc du bandeau est un <span> FRÈRE indépendant dans
    le template (jamais imbriqué) -- on ancre donc sur le span lui-même
    plutôt que sur le texte aplati du conteneur. Un badge futur ajouté entre
    Thèmes et CONFIDENTIEL ne peut plus fuiter, quel que soit son contenu.
    Testé avec/sans badge SR, avec/sans thèmes présents — comportement
    identique à l'ancienne version dans les deux cas où elle était correcte,
    corrigé dans le cas où elle ne l'était pas."""
    subbar = soup.find(class_="page-subbar")
    if subbar is None:
        return ()
    for span in subbar.find_all("span", recursive=False):
        text = span.get_text(" ", strip=True)
        m = re.match(r"Th[eè]mes\s*:\s*(.+)", text)
        if m:
            return tuple(t.strip() for t in m.group(1).split(",") if t.strip())
    return ()


_FACTOR_LABEL_RE = re.compile(r"^(F\d|Q-rang)")


def _extract_pair(block: Tag) -> str:
    """Champ obligatoire : un bloc .setup sans .pair est un document desk
    malformé (DeskDocumentError, code CLI 2), pas une anomalie interne
    imprévue (code CLI 5). Distinction ajoutée lors du round d'audit du
    19/07/2026 (trouvé par l'audit GLM, confirmé par exécution)."""
    pair_tag = block.find(class_="pair")
    if pair_tag is None:
        raise DeskDocumentError(
            "Bloc .setup sans élément .pair — document desk structurellement "
            "malformé (paire non identifiable)."
        )
    return pair_tag.get_text(strip=True)


def _extract_direction(block: Tag, pair: str) -> Direction:
    """
    Champ obligatoire (même justification que _extract_pair) — ET validé
    explicitement depuis le round d'audit du 21/07/2026.

    AVANT ce correctif : `Direction.LONG if "long" in classes else
    Direction.SHORT` — toute classe .dir ne contenant pas "long" (y compris
    une valeur invalide, ex. "neutral", une faute de frappe, un export
    corrompu) devenait silencieusement SHORT. Un document desk malformé
    produisait un rapport valide, un code CLI 0 ou 6, avec une direction
    potentiellement inversée par rapport à l'intention réelle du document —
    corruption sémantique silencieuse, pas une exception. Trouvé par un
    audit qui a spécifiquement testé une classe .dir présente mais invalide
    (les rounds précédents ne testaient que .dir ABSENT, jamais .dir
    invalide) ; confirmé par exécution avant correction.

    Seules "long" et "short" sont des classes valides dans le système réel
    (confirmé par inspection du HTML source de production : aucune autre
    valeur de classe direction n'existe). Toute autre valeur est maintenant
    un document malformé (DeskDocumentError, code CLI 2), jamais un fallback
    silencieux.
    """
    dir_tag = block.find(class_="dir")
    if dir_tag is None:
        raise DeskDocumentError(
            f"Setup {pair!r} : élément .dir manquant — direction non déterminable."
        )
    classes = dir_tag.get("class", [])
    is_long = "long" in classes
    is_short = "short" in classes
    if is_long and not is_short:
        return Direction.LONG
    if is_short and not is_long:
        return Direction.SHORT
    raise DeskDocumentError(
        f"Setup {pair!r} : classe .dir invalide {classes!r} — attendu "
        f"exactement 'long' ou 'short', document desk malformé."
    )


def _extract_conviction(block: Tag, pair: str) -> tuple[str, float]:
    """Champ obligatoire, même justification que _extract_pair. Retourne
    (grade, valeur) — ex: ("BBB", 0.77)."""
    conv_tag = block.find(class_="conv")
    if conv_tag is None:
        raise DeskDocumentError(
            f"Setup {pair!r} : élément .conv manquant — conviction non déterminable."
        )
    conv_text = conv_tag.get_text(strip=True)  # ex: "BBB (0.77)"
    conv_match = re.match(r"([A-Z+]+)\s*\(([\d.]+)\)", conv_text)
    grade = conv_match.group(1) if conv_match else conv_text
    value = float(conv_match.group(2)) if conv_match else float("nan")
    return grade, value


def _extract_cluster(block: Tag) -> str:
    """Champ optionnel — absence = chaîne vide, jamais une erreur."""
    cluster_tag = block.find(class_="cluster-tag")
    return cluster_tag.get_text(strip=True) if cluster_tag else ""


def _extract_factors(block: Tag) -> dict[str, float]:
    """Grille des facteurs (F1-F7, Q-rang). Optionnelle ; une valeur
    individuelle non numérique est ignorée silencieusement (comportement
    préexistant, distinct du traitement des champs de prix — cf. les factors
    ne bloquent jamais une décision à eux seuls, contrairement à R:R)."""
    factors: dict[str, float] = {}
    factor_grid = block.find(class_="factor-grid")
    if not factor_grid:
        return factors
    for f in factor_grid.find_all(class_="factor"):
        lbl = f.find(class_="factor-lbl")
        val = f.find(class_="factor-val")
        if lbl and val:
            key = lbl.get_text(strip=True)
            try:
                factors[key] = float(val.get_text(strip=True))
            except ValueError:
                pass
    return factors


def _extract_metrics(block: Tag) -> tuple[str | None, float | None, int | None]:
    """Grille des métriques (Quality, MTF %, Age). Optionnelle. Retourne
    (quality, mtf_pct, age_days).

    Garde ajoutée lors du round d'audit du 21/07/2026 (trouvé par un audit
    docx, confirmé par exécution) : cette fonction était la seule des trois
    boucles d'extraction de grille (factors, metrics, prices) à accéder
    directement `.find(...).get_text()` sans vérifier la présence de
    l'élément — `_extract_factors` et `_extract_prices` avaient déjà la
    garde équivalente. Un bloc `.metric` structurellement incomplet (ex.
    `.metric-lbl` sans `.metric-val` voisin) levait `AttributeError`,
    rattrapée par le filet générique de cli.py et catégorisée en code 5
    au lieu du code 2 catégorisé — même classe de défaut que le `.pair`
    manquant (2.1.1) et les casts `float()` non protégés (2.1.3)."""
    quality = mtf = age_days = None
    metrics_grid = block.find(class_="metrics-grid")
    if not metrics_grid:
        return quality, mtf, age_days
    for m in metrics_grid.find_all(class_="metric"):
        lbl_tag = m.find(class_="metric-lbl")
        val_tag = m.find(class_="metric-val")
        if not (lbl_tag and val_tag):
            continue
        lbl = lbl_tag.get_text(strip=True)
        val = val_tag.get_text(strip=True)
        if lbl == "Quality":
            quality = val
        elif lbl == "MTF %":
            mtf_match = re.match(r"([\d.]+)%", val)
            mtf = float(mtf_match.group(1)) if mtf_match else None
        elif lbl == "Age":
            age_match = re.match(r"(\d+)j", val)
            age_days = int(age_match.group(1)) if age_match else None
    return quality, mtf, age_days


def _extract_prices(block: Tag, pair: str) -> tuple[float | None, float | None, float | None]:
    """Grille des prix (entry, sl, rr). Optionnelle au niveau du bloc, mais
    toute valeur présente doit être numérique (cf. _safe_float — round
    d'audit du 20/07/2026, trouvé par l'audit Gemini Pro). Retourne
    (entry, stop_loss, risk_reward)."""
    entry = stop_loss = rr = None
    px_grid = block.find(class_="px-grid")
    if not px_grid:
        return entry, stop_loss, rr
    entry_card = px_grid.find(class_="entry")
    if entry_card:
        v = entry_card.find(class_="px-val")
        entry = _safe_float(v.get_text(strip=True), field="entry", pair=pair) if v else None
    sl_card = px_grid.find(class_="sl")
    if sl_card:
        v = sl_card.find(class_="px-val")
        stop_loss = _safe_float(v.get_text(strip=True), field="stop_loss", pair=pair) if v else None
    rr_card = px_grid.find(class_="rr")
    if rr_card:
        v = rr_card.find(class_="px-val")
        rr = _safe_float(v.get_text(strip=True), field="risk_reward", pair=pair) if v else None
    return entry, stop_loss, rr


def _extract_flags(block: Tag) -> tuple[dict, ...]:
    """Flags de contradiction (C1-C10) affichés sous un setup validé.

    PATCH-DESKPARSE-F6 (round de validation zero-régression, 31/07/2026) --
    voir audit F6 : "Les avertissements majeurs émis par la couche technique
    ne sont pas extractibles par la couche de décision : le Comité ne peut
    pas les prendre en compte, même s'il le voulait." Le moteur Desk
    (SECTION 10 CONTRADICTIONS C1..C5, `apply_caps`) produit et rend déjà
    ces flags dans le HTML (`s.flags`, un `<span class="flag {severity}">
    {code} · {detail}</span>` par flag dans un conteneur `.flags-row`) --
    mais aucune fonction du parser Comité ne les lisait. Champ optionnel :
    un setup sans contradiction n'a légitimement pas de `.flags-row`."""
    flags_row = block.find(class_="flags-row")
    if flags_row is None:
        return ()
    out: list[dict] = []
    for f in flags_row.find_all(class_="flag"):
        classes = f.get("class", [])
        severity = next((c for c in classes if c != "flag"),
                        "non disponible dans les documents fournis")
        text = f.get_text(" ", strip=True)
        code, _, detail = text.partition(" · ")
        out.append({"code": code.strip() or "non disponible dans les documents fournis",
                    "severity": severity, "detail": detail.strip()})
    return tuple(out)


def _parse_setup(block: Tag) -> DeskSetup:
    """
    Orchestrateur pur : délègue chaque champ à une sous-fonction dédiée,
    assemble le résultat. Décomposé lors du round d'audit du 20/07/2026
    (complexité cyclomatique D/27 signalée par un outil d'analyse statique
    déterministe — radon — confirmée par exécution, contrairement aux audits
    LLM précédents qui n'avaient pas ce type de métrique). Comportement
    strictement identique à l'ancienne version monolithique ; seule la
    structure change. Chaque sous-fonction est individuellement plus simple
    à lire, tester et modifier sans risquer de casser un champ voisin.
    """
    pair = _extract_pair(block)
    direction = _extract_direction(block, pair)
    grade, value = _extract_conviction(block, pair)
    cluster = _extract_cluster(block)
    factors = _extract_factors(block)
    quality, mtf, age_days = _extract_metrics(block)
    entry, stop_loss, rr = _extract_prices(block, pair)
    flags = _extract_flags(block)

    # PATCH-DESKPARSE-F6 : bluestar.models.DeskSetup n'est PAS dans le
    # corpus audité -- son schéma exact (accepte-t-il déjà un champ
    # `flags` ?) est donc non démontrable ici, au même titre que les autres
    # absences listées par l'audit (§11.2). Construction défensive : si le
    # modèle réel accepte déjà `flags=`, il est peuplé immédiatement et
    # devient consommable par comite_final_selection_grid.py ; sinon,
    # échec sans régression (log + retour à la construction actuelle) au
    # lieu de faire planter tout le pipeline d'extraction sur un champ
    # inconnu. CE FALLBACK EST TEMPORAIRE : la correction définitive est
    # l'ajout d'un champ `flags: tuple[FlagRef, ...] = ()` à DeskSetup dans
    # models.py (hors périmètre de ce corpus) ; une fois ce champ ajouté,
    # ce bloc peut être simplifié pour appeler DeskSetup(..., flags=flags)
    # sans try/except.
    try:
        return DeskSetup(
            pair=pair,
            direction=direction,
            conviction_grade=grade,
            conviction_value=value,
            cluster_tag=cluster,
            quality=quality,
            mtf_pct=mtf,
            age_days=age_days,
            risk_reward=rr,
            factors=factors,
            entry=entry,
            stop_loss=stop_loss,
            flags=flags,
        )
    except TypeError:
        if flags:
            logger.warning(
                "desk_setup_flags_not_supported_by_model pair=%s flags_lost=%d "
                "— models.py doit gagner un champ `flags` sur DeskSetup (cf. audit F6)",
                pair, len(flags),
            )
        return DeskSetup(
            pair=pair,
            direction=direction,
            conviction_grade=grade,
            conviction_value=value,
            cluster_tag=cluster,
            quality=quality,
            mtf_pct=mtf,
            age_days=age_days,
            risk_reward=rr,
            factors=factors,
            entry=entry,
            stop_loss=stop_loss,
        )


_JSON_DIRECTION_MAP = {"Bullish": Direction.LONG, "Bearish": Direction.SHORT}
# "Neutral" (et toute valeur non reconnue) -> None : le modèle Direction du
# comité n'a que long/short, même choix que pour _parse_rejected().


def _parse_correlation_groups(soup: BeautifulSoup) -> dict[str, tuple[CorrelationSignal, ...]]:
    """Lit le bloc `<script id="correlation-groups">` embarqué par le moteur
    depuis le correctif du 27/07/2026. Absent (document produit par un moteur
    plus ancien) ou malformé -> dict vide, jamais une erreur : c'est une
    donnée d'appoint pour les advisories, elle ne conditionne aucune décision
    et son absence ne rend pas le document desk invalide."""
    tag = soup.find("script", id="correlation-groups")
    if tag is None or not tag.string:
        return {}
    try:
        raw = json.loads(tag.string)
    except json.JSONDecodeError:
        logger.warning("correlation_groups_json_invalide — ignoré, advisories techniques indisponibles ce cycle")
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, tuple[CorrelationSignal, ...]] = {}
    for currency, signals in raw.items():
        if not isinstance(signals, list):
            continue
        parsed = []
        for s in signals:
            if not isinstance(s, dict) or "symbol" not in s:
                continue
            parsed.append(CorrelationSignal(
                symbol=s.get("symbol", "non disponible dans les documents fournis"),
                direction=_JSON_DIRECTION_MAP.get(s.get("direction")),
                kind=s.get("kind", "non disponible dans les documents fournis"),
                timeframe=s.get("timeframe", "non disponible dans les documents fournis"),
                mtf_pct=s.get("mtf_pct"),
                quality=s.get("quality", "non disponible dans les documents fournis"),
                confluence=s.get("confluence"),
            ))
        if parsed:
            out[currency] = tuple(parsed)
    return out


def _extract_reject_direction(container: Tag, pair: str) -> Direction | None:
    """Direction d'un actif rejeté/suspendu, lue depuis la classe CSS `.dir`
    -- MÊME CONTRAT que `_extract_direction()` (setups validés) : exactement
    'long' ou 'short' sur l'élément `.dir`, jamais une recherche de
    sous-chaîne dans du texte libre.

    PATCH-DESKPARSE-C03 (round de validation zero-régression, 31/07/2026) :
    remplace l'ancienne recherche de "Bullish"/"Bearish" dans le texte
    aplati de toute la ligne (`row_text`), qui incluait `reject_detail`. Un
    détail de rejet contenant accidentellement le mot "Bullish" (ex. un futur
    message "CHoCH Bullish invalidé") inversait silencieusement la direction
    affichée au comité -- confirmé exploitable par l'audit (C-03). La classe
    `.dir` est un champ structurel dédié, jamais du texte de commentaire :
    même garantie que pour les setups validés, où ce risque n'existe pas.
    """
    dir_tag = container.find(class_="dir")
    if dir_tag is None:
        return None
    classes = dir_tag.get("class", [])
    is_long = "long" in classes
    is_short = "short" in classes
    if is_long and not is_short:
        return Direction.LONG
    if is_short and not is_long:
        return Direction.SHORT
    if classes:
        logger.warning(
            "desk_reject_direction_unrecognized pair=%s classes=%r", pair, classes,
        )
    return None


def _parse_rejected_suspended(soup: BeautifulSoup) -> list[DeskRejectedSetup]:
    """Actifs suspendus (ex: CAL_BLACKOUT) -- structure `.sus-item` (div),
    jamais une ligne de table. Sélecteurs dédiés à cette structure : pas de
    devinette de parent. (PATCH-DESKPARSE-C01/C02, 31/07/2026)"""
    out: list[DeskRejectedSetup] = []
    for item in soup.find_all(class_="sus-item"):
        pair_tag = item.find(class_="sus-item-pair")
        pair = (pair_tag.get_text(strip=True) if pair_tag
                else "non disponible dans les documents fournis")
        direction = _extract_reject_direction(item, pair)
        code_tag = item.find(class_="reject-code")
        reject_code = (code_tag.get_text(strip=True) if code_tag
                       else "non disponible dans les documents fournis")
        txt_tag = item.find(class_="sus-item-txt")
        detail = (txt_tag.get_text(" ", strip=True) if txt_tag
                  else item.get_text(" | ", strip=True))
        out.append(DeskRejectedSetup(pair=pair, direction=direction,
                                     reject_code=reject_code, detail=detail))
    return out


def _parse_rejected_table(soup: BeautifulSoup) -> list[DeskRejectedSetup]:
    """Rejets définitifs (ex: LOW_QUALITY, PRICE_PAST_TP, CLUSTER_DUP) --
    structure `<tr>` (table), jamais un `.sus-item`. Sélecteurs dédiés à
    cette structure : pas de devinette de parent.
    (PATCH-DESKPARSE-C01/C02, 31/07/2026)"""
    out: list[DeskRejectedSetup] = []
    for code_tag in soup.find_all("td", class_="reject-code"):
        row = code_tag.find_parent("tr")
        if row is None:
            continue
        cells = row.find_all("td", recursive=False)
        pair = (cells[0].get_text(strip=True) if cells
                else "non disponible dans les documents fournis")
        direction = _extract_reject_direction(row, pair)
        reject_code = code_tag.get_text(strip=True)
        detail = row.get_text(" | ", strip=True)
        out.append(DeskRejectedSetup(pair=pair, direction=direction,
                                     reject_code=reject_code, detail=detail))
    return out


def _parse_rejected(soup: BeautifulSoup) -> tuple[DeskRejectedSetup, ...]:
    """RÉÉCRITURE (PATCH-DESKPARSE, round de validation zero-régression,
    31/07/2026) -- voir audit C-01/C-02/C-03.

    Cause racine de l'ancienne version : elle itérait sur TOUS les éléments
    `class_="reject-code"` (span dans `.sus-item` OU `<td>` dans une table)
    et devinait leur parent via `find_parent(class_="cal-row")` -- une
    classe absente des deux templates réels (0 occurrence vérifiée dans le
    HTML de production) -- avec repli sur `.parent`. Le compte total (31)
    tombait juste par coïncidence numérique ; la paire (split sur "|") et la
    direction (sous-chaîne dans tout le texte de la ligne) n'avaient aucun
    contrat structurel et cassaient silencieusement sur toute variation de
    template (ordre des colonnes, badge additionnel, mot "Bullish" dans un
    détail).

    Nouvelle version : deux extracteurs dédiés, un par structure HTML réelle
    (`.sus-item` / `<tr>`), chacun avec un sélecteur explicite pour la paire
    et une lecture de `.dir` pour la direction -- même contrat que
    `_extract_direction()`. Une structure future qui ne correspond à AUCUN
    des deux formats sera absente du résultat (pas de paire/direction
    fausse) ; l'invariant `setups + rejected == universe_total`, déjà
    vérifié dans `parse_desk`, le détectera.

    Zero-régression vérifiée par rejeu sur le HTML du 31/07/2026 : les 31
    paires, directions et reject_code produits sont identiques,
    caractère pour caractère, à ceux de l'ancienne implémentation."""
    return tuple(_parse_rejected_suspended(soup) + _parse_rejected_table(soup))


def parse_desk(html: str) -> DeskSnapshot:
    soup = BeautifulSoup(html, "html.parser")

    report_dt, report_tz = _parse_report_datetime(soup)
    universe_evaluated, universe_total = _parse_universe(soup)
    event_risk = _parse_event_risk(soup)
    themes = _parse_themes(soup)

    setups = tuple(_parse_setup(block) for block in soup.find_all(class_="setup"))
    rejected = _parse_rejected(soup)
    correlation_groups = _parse_correlation_groups(soup)

    if len(setups) + len(rejected) != universe_total:
        logger.warning(
            "desk_universe_mismatch declared_total=%d validated=%d rejected=%d sum=%d "
            "— incohérence interne au document desk, à signaler avant toute décision",
            universe_total, len(setups), len(rejected), len(setups) + len(rejected),
        )

    result = DeskSnapshot(
        report_datetime=report_dt,
        report_timezone=report_tz,
        universe_evaluated=universe_evaluated,
        universe_total=universe_total,
        event_risk=event_risk,
        themes=themes,
        setups=setups,
        rejected=rejected,
        correlation_groups=correlation_groups,
    )
    logger.info(
        "desk_parsed datetime=%s universe=%d/%d validated=%d rejected=%d",
        result.report_datetime, result.universe_evaluated, result.universe_total,
        len(result.setups), len(result.rejected),
    )
    return result
