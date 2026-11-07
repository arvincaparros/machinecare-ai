"""Password policy checks shared by every password-setting path.

Rules (authentication-and-rbac-plan.md §3.5): minimum length, no
composition rules, reject passwords from the bundled common-password
list, reject passwords containing the username.

The common-password list is deliberately small in Phase 1: the
notorious shortlist below covers the highest-frequency leaked
passwords. Replacing it with a full top-10k list is a data-only change;
keep the module interface stable when that happens.
"""

from __future__ import annotations

# Lowercase entries; matching is case-insensitive.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "1234",
        "12345",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "1234567890",
        "123123",
        "112233",
        "111111",
        "000000",
        "121212",
        "123321",
        "123qwe",
        "1q2w3e",
        "1q2w3e4r",
        "1qaz2wsx",
        "qazwsx",
        "qwerty",
        "qwerty123",
        "qwertyuiop",
        "qwe123",
        "abc123",
        "abcd1234",
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "passw0rd",
        "passwd",
        "trustno1",
        "letmein",
        "letmein123",
        "welcome",
        "welcome1",
        "welcome123",
        "admin",
        "admin123",
        "admin1234",
        "administrator",
        "root",
        "toor",
        "login",
        "guest",
        "master",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "basketball",
        "soccer",
        "hockey",
        "iloveyou",
        "princess",
        "sunshine",
        "shadow",
        "superman",
        "batman",
        "michael",
        "jennifer",
        "jordan",
        "harley",
        "ranger",
        "hunter",
        "buster",
        "tigger",
        "soccer1",
        "purple",
        "chocolate",
        "cookie",
        "flower",
        "samsung",
        "google",
        "facebook",
        "zaq12wsx",
        "asdfgh",
        "asdfghjkl",
        "zxcvbnm",
        "a123456",
        "aa123456",
        "abc12345",
        "p@ssw0rd",
        "p@ssword",
        "pass@word1",
        "whatever",
        "freedom",
        "computer",
        "starwars",
        "summer",
        "winter",
        "spring",
        "autumn",
        "secret",
        "test123",
        "test1234",
        "changeme",
        "default",
        "machinecare",
    }
)

_PASSWORD_TOO_SHORT = "too_short"
_PASSWORD_COMMON = "too_common"
_PASSWORD_CONTAINS_USERNAME = "contains_username"

DEFAULT_MESSAGES = {
    _PASSWORD_TOO_SHORT: "Password must be at least {min_length} characters long.",
    _PASSWORD_COMMON: "Password is too common and easy to guess.",
    _PASSWORD_CONTAINS_USERNAME: "Password must not contain the username.",
}


def validate_password(
    password: str,
    *,
    username: str | None = None,
    min_length: int = 12,
) -> list[str]:
    """Return the policy violation codes for a candidate password.

    An empty list means the password is acceptable. All checks run so
    callers can report every violation at once; the codes are stable
    identifiers, use ``DEFAULT_MESSAGES`` for user-facing text.
    """

    violations: list[str] = []

    if len(password) < min_length:
        violations.append(_PASSWORD_TOO_SHORT)

    if password.lower() in COMMON_PASSWORDS:
        violations.append(_PASSWORD_COMMON)

    if username:
        lowered_username = username.strip().lower()

        if lowered_username and lowered_username in password.lower():
            violations.append(_PASSWORD_CONTAINS_USERNAME)

    return violations


def describe_violations(
    violations: list[str],
    *,
    min_length: int = 12,
) -> str:
    """Human-readable sentence for a violation list."""

    if not violations:
        return ""

    messages: list[str] = []

    for code in violations:
        if code == _PASSWORD_TOO_SHORT:
            messages.append(
                DEFAULT_MESSAGES[code].format(min_length=min_length)
            )
        else:
            messages.append(DEFAULT_MESSAGES[code])

    return " ".join(messages)
