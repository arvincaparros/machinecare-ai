"""Digest of an import upload.

The preview returns the SHA-256 of the bytes it checked, and the import
has to send the same value back, so a file that changed between the two
requests is refused instead of imported. Keeping the comparison here --
with no database, model or vector-store import -- lets the read-only
preview and the writing endpoint share it without either of them
depending on the other.
"""

import hashlib
import hmac
import re


SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


def sha256_hex(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of an upload."""

    return hashlib.sha256(content).hexdigest()


def normalise_sha256(value: str | None) -> str | None:
    """Accept a digest as a client sends it, or ``None`` when absent."""

    if value is None:
        return None

    digest = value.strip().lower()

    return digest or None


def digest_matches(expected: str | None, content: bytes) -> bool:
    """Compare a client digest with the upload, in constant time.

    A value that is not 64 hexadecimal characters can never match, so a
    malformed digest is refused instead of being compared.
    """

    digest = normalise_sha256(expected)

    if digest is None or not SHA256_PATTERN.match(digest):
        return False

    return hmac.compare_digest(digest, sha256_hex(content))
