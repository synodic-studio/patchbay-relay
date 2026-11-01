"""Model validation must use the authenticated catalog, not ID spelling."""

import pytest

from patchbay import models


def test_codex_accepts_only_models_returned_by_catalog(monkeypatch):
    monkeypatch.setattr(models.codex_models, "list_codex_models", lambda: [("gpt-5.6-sol", "GPT-5.6 Sol")])
    assert models.model_is_valid("gpt-5.6-sol", harness="codex")
    assert not models.model_is_valid("gpt-6.1-sol", harness="codex")
    assert not models.model_is_valid("small", harness="codex")


def test_catalog_unavailable_is_not_treated_as_valid(monkeypatch):
    def unavailable():
        raise models.codex_models.CodexModelsUnavailable("Try again")

    monkeypatch.setattr(models.codex_models, "list_codex_models", unavailable)
    with pytest.raises(models.codex_models.CodexModelsUnavailable):
        models.model_is_valid("gpt-6.1-sol", harness="codex")


def test_pi_validation_never_queries_codex_catalog(monkeypatch):
    def forbidden():
        pytest.fail("pi validation queried Codex")

    monkeypatch.setattr(models.codex_models, "list_codex_models", forbidden)
    assert models.model_is_valid("dsf")
    assert not models.model_is_valid("gpt-6.1-sol")
