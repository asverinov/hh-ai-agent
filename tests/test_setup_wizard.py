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


def test_build_env_includes_circuit_breaker_and_limits() -> None:
    rendered = setup_wizard.build_env(
        {"TG_BOT_TOKEN": "token", "TG_USER_ID": "123"},
        {"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3"},
        {"APP_MODE": "dry_run", "ENABLE_REAL_APPLY": "false"},
        {
            "CIRCUIT_BREAKER_PAGE_ERRORS": "4",
            "CIRCUIT_BREAKER_MIN_SAMPLE": "10",
            "CIRCUIT_BREAKER_UNKNOWN_RATIO": "0.7",
        },
    )

    assert "CIRCUIT_BREAKER_PAGE_ERRORS=4" in rendered
    assert "CIRCUIT_BREAKER_MIN_SAMPLE=10" in rendered
    assert "CIRCUIT_BREAKER_UNKNOWN_RATIO=0.7" in rendered
    assert "TG_BOT_TOKEN=token" in rendered
    assert "TG_USER_ID=123" in rendered


def test_build_profile_generates_valid_yaml(tmp_path) -> None:
    import yaml
    from config import load_settings
    from tests.test_config import VALID_ENV

    required = {
        "name": "Иван Иванов",
        "experience_summary": "Разработчик Python с 3 годами опыта.",
        "desired_positions": ["Python Developer", "Backend Developer"],
        "resume_name": "Python Developer",
        "search_queries": ["Python", "Django"],
    }
    optional = {
        "location": "Москва",
        "technologies": ["Python", "PostgreSQL", "Docker"],
        "salary_expectation": "от 150 000 руб.",
        "work_format": ["remote"],
        "excluded_positions": ["intern", "junior"],
        "areas": ["1"],
        "experience_filters": ["between1And3"],
        "cover_language": "ru",
        "cover_style": "professional",
    }

    content = setup_wizard.build_profile(required, optional)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(content, encoding="utf-8")

    parsed = yaml.safe_load(content)
    assert parsed["candidate"]["name"] == "Иван Иванов"
    assert parsed["candidate"]["desired_positions"] == ["Python Developer", "Backend Developer"]

    settings = load_settings(profile_path=profile_path, environ=VALID_ENV)
    assert settings.profile.candidate.name == "Иван Иванов"
    assert settings.profile.hh.search_queries == ("Python", "Django")


def test_step_app_mode_auto(monkeypatch) -> None:
    monkeypatch.setattr(setup_wizard, "ask_choice", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(setup_wizard, "ask_yes_no", lambda *_args, **_kwargs: True)
    answers = iter(("0.90", "4", "6", "3", "5", "9", "21", "Europe/Moscow"))
    monkeypatch.setattr(setup_wizard, "ask", lambda *_args, **_kwargs: next(answers))

    res = setup_wizard.step_app_mode({})

    assert res["APP_MODE"] == "approval"
    assert res["ENABLE_REAL_APPLY"] == "true"
    assert res["AUTO_APPLY_ENABLED"] == "true"
    assert res["AUTO_APPLY_MIN_CONFIDENCE"] == "0.90"
    assert res["AUTO_APPLY_MIN_BATCH_SIZE"] == "4"
    assert res["AUTO_APPLY_MAX_BATCH_SIZE"] == "6"
    assert res["AUTO_APPLY_MIN_INTERVAL_HOURS"] == "3"
    assert res["AUTO_APPLY_MAX_INTERVAL_HOURS"] == "5"
    assert res["AUTO_APPLY_START_HOUR"] == "9"
    assert res["AUTO_APPLY_END_HOUR"] == "21"
    assert res["AUTO_APPLY_TIMEZONE"] == "Europe/Moscow"


def test_build_env_includes_auto_apply_settings() -> None:
    rendered = setup_wizard.build_env(
        {"TG_BOT_TOKEN": "token", "TG_USER_ID": "123"},
        {"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3"},
        {"APP_MODE": "approval", "ENABLE_REAL_APPLY": "true"},
        {
            "AUTO_APPLY_ENABLED": "true",
            "AUTO_APPLY_MIN_CONFIDENCE": "0.88",
            "AUTO_APPLY_MIN_BATCH_SIZE": "2",
            "AUTO_APPLY_MAX_BATCH_SIZE": "4",
            "AUTO_APPLY_TIMEZONE": "Europe/Moscow",
        },
    )

    assert "AUTO_APPLY_ENABLED=true" in rendered
    assert "AUTO_APPLY_MIN_CONFIDENCE=0.88" in rendered
    assert "AUTO_APPLY_MIN_BATCH_SIZE=2" in rendered
    assert "AUTO_APPLY_MAX_BATCH_SIZE=4" in rendered
    assert "AUTO_APPLY_TIMEZONE=Europe/Moscow" in rendered

