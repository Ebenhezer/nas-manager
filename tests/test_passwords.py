from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.passwords import (
    hash_password,
    validate_password_policy,
    verify_password,
)


@pytest.fixture
def test_settings(settings: Settings) -> Settings:
    return settings


def test_hash_uses_argon2id(test_settings: Settings) -> None:
    digest = hash_password("a strongly random password", test_settings)
    assert digest.startswith("$argon2id$")


def test_verify_roundtrip(test_settings: Settings) -> None:
    digest = hash_password("a strongly random password", test_settings)
    assert verify_password("a strongly random password", digest, test_settings) is True
    assert verify_password("wrong password", digest, test_settings) is False


def test_verify_handles_garbage_without_raising(test_settings: Settings) -> None:
    assert verify_password("x", "not-an-argon2-hash", test_settings) is False
    assert verify_password("x", "", test_settings) is False


def test_different_salts_produce_different_hashes(test_settings: Settings) -> None:
    first = hash_password("same password", test_settings)
    second = hash_password("same password", test_settings)
    assert first != second


def test_defined_costs_are_used_in_testing(test_settings: Settings) -> None:
    assert test_settings.argon2_time_cost == 1
    assert test_settings.argon2_memory_cost == 64


@pytest.mark.parametrize("password", ["short", "x" * 11])
def test_too_short_rejected(password: str, test_settings: Settings) -> None:
    with pytest.raises(ValueError, match="at least"):
        validate_password_policy(password, settings=test_settings)


def test_too_long_rejected(test_settings: Settings) -> None:
    with pytest.raises(ValueError, match="at most"):
        too_long = "x" * (test_settings.password_max_length + 1)
        validate_password_policy(too_long, settings=test_settings)


def test_whitespace_only_rejected(test_settings: Settings) -> None:
    with pytest.raises(ValueError, match="whitespace"):
        validate_password_policy((" " * test_settings.password_min_length), settings=test_settings)


def test_password_equal_to_username_rejected(test_settings: Settings) -> None:
    with pytest.raises(ValueError, match="identical"):
        validate_password_policy("alicearduino", username="alicearduino", settings=test_settings)


def test_valid_password_accepted(test_settings: Settings) -> None:
    validate_password_policy(
        "correct horse battery staple", username="alice", settings=test_settings
    )
