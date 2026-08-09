import sqlite3
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from config import load_settings
from database import Database
from llm.errors import (
    LLMConfigurationError,
    LLMError,
    LLMNoAvailableKeysError,
    MistralKeyDuplicateError,
    MistralKeyInputError,
)
from llm.mistral_keys import (
    MistralKeyCheckResult,
    MistralKeyCipher,
    MistralKeyManager,
    MistralKeyView,
)
from tests.test_config import VALID_ENV, write_profile


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def mistral_settings(tmp_path, master_key: str, legacy_key: str = ""):
    return load_settings(
        profile_path=write_profile(tmp_path),
        environ={
            **VALID_ENV,
            "LLM_PROVIDER": "mistral",
            "LLM_MODEL": "mistral-small-latest",
            "MISTRAL_KEYS_MASTER_KEY": master_key,
            "MISTRAL_API_KEY": legacy_key,
        },
    ).llm


def no_adapter(_api_key: str):
    raise AssertionError("legacy migration must not use the network")


def test_cipher_round_trip_never_stores_plaintext(tmp_path) -> None:
    raw = "mistral-super-secret-key"
    cipher = MistralKeyCipher(Fernet.generate_key().decode())
    protected = cipher.protect(raw)

    assert cipher.reveal(protected.encrypted_key) == raw
    assert raw not in repr(protected)
    assert raw not in protected.encrypted_key
    assert protected.suffix == "-key"


def test_wrong_master_key_fails_without_exposing_ciphertext() -> None:
    first = MistralKeyCipher(Fernet.generate_key().decode())
    second = MistralKeyCipher(Fernet.generate_key().decode())
    encrypted = first.protect("mistral-super-secret-key").encrypted_key

    with pytest.raises(LLMConfigurationError) as raised:
        second.reveal(encrypted)

    assert encrypted not in str(raised.value)
    assert "MISTRAL_KEYS_MASTER_KEY" in str(raised.value)


def test_manager_imports_legacy_key_once_without_plaintext_in_sqlite(tmp_path) -> None:
    raw = "mistral-legacy-super-secret"
    master_key = Fernet.generate_key().decode()
    settings = mistral_settings(tmp_path, master_key, raw)
    database = Database(tmp_path / "agent.db")
    database.init()

    MistralKeyManager(
        settings,
        database,
        adapter_factory=no_adapter,
        now_factory=lambda: NOW,
    )
    MistralKeyManager(
        settings,
        database,
        adapter_factory=no_adapter,
        now_factory=lambda: NOW,
    )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute("SELECT * FROM mistral_api_keys").fetchall()
    assert len(rows) == 1
    assert raw not in repr(rows)
    assert raw not in database.path.read_bytes().decode(errors="ignore")


def test_empty_legacy_key_closes_one_shot_migration(tmp_path) -> None:
    master_key = Fernet.generate_key().decode()
    database = Database(tmp_path / "agent.db")
    database.init()

    MistralKeyManager(
        mistral_settings(tmp_path, master_key),
        database,
        adapter_factory=no_adapter,
        now_factory=lambda: NOW,
    )
    MistralKeyManager(
        mistral_settings(tmp_path, master_key, "mistral-too-late-secret"),
        database,
        adapter_factory=no_adapter,
        now_factory=lambda: NOW,
    )

    assert database.mistral_keys(NOW) == []


def test_key_views_and_check_results_contain_no_secret_fields() -> None:
    view = MistralKeyView(1, "aB7x", "ready", None, NOW, "", True)
    result = MistralKeyCheckResult(view, True, "")

    assert result.key is view
    assert "encrypted_key" not in view.__dict__
    assert "key_hmac" not in view.__dict__


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (LLMNoAvailableKeysError, "no_available_keys"),
        (MistralKeyInputError, "invalid_key"),
        (MistralKeyDuplicateError, "duplicate_key"),
    ],
)
def test_key_errors_are_typed_and_safe(error_type, category: str) -> None:
    error = error_type()

    assert isinstance(error, LLMError)
    assert error.category == category
