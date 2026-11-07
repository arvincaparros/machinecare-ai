"""Unit tests for Argon2id hashing and the password policy.

No database, API or network access: these exercise only
``app.core.security`` and ``app.core.password_policy``.
"""

import pytest

from argon2 import PasswordHasher as Argon2PasswordHasher

from app.core.password_policy import (
    describe_violations,
    validate_password,
)

from app.core.security import (
    hash_password,
    password_needs_rehash,
    verify_and_update_password,
    verify_dummy_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    password_hash = hash_password("correct horse battery staple")

    assert password_hash != "correct horse battery staple"
    assert password_hash.startswith("$argon2")
    assert verify_password("correct horse battery staple", password_hash)


def test_hash_uses_argon2id():
    password_hash = hash_password("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")


def test_wrong_password_is_rejected():
    password_hash = hash_password("correct horse battery staple")

    assert not verify_password("incorrect horse", password_hash)


def test_hashes_are_salted_and_unique():
    first = hash_password("same password both times")
    second = hash_password("same password both times")

    assert first != second
    assert verify_password("same password both times", first)
    assert verify_password("same password both times", second)


def test_password_needs_rehash_detects_weak_parameters():
    current = hash_password("a-fresh-hash")

    assert not password_needs_rehash(current)

    # A real Argon2id hash produced with deliberately minimal
    # parameters; the library's rehash check must flag it for upgrade
    # on the next login.
    weak_hasher = Argon2PasswordHasher(
        memory_cost=8,
        time_cost=1,
        parallelism=1,
    )

    assert password_needs_rehash(weak_hasher.hash("a-weak-params-hash"))


def test_verify_and_update_returns_upgraded_hash_for_weak_parameters():
    weak_hasher = Argon2PasswordHasher(
        memory_cost=8,
        time_cost=1,
        parallelism=1,
    )
    stale = weak_hasher.hash("correct horse battery staple")

    valid, upgraded = verify_and_update_password(
        "correct horse battery staple",
        stale,
    )

    assert valid is True
    assert upgraded is not None
    assert upgraded != stale
    assert verify_password("correct horse battery staple", upgraded)


def test_verify_and_update_keeps_a_current_hash_unchanged():
    current = hash_password("correct horse battery staple")

    valid, upgraded = verify_and_update_password(
        "correct horse battery staple",
        current,
    )

    assert valid is True
    assert upgraded is None


def test_verify_and_update_rejects_a_wrong_password():
    stored = hash_password("correct horse battery staple")

    valid, upgraded = verify_and_update_password("wrong", stored)

    assert valid is False
    assert upgraded is None


def test_dummy_verification_always_returns_false_but_takes_place():
    assert verify_dummy_password("anything") is False


class TestPasswordPolicy:
    def test_accepts_a_long_uncommon_password(self):
        assert validate_password("wrench-turbine-cascade-91") == []

    @pytest.mark.parametrize("min_length", [12, 16])
    def test_rejects_short_passwords(self, min_length):
        password = "a" * (min_length - 1)

        assert validate_password(password, min_length=min_length) == [
            "too_short"
        ]

    def test_length_boundary_is_inclusive(self):
        password = "a" * 12

        assert validate_password(password, min_length=12) == []

    def test_rejects_common_password_even_when_long(self):
        long_common = "password1234"

        assert validate_password(long_common) == ["too_common"]

    def test_common_check_is_case_insensitive(self):
        assert validate_password("PASSWORD") == ["too_short", "too_common"]

    def test_rejects_password_containing_the_username(self):
        violations = validate_password(
            "hopeless-engineer-42",
            username="engineer",
        )

        assert violations == ["contains_username"]

    def test_username_substring_is_case_insensitive(self):
        violations = validate_password(
            "TrustTheADMIN2026",
            username="Admin",
        )

        assert violations == ["contains_username"]

    def test_reports_every_violation_at_once(self):
        violations = validate_password("admin", username="admin")

        assert violations == [
            "too_short",
            "too_common",
            "contains_username",
        ]

    def test_describe_violations_mentions_the_minimum_length(self):
        text = describe_violations(["too_short"], min_length=12)

        assert "12" in text

    def test_describe_violations_empty_list_is_empty_text(self):
        assert describe_violations([]) == ""
