import os

from cryptography.fernet import Fernet

import setup_wizard


def test_mistral_setup_generates_unprinted_master_key(monkeypatch, capsys) -> None:
    answers = iter(("mistral-api-key", "mistral-small-latest"))
    monkeypatch.setattr(setup_wizard, "ask", lambda *_args, **_kwargs: next(answers))

    result = setup_wizard._setup_mistral({})

    Fernet(result["MISTRAL_KEYS_MASTER_KEY"].encode())
    assert result["MISTRAL_KEYS_MASTER_KEY"] not in capsys.readouterr().out


def test_build_env_preserves_mistral_master_key() -> None:
    master_key = Fernet.generate_key().decode()

    rendered = setup_wizard.build_env(
        {},
        {
            "LLM_PROVIDER": "mistral",
            "MISTRAL_API_KEY": "legacy-key",
            "MISTRAL_KEYS_MASTER_KEY": master_key,
        },
        {},
    )

    assert f"MISTRAL_KEYS_MASTER_KEY={master_key}" in rendered


def test_wizard_restricts_env_permissions(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    profile_path = tmp_path / "profile.yaml"
    monkeypatch.setattr(setup_wizard, "ENV_PATH", env_path)
    monkeypatch.setattr(setup_wizard, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(setup_wizard, "section", lambda _title: None)
    monkeypatch.setattr(setup_wizard, "ok", lambda _message: None)

    setup_wizard.write_files("MISTRAL_KEYS_MASTER_KEY=secret\n", "candidate: {}\n")

    if os.name != "nt":
        assert env_path.stat().st_mode & 0o777 == 0o600
