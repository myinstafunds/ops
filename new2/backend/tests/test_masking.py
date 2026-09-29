"""Masking rules (PRD 30, Rule 5).

Mobiles are the case the PRD names explicitly, so the 10-digit example is
asserted exactly. These are pure functions - no database, no fixtures.
"""

from __future__ import annotations

import pytest

from app.security.masking import (
    mask_aadhaar,
    mask_bank_account,
    mask_by_field,
    mask_email,
    mask_mapping,
    mask_mobile,
    mask_pan,
    mask_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "98XXXX3210"),
        ("98 7654 3210", "98XXXX3210"),
        # A 12-digit value is recognised as country code + number. The prefix
        # is kept because it is part of how the number is dialled, not PII.
        ("919876543210", "9198XXXX3210"),
        ("+91 98765 43210", "9198XXXX3210"),
    ],
)
def test_mobile_keeps_two_leading_and_four_trailing(raw: str, expected: str) -> None:
    """The PRD's canonical form: keep 2 leading and 4 trailing digits."""
    assert mask_mobile(raw) == expected


def test_mobile_of_eleven_digits_keeps_all_three_visible_parts() -> None:
    """An 11-digit input is not reshaped - only the middle is hidden."""
    assert mask_mobile("09876543210") == "09XXXXX3210"


def test_mobile_hides_the_middle_digits() -> None:
    masked = mask_mobile("9876543210")
    assert masked is not None
    # The subscriber's middle digits must not survive in any form.
    assert "765" not in masked
    assert "6543" not in masked


def test_mobile_of_short_value_is_fully_redacted() -> None:
    assert mask_mobile("12345") == "*****"
    assert mask_mobile("") is None
    assert mask_mobile(None) is None


def test_pan_is_never_shown_in_full() -> None:
    """Only the last four characters of a PAN survive."""
    masked = mask_pan("ABCDE1234F")
    assert masked == "******234F"
    assert "ABCDE" not in (masked or "")
    assert masked is not None and masked.endswith("234F")


def test_aadhaar_and_bank_account_keep_only_the_tail() -> None:
    assert mask_aadhaar("1234 5678 9012") == "XXXX XXXX 9012"
    assert mask_bank_account("000123456789") == "********6789"


def test_email_keeps_the_domain_and_one_leading_character() -> None:
    # "rahul.k" -> first character kept, the remaining 6 hidden.
    assert mask_email("rahul.k@bps.com") == "r******@bps.com"
    assert mask_email("not-an-email") == "*" * len("not-an-email")


def test_address_is_neutralised() -> None:
    assert mask_text("12 MG Road, Bengaluru") == "*" * len("12 MG Road, Bengaluru")


def test_mask_by_field_dispatches_on_the_field_name() -> None:
    assert mask_by_field("customer_mobile", "9876543210") == "98XXXX3210"
    assert mask_by_field("pan", "ABCDE1234F") == "******234F"
    assert mask_by_field("title", "Loan query") == "Loan query"
    assert mask_by_field("title", None) is None


def test_mask_mapping_masks_every_sensitive_key() -> None:
    masked = mask_mapping(
        {
            "customer_mobile": "9876543210",
            "email": "rahul.k@bps.com",
            "title": "Loan query",
            "count": 3,
        }
    )
    assert masked["customer_mobile"] == "98XXXX3210"
    assert masked["email"] == "r******@bps.com"
    assert masked["title"] == "Loan query"
    assert masked["count"] == 3
