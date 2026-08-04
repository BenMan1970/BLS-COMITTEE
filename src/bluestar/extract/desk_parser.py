"""Extraction du rapport desk technique BLUESTAR (setups validés + rejets)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup, Tag

from bluestar.errors import DeskDocumentError
from bluestar.models import CorrelationSignal, DeskRejectedSetup, DeskSetup, DeskSnapshot, Direction

logger = logging.getLogger("bluestar.extract.desk")

# PATCH-FRESHNESS (round du 31/07/2026, audit B-2/F-04) : seuil au-delà
# duquel le document desk est considéré périmé. Aligné sur
# bluestar.decide.MAX_DESK_DOC_AGE_H — déclaré ici pour que cli.py puisse
# auditer AVANT de décider, sans importer la couche décision.
MAX_DESK_DOC_AGE_H = 3.0  # NON CALIBRÉ — même commentaire que côté decide.

# Décalages horaires nommés acceptés dans le bandeau desk.
_TZ_NAMED_OFFSETS = {"UTC": 0, "GMT": 0, "CET": 1, "CEST": 2}


def audit_document_freshness(desk: DeskSnapshot, now: datetime | None = None) -> str | None:
    """Retourne un message d'alerte si le document desk est périmé ou non
    datable, None sinon. Destiné à cli.py : le message DOIT figurer dans le
    rapport final. Jamais d'exception : un doute sur la fraîcheur est un
    constat, pas un crash."""
    now = now or datetime.now(timezone.utc)
    try:
        dt_naive = datetime.strptime(desk.report_datetime, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ("date du document desk non vérifiable — fraîcheur NON démontrable, "
                "à déclarer dans le rapport")
    tzs = (desk.report_timezone or "").upper()
    if tzs in _TZ_NAMED_OFFSETS:
        desk_dt = dt_naive.replace(tzinfo=timezone(timedelta(hours=_TZ_NAMED_OFFSETS[tzs])))
    elif tzs.startswith("GMT"):
        try:
            desk_dt = dt_naive.replace(tzinfo=timezone(timedelta(hours=int(tzs[3:]))))
        except ValueError:
            return f"fuseau du document desk non vérifiable ({tzs!r}) — fraîcheur NON démontrable"
    else:
        return f"fuseau du document desk non vérifiable ({tzs!r}) — fraîcheur NON démontrable"
    age_h = (now - desk_dt).total_seconds() / 3600.0
    if age_h > MAX_DESK_DOC_AGE_H:
        return (f"document desk âgé de {age_h:.1f}h (> seuil {MAX_DESK_DOC_AGE_H:.1f}h) — "
                f"prix et statuts calendaires potentiellement périmés")
    return None


def _safe_float(raw_text: str, *, field: str, pair: str) -> float:
    """Cast float() défensif — lève DeskDocumentError (code CLI 2) au lieu de
    laisser fuiter ValueError (rattrapée par le filet générique de cli.py et
    catégorisée en code 5, "erreur inattendue")."""
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


# ICF v2 (04/08/2026), mitigation C-9 LIMITÉE AU PARSER DESK.
# Les documents desk observés arrivent parfois avec les caractères non-ASCII
# supprimés ("Rgime", "Thmes", "valids"). Les regex ci-dessous rendent
# l'accent OPTIONNEL : `R[eé]?gime` matche "Régime", "Regime" ET "Rgime".
# C'est STRICTEMENT plus permissif — aucun document qui matchait avant ne
# cesse de matcher. Volontairement NON appliqué à macro_parser._parse_regime,
# qui relève d'une décision de gouvernance distincte (cf. audit C-9/F9).
_RE_DESK_REGIME = re.compile(r"R[eé]?gime\s*:\s*(.+)")
_RE_DESK_THEMES = re.compile(r"Th[eè]?mes\s*:\s*(.+)")


def _parse_macro_regime_label(soup: BeautifulSoup) -> str | None:
    """PATCH-DUALREGIME (ICF v2, Proposition 6 — Règle Absolue 4). Le bandeau
    Desk (.page-subbar) porte son propre "Régime : X" (ex: EVENT_DRIFT — état
    CALENDAIRE, dérivé de la fenêtre d'événements macro autour du cycle),
    totalement distinct du "Régime « Y »" du Macro Briefing (ex: Mixed /
    Selective — état de MARCHÉ, issu d'un vote multi-facteur). Les deux
    portent le même mot "régime" pour deux constructions différentes ; avant
    ce patch, le Comité n'affichait que celui du Macro, sous une étiquette
    qui laissait croire à une source unique.

    Ce patch ne fait QUE divulguer — aucune décision, aucun état, aucun gate
    ne dépend de ce champ.

    Patron d'extraction : span-frère indépendant (même raison que
    PATCH-THEMES-BLEED — ne jamais ancrer sur le texte aplati du bandeau, un
    badge intercalé pourrait fuiter)."""
    subbar = soup.find(class_="page-subbar")
    if subbar is None:
        return None
    for span in subbar.find_all("span", recursive=False):
        text = span.get_text(" ", strip=True)
        m = _RE_DESK_REGIME.match(text)
        if m:
            return m.group(1).strip()
    return None


def _parse_themes(soup: BeautifulSoup) -> tuple[str, ...]:
    """PATCH-THEMES-BLEED (31/07/2026) : l'ancienne version regexait le texte
    aplati de tout le page-subbar — si un badge s'intercalait entre le span
    Thèmes et le span CONFIDENTIEL, il était absorbé dans le dernier thème.

    Correctif : chaque bloc du bandeau est un <span> FRÈRE indépendant dans le
    template (jamais imbriqué) — on ancre donc sur le span lui-même plutôt que
    sur le texte aplati du conteneur.

    ICF v2 : regex rendue tolérante à l'accent manquant (cf. _RE_DESK_THEMES).
    Sans cela, sur un document dont les accents ont été perdus en transport,
    `themes` valait () — et le diagnostic Force↔Thème (Proposition 3) était
    mort silencieusement."""
    subbar = soup.find(class_="page-subbar")
    if subbar is None:
        return ()
    for span in subbar.find_all("span", recursive=False):
        text = span.get_text(" ", strip=True)
        m = _RE_DESK_THEMES.match(text)
        if m:
            return tuple(t.strip() for t in m.group(1).split(",") if t.strip())
    return ()


def _parse_banners(soup: BeautifulSoup) -> tuple[str, ...]:
    """K-3/R-3/G4 FIX (02/08/2026). Le moteur Desk émet un
    `<div class="banner">` par alerte document-niveau (fuseau incohérent,
    couverture calendrier tronquée). Champ optionnel : un cycle sans alerte
    n'a légitimement aucun `.banner`, et le résultat est alors un tuple vide."""
    return tuple(b.get_text(" ", strip=True) for b in soup.find_all(class_="banner"))


def _extract_pair(block: Tag) -> str:
    """Champ obligatoire : un bloc .setup sans .pair est un document desk
    malformé (DeskDocumentError, code CLI 2), pas une anomalie interne
    imprévue (code CLI 5)."""
    pair_tag = block.find(class_="pair")
    if pair_tag is None:
        raise DeskDocumentError(
            "Bloc .setup sans élément .pair — document desk structurellement "
            "malformé (paire non identifiable)."
        )
    return pair_tag.get_text(strip=True)


def _extract_direction(block: Tag, pair: str) -> Direction:
    """Champ obligatoire ET validé explicitement (round du 21/07/2026).
    Seules "long" et "short" sont des classes valides ; toute autre valeur est
    un document malformé, jamais un fallback silencieux."""
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
    """Champ obligatoire. Retourne (grade, valeur) — ex: ("BBB", 0.77)."""
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


def _extract_cap_reason(block: Tag) -> str | None:
    """PATCH-CAPREASON (ICF v2, Proposition 1 — audit C-05). Le moteur Desk
    rend `<div class="cap-note">{message}</div>` quand un plafond de conviction
    est appliqué (ex: "Plafond conviction appliqué : risque macro NON ÉVALUÉ
    (couverture calendaire insuffisante) — cap prudentiel") — mais aucune
    fonction du parser Comité ne le lisait avant ce correctif, et jusqu'à ce
    round `bluestar.models.DeskSetup` ne pouvait même pas l'accueillir.

    Conséquence observée : des setups explicitement plafonnés par la couche
    technique s'affichaient au Comité avec un facteur limitant qui n'en disait
    rien.

    Champ optionnel, même statut que `entry_type` : il n'alimente qu'un
    enrichissement du `limiting_factor`, jamais un état (`DecisionState`).
    Absence de `.cap-note` = setup non plafonné = None, jamais une erreur."""
    cap_tag = block.find(class_="cap-note")
    return cap_tag.get_text(" ", strip=True) if cap_tag else None


def _extract_factors(block: Tag) -> tuple[dict[str, float], frozenset[str]]:
    """Grille des facteurs (F1-F7, Q-rang). Optionnelle ; une valeur
    individuelle non numérique est ignorée silencieusement (comportement
    préexistant, distinct du traitement des champs de prix — les factors ne
    bloquent jamais une décision à eux seuls, contrairement à R:R).

    PATCH-FACTORSMISS (ICF v2, Proposition 7) : retourne EN PLUS l'ensemble
    des clés dont le `.factor-val` porte la classe CSS `miss`, c.-à-d. les
    facteurs NON MESURÉS par le Desk (et donc exclus de son `absolute_mean`).
    Sans cette information, un F4=0.00 mesuré et un F4=0.00 non mesuré sont
    strictement indiscernables côté Comité (audit C-6).

    PLOMBERIE UNIQUEMENT : aucune règle de décision n'est ajoutée dans ce
    patch, délibérément — sinon la Proposition changerait des états et
    sortirait du contrat Zéro Régression."""
    factors: dict[str, float] = {}
    missing: set[str] = set()
    factor_grid = block.find(class_="factor-grid")
    if not factor_grid:
        return factors, frozenset()
    for f in factor_grid.find_all(class_="factor"):
        lbl = f.find(class_="factor-lbl")
        val = f.find(class_="factor-val")
        if lbl and val:
            key = lbl.get_text(strip=True)
            try:
                factors[key] = float(val.get_text(strip=True))
            except ValueError:
                continue
            if "miss" in (val.get("class") or []):
                missing.add(key)
    return factors, frozenset(missing)


def _extract_metrics(block: Tag) -> tuple[str | None, float | None, int | None]:
    """Grille des métriques (Quality, MTF %, Age). Optionnelle. Retourne
    (quality, mtf_pct, age_days). Garde de présence sur chaque sous-élément
    (round du 21/07/2026) : un bloc `.metric` structurellement incomplet
    levait AttributeError, catégorisée en code 5 au lieu du code 2."""
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
    toute valeur présente doit être numérique (cf. _safe_float)."""
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


def _extract_entry_type(block: Tag) -> str | None:
    """X-9 FIX (02/08/2026). Le moteur Desk rend
    `<div class="px-card entry">...<div class="px-sub">{entry_type}</div></div>`
    — "Market" ou "Limit". C'est l'information nécessaire pour qualifier un
    ELIGIBLE dont l'entrée est "Market" alors que le marché FX est fermé au
    moment de la génération. Champ optionnel : None si absent."""
    px_grid = block.find(class_="px-grid")
    if not px_grid:
        return None
    entry_card = px_grid.find(class_="entry")
    if not entry_card:
        return None
    sub = entry_card.find(class_="px-sub")
    return sub.get_text(strip=True) if sub else None


def _extract_flags(block: Tag) -> tuple[dict, ...]:
    """Flags de contradiction (C1-C10) affichés sous un setup validé.
    PATCH-DESKPARSE-F6 (31/07/2026) : le moteur Desk produit et rend ces flags
    dans le HTML (`<span class="flag {severity}">{code} · {detail}</span>`)
    mais aucune fonction du parser Comité ne les lisait. Champ optionnel : un
    setup sans contradiction n'a légitimement pas de `.flags-row`."""
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


def _extract_cal_status(block: Tag) -> tuple[str | None, str]:
    """Statut calendaire par setup (PATCH-CALSTATUS, audit F-05/C-04). Le Desk
    rend `<div class="cal-row"><span class="cal-{status}">{STATUS}</span>
    <span>{note}</span></div>` ; la ligne « Horizon cible ≈ … » partage la
    classe conteneur `.cal-row` mais son span n'a pas de classe cal-*, elle est
    donc ignorée sans ambiguïté."""
    for row in block.find_all(class_="cal-row"):
        span = row.find(class_=re.compile(r"^cal-"))
        if span is None:
            continue
        status_cls = next((c for c in span.get("class", [])
                           if c.startswith("cal-") and c != "cal-row"), None)
        if not status_cls:
            continue
        status = status_cls[len("cal-"):].upper()
        note = row.get_text(" ", strip=True)
        note = note.replace(span.get_text(strip=True), "", 1).strip()
        return status, note
    return None, ""


# ---------------------------------------------------------------------------
# Cascade de construction défensive (généralisation ICF v2)
#
# Historique : chaque nouveau champ optionnel (flags/cal_status B-1, entry_type
# X-9, cap_reason/factors_missing ICF v2) ajoutait un `try/except TypeError`
# imbriqué supplémentaire, jusqu'à rendre `_parse_setup` illisible et rendre
# probable l'oubli d'un log de perte. Cette table remplace l'imbrication par
# une boucle, à comportement STRICTEMENT identique :
#   - on tente le schéma le plus complet d'abord ;
#   - on retombe palier par palier si `bluestar.models.DeskSetup` ne connaît
#     pas encore un champ ;
#   - toute donnée réellement présente mais non transportable est journalisée ;
#   - la perte de `flags` ou `cal_status` NON VIDES reste INTERDITE (raise),
#     exactement comme avant (audit C-02 : la perte d'un avertissement majeur
#     de la couche technique ne doit jamais être dégradée en warning de log).
# ---------------------------------------------------------------------------
_SETUP_OPTIONAL_TIERS: tuple[tuple[str, ...], ...] = (
    ("flags", "cal_status", "cal_note", "entry_type", "cap_reason", "factors_missing"),
    ("flags", "cal_status", "cal_note", "entry_type", "cap_reason"),
    ("flags", "cal_status", "cal_note", "entry_type"),
    ("flags", "cal_status", "cal_note"),
    (),
)

_SETUP_FIELD_HINT = {
    "flags": "champ `flags: tuple = ()` (patch B-1)",
    "cal_status": "champ `cal_status: str | None = None` (patch B-1)",
    "cal_note": "champ `cal_note: str = ''` (patch B-1)",
    "entry_type": "champ `entry_type: str | None = None` (audit X-9)",
    "cap_reason": "champ `cap_reason: str | None = None` (ICF v2, Proposition 1)",
    "factors_missing": "champ `factors_missing: frozenset[str] = frozenset()` (ICF v2, Proposition 7)",
}


def _parse_setup(block: Tag) -> DeskSetup:
    """Orchestrateur pur : délègue chaque champ à une sous-fonction dédiée,
    assemble le résultat. Décomposé lors du round du 20/07/2026 (complexité
    cyclomatique D/27 signalée par radon)."""
    pair = _extract_pair(block)
    direction = _extract_direction(block, pair)
    grade, value = _extract_conviction(block, pair)
    cluster = _extract_cluster(block)
    factors, factors_missing = _extract_factors(block)
    quality, mtf, age_days = _extract_metrics(block)
    entry, stop_loss, rr = _extract_prices(block, pair)
    entry_type = _extract_entry_type(block)
    flags = _extract_flags(block)
    cal_status, cal_note = _extract_cal_status(block)
    cap_reason = _extract_cap_reason(block)

    base_kwargs = dict(
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
    optional = {
        "flags": flags,
        "cal_status": cal_status,
        "cal_note": cal_note,
        "entry_type": entry_type,
        "cap_reason": cap_reason,
        "factors_missing": factors_missing,
    }

    last_exc: TypeError | None = None
    for tier in _SETUP_OPTIONAL_TIERS:
        try:
            setup = DeskSetup(**base_kwargs, **{k: optional[k] for k in tier})
        except TypeError as exc:
            last_exc = exc
            continue

        # Perte INTERDITE : flags majeurs / statut calendaire non vides.
        if ("flags" not in tier and flags) or ("cal_status" not in tier and cal_status):
            raise DeskDocumentError(
                f"Setup {pair!r} : {len(flags)} flag(s) et/ou cal_status={cal_status!r} "
                f"extraits du document mais refusés par bluestar.models.DeskSetup "
                f"({last_exc}) — appliquer le patch B-1 dans bluestar/models.py "
                f"avant de relancer. Perte de données interdite (audit C-02)."
            ) from last_exc

        # Perte TOLÉRÉE mais journalisée : champs d'enrichissement/divulgation.
        for key, val in optional.items():
            if key in tier or not val:
                continue
            logger.info(
                "desk_setup_field_unsupported pair=%s field=%s value=%r — "
                "bluestar.models.DeskSetup n'a pas (encore) le %s ; l'information "
                "extraite du document n'atteindra pas le rapport du Comité.",
                pair, key, val, _SETUP_FIELD_HINT.get(key, f"champ `{key}`"),
            )
        return setup

    raise DeskDocumentError(
        f"Setup {pair!r} : bluestar.models.DeskSetup n'accepte même pas le schéma "
        f"minimal attendu ({last_exc}) — modèle et parser désynchronisés."
    )


_JSON_DIRECTION_MAP = {"Bullish": Direction.LONG, "Bearish": Direction.SHORT}
# "Neutral" (et toute valeur non reconnue) -> None : le modèle Direction du
# comité n'a que long/short, même choix que pour _parse_rejected().


def _parse_correlation_groups(soup: BeautifulSoup) -> dict[str, tuple[CorrelationSignal, ...]]:
    """Lit le bloc `<script id="correlation-groups">` embarqué par le moteur
    depuis le correctif du 27/07/2026. Absent ou malformé -> dict vide, jamais
    une erreur : c'est une donnée d'appoint pour les advisories, elle ne
    conditionne aucune décision."""
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
    """Direction d'un actif rejeté/suspendu, lue depuis la classe CSS `.dir` —
    MÊME CONTRAT que `_extract_direction()` : exactement 'long' ou 'short',
    jamais une recherche de sous-chaîne dans du texte libre (audit C-03 : un
    détail de rejet contenant "Bullish" inversait silencieusement la direction
    affichée au comité)."""
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
        # R-19 : "neutral" est une classe légitime du template Desk (ex.
        # DE30/EUR), pas une anomalie — debug plutôt que warning, pour ne pas
        # noyer les vrais avertissements. Retour inchangé (None).
        if "neutral" in classes:
            logger.debug("desk_reject_direction_neutral pair=%s (classe légitime)", pair)
        else:
            logger.warning(
                "desk_reject_direction_unrecognized pair=%s classes=%r", pair, classes,
            )
    return None


def _parse_rejected_suspended(soup: BeautifulSoup) -> list[DeskRejectedSetup]:
    """Actifs suspendus (ex: CAL_BLACKOUT) — structure `.sus-item` (div),
    jamais une ligne de table."""
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
    """Rejets définitifs (ex: LOW_QUALITY, PRICE_PAST_TP, CLUSTER_DUP) —
    structure `<tr>` (table), jamais un `.sus-item`."""
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
    """RÉÉCRITURE (PATCH-DESKPARSE, 31/07/2026 — audit C-01/C-02/C-03).
    Deux extracteurs dédiés, un par structure HTML réelle (`.sus-item` / `<tr>`),
    chacun avec un sélecteur explicite pour la paire et une lecture de `.dir`
    pour la direction. Une structure future qui ne correspond à AUCUN des deux
    formats sera absente du résultat (pas de paire/direction fausse) ;
    l'invariant `setups + rejected == universe_total` le détectera."""
    return tuple(_parse_rejected_suspended(soup) + _parse_rejected_table(soup))


def _parse_calendar_coverage(soup: BeautifulSoup) -> dict[str, frozenset[str]]:
    """PATCH-CALCOVERAGE (ICF v2, Proposition 2 — audit C-3). Lit le bloc
    `<script type="application/json" id="calendar-coverage">
    {"covered": [...], "uncovered": [...]}</script>` émis par le moteur Desk,
    document-niveau (même patron que `_parse_correlation_groups`).

    Constat qui motive ce patch : la bannière du document dit déjà, en prose
    française, qu'un statut `OK` sur une devise hors du filtre producteur
    signifie "non mesuré", pas "dégagé". Mais rien ne joignait cette
    information à la ligne de décision par actif : le lecteur voyait
    `cal_status = OK` en vert sans savoir que la jambe est hors couverture.

    Absent ou malformé -> covered/uncovered vides : donnée d'appoint, ne
    conditionne aucune décision (elle ne produit qu'une advisory non
    bloquante), son absence ne rend pas le document desk invalide."""
    empty = {"covered": frozenset(), "uncovered": frozenset()}
    tag = soup.find("script", id="calendar-coverage")
    if tag is None or not tag.string:
        return empty
    try:
        raw = json.loads(tag.string)
    except json.JSONDecodeError:
        logger.warning("calendar_coverage_json_invalide — ignoré, advisory de couverture indisponible ce cycle")
        return empty
    if not isinstance(raw, dict):
        return empty
    covered = raw.get("covered", [])
    uncovered = raw.get("uncovered", [])
    if not isinstance(covered, list) or not isinstance(uncovered, list):
        return empty
    return {
        "covered": frozenset(c.upper() for c in covered if isinstance(c, str)),
        "uncovered": frozenset(c.upper() for c in uncovered if isinstance(c, str)),
    }


_SNAPSHOT_OPTIONAL_TIERS: tuple[tuple[str, ...], ...] = (
    ("banners", "calendar_coverage", "macro_regime_label"),
    ("banners", "calendar_coverage"),
    ("banners",),
    (),
)

_SNAPSHOT_FIELD_HINT = {
    "banners": "champ `banners: tuple[str, ...] = ()` (audit K-3/R-3/G4)",
    "calendar_coverage": "champ `calendar_coverage: Mapping[str, frozenset[str]] = {}` (ICF v2, Proposition 2)",
    "macro_regime_label": "champ `macro_regime_label: str | None = None` (ICF v2, Proposition 6)",
}


def parse_desk(html: str) -> DeskSnapshot:
    soup = BeautifulSoup(html, "html.parser")

    report_dt, report_tz = _parse_report_datetime(soup)
    universe_evaluated, universe_total = _parse_universe(soup)
    event_risk = _parse_event_risk(soup)
    themes = _parse_themes(soup)

    setups = tuple(_parse_setup(block) for block in soup.find_all(class_="setup"))
    rejected = _parse_rejected(soup)
    correlation_groups = _parse_correlation_groups(soup)
    banners = _parse_banners(soup)
    calendar_coverage = _parse_calendar_coverage(soup)
    macro_regime_label = _parse_macro_regime_label(soup)

    if len(setups) + len(rejected) != universe_total:
        logger.warning(
            "desk_universe_mismatch declared_total=%d validated=%d rejected=%d sum=%d "
            "— incohérence interne au document desk, à signaler avant toute décision",
            universe_total, len(setups), len(rejected), len(setups) + len(rejected),
        )

    base_kwargs = dict(
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
    optional = {
        "banners": banners,
        "calendar_coverage": calendar_coverage,
        "macro_regime_label": macro_regime_label,
    }

    result: DeskSnapshot | None = None
    last_exc: TypeError | None = None
    for tier in _SNAPSHOT_OPTIONAL_TIERS:
        try:
            result = DeskSnapshot(**base_kwargs, **{k: optional[k] for k in tier})
        except TypeError as exc:
            last_exc = exc
            continue
        for key, val in optional.items():
            if key in tier:
                continue
            has_data = bool(val) if key != "calendar_coverage" else bool(
                val.get("covered") or val.get("uncovered")
            )
            if not has_data:
                continue
            logger.warning(
                "desk_snapshot_field_unsupported field=%s — appliquer le %s dans "
                "bluestar/models.py::DeskSnapshot pour ne pas perdre cette information "
                "document-niveau (%s)",
                key, _SNAPSHOT_FIELD_HINT.get(key, f"champ `{key}`"), last_exc,
            )
        break

    if result is None:
        raise DeskDocumentError(
            f"bluestar.models.DeskSnapshot n'accepte même pas le schéma minimal "
            f"attendu ({last_exc}) — modèle et parser désynchronisés."
        )

    logger.info(
        "desk_parsed datetime=%s universe=%d/%d validated=%d rejected=%d "
        "themes=%d uncovered=%d desk_regime=%r",
        result.report_datetime, result.universe_evaluated, result.universe_total,
        len(result.setups), len(result.rejected), len(themes),
        len(calendar_coverage.get("uncovered", ())), macro_regime_label,
    )
    return result

  
