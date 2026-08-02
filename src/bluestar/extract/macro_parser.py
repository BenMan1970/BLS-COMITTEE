"""
Extraction du briefing macro BLUESTAR.

Ancrage structurel utilisé (vérifié sur le document réel avant écriture de ce
parseur, cf. exploration DOM) :
- Le classement de force relative est ancré au sous-titre exact
  "CURRENCY STRENGTH RANKING".
- L'IPS est ancré au sous-titre exact "INSTITUTIONAL POSITIONING SCORE".
- Les fiches actifs prioritaires sont les blocs `.asset` de la section
  "Fiches Actifs".
Ces ancrages sont plus robustes qu'un ordre positionnel car `rank-row` est une
classe CSS réutilisée pour au moins 4 blocs sémantiquement différents dans ce
document (force relative, IPS, facteurs de régime, chaîne causale) — un parsing
par position sans ancrage sémantique romprait silencieusement si l'ordre des
sections change.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup, Tag

from bluestar.errors import MacroDocumentError
from bluestar.models import (
    CurrencyMacroData,
    Direction,
    MacroPrioritySetup,
    MacroSnapshot,
)


import logging

logger = logging.getLogger("bluestar.extract.macro")


# O-8/R-10 FIX (round de validation zero-régression, 02/08/2026) : le Desk
# possédait déjà `audit_document_freshness` (bluestar.extract.desk_parser) ;
# le Macro n'avait AUCUN équivalent -- asymétrie confirmée par l'audit
# indépendant (O-8/R-10 : "aucun audit de fraîcheur macro"). Seuil aligné sur
# bluestar.decide.MAX_DESK_DOC_AGE_H / desk_parser.MAX_DESK_DOC_AGE_H (même
# statut de calibration honnête : NON CALIBRÉ, valeur provisoire).
#
# Format de date DÉLIBÉRÉMENT distinct de celui du desk : le Macro rend
# "JJ/MM/AAAA HH:MM" (cf. _parse_report_datetime ci-dessous) là où le Desk
# rend "%Y-%m-%d %H:%M" -- l'ajout de cette garde n'est donc pas un
# copier-coller du parseur desk, comme relevé par l'audit (R-10, "aggravant
# relevé").
MAX_MACRO_DOC_AGE_H = 3.0  # NON CALIBRÉ — même statut que MAX_DESK_DOC_AGE_H
_MACRO_TZ_NAMED_OFFSETS = {"UTC": 0, "GMT": 0, "CET": 1, "CEST": 2}
_MACRO_TZ_OFFSET_RE = re.compile(r"^GMT([+-])(\d+)$")


def audit_macro_document_freshness(macro: "MacroSnapshot", now: datetime | None = None) -> str | None:
    """Retourne un message d'alerte si le document macro est périmé ou non
    datable, None sinon. Jamais d'exception : un doute sur la fraîcheur est
    un constat, pas un crash — même contrat que
    `bluestar.extract.desk_parser.audit_document_freshness`."""
    now = now or datetime.now(timezone.utc)
    try:
        dt_naive = datetime.strptime(macro.report_datetime, "%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return ("date du document macro non vérifiable — fraîcheur NON démontrable, "
                "à déclarer dans le rapport")
    tzs = (macro.report_timezone or "").upper()
    if tzs in _MACRO_TZ_NAMED_OFFSETS:
        macro_dt = dt_naive.replace(tzinfo=timezone(timedelta(hours=_MACRO_TZ_NAMED_OFFSETS[tzs])))
    else:
        m = _MACRO_TZ_OFFSET_RE.match(tzs)
        if not m:
            return f"fuseau du document macro non vérifiable ({tzs!r}) — fraîcheur NON démontrable"
        sign = 1 if m.group(1) == "+" else -1
        macro_dt = dt_naive.replace(tzinfo=timezone(sign * timedelta(hours=int(m.group(2)))))
    age_h = (now - macro_dt).total_seconds() / 3600.0
    if age_h > MAX_MACRO_DOC_AGE_H:
        return (f"document macro âgé de {age_h:.1f}h (> seuil {MAX_MACRO_DOC_AGE_H:.1f}h) — "
                f"régime, IPS et priorités potentiellement périmés")
    return None


def _find_section_by_subtitle(soup: BeautifulSoup, subtitle_substring: str) -> Tag:
    for sub_lbl in soup.find_all(class_="sub-lbl"):
        if subtitle_substring in sub_lbl.get_text():
            return sub_lbl
    raise MacroDocumentError(f"Sous-titre introuvable dans le document : {subtitle_substring!r}")


def _parse_currency_strength(soup: BeautifulSoup) -> dict[str, tuple[int, float]]:
    """Retourne {devise: (rang, score)} depuis le bloc CURRENCY STRENGTH RANKING."""
    anchor = _find_section_by_subtitle(soup, "CURRENCY STRENGTH RANKING")
    container = anchor.find_next_sibling("div")
    if container is None:
        raise MacroDocumentError("Conteneur du classement de force introuvable après l'ancre.")

    result: dict[str, tuple[int, float]] = {}
    for row in container.find_all(class_="rank-row"):
        lbl = row.find(class_="rank-lbl")
        val = row.find(class_="rank-val")
        if lbl is None or val is None:
            continue
        m = re.match(r"(\d+)\.\s*([A-Z]{3})", lbl.get_text(strip=True))
        if not m:
            continue
        rank, code = int(m.group(1)), m.group(2)
        try:
            score = float(val.get_text(strip=True))
        except ValueError as exc:
            # Correction du round d'audit du 20/07/2026 (même classe de défaut
            # que le cast R:R de desk_parser.py, trouvée par l'audit Gemini Pro
            # sur ce champ-là et étendue ici par cohérence) : une valeur non
            # numérique doit lever MacroDocumentError (code CLI 1), pas fuiter
            # en ValueError catégorisée code 5.
            raise MacroDocumentError(
                f"Devise {code!r} : score de force relative non numérique "
                f"({val.get_text(strip=True)!r}) — document macro malformé."
            ) from exc
        result[code] = (rank, score)
    if len(result) != 8:
        raise MacroDocumentError(
            f"Nombre de devises inattendu dans le classement de force : {len(result)} (attendu 8)."
        )
    return result


def _parse_ips(soup: BeautifulSoup) -> dict[str, tuple[float | None, str]]:
    """Retourne {devise: (ips, source_date_label)} depuis le bloc IPS.
    IPS = None si le document indique explicitement l'absence (ex. USD)."""
    anchor = _find_section_by_subtitle(soup, "INSTITUTIONAL POSITIONING SCORE")
    container = anchor.find_next_sibling("div")
    if container is None:
        raise MacroDocumentError("Conteneur IPS introuvable après l'ancre.")

    # V4-22 FIX : chercher la date dans les spans frères (même pattern que
    # _parse_themes dans desk_parser.py) plutôt que dans le texte brut du parent.
    # Cela rend la capture robuste aux badges ajoutés entre l'ancre et la date.
    source_date = "non disponible dans les documents fournis"
    for span in container.find_all("span", recursive=False):
        span_text = span.get_text(" ", strip=True)
        date_match = re.search(r"(Vendredi|Lundi|Mardi|Mercredi|Jeudi|Samedi|Dimanche)\s+\d{1,2}\s+\w+\s+\d{4}",
                               span_text)
        if date_match:
            source_date = date_match.group(0)
            break

    result: dict[str, tuple[float | None, str]] = {}
    for row in container.find_all(class_="rank-row"):
        lbl = row.find(class_="rank-lbl")
        if lbl is None:
            continue
        code = lbl.get_text(strip=True)
        if not re.fullmatch(r"[A-Z]{3}", code):
            continue
        row_text = row.get_text(" ", strip=True)
        m = re.search(rf"{code}\s+(\d+)", row_text)
        ips_val = float(m.group(1)) if m else None
        result[code] = (ips_val, source_date)
    return result


def _parse_priority_setups(soup: BeautifulSoup) -> tuple[MacroPrioritySetup, ...]:
    setups = []
    for asset in soup.find_all(class_="asset"):
        name_tag = asset.find(class_="asset-name")
        action_tag = asset.find(class_="asset-action")
        stars_tag = asset.find(class_=lambda c: c and c.startswith("stars-"))
        if name_tag is None or action_tag is None:
            continue
        pair = name_tag.get_text(strip=True)
        action_classes = action_tag.get("class", [])
        # R-9 FIX (round de validation zero-régression, 02/08/2026, MAJEUR).
        # AVANT ce correctif : `Direction.SHORT if "short" in action_classes
        # else Direction.LONG` — toute classe absente de "short" (y compris
        # une classe légale mais non directionnelle comme "wait", cf.
        # commentaire de `AssetSetup.action_class` dans le moteur Macro, ou
        # une classe invalide/absente) devenait silencieusement LONG. Asymétrie
        # non justifiée avec `desk_parser._extract_direction`, qui lève
        # `DeskDocumentError` sur toute classe .dir ambiguë ou absente plutôt
        # que de deviner (audit indépendant, rapport RUN-4, R-9). Inatteignable
        # ce cycle (`select_priority_assets` écarte déjà `direction == 0` en
        # amont, donc seuls "long"/"short" atteignent ce point) — mais la
        # défense en profondeur doit être symétrique entre les deux parsers
        # plutôt que de dépendre silencieusement d'une garantie amont non
        # documentée ici.
        is_long = "long" in action_classes
        is_short = "short" in action_classes
        if is_long and not is_short:
            direction = Direction.LONG
        elif is_short and not is_long:
            direction = Direction.SHORT
        else:
            raise MacroDocumentError(
                f"Setup prioritaire {pair!r} : classe .asset-action ambiguë ou "
                f"non directionnelle ({action_classes!r}) — direction non "
                f"déterminable, document macro malformé."
            )
        stars = 0
        if stars_tag:
            star_class = [c for c in stars_tag.get("class", []) if c.startswith("stars-")]
            if star_class:
                stars = int(star_class[0].split("-")[1])
        # rationale : cherché dans la chaîne causale (rank-row dont le label == pair)
        rationale = ""
        for row in soup.find_all(class_="rank-row"):
            lbl = row.find(class_="rank-lbl")
            if lbl and lbl.get_text(strip=True) == pair:
                rationale = row.get_text(" ", strip=True)
                break
        setups.append(MacroPrioritySetup(
            pair=pair, direction=direction, conviction_stars=stars, rationale=rationale
        ))
    return tuple(setups)


def _parse_regime(soup: BeautifulSoup) -> tuple[str, float | None]:
    text = soup.get_text(" ", strip=True)
    # Motif principal (inchangé) : "Régime « X »", fermé par un guillemet
    # français explicite — essayé en premier, comportement identique à avant
    # ce correctif pour tout document où il matche.
    regime_match = re.search(r"Régime\s*[:\u00a0]?\s*«\s*([A-Za-z /]+?)\s*»", text)
    if regime_match is None:
        # R-7 FIX (round de validation zero-régression, 02/08/2026) : l'audit
        # indépendant a re-dérivé que la regex d'origine n'atteignait QUE la
        # 3e occurrence du mot "Régime" dans le document réel (section
        # Volatilité ↔ Sentiment), et ratait silencieusement les deux formats
        # sans guillemet fermant observés ailleurs dans le même document :
        # "Régime du jour Mixed / Selective VIX : …" et
        # "Régime identifié Mixed / Selective Confiance : 8%". Une régression
        # de l'interpretation layer (build_interpretation, absent de ce
        # corpus) sur le chemin déjà couvert ferait alors disparaître le
        # régime du rapport terminal (cf. audit R-7).
        # Repli additif : essayé UNIQUEMENT si le motif principal ne matche
        # pas, donc zéro changement pour le cas déjà correct.
        regime_match = re.search(
            r"Régime\s+(?:du\s+jour|identifié)\s+([A-Za-z]+(?:\s*/\s*[A-Za-z]+)?)", text
        )
    regime = regime_match.group(1).strip() if regime_match else "non disponible dans les documents fournis"
    conf_match = re.search(r"[Cc]onfiance\D{0,10}(\d+)\s*%", text)
    confidence = float(conf_match.group(1)) if conf_match else None
    return regime, confidence


def _parse_extreme_count(soup: BeautifulSoup) -> int | None:
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d+)\s+devises?\s+en\s+positionnement\s+extr[êe]me", text)
    return int(m.group(1)) if m else None


def _parse_report_datetime(soup: BeautifulSoup) -> tuple[str, str]:
    subbar = soup.find(class_="page-subbar")
    if subbar is None:
        raise MacroDocumentError("Bandeau de date (page-subbar) introuvable.")
    text = subbar.get_text(" ", strip=True)
    date_match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text)
    tz_match = re.search(r"\d{1,2}:\d{2}\s*(CET|CEST|GMT[+-]\d|UTC)", text)
    dt = date_match.group(0) if date_match else "non disponible dans les documents fournis"
    tz = tz_match.group(1) if tz_match else "non disponible dans les documents fournis"
    time_match = re.search(r"(\d{1,2}:\d{2})", text)
    full_dt = f"{dt} {time_match.group(1)}" if time_match else dt
    return full_dt, tz


def parse_macro(html: str) -> MacroSnapshot:
    soup = BeautifulSoup(html, "html.parser")

    strength = _parse_currency_strength(soup)
    ips = _parse_ips(soup)

    all_codes = set(strength.keys()) | set(ips.keys())
    currencies: dict[str, CurrencyMacroData] = {}
    for code in all_codes:
        rank, score = strength.get(code, (None, None))
        ips_val, ips_date = ips.get(code, (None, "non disponible dans les documents fournis"))
        currencies[code] = CurrencyMacroData(
            code=code,
            strength_rank=rank,
            strength_score=score,
            ips=ips_val,
            ips_source="CFTC Non-Commercials" if ips_val is not None else None,
            ips_date=ips_date if ips_val is not None else None,
        )

    regime, confidence = _parse_regime(soup)
    report_dt, report_tz = _parse_report_datetime(soup)

    result = MacroSnapshot(
        report_datetime=report_dt,
        report_timezone=report_tz,
        regime=regime,
        regime_confidence_pct=confidence,
        currencies=currencies,
        priority_setups=_parse_priority_setups(soup),
        extreme_currency_count=_parse_extreme_count(soup),
    )
    logger.info(
        "macro_parsed datetime=%s regime=%s currencies=%d priority_setups=%d",
        result.report_datetime, result.regime, len(result.currencies), len(result.priority_setups),
    )
    return result

    
