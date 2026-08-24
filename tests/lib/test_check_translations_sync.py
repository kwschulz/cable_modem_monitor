"""Tests for scripts/dev/check-translations-sync.py.

strings.json is the source of truth for a HA custom component and
translations/*.json is what the UI renders. Drift shows users raw key
names. Three distinct failures are gated: structural drift, values left
identical to English, and silent de-accenting.

That last one is why this script matters most. In cd0376a1, regenerating
the locale files stripped accents wholesale — French diacritic density
fell from 30.5 per 1000 to 0.6, Spanish from 24.4 to 1.0 — and nothing
caught it. It is not cosmetic: Italian "e corretto" means "and correct"
rather than "is correct". The density floors exist to catch exactly that
collapse, and until now nothing proved they still fire.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- The five pure checkers — table-driven and behavioural on small dicts.
- ``main`` — behavioural, with the module's path constants pointed at a
  tmp_path tree.
- Guard cases (_ALLOW_IDENTICAL, out-of-scope keys, unlisted locales)
  each carry a live counterpart per § Gate Scripts Require Tests.
"""

# cspell:ignore Vérifiez Verifiez réessayez reessayez adresse tard Configurer
# cspell:ignore Saisissez utiliser Redémarrer immédiatement ééééééééé

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixture_helpers import load_script

_mod = load_script("scripts/dev/check-translations-sync.py")


# ---------------------------------------------------------------------------
# Key and string extraction
# ---------------------------------------------------------------------------


def test_extract_keys_returns_every_path_including_intermediates() -> None:
    data = {"config": {"step": {"user": {"title": "T"}}}}
    assert _mod._extract_keys(data) == {
        "config",
        "config.step",
        "config.step.user",
        "config.step.user.title",
    }


def test_extract_strings_returns_only_leaf_values() -> None:
    data = {"config": {"step": {"title": "Set up", "count": 3}}}
    assert _mod._extract_strings(data) == {"config.step.title": "Set up"}


# fmt: off
SCOPE = [
    pytest.param("config.step.user.title",  True,  id="config-in-scope"),
    pytest.param("options.step.init.data",  True,  id="options-in-scope"),
    pytest.param("services.restart.name",   False, id="services-out-of-scope"),
    pytest.param("entity.sensor.name",      False, id="entity-out-of-scope"),
]
# fmt: on


@pytest.mark.parametrize(("path", "expected"), SCOPE)
def test_in_scope(path: str, expected: bool) -> None:
    """Only config and options are translated; the rest falls back to English."""
    assert _mod._in_scope(path) is expected


# ---------------------------------------------------------------------------
# en.json must mirror strings.json exactly
# ---------------------------------------------------------------------------


def test_identical_en_passes() -> None:
    data = {"config": {"title": "Set up"}}
    assert _mod._check_en_exact(data, dict(data), "en.json") is None


def test_diverged_en_is_reported() -> None:
    """Live counterpart: a single changed value is caught."""
    strings = {"config": {"title": "Set up"}}
    problem = _mod._check_en_exact(strings, {"config": {"title": "Set  up"}}, "en.json")
    assert problem is not None
    assert "differs from strings.json" in problem


# ---------------------------------------------------------------------------
# Scope coverage
# ---------------------------------------------------------------------------


def test_complete_language_file_passes() -> None:
    keys = _mod._extract_keys({"config": {"step": {"title": "T"}}, "services": {"restart": {"name": "R"}}})
    assert _mod._check_scope(keys, {"config": {"step": {"title": "Titre"}}}, "fr.json") == []


def test_missing_in_scope_keys_are_reported() -> None:
    keys = _mod._extract_keys({"config": {"step": {"title": "T", "description": "D"}}})
    errors = _mod._check_scope(keys, {"config": {"step": {"title": "Titre"}}}, "fr.json")
    assert any("missing" in e for e in errors)


def test_out_of_scope_keys_are_reported() -> None:
    """A language file carrying services/ defeats HA's per-key fallback."""
    keys = _mod._extract_keys({"config": {"step": {"title": "T"}}})
    trans = {"config": {"step": {"title": "Titre"}}, "services": {"restart": {"name": "Redémarrer"}}}
    errors = _mod._check_scope(keys, trans, "fr.json")
    assert any("outside the translated scope" in e for e in errors)


# ---------------------------------------------------------------------------
# Untranslated values
# ---------------------------------------------------------------------------


def test_value_identical_to_english_is_reported() -> None:
    """The signature of a key added to satisfy the hook, never translated."""
    en = {"config.step.title": "Set up"}
    errors = _mod._check_translated(en, {"config": {"step": {"title": "Set up"}}}, "fr.json")
    assert errors and "identical to English" in errors[0]


def test_allowlisted_identical_value_is_not_reported() -> None:
    """Loanwords and the product name are legitimately the same."""
    en = {"config.step.title": "Password"}
    assert _mod._check_translated(en, {"config": {"step": {"title": "Password"}}}, "fr.json") == []


def test_allowlist_does_not_swallow_other_identical_values() -> None:
    """Live counterpart: only the listed strings are exempt."""
    en = {"config.step.title": "Password", "config.step.description": "Enter it"}
    trans = {"config": {"step": {"title": "Password", "description": "Enter it"}}}
    errors = _mod._check_translated(en, trans, "fr.json")
    assert errors and "config.step.description" in errors[0]


def test_out_of_scope_identical_values_are_ignored() -> None:
    """Services stay English by design, so matching English is correct there."""
    en = {"services.restart.name": "Restart"}
    assert _mod._check_translated(en, {"services": {"restart": {"name": "Restart"}}}, "fr.json") == []


# ---------------------------------------------------------------------------
# Diacritic density — the cd0376a1 regression
# ---------------------------------------------------------------------------


_WITH_ACCENTS = {"config": {"step": {"title": "Vérifiez l'adresse et réessayez plus tard"}}}
_WITHOUT_ACCENTS = {"config": {"step": {"title": "Verifiez l'adresse et reessayez plus tard"}}}


def test_accented_language_passes() -> None:
    assert _mod._check_diacritics(_WITH_ACCENTS, "fr", "fr.json") == []


def test_de_accented_language_is_reported() -> None:
    """Live counterpart, and the exact regression this floor exists for."""
    errors = _mod._check_diacritics(_WITHOUT_ACCENTS, "fr", "fr.json")
    assert errors and "diacritic density" in errors[0]


def test_locale_without_a_floor_is_skipped() -> None:
    """Dutch has no diacritics in this content — a documented blind spot."""
    assert _mod._check_diacritics(_WITHOUT_ACCENTS, "nl", "nl.json") == []


def test_empty_in_scope_text_is_not_a_density_failure() -> None:
    """No in-scope strings means no denominator, not a zero-density fail."""
    assert _mod._check_diacritics({"services": {"a": {"name": "x"}}}, "fr", "fr.json") == []


def test_out_of_scope_text_does_not_prop_up_density() -> None:
    """Accents outside the translated scope must not mask a de-accented body.

    Counting them would let a stripped config/ section hide behind an
    accented services/ section.
    """
    mixed = {
        "config": {"step": {"title": "Verifiez l'adresse et reessayez plus tard"}},
        "services": {"restart": {"name": "Redémarrer immédiatement ééééééééé"}},
    }
    assert _mod._check_diacritics(mixed, "fr", "fr.json") != []


# ---------------------------------------------------------------------------
# main — the gate itself
# ---------------------------------------------------------------------------


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the module's path constants at a throwaway component tree."""

    def _build(strings: dict, languages: dict[str, dict]) -> int:
        strings_path = tmp_path / "strings.json"
        strings_path.write_text(json.dumps(strings), encoding="utf-8")
        trans_dir = tmp_path / "translations"
        trans_dir.mkdir()
        for locale, body in languages.items():
            (trans_dir / f"{locale}.json").write_text(json.dumps(body), encoding="utf-8")
        monkeypatch.setattr(_mod, "_STRINGS", strings_path)
        monkeypatch.setattr(_mod, "_TRANSLATIONS_DIR", trans_dir)
        return _mod.main()

    return _build


_STRINGS_FIXTURE = {
    "config": {"step": {"user": {"title": "Set up", "description": "Enter the address"}}},
    "services": {"restart": {"name": "Restart"}},
}
_FR_GOOD = {"config": {"step": {"user": {"title": "Configurer", "description": "Saisissez l'adresse à utiliser"}}}}


def test_gate_passes_on_a_consistent_tree(gate) -> None:
    assert gate(_STRINGS_FIXTURE, {"en": _STRINGS_FIXTURE, "fr": _FR_GOOD}) == 0


def test_gate_fails_when_en_diverges(gate, capsys: pytest.CaptureFixture[str]) -> None:
    diverged = {"config": {"step": {"user": {"title": "Set up!", "description": "Enter the address"}}}}
    assert gate(_STRINGS_FIXTURE, {"en": diverged, "fr": _FR_GOOD}) == 1
    assert "out of sync" in capsys.readouterr().out


def test_gate_fails_on_a_de_accented_language(gate, capsys: pytest.CaptureFixture[str]) -> None:
    stripped = {"config": {"step": {"user": {"title": "Configurer", "description": "Saisissez l'adresse a utiliser"}}}}
    assert gate(_STRINGS_FIXTURE, {"en": _STRINGS_FIXTURE, "fr": stripped}) == 1
    assert "diacritic density" in capsys.readouterr().out


def test_absent_strings_file_skips_the_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Documented, not endorsed: a missing strings.json returns 0.

    ``_COMPONENT_DIR`` is a *relative* path, so this is also what happens
    when the hook is invoked from anywhere but the repo root — the exit
    code is indistinguishable from a real pass. pre-commit always runs
    from the repo root, which is what keeps it from mattering today.
    """
    monkeypatch.setattr(_mod, "_STRINGS", tmp_path / "absent.json")
    assert _mod.main() == 0


# ---------------------------------------------------------------------------
# Real-repo regression
# ---------------------------------------------------------------------------


def test_committed_translations_are_in_sync() -> None:
    """What the pre-commit hook runs, against the real component."""
    assert _mod.main() == 0
