"""The type used for account identifiers.

``EmailStr`` asks a narrower question than this system needs: *is this address
deliverable over the public internet?* The controller's own default admin is
``admin@local``, and email_validator rejects that unconditionally -- ``.local``
is a reserved name under RFC 6762 and no combination of its flags will pass it.
So the stack seeded an account it then refused to authenticate, and the only
visible symptom was a validation error on the login form.

These are login identifiers on a private appliance, not addresses anything
sends mail to. A reserved or single-label domain is legitimate here. Everything
structural is still enforced, and anything email_validator accepts is still
accepted -- and normalised the same way it always was.
"""

from __future__ import annotations

import re
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator

# RFC 1035 label: alphanumeric, inner hyphens, 63 octets at most.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# RFC 5322 dot-atom, which is every unquoted local part in practice.
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_PRIVATE_FORM = re.compile(rf"{_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL})*")

_MAX_TOTAL = 254
_MAX_LOCAL = 64


def normalise_account_email(value: str) -> str:
    value = value.strip()

    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        # Keep its wording for the final failure: "An email address must have
        # an @-sign" beats anything a regex can say about why it did not match.
        detail = str(exc)

    local, _, domain = value.rpartition("@")
    if not _PRIVATE_FORM.fullmatch(value):
        raise ValueError(detail)
    if len(local) > _MAX_LOCAL:
        raise ValueError(f"The part before the @-sign is longer than {_MAX_LOCAL} characters.")
    if len(value) > _MAX_TOTAL:
        raise ValueError(f"The email address is longer than {_MAX_TOTAL} characters.")

    # Domain case-insensitive, local part not -- the same normalisation
    # email_validator applies, so a lookup matches either way in.
    return f"{local}@{domain.lower()}"


AccountEmail = Annotated[str, AfterValidator(normalise_account_email)]
