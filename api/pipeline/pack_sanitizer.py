"""T8 — Sanitisation PII sur les champs libres avant écriture dans le pack partagé.

Module pur (aucune I/O). Appliqué par `expand_pack` sur :
- focus_symptoms[] en entrée (texte tenant brut)
- tous les champs libres des facts produits par le Scout (description,
  notes, properties.values(), symptoms[], etc.)

Politique V1 = regex + listes. L'interface est designed pour permettre une
impl LLM redactor en V2 (cf. spec §5b "Interface pour V2"). Le sanitizer
LOGUE chaque action pour audit (provenance.sanitizer_actions[] + journal).

NE PAS confondre avec api/agent/sanitize.py (refdes hallucination guard
sur les réponses agent — c'est un autre vecteur).

Limitations connues V1 (best-effort regex+listes ; à durcir en V2 si besoin) :
- IPv4 capture aussi des chaînes type firmware version (10.0.1.2) — accepté
  comme over-redact (safe).
- IPv6 : ::1 (loopback seul) et fe80::1%eth0 (scope) non couverts.
- Mention client : nécessite ponctuation finale de phrase (.!?). 'le client
  Dupont reports X' sans point ne match pas.
- Honorifique : 'le client M. Dupont dit X.' ne match pas car le '.' de
  'M.' coupe la frontière de phrase regex.
- Phone : forme '(0)6 12 34 56 78' et '+33 (0)6 12 34 56 78' non couvertes.
- TI chips SN74xxx ne sont pas pris pour des serials (fixé après revue).
- IBAN : seuil corps ≥ 13 chars (total ≥ 17) → BE (16 total) et NO (15 total)
  non couverts ; compromis accepté pour éviter les faux positifs SN74xxx.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# On évite la dépendance Pydantic ici pour garder ce module utilisable hors
# pipeline (par les tests, et plus tard par un LLM redactor offline).
# Task 5 convertira les SanitizerAction (dataclass) → SanitizerAction (Pydantic,
# défini dans schemas.py) au moment de construire l'objet Provenance.


@dataclass
class SanitizerAction:
    """Trace d'une opération de redaction. Sera convertie en Provenance.SanitizerAction
    par expand_pack avant écriture du fact."""

    field: str
    action: str  # parmi: redacted_email, redacted_phone, redacted_serial,
                 #         redacted_iban, redacted_ip, redacted_customer_mention,
                 #         dropped_invalid_identifier
    count: int = 1


# --- Regex compilées une fois au chargement du module (perf + thread-safety) ---

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Téléphone FR/international : +33 ou 0 suivi de 9 chiffres groupés ; séparateurs
# espace, tiret, point. On exige une frontière NOT alphanum pour éviter de
# matcher au milieu d'un refdes comme U1300 (lookbehind + lookahead).
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+33|0)[\s\-.]?\d(?:[\s\-.]?\d){8}(?![A-Za-z0-9])"
)

# IMEI strict = exactement 15 chiffres non entourés d'autres chiffres.
_IMEI_RE = re.compile(r"(?<!\d)\d{15}(?!\d)")

# Alphanumérique 10-18 chars SI précédé d'un mot-clé serial/sn/imei/s/n
# AVEC séparateur explicite entre le mot-clé et l'alphanumérique.
# Évite le trop-large : un refdes long sans mot-clé ne sera pas redacté.
# Le lookahead (?=[\s:=/]) exige au moins un séparateur avant consommation,
# ce qui exclut les chip-prefixes TI du type SN74HC595PWR (le chiffre '7'
# suit immédiatement 'SN' sans espace, ':', '=' ou '/').
_KEYWORDED_SERIAL_RE = re.compile(
    r"\b(?:s/?n|imei|serial|s\.?n\.?)(?=[\s:=/])[\s:=/]+([A-Z0-9]{10,18})\b",
    re.IGNORECASE,
)

# IBAN générique : 2 lettres pays + 2 chiffres de contrôle + 13-30 chars alphanum.
# Corps ≥ 13 (total ≥ 17) : couvre FR (27), DE (22), GB (22), etc.
# Seuil relevé de 11 à 13 pour éviter les faux positifs sur les chip-prefixes TI
# type SN74AHCT1G08DCKR (corps = 12). Compromis : BE (16 total, corps 12) et
# NO (15 total, corps 11) ne sont pas capturés — tradeoff acceptable en V1.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{13,30}\b")

# IPv4 : 4 octets séparés par des points, avec frontière mot.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# IPv6 : au moins 2 groupes hex séparés par ':', suivi de groupes optionnels
# (couvre les formes compressées avec '::' grâce à [0-9a-fA-F]{0,4}).
_IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4})*\b"
)

# Mention client : mot-clé (client/customer/boutique/magasin/propriétaire)
# suivi d'un nom propre (capitale + min. 2 lettres minuscules françaises)
# dans la même phrase (délimitée par . ! ?).
# Le pattern n'exige pas que le nom propre soit DIRECTEMENT après le mot-clé
# (il peut y avoir des mots intermédiaires), mais tout doit rester dans la
# même phrase (pas de traversée de ponctuation finale).
# NOTE : le test négatif ("the client reports no charge") n'a pas de ponctuation
# de fin → ne matche pas → correct.
_CUSTOMER_RE = re.compile(
    r"\b(?:client|customer|boutique|magasin|propri[ée]taire)e?s?\b"
    r"[^.!?]*?\b[A-ZÉÈÊÀÂÔÎÛÇ][a-zéèêàâôîûçñ]{2,}\b[^.!?]*?[.!?]",
    re.IGNORECASE,
)

# Filet de sécurité longueur : au-delà de 500 chars dans un seul champ libre,
# on tronque avec un marqueur visible. Le Scout est borné en taille en amont,
# mais un input tenant pathologique pourrait passer ce seuil.
_MAX_FREE_TEXT_LEN = 500
_TRUNCATION_MARKER = "[…truncated]"


class PackSanitizer:
    """Sanitizer PII V1 basé sur regex + listes.

    Stateless et thread-safe : les regex sont compilées au niveau module,
    l'instance ne porte aucun état mutable.

    Étendu vers un LLM redactor en V2 sans toucher le pipeline (même
    interface sanitize_text / sanitize_many → mêmes SanitizerAction).
    """

    def sanitize_text(self, text: str | None, *, field_name: str) -> tuple[str | None, list[SanitizerAction]]:
        """Sanitize une chaîne. Retourne (texte_sanitisé, liste d'actions).

        Si text est None, retourne (None, []) sans crasher — utile pour les
        schémas où les champs descriptifs sont Optional.

        Les actions sont ordonnées dans l'ordre d'application des patterns.
        Chaque action porte le field_name pour traçabilité dans la Provenance.
        """
        if text is None:
            return text, []

        actions: list[SanitizerAction] = []
        out = text

        # 1. Email
        out, n = _EMAIL_RE.subn("[REDACTED:email]", out)
        if n:
            actions.append(SanitizerAction(field=field_name, action="redacted_email", count=n))

        # 2. Téléphone FR/international
        out, n = _PHONE_RE.subn("[REDACTED:phone]", out)
        if n:
            actions.append(SanitizerAction(field=field_name, action="redacted_phone", count=n))

        # 3. IMEI (15 chiffres stricts) + serial alphanum avec mot-clé.
        #    On applique les deux pour couvrir "IMEI 350123..." (le keyword match
        #    + le 15-digits match peuvent se chevaucher — le 15-digits passe en
        #    premier sur les chiffres bruts, le keyword match remplace le token
        #    alphanum dans les cas mixtes type "S/N: F2LMQ1ABXY7G").
        out, n1 = _IMEI_RE.subn("[REDACTED:serial]", out)
        out, n2 = _KEYWORDED_SERIAL_RE.subn(
            # On remplace seulement le groupe 1 (le numéro lui-même) pour
            # conserver le mot-clé lisible (ex: "S/N: [REDACTED:serial]").
            lambda m: m.group(0).replace(m.group(1), "[REDACTED:serial]"),
            out,
        )
        if n1 + n2:
            actions.append(
                SanitizerAction(field=field_name, action="redacted_serial", count=n1 + n2)
            )

        # 4. IBAN — appliqué APRÈS serial pour ne pas interférer avec
        #    la séquence alphanum du numéro de série.
        out, n = _IBAN_RE.subn("[REDACTED:iban]", out)
        if n:
            actions.append(SanitizerAction(field=field_name, action="redacted_iban", count=n))

        # 5. IP (v4 puis v6)
        out, n1 = _IPV4_RE.subn("[REDACTED:ip]", out)
        out, n2 = _IPV6_RE.subn("[REDACTED:ip]", out)
        if n1 + n2:
            actions.append(
                SanitizerAction(field=field_name, action="redacted_ip", count=n1 + n2)
            )

        # 6. Mention client avec nom propre
        out, n = _CUSTOMER_RE.subn("[REDACTED:customer_mention]", out)
        if n:
            actions.append(
                SanitizerAction(field=field_name, action="redacted_customer_mention", count=n)
            )

        # 7. Filet de sécurité longueur — après redaction (un texte long de
        #    marqueurs "[REDACTED:...]" reste long mais on coupe le payload utile).
        if len(out) > _MAX_FREE_TEXT_LEN:
            out = out[:_MAX_FREE_TEXT_LEN] + _TRUNCATION_MARKER

        return out, actions

    def sanitize_many(
        self, texts: Iterable[str], *, field_name: str
    ) -> tuple[list[str], list[SanitizerAction]]:
        """Sanitize une liste de chaînes (ex: focus_symptoms[]).

        Le field_name de chaque élément est dérivé automatiquement :
        field_name[0], field_name[1], etc. — pour la traçabilité Provenance.
        Les actions sont agrégées dans l'ordre de traitement.
        """
        outs: list[str] = []
        all_actions: list[SanitizerAction] = []
        for i, t in enumerate(texts):
            s, acts = self.sanitize_text(t, field_name=f"{field_name}[{i}]")
            outs.append(s)
            all_actions.extend(acts)
        return outs, all_actions
