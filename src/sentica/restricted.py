"""Restricted patterns the broker would reroute.

The engine cannot write the row into another workspace. When the
workspace ceiling cannot hold Restricted, a derived conclusion that
matches is dropped. An explicit conclusion the broker created is
exempt: the broker already classified it.
"""

from __future__ import annotations

import re

from src.sentica.workspace_metadata import ceiling_holds_restricted

REASON_CODES: tuple[str, ...] = (
    "card",
    "routing",
    "ssn",
    "account",
    "salary",
    "disciplinary",
)

_SSN = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
_SALARY = re.compile(
    r"\b(?:salary|wage|paycheck|pay rate|hourly rate|annual pay)\b",
    re.IGNORECASE,
)
_DIGIT_RUN = re.compile(r"\d[\d\s-]{5,30}\d")
_ACCOUNT_CONTEXT = re.compile(r"routing|account|acct", re.IGNORECASE)
_DISCIPLINARY: tuple[str, ...] = (
    "written warning",
    "suspension",
    "harassment",
    "disciplinary",
    "misconduct",
    "performance improvement",
    "terminated for cause",
    "fired for",
)


def _luhn_ok(digits: str) -> bool:
    """True for a 13 to 19 digit number that passes the Luhn check."""
    if len(digits) < 13 or len(digits) > 19 or not digits.isdigit():
        return False
    total = 0
    alt = False
    for index in range(len(digits) - 1, -1, -1):
        digit = ord(digits[index]) - 48
        if alt:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alt = not alt
    return total % 10 == 0


def _aba_ok(digits: str) -> bool:
    """True for a 9 digit ABA routing number with a non-zero checksum."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    weights = [ord(char) - 48 for char in digits]
    total = (
        3 * (weights[0] + weights[3] + weights[6])
        + 7 * (weights[1] + weights[4] + weights[7])
        + (weights[2] + weights[5] + weights[8])
    )
    return total % 10 == 0 and total != 0


def _ssn_in_range(area: str, group: str, serial: str) -> bool:
    """Reject area 000, 666, and 900-999, group 00, and serial 0000."""
    area_number = int(area)
    if area_number == 0 or area_number == 666 or area_number >= 900:
        return False
    return not (group == "00" or serial == "0000")


def restricted_reason(text: str) -> str | None:
    """Return a reason code, or None when the text is not Restricted."""
    if not text:
        return None
    for match in _SSN.finditer(text):
        area, group, serial = match.groups()
        if _ssn_in_range(area, group, serial):
            return "ssn"
    folded = text.casefold()
    for term in _DISCIPLINARY:
        if term in folded:
            return "disciplinary"
    if _SALARY.search(text) is not None and re.search(r"\d", text) is not None:
        return "salary"
    near_account = _ACCOUNT_CONTEXT.search(text) is not None
    for run in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", run.group(0))
        if _luhn_ok(digits):
            return "card"
        if not near_account:
            continue
        if _aba_ok(digits):
            return "routing"
        if 4 <= len(digits) <= 17:
            return "account"
    return None


def restricted_drop_reason(
    text: str, ceiling: str, *, broker_explicit: bool
) -> str | None:
    """Reason to drop, or None when the row may be stored.

    Broker-created explicit conclusions are exempt. A ceiling of
    ``restricted`` or ``personal`` may store the pattern. Anything
    else, including a malformed ceiling, drops it.
    """
    if broker_explicit:
        return None
    if ceiling_holds_restricted(ceiling):
        return None
    return restricted_reason(text)
