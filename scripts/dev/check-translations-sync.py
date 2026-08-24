#!/usr/bin/env python3
"""Pre-commit hook to verify translation files match strings.json.

HA custom components require strings.json (source of truth) and
translations/*.json (runtime). If they drift apart, labels show as raw
key names in the UI.

Translated scope is deliberately limited to the config and options
flows — the surface a user meets during setup. Everything else
(services, and entity names if they are ever added) stays English and
relies on Home Assistant's per-key fallback. See
docs/TRANSLATION_GUIDE.md for why.

Checks:
- translations/en.json must be an exact copy of strings.json
- Other languages must cover every in-scope key, and must not carry
  keys outside the translated scope
- Other languages must not hold a value identical to English, which is
  the signature of a key added to satisfy this hook and never actually
  translated. Genuinely identical strings go in _ALLOW_IDENTICAL.

Exit codes:
- 0: All files in sync
- 1: Structural drift or untranslated values detected
- 2: Cannot check — strings.json or translations/ is missing

2 is deliberately not 0. Collapsing "couldn't check" into "passed"
makes an unverified run indistinguishable from a verified one, and a
gate that wrongly passes generates no signal to correct it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPONENT_DIR = _REPO_ROOT / "custom_components" / "cable_modem_monitor"
_STRINGS = _COMPONENT_DIR / "strings.json"
_TRANSLATIONS_DIR = _COMPONENT_DIR / "translations"

# Sections carried by every language file. strings.json and en.json hold
# more than this; other languages deliberately do not.
_TRANSLATED_SCOPE = ("config", "options")

# English values that may legitimately appear untranslated in any
# language: the product name, and loanwords that are spelled the same
# in some target languages. Mirrors the "What NOT to Translate" list in
# docs/TRANSLATION_GUIDE.md.
_ALLOW_IDENTICAL = frozenset(
    {
        "Cable Modem Monitor",
        "Password",
        "Variant",
    }
)

# Minimum non-ASCII characters per 1000, per language.
#
# Guards against the cd0376a1 regression, where regenerating the locale
# files silently de-accented French and Spanish — fr fell from 30.5 to
# 0.6 per 1000, es from 24.4 to 1.0. Stripped accents are not cosmetic:
# Italian "e corretto" means "and correct" rather than "is correct".
#
# Floors sit near half of measured density, so ordinary editing has wide
# headroom and only a collapse trips them. If a translation legitimately
# shifts density, update the floor deliberately.
#
# BLIND SPOTS: nl has no diacritics in this content at all, so it is
# absent here and cannot be protected by this check. it is listed but
# its floor is necessarily weak — its damage was found by reading, not
# by measuring. A low-diacritic language can still be degraded
# invisibly; only diff review catches that.
_MIN_DIACRITIC_DENSITY = {
    "de": 6.0,
    "es": 12.0,
    "fr": 15.0,
    "it": 2.0,
    "pl": 25.0,
    "pt-BR": 15.0,
    "ru": 400.0,
    "sv": 15.0,
    "uk": 400.0,
    "zh-CN": 350.0,
}


def _extract_keys(obj: Any, prefix: str = "") -> set[str]:
    """Recursively extract all key paths from a nested dict."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            keys.update(_extract_keys(v, path))
    return keys


def _extract_strings(obj: Any, prefix: str = "") -> dict[str, str]:
    """Recursively extract all leaf string values, keyed by dotted path."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, str):
                out[path] = v
            else:
                out.update(_extract_strings(v, path))
    return out


def _in_scope(path: str) -> bool:
    """True when a dotted key path belongs to the translated scope."""
    return path.split(".", maxsplit=1)[0] in _TRANSLATED_SCOPE


def _check_en_exact(strings: dict[str, Any], trans: dict[str, Any], name: str) -> str | None:
    """Check that en.json is an exact copy of strings.json."""
    if trans == strings:
        return None
    diffs = [s for s in strings if strings.get(s) != trans.get(s)]
    sections = ", ".join(diffs) if diffs else "unknown"
    return (
        f"  {name}: content differs from strings.json ({sections})\n"
        f"    Fix: cp {_STRINGS} {_TRANSLATIONS_DIR / name}"
    )


def _check_scope(strings_keys: set[str], trans: dict[str, Any], name: str) -> list[str]:
    """Check a language file covers the translated scope and nothing beyond it."""
    errors: list[str] = []
    expected = {k for k in strings_keys if _in_scope(k)}
    actual = _extract_keys(trans)

    missing = expected - actual
    if missing:
        top = sorted({".".join(k.split(".")[:2]) for k in missing if "." in k})
        errors.append(f"  {name}: missing {len(missing)} in-scope keys (e.g., {', '.join(top[:3])})")

    out_of_scope = {k for k in actual if not _in_scope(k)}
    if out_of_scope:
        top = sorted({k.split(".")[0] for k in out_of_scope})
        errors.append(
            f"  {name}: carries {len(out_of_scope)} keys outside the translated scope ({', '.join(top)})\n"
            f"    Fix: remove those sections — Home Assistant falls back to English per key"
        )
    return errors


def _check_translated(en_strings: dict[str, str], trans: dict[str, Any], name: str) -> list[str]:
    """Check no in-scope value was left identical to English."""
    trans_strings = _extract_strings(trans)
    untranslated = [
        path
        for path, value in trans_strings.items()
        if _in_scope(path) and en_strings.get(path) == value and value not in _ALLOW_IDENTICAL
    ]
    if not untranslated:
        return []
    shown = ", ".join(sorted(untranslated)[:3])
    return [
        f"  {name}: {len(untranslated)} value(s) identical to English (e.g., {shown})\n"
        f"    Fix: translate them, or add the string to _ALLOW_IDENTICAL if it is "
        f"genuinely the same in this language"
    ]


def _check_diacritics(trans: dict[str, Any], locale: str, name: str) -> list[str]:
    """Check a language has not been silently de-accented."""
    floor = _MIN_DIACRITIC_DENSITY.get(locale)
    if floor is None:
        return []
    text = "".join(v for k, v in _extract_strings(trans).items() if _in_scope(k))
    if not text:
        return []
    density = sum(1 for c in text if ord(c) > 127) * 1000 / len(text)
    if density >= floor:
        return []
    return [
        f"  {name}: diacritic density {density:.1f} per 1000 is below the {floor:.1f} floor\n"
        f"    This is the signature of translations being regenerated and de-accented.\n"
        f"    Fix: restore the accents, or update the floor if the drop is legitimate"
    ]


def main() -> int:
    """Compare strings.json against all translation files."""
    if not _STRINGS.is_file():
        print(f"ERROR: {_STRINGS} not found — this check verified nothing.", file=sys.stderr)
        return 2
    if not _TRANSLATIONS_DIR.is_dir():
        print(f"ERROR: {_TRANSLATIONS_DIR} not found — this check verified nothing.", file=sys.stderr)
        return 2

    strings = json.loads(_STRINGS.read_text(encoding="utf-8"))
    strings_keys = _extract_keys(strings)
    en_strings = _extract_strings(strings)
    errors: list[str] = []

    for trans_path in sorted(_TRANSLATIONS_DIR.glob("*.json")):
        trans = json.loads(trans_path.read_text(encoding="utf-8"))
        if trans_path.stem == "en":
            err = _check_en_exact(strings, trans, trans_path.name)
            if err:
                errors.append(err)
            continue
        errors.extend(_check_scope(strings_keys, trans, trans_path.name))
        errors.extend(_check_translated(en_strings, trans, trans_path.name))
        errors.extend(_check_diacritics(trans, trans_path.stem, trans_path.name))

    if errors:
        print("FAIL: translation files out of sync with strings.json:")
        for e in errors:
            print(e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
