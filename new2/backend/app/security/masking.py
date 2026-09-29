"""Customer data masking (PRD 30, Rule 5).

Masking is applied at the schema layer so a raw value cannot accidentally be
serialised. The stored value stays intact for the systems that legitimately
need it; only the representation that leaves the API is masked.
"""

from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D")

#: Keys that must be masked anywhere they appear in a serialised structure.
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "customer_mobile",
        "mobile",
        "mobile_number",
        "phone",
        "contact_number",
        "pan",
        "pan_number",
        "aadhaar",
        "aadhaar_number",
        "bank_account",
        "bank_account_number",
        "account_number",
        "ifsc",
        "email",
        "address",
    }
)


def mask_mobile(value: str | None, *, keep_last: int = 4) -> str | None:
    """``9876543210`` -> ``98XXXX3210``.

    Indian numbers keep the first two digits and the last ``keep_last``.
    Anything shorter or non numeric is fully redacted rather than guessed at.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) < 6:
        return "*" * len(digits) if digits else "***"
    country = ""
    if len(digits) == 12 and digits.startswith("91"):
        country = "91"
        digits = digits[2:]
    prefix_len = 2
    visible = digits[-keep_last:] if keep_last > 0 else ""
    hidden_len = max(len(digits) - prefix_len - keep_last, 0)
    masked = f"{digits[:prefix_len]}{'X' * hidden_len}{visible}"
    return f"{country}{masked}" if country else masked


def mask_pan(value: str | None, *, keep_last: int = 4) -> str | None:
    """``ABCDE1234F`` -> ``XXXXX1234F``. A PAN is never shown in full."""
    if not value:
        return None
    raw = str(value).strip().upper()
    if len(raw) <= keep_last:
        return "*" * len(raw)
    return f"{'*' * (len(raw) - keep_last)}{raw[-keep_last:]}"


def mask_aadhaar(value: str | None) -> str | None:
    """``1234 5678 9012`` -> ``XXXX XXXX 9012``."""
    if not value:
        return None
    digits = _NON_DIGIT.sub("", str(value))
    if len(digits) < 4:
        return "*" * len(digits)
    return f"XXXX XXXX {digits[-4:]}"


def mask_bank_account(value: str | None, *, keep_last: int = 4) -> str | None:
    if not value:
        return None
    digits = _NON_DIGIT.sub("", str(value))
    if len(digits) <= keep_last:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - keep_last)}{digits[-keep_last:]}"


def mask_email(value: str | None) -> str | None:
    """``rahul.k@bps.com`` -> ``r****@bps.com``."""
    if not value:
        return None
    raw = str(value).strip()
    if "@" not in raw:
        return mask_text(raw)
    local, _, domain = raw.partition("@")
    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"


def mask_text(value: str | None) -> str | None:
    """Neutral masker for free text fields such as an address."""
    if not value:
        return None
    return "*" * len(str(value))


def mask_by_field(field_name: str, value: object) -> object:
    """Dispatch to the correct masker based on the field name."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    name = field_name.lower()
    if name in {"customer_mobile", "mobile", "mobile_number", "phone", "contact_number"}:
        return mask_mobile(value)
    if name in {"pan", "pan_number"}:
        return mask_pan(value)
    if name in {"aadhaar", "aadhaar_number"}:
        return mask_aadhaar(value)
    if name in {"bank_account", "bank_account_number", "account_number"}:
        return mask_bank_account(value)
    if name == "email":
        return mask_email(value)
    if name == "address":
        return mask_text(value)
    return value


def mask_mapping(data: dict[str, object]) -> dict[str, object]:
    """Mask every sensitive key in a flat mapping (e.g. a sync payload)."""
    return {
        key: mask_by_field(key, value) if key.lower() in SENSITIVE_FIELD_NAMES else value
        for key, value in data.items()
    }
