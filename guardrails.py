"""Output guardrail: no full IBAN or card number ever leaves the system."""
import re

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b")
_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def mask(text):
    if not text:
        return text
    text = _IBAN.sub(lambda m: m.group(0)[:2] + "** **** " + m.group(0).replace(" ", "")[-4:], text)
    return _PAN.sub(lambda m: "**** **** **** " + re.sub(r"\D", "", m.group(0))[-4:], text)
