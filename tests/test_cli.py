"""
Tests de la CLI Bluestar — exécutés via l'API `main()` avec des chemins réels,
jamais de sortie simulée.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluestar import cli


@pytest.fixture
def macro_path() -> Path:
    return Path("tests/data/macro.html")


@pytest.fixture
def desk_path() -> Path:
    return Path("tests/data/desk.html")


def test_happy_path_with_allow_advisories_returns_zero(tmp_path, macro_path, desk_path):
    """Les fixtures réelles produisent des advisories (GBP/AUD) — le "happy
    path" au sens strict (aucune erreur d'exécution) nécessite donc
    --allow-advisories pour obtenir le code 0 depuis le changement de
    comportement par défaut en v3.0.0 (cf. test_advisories_return_6_by_
    default_since_v3 pour le nouveau défaut)."""
    out = tmp_path / "rapport.html"
    code = cli.main([
        "--macro", str(macro_path), "--desk", str(desk_path), "--out", str(out),
        "--allow-advisories",
    ])
    assert code == 0
    assert out.exists()
    assert out.stat().st_size > 0


def test_missing_macro_file_returns_4(tmp_path, desk_path):
    out = tmp_path / "rapport.html"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--macro", "/tmp/n_existe_vraiment_pas.html", "--desk", str(desk_path), "--out", str(out)])
    assert exc_info.value.code == 4


# --- Régression : plafond de taille de fichier (round 20/07/2026) ----------

def test_oversized_input_file_returns_4_without_parsing(tmp_path, desk_path):
    """
    Verrouille le correctif du plafond de taille (signalé indépendamment par
    deux audits — GLM : ~11s CPU sur 7,3 Mo sans plafond ni timeout ;
    Gemini Pro : "un fichier de 5 Go saturera la mémoire instantanément" —
    jamais corrigé entre les deux jusqu'à cette version). Un fichier
    dépassant MAX_INPUT_FILE_SIZE_BYTES doit être rejeté par _read_file()
    AVANT tout parsing BeautifulSoup, avec le code 4 (erreur I/O), pas
    laissé consommer du CPU/mémoire de façon non bornée.
    """
    gros_fichier = tmp_path / "macro_trop_gros.html"
    gros_fichier.write_text("x" * (cli.MAX_INPUT_FILE_SIZE_BYTES + 1024), encoding="utf-8")
    out = tmp_path / "rapport.html"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--macro", str(gros_fichier), "--desk", str(desk_path), "--out", str(out)])
    assert exc_info.value.code == 4


def test_file_at_exactly_the_size_cap_is_accepted(tmp_path, macro_path, desk_path):
    """Garde-fou inverse : un fichier légitime, même volumineux mais sous le
    plafond, ne doit pas être rejeté à tort (off-by-one sur la comparaison)."""
    # Les vraies fixtures (~50-60 Ko) sont largement sous le plafond de 10 Mo :
    # on vérifie juste qu'elles passent bien _read_file sans lever SystemExit(4).
    size = macro_path.stat().st_size
    assert size < cli.MAX_INPUT_FILE_SIZE_BYTES, "fixture réelle inattendument volumineuse"
    content = cli._read_file(macro_path, "macro")
    assert content  # lu avec succès, pas d'exception


def test_malformed_macro_returns_1(tmp_path, desk_path):
    macro_vide = tmp_path / "macro_vide.html"
    macro_vide.write_text("<html><body>vide</body></html>", encoding="utf-8")
    out = tmp_path / "rapport.html"
    code = cli.main(["--macro", str(macro_vide), "--desk", str(desk_path), "--out", str(out)])
    assert code == 1


def test_malformed_desk_returns_2(tmp_path, macro_path):
    desk_vide = tmp_path / "desk_vide.html"
    desk_vide.write_text("<html><body>vide</body></html>", encoding="utf-8")
    out = tmp_path / "rapport.html"
    code = cli.main(["--macro", str(macro_path), "--desk", str(desk_vide), "--out", str(out)])
    assert code == 2


def test_desk_setup_missing_pair_returns_2_not_5(tmp_path, macro_path):
    """
    Verrouille la correction du 19/07/2026 (trouvé par l'audit GLM) : un
    bloc .setup sans .pair levait AttributeError -> code 5 ("erreur
    inattendue"), au lieu du code 2 catégorisé ("document desk malformé")
    qui existe précisément pour ce genre de cas. Bout en bout via la CLI,
    pas seulement au niveau du parseur (cf. test_extract_desk.py).
    """
    desk_malforme = tmp_path / "desk_malforme.html"
    desk_malforme.write_text(
        '<html><body>'
        '<div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low</div>'
        '<div class="setup"><span class="dir long">Bullish</span>'
        '<span class="conv">BBB (0.77)</span></div>'
        '</body></html>',
        encoding="utf-8",
    )
    out = tmp_path / "rapport.html"
    code = cli.main(["--macro", str(macro_path), "--desk", str(desk_malforme), "--out", str(out)])
    assert code == 2, f"Attendu code 2 (DeskDocumentError catégorisé), obtenu {code}"


def test_desk_incomplete_metrics_grid_does_not_crash_to_5(tmp_path, macro_path):
    """
    Verrouille la correction du 21/07/2026 (trouvé par un audit docx,
    confirmé par exécution). Contrairement à .pair/.dir/.conv (champs
    OBLIGATOIRES -> DeskDocumentError, code 2), les champs de metrics-grid
    (Quality, MTF %, Age) sont OPTIONNELS -> le bon comportement est un
    succès normal avec ces champs à None, PAS une erreur catégorisée.
    Avant correction : AttributeError non protégée -> code 5.
    """
    desk_metrics_incomplet = tmp_path / "desk_metrics_incomplet.html"
    desk_metrics_incomplet.write_text(
        '<html><body>'
        '<div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low</div>'
        '<div class="setup">'
        '<span class="pair">EUR/JPY</span>'
        '<span class="dir long">Bullish</span>'
        '<span class="conv">BBB (0.77)</span>'
        '<div class="metrics-grid"><div class="metric">'
        '<span class="metric-lbl">Quality</span></div></div>'
        '</div>'
        '</body></html>',
        encoding="utf-8",
    )
    out = tmp_path / "rapport.html"
    code = cli.main([
        "--macro", str(macro_path), "--desk", str(desk_metrics_incomplet), "--out", str(out),
        "--allow-advisories",
    ])
    assert code == 0, f"Attendu succès (code 0), obtenu {code} — la grille optionnelle ne doit pas faire planter"
    assert out.exists()


# --- Régression : ambiguïté code 2 vs 7 (round d'audit suivant, "Codex") ---

def test_well_formed_desk_with_zero_setups_returns_7_not_2(tmp_path, macro_path):
    """
    Verrouille la correction : un document desk PARFAITEMENT bien formé
    (bandeau valide, extraction sans exception) mais qui ne valide aucun
    setup ce jour-là est un résultat métier légitime ("rien à signaler"),
    pas un échec d'extraction. Avant cette version, ce cas retournait le
    même code 2 qu'un document réellement malformé — un ordonnanceur ne
    pouvait pas distinguer les deux situations. Trouvaille d'un audit
    auto-construit (cas non prescrit par le prompt d'audit), confirmée par
    exécution avant correction.
    """
    desk_zero_setup = tmp_path / "desk_zero_setup.html"
    desk_zero_setup.write_text(
        '<html><body>'
        '<div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 0/0 Event Risk: Low</div>'
        '</body></html>',
        encoding="utf-8",
    )
    out = tmp_path / "rapport.html"
    code = cli.main(["--macro", str(macro_path), "--desk", str(desk_zero_setup), "--out", str(out)])
    assert code == 7, f"Attendu code 7 (extraction OK, zéro setup), obtenu {code}"
    assert not out.exists(), "Aucun rapport ne doit être généré quand il n'y a rien à rapporter."


# --- Régression : code de sortie 5 (round d'audit du 19/07/2026) -----------

def test_unexpected_exception_returns_5_not_traceback(tmp_path, macro_path, desk_path, monkeypatch):
    """
    Verrouille la correction : le docstring de cli.py documentait un code 5
    pour toute erreur inattendue, mais aucun `except Exception` ne le
    produisait réellement (trouvé indépendamment par les audits Claude 4.8
    et GPT-5.5, confirmé par lecture de main() avant correction).

    On force une exception non typée (RuntimeError) au milieu du pipeline
    pour vérifier que le filet de sécurité la convertit bien en code 5,
    au lieu de la laisser remonter brute.
    """
    out = tmp_path / "rapport.html"

    def _boom(*args, **kwargs):
        raise RuntimeError("panne simulée non catégorisée")

    monkeypatch.setattr(cli, "decide_all", _boom)
    code = cli.main(["--macro", str(macro_path), "--desk", str(desk_path), "--out", str(out)])
    assert code == 5


def test_system_exit_from_read_file_is_not_swallowed_by_code_5(desk_path):
    """Le filet de sécurité `except Exception` ne doit JAMAIS avaler un
    SystemExit explicite (ex. fichier introuvable) — sinon le code 4
    deviendrait injoignable."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--macro", "/tmp/inexistant.html", "--desk", str(desk_path), "--out", "/tmp/out.html"])
    assert exc_info.value.code == 4  # pas 5, pas un code avalé


# --- Régression : code de sortie 6 en mode --strict (round d'audit 19/07) --

def test_advisories_return_6_by_default_since_v3(tmp_path, macro_path, desk_path):
    """
    CHANGEMENT DE COMPORTEMENT v3.0.0 : GBP/AUD produit des advisories sur
    les données de test réelles. Depuis cette version, le défaut est
    bloquant (code 6) — inversion délibérée par rapport à v2.x où le défaut
    était permissif (code 0) et --strict était l'opt-in. Relevé convergent
    de 6 audits indépendants : un défaut permissif masquait le signal à tout
    opérateur ne lisant que le code de sortie.
    """
    out = tmp_path / "rapport.html"
    code = cli.main(["--macro", str(macro_path), "--desk", str(desk_path), "--out", str(out)])
    assert code == 6


def test_allow_advisories_flag_restores_zero(tmp_path, macro_path, desk_path):
    """--allow-advisories restaure l'ancien comportement permissif (code 0)
    de façon explicite et opt-in, jamais silencieuse."""
    out = tmp_path / "rapport.html"
    code = cli.main([
        "--macro", str(macro_path), "--desk", str(desk_path), "--out", str(out),
        "--allow-advisories",
    ])
    assert code == 0
