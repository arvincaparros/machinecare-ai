"""Tests for the upload digest that ties an import to its preview."""

import pytest

from app.services.incident_import_digest import (
    digest_matches,
    normalise_sha256,
    sha256_hex,
)


CONTENT = b"Incident ID,Date\nDT-2026-101,2026-08-01\n"


def test_sha256_hex_is_lowercase_and_matches_known_digests():
    assert sha256_hex(CONTENT) == (
        "19d5aa3862dc5b634c4343f05a8a99ca60dc13d62361045b367fc7"
        "1ef679bf3b"
    )
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca49599"
        "1b7852b855"
    )


def test_digest_matches_requires_the_exact_bytes():
    assert digest_matches(sha256_hex(CONTENT), CONTENT) is True
    assert digest_matches(sha256_hex(CONTENT + b"\n"), CONTENT) is False


def test_digest_matches_accepts_case_and_surrounding_space():
    digest = sha256_hex(CONTENT)

    assert digest_matches(f"  {digest.upper()}  ", CONTENT) is True


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "abc", "z" * 64, "0" * 63, "0" * 65],
)
def test_digest_matches_refuses_missing_or_malformed_values(value):
    assert digest_matches(value, CONTENT) is False


def test_normalise_sha256_returns_none_for_blank_values():
    assert normalise_sha256(None) is None
    assert normalise_sha256("  ") is None
    assert normalise_sha256(" ABC ") == "abc"
