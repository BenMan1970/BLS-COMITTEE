"""Hiérarchie d'exceptions Bluestar.

Chaque erreur métier est typée pour permettre à l'appelant (CLI, futur service,
tests) de distinguer un problème de données d'un problème de logique interne,
et de choisir un code de sortie / une réponse appropriés — jamais une exception
générique attrapée à l'aveugle.
"""


class BluestarError(Exception):
    """Racine commune à toutes les erreurs du système. Ne jamais lever
    directement — toujours une sous-classe plus précise."""


class DocumentParseError(BluestarError):
    """Un document source (macro ou desk) n'a pas pu être extrait correctement :
    structure DOM inattendue, ancrage sémantique introuvable, champ obligatoire
    manquant. Indique un changement de format en amont, pas une donnée absente
    légitime (qui doit être représentée par None dans le modèle, pas par une
    exception)."""


class MacroDocumentError(DocumentParseError):
    """Erreur spécifique au parsing du briefing macro."""


class DeskDocumentError(DocumentParseError):
    """Erreur spécifique au parsing du rapport desk technique."""


class RenderError(BluestarError):
    """La génération du rapport HTML de sortie a échoué."""


class ConfigurationError(BluestarError):
    """Un seuil ou un paramètre de configuration est invalide (ex. seuils
    incohérents entre eux, valeur hors domaine)."""
