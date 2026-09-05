"""Security hardening: identity pinning, login throttling, key derivation.

These cover the gaps an audit of the first release turned up. Each test names
the attack it prevents, because a security test whose purpose is not obvious
gets deleted by someone tidying up later.
"""

from __future__ import annotations

import pytest

from app.drivers.identity import (
    IdentityMismatch,
    certificate_fingerprint,
    check_pin,
    format_fingerprint,
)
from app.security import SecretBox, hash_password, verify_password
from app.services.throttle import LoginThrottle, throttle_key

# -- device identity pinning ------------------------------------------------


def test_fingerprint_is_the_colon_form_people_compare_by_eye() -> None:
    assert format_fingerprint("aabbcc") == "AA:BB:CC"


def test_certificate_fingerprint_is_sha256_over_der() -> None:
    import hashlib

    der = b"not really a certificate"
    expected = format_fingerprint(hashlib.sha256(der).hexdigest())
    assert certificate_fingerprint(der) == expected


def test_first_contact_is_accepted() -> None:
    """TOFU: with nothing pinned there is nothing to compare against."""
    check_pin(None, "AA:BB", what="TLS certificate", host="r1")


def test_matching_identity_passes() -> None:
    check_pin("AA:BB:CC", "AA:BB:CC", what="TLS certificate", host="r1")


def test_comparison_ignores_formatting() -> None:
    """A pin copied from OpenSSL and one from the API must compare equal."""
    check_pin("aabbcc", "AA:BB:CC", what="TLS certificate", host="r1")


def test_a_different_identity_is_refused() -> None:
    """The attack: someone intercepts traffic to the router. Without pinning
    they collect the credentials and every PSK pushed through it."""
    with pytest.raises(IdentityMismatch) as exc:
        check_pin("AA:BB:CC", "DD:EE:FF", what="TLS certificate", host="r1")

    message = str(exc.value)
    assert "does not match" in message
    # Both values are shown so an operator can tell a rebuild from an attack.
    assert "AA:BB:CC" in message and "DD:EE:FF" in message
    assert "compromised" in message


def test_mismatch_is_not_a_driver_error() -> None:
    """It must not be caught by the retry-the-transport handlers: this is a
    refusal to continue, not a connection problem."""
    from app.drivers.base import DriverError

    assert not issubclass(IdentityMismatch, DriverError)


# -- login throttling -------------------------------------------------------


def test_throttle_allows_attempts_below_the_limit() -> None:
    t = LoginThrottle(max_attempts=3, lockout_seconds=60)
    assert t.check("k") == 0.0
    t.record_failure("k")
    t.record_failure("k")
    assert t.check("k") == 0.0


def test_throttle_locks_out_after_the_limit() -> None:
    """The attack: unlimited guessing against the one service that can decrypt
    every router credential you own."""
    t = LoginThrottle(max_attempts=3, lockout_seconds=60)
    for _ in range(3):
        t.record_failure("k")

    remaining = t.check("k")
    assert 0 < remaining <= 60


def test_a_success_clears_the_counter() -> None:
    t = LoginThrottle(max_attempts=3, lockout_seconds=60)
    t.record_failure("k")
    t.record_failure("k")
    t.record_success("k")

    t.record_failure("k")
    assert t.check("k") == 0.0


def test_lockouts_are_per_account_and_source() -> None:
    """Keying on the account alone would let anyone lock a colleague out just by
    guessing at their address."""
    t = LoginThrottle(max_attempts=2, lockout_seconds=60)
    victim = throttle_key("nina@example.com", "203.0.113.9")
    attacker = throttle_key("nina@example.com", "198.51.100.1")

    t.record_failure(attacker)
    t.record_failure(attacker)

    assert t.check(attacker) > 0
    assert t.check(victim) == 0.0


def test_throttle_key_is_case_insensitive_on_the_address() -> None:
    """Otherwise NINA@ and nina@ get separate budgets."""
    assert throttle_key("Nina@Example.com", "1.2.3.4") == throttle_key(
        "nina@example.com", "1.2.3.4"
    )


def test_old_failures_stop_counting() -> None:
    """Someone who mistypes twice a week should never be locked out."""
    t = LoginThrottle(max_attempts=3, lockout_seconds=60, window_seconds=0)
    for _ in range(5):
        t.record_failure("k")
    # Every failure fell outside the window, so the count keeps resetting to 1.
    assert t.check("k") == 0.0


# -- key derivation ---------------------------------------------------------


def test_secrets_round_trip() -> None:
    box = SecretBox("a-secret-that-is-long-enough-to-use")
    assert box.decrypt(box.encrypt("device-password")) == "device-password"


def test_ciphertext_is_not_the_plaintext() -> None:
    box = SecretBox("a-secret-that-is-long-enough-to-use")
    assert "device-password" not in box.encrypt("device-password")


def test_a_different_key_cannot_decrypt() -> None:
    written = SecretBox("secret-number-one-long-enough-yes").encrypt("psk")
    with pytest.raises(ValueError, match="SDWAN_SECRET_KEY has changed"):
        SecretBox("secret-number-two-long-enough-yes").decrypt(written)


def test_values_written_under_the_old_derivation_still_decrypt() -> None:
    """The scheme changed from bare SHA-256 to PBKDF2. Existing installs must
    not lose every stored credential on upgrade -- there is no flag day."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    secret = "an-existing-deployment-secret-key"
    legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
    written_before_upgrade = legacy.encrypt(b"old-device-password").decode()

    assert SecretBox(secret).decrypt(written_before_upgrade) == "old-device-password"


def test_new_writes_use_the_new_derivation() -> None:
    """A value written now must NOT be readable by the old scheme, or the
    upgrade bought nothing."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet, InvalidToken

    secret = "an-existing-deployment-secret-key"
    written_now = SecretBox(secret).encrypt("new-device-password")
    legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    with pytest.raises(InvalidToken):
        legacy.decrypt(written_now.encode())


# -- password hashing -------------------------------------------------------


def test_passwords_round_trip() -> None:
    assert verify_password("correct-horse", hash_password("correct-horse"))


def test_wrong_password_fails() -> None:
    assert not verify_password("wrong", hash_password("correct-horse"))


def test_overlong_passwords_are_rejected_not_truncated() -> None:
    """bcrypt silently truncates at 72 bytes. Accepting a longer one would mean
    two different passwords authenticate the same account."""
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)


def test_a_malformed_stored_hash_fails_closed() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")
