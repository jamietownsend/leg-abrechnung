"""Reading a Teilnahmeerklärung pasted from an email into a `WebRegistration` for the inbox.

The text comes from a form that writes one `Label: value` line per field. Mail programs add links,
quote marks and signatures, so only known labels count, and only their first occurrence: a
signature's own "email:" line must not replace the one the form wrote.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.domain.email_validation import validate_email
from app.domain.iban_validation import normalize_iban, validate_iban
from app.domain.metering_point_validation import validate_metering_point_designation
from app.models.metering_point import DIRECTION_CONSUMPTION, DIRECTION_FEED_IN
from app.models.web_registration import SOURCE_EMAIL, WebRegistration, WebRegistrationMeter

#: Label as the form writes it (compared without case) -> field name.
_LABELS = {
    "firma": "company",
    "vorname": "first_name",
    "nachname": "last_name",
    "strasse": "street",
    "nr.": "house_number",
    "nr": "house_number",
    "plz": "postal_code",
    "ort": "city",
    "email": "email",
    "e-mail": "email",
    "telefon": "phone",
    "anschluss an der postadresse": "site_is_postal",
    "anschluss strasse": "site_street",
    "anschluss nr.": "site_house_number",
    "anschluss nr": "site_house_number",
    "anschluss plz": "site_postal_code",
    "anschluss ort": "site_city",
    "messpunktnummer bezug": "consumption",
    "messpunktnummer einspeisung": "feed_in",
    "iban": "iban",
    "bkw-kundennummer": "bkw_customer_number",
    "ich": "role",
    "bemerkung": "message",
    "datum": "date",
}

#: Fewer recognised labels than this, and the text is not a Teilnahmeerklärung at all.
_MIN_LABELS = 5

#: `Label: value`, allowing `>` quote marks and `**bold**` a mail program may add.
_LINE = re.compile(r"^[\s>]*\**\s*(?P<label>[^:*\n]{1,40}?)\s*\**\s*:\s*\**(?P<value>.*?)\**\s*$")
#: `[text](mailto:x)` or `[text](tel:x)` -- a link a mail program made of an address.
_MARKDOWN_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?:mailto:|tel:|https?://)[^)]*\)")
#: `<x>` or `<mailto:x>` around an address.
_ANGLE = re.compile(r"<(?:mailto:)?(?P<inner>[^<>\s]+)>")
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿"), None)


@dataclass
class ParsedParticipation:
    """What a pasted text says, and what stands in the way of taking it into the inbox."""

    registration: Optional[WebRegistration]
    #: Each one keeps the text out of the inbox.
    problems: list[str] = field(default_factory=list)
    #: Shown, but the text still goes in -- someone may check them when taking it over.
    warnings: list[str] = field(default_factory=list)


def parse_participation_email(text: str, *, today: Optional[date] = None) -> ParsedParticipation:
    """Read one pasted Teilnahmeerklärung."""
    values = _read_fields(text)
    if len(values) < _MIN_LABELS:
        return ParsedParticipation(
            None,
            problems=[
                "Kein Text einer Teilnahmeerklärung erkannt -- bitte den ganzen Inhalt der Email einfügen."
            ],
        )

    problems: list[str] = []
    warnings: list[str] = []
    get = lambda name: values.get(name, "")  # noqa: E731 -- a short reader, local on purpose

    email = get("email")
    if not email:
        problems.append("Keine Email-Adresse angegeben.")
    elif (email_problem := validate_email(email)) is not None:
        problems.append(f"Email-Adresse „{email}“: {email_problem}")
    if not (get("company") or get("first_name") or get("last_name")):
        problems.append("Weder Firma noch Name angegeben.")

    consumption = _compact(get("consumption"))
    feed_in = _compact(get("feed_in"))
    if not consumption and not feed_in:
        problems.append("Keine Messpunktnummer angegeben.")
    if consumption and consumption == feed_in:
        problems.append(f"Bezug und Einspeisung haben dieselbe Messpunktnummer {consumption}.")
    for label, number in (("Bezug", consumption), ("Einspeisung", feed_in)):
        if number and (designation_problem := validate_metering_point_designation(number)) is not None:
            warnings.append(f"Messpunktnummer {label} „{number}“: {designation_problem}")

    iban = normalize_iban(get("iban"))
    if iban and (iban_problem := validate_iban(iban)) is not None:
        warnings.append(f"IBAN „{get('iban')}“: {iban_problem}")
    if feed_in and not iban:
        warnings.append("Einspeisung ohne IBAN -- für die Gutschrift fehlt die Bankverbindung.")

    warnings.extend(_role_warnings(get("role"), consumption=bool(consumption), feed_in=bool(feed_in)))

    for label, postal_code in (("PLZ", get("postal_code")), ("Anschluss PLZ", get("site_postal_code"))):
        if postal_code and not re.fullmatch(r"\d{4}", postal_code):
            warnings.append(f"{label} „{postal_code}“ hat nicht vier Ziffern.")

    bkw_customer_number = re.sub(r"\s", "", get("bkw_customer_number"))
    if bkw_customer_number and not bkw_customer_number.isdigit():
        warnings.append(f"BKW-Kundennummer „{get('bkw_customer_number')}“ enthält nicht nur Ziffern.")

    site = _site_address(values, warnings)
    submitted = _date(get("date"), warnings, today or date.today())

    if problems:
        return ParsedParticipation(None, problems=problems, warnings=warnings)

    meters = [
        WebRegistrationMeter(
            id=None, web_registration_id=None, meter_number=number, note="", direction=direction
        )
        for number, direction in ((consumption, DIRECTION_CONSUMPTION), (feed_in, DIRECTION_FEED_IN))
        if number
    ]
    registration = WebRegistration(
        id=None,
        cloudflare_id=None,
        source=SOURCE_EMAIL,
        company=get("company"),
        salutation="",
        first_name=get("first_name"),
        last_name=get("last_name"),
        street=get("street"),
        house_number=get("house_number"),
        postal_code=get("postal_code"),
        city=get("city"),
        site_street=site[0],
        site_house_number=site[1],
        site_postal_code=site[2],
        site_city=site[3],
        email=email,
        phone=get("phone"),
        bkw_customer_number=bkw_customer_number,
        iban=iban,
        message=get("message"),
        submitted_at=submitted.isoformat(),
        imported_at="",
        meters=meters,
    )
    return ParsedParticipation(registration, problems=[], warnings=warnings)


def _read_fields(text: str) -> dict[str, str]:
    """The first value of every known label; unknown lines and later repeats are ignored."""
    values: dict[str, str] = {}
    for raw_line in text.translate(_INVISIBLE).replace(" ", " ").splitlines():
        match = _LINE.match(raw_line)
        if match is None:
            continue
        name = _LABELS.get(" ".join(match["label"].split()).lower())
        if name is None or name in values:
            continue
        values[name] = _clean(match["value"])
    return values


def _clean(value: str) -> str:
    """A value without the link syntax a mail program wrapped around it."""
    value = _MARKDOWN_LINK.sub(lambda m: m["text"], value)
    value = _ANGLE.sub(lambda m: m["inner"], value)
    value = re.sub(r"^mailto:", "", value.strip(), flags=re.IGNORECASE)
    return " ".join(value.split())


def _compact(value: str) -> str:
    """A Messpunktnummer without blanks, in capitals, as the form writes it."""
    return re.sub(r"\s", "", value).upper()


def _site_address(values: dict[str, str], warnings: list[str]) -> tuple[str, str, str, str]:
    """The connection's own address, or all empty when it is the postal address."""
    site = tuple(
        values.get(name, "") for name in ("site_street", "site_house_number", "site_postal_code", "site_city")
    )
    postal = tuple(values.get(name, "") for name in ("street", "house_number", "postal_code", "city"))
    same = [a.strip().lower() for a in site] == [a.strip().lower() for a in postal]
    said_same = values.get("site_is_postal", "").strip().lower()
    if not any(site) or same:
        return ("", "", "", "")
    if said_same == "ja":
        warnings.append(
            "„Anschluss an der Postadresse: ja“, aber die Anschlussadresse weicht ab -- "
            "die Anschlussadresse wird verwendet."
        )
    return site  # type: ignore[return-value]


def _role_warnings(role: str, *, consumption: bool, feed_in: bool) -> list[str]:
    """Whether "Ich: …" matches the Messpunktnummern given."""
    said = role.strip().lower()
    if not said:
        return []
    says_consumption = "beziehe" in said
    says_feed_in = "speise" in said
    if (says_consumption, says_feed_in) == (consumption, feed_in):
        return []
    return [f"„Ich: {role}“ passt nicht zu den angegebenen Messpunktnummern."]


def _date(value: str, warnings: list[str], today: date) -> date:
    """The form's `Datum: TT.MM.JJJJ`; today, with a warning, if it is missing or unreadable."""
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        warnings.append(
            f"Datum „{value}“ nicht lesbar -- heute ({today:%d.%m.%Y}) wird verwendet."
            if value
            else f"Kein Datum angegeben -- heute ({today:%d.%m.%Y}) wird verwendet."
        )
        return today
