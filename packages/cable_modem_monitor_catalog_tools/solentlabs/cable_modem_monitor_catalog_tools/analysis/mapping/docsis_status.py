"""Phase 6 - docsis_status canonical mapping.

``docsis_status`` has one canonical value, ``"Operational"``, and
downstream consumers depend on it. Vendors spell success many ways
(``Allowed``, ``Connected``, ``operational``, ``1``), so the parser
normalizes through a ``map`` entry.

Only success spellings are mapped. In-progress and error states
(``Ranging``, ``Scanning``, ``Access Denied``) must reach the status
sensor unchanged, so an unrecognized value gets no map rather than a
guess.

Per SYSTEM_INFO_SPEC.md Canonical values, including its Diagnostic
Pass-Through rule.
"""

from __future__ import annotations

from collections.abc import Collection

CANONICAL_OPERATIONAL = "Operational"


def detect_docsis_status_map(raw: object, success_values: Collection[str]) -> dict[str, str]:
    """Return the map normalizing this raw value, or empty to pass it through.

    ``success_values`` are lowercased spellings the fleet has proven mean
    operational. The emitted key keeps the observed casing, because Core
    applies the map to the raw stripped text.
    """
    text = str(raw).strip()
    if not text:
        return {}

    if text == CANONICAL_OPERATIONAL:
        # Already canonical on the wire; a map would be a no-op.
        return {}

    if text.lower() in success_values:
        return {text: CANONICAL_OPERATIONAL}

    return {}
