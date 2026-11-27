"""Tests for app.importers.participation_email -- reading a pasted Teilnahmeerklärung.

The two texts below have the exact shape of real ones (links a mail program made of the addresses,
a signature with its own "email:" line), with invented people, numbers and addresses.
"""

from datetime import date

import pytest

from app.importers.participation_email import parse_participation_email
from app.models.metering_point import DIRECTION_CONSUMPTION, DIRECTION_FEED_IN
from app.models.web_registration import SOURCE_EMAIL

BEZUG = "CH1000000000000000000000000001001"
EINSPEISUNG = "CH1000000000000000000000000001002"
IBAN = "CH93 0076 2011 6238 5295 7"

PRIVATE = f"""Teilnahmeerklärung LEG Beispiel

Ich nehme an einer lokalen Elektrizitätsgemeinschaft (LEG) teil, die der Verein LEG Beispiel verwaltet.

Firma:
Vorname: Anna
Nachname: Muster
Strasse: Seeweg
Nr.: 27
PLZ: 3652
Ort: Musterwil
Email: [anna.muster@example.invalid](mailto:anna.muster@example.invalid)
Telefon: 079 000 00 01
Anschluss an der Postadresse: ja
Anschluss Strasse: Seeweg
Anschluss Nr.: 27
Anschluss PLZ: 3652
Anschluss Ort: Musterwil
Messpunktnummer Bezug: {BEZUG}
Messpunktnummer Einspeisung: {EINSPEISUNG}
IBAN: {IBAN}
BKW-Kundennummer: 90000001
Ich: beziehe Strom aus der LEG und speise ein
Bemerkung:

Ich akzeptiere das Teilnahmereglement und das aktuelle Preisblatt von LEG Beispiel. Mein Name und meine Adresse dürfen auf der Liste der Teilnehmenden meiner LEG erscheinen.

Datum: 27.11.2026

Freundliche Grüsse
"""

BUSINESS = f"""Teilnahmeerklärung LEG Beispiel

Ich nehme an einer lokalen Elektrizitätsgemeinschaft (LEG) teil, die der Verein LEG Beispiel verwaltet.

Firma: Bootsverleih Muster
Vorname: Anna
Nachname: Muster
Strasse: Uferstrasse
Nr.: 28
PLZ: 3652
Ort: Musterwil
Email: [anna@bootsverleih.example.invalid](mailto:anna@bootsverleih.example.invalid)
Telefon: 079 000 00 02
Anschluss an der Postadresse: nein
Anschluss Strasse: Uferstrasse
Anschluss Nr.: 41
Anschluss PLZ: 3652
Anschluss Ort: Musterwil
Messpunktnummer Bezug: {BEZUG}
Messpunktnummer Einspeisung:
IBAN:
BKW-Kundennummer: 80000002
Ich: beziehe Strom aus der LEG
Bemerkung:

Ich akzeptiere das Teilnahmereglement und das aktuelle Preisblatt von LEG Beispiel. Mein Name und meine Adresse dürfen auf der Liste der Teilnehmenden meiner LEG erscheinen.

Datum: 27.11.2026

Freundliche Grüsse

_____________________
Bootsverleih Muster
Uferstrasse 28
3652 Musterwil

Switzerland

email: [info@bootsverleih.example.invalid](mailto:info@bootsverleih.example.invalid)
phone: [+41 79 000 00 02](tel:+41790000002)
[www.bootsverleih.example.invalid](https://www.bootsverleih.example.invalid)
"""


def _parse(text: str):
    return parse_participation_email(text, today=date(2026, 12, 1))


def _with(text: str, label: str, value: str) -> str:
    """The text with one form line's value replaced."""
    lines = [f"{label}: {value}" if line.startswith(f"{label}:") else line for line in text.splitlines()]
    return "\n".join(lines)


# --- The two shapes ----------------------------------------------------------------------------


def test_a_private_person_with_consumption_and_feed_in_at_the_postal_address():
    result = _parse(PRIVATE)
    assert result.problems == [] and result.warnings == []
    reg = result.registration
    assert (reg.source, reg.cloudflare_id) == (SOURCE_EMAIL, None)
    assert (reg.company, reg.first_name, reg.last_name) == ("", "Anna", "Muster")
    assert (reg.street, reg.house_number, reg.postal_code, reg.city) == (
        "Seeweg",
        "27",
        "3652",
        "Musterwil",
    )
    assert reg.email == "anna.muster@example.invalid"
    assert reg.phone == "079 000 00 01"
    assert not reg.has_own_site_address
    assert reg.site_address == ("Seeweg", "27", "3652", "Musterwil")
    assert [(m.meter_number, m.direction) for m in reg.meters] == [
        (BEZUG, DIRECTION_CONSUMPTION),
        (EINSPEISUNG, DIRECTION_FEED_IN),
    ]
    assert reg.iban == "CH9300762011623852957"
    assert reg.bkw_customer_number == "90000001"
    assert reg.submitted_at == "2026-11-27"
    assert reg.message == ""


def test_a_business_whose_connection_is_elsewhere_and_whose_signature_is_ignored():
    """The signature's own 'email:' line must not replace the address the form wrote."""
    result = _parse(BUSINESS)
    assert result.problems == [] and result.warnings == []
    reg = result.registration
    assert reg.display_name == "Bootsverleih Muster (Anna Muster)"
    assert reg.email == "anna@bootsverleih.example.invalid"
    assert reg.phone == "079 000 00 02"
    assert (reg.street, reg.house_number) == ("Uferstrasse", "28")
    assert reg.has_own_site_address
    assert reg.site_address == ("Uferstrasse", "41", "3652", "Musterwil")
    assert [(m.meter_number, m.direction) for m in reg.meters] == [(BEZUG, DIRECTION_CONSUMPTION)]
    assert reg.iban == ""


# --- What mail programs do to the text ------------------------------------------------------------


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda t: t.replace("\n", "\r\n"), id="crlf"),
        pytest.param(lambda t: "\n".join("> " + line for line in t.splitlines()), id="quoted-reply"),
        pytest.param(lambda t: t.replace("Vorname:", "**Vorname:**").replace("PLZ:", "**PLZ**:"), id="bold"),
        pytest.param(lambda t: t.replace(": ", ": "), id="non-breaking-space"),
        pytest.param(
            lambda t: t.replace(
                "Email: [anna.muster@example.invalid](mailto:anna.muster@example.invalid)",
                "E-Mail: <mailto:anna.muster@example.invalid>",
            ),
            id="angle-mailto",
        ),
        pytest.param(
            lambda t: t.replace("Vorname", "VORNAME").replace(
                "Messpunktnummer Bezug", "messpunktnummer bezug"
            ),
            id="case",
        ),
        pytest.param(
            lambda t: "Weitergeleitete Nachricht\nVon: Anna\nBetreff: Teilnahme\n\n" + t, id="forwarded"
        ),
    ],
)
def test_what_mail_programs_add_does_not_change_the_result(mangle):
    expected = _parse(PRIVATE).registration
    result = _parse(mangle(PRIVATE))
    assert result.problems == []
    reg = result.registration
    assert (reg.first_name, reg.email, reg.postal_code, reg.iban) == (
        expected.first_name,
        expected.email,
        expected.postal_code,
        expected.iban,
    )
    assert [m.meter_number for m in reg.meters] == [m.meter_number for m in expected.meters]


def test_meter_numbers_and_iban_lose_their_blanks_and_get_capitals():
    spaced_meter = " ".join(BEZUG.lower()[i : i + 4] for i in range(0, len(BEZUG), 4))
    text = _with(_with(PRIVATE, "Messpunktnummer Bezug", spaced_meter), "IBAN", IBAN.lower())
    reg = _parse(text).registration
    assert reg.meters[0].meter_number == BEZUG
    assert reg.iban == "CH9300762011623852957"


# --- What keeps a text out of the inbox ---------------------------------------------------------


def test_a_text_that_is_no_teilnahmeerklaerung():
    result = _parse("Hallo zusammen\n\nWann ist die nächste Versammlung?\n\nGruss Anna")
    assert result.registration is None
    assert "Kein Text einer Teilnahmeerklärung" in result.problems[0]


@pytest.mark.parametrize(
    ("label", "value", "problem"),
    [
        ("Email", "", "Keine Email-Adresse"),
        ("Email", "anna.muster@example", "Email-Adresse"),
        ("Messpunktnummer Bezug", "", "Keine Messpunktnummer"),
    ],
)
def test_missing_essentials_keep_it_out(label, value, problem):
    text = _with(PRIVATE, label, value)
    if label == "Messpunktnummer Bezug":
        text = _with(text, "Messpunktnummer Einspeisung", "")
    result = _parse(text)
    assert result.registration is None
    assert any(problem in p for p in result.problems)


def test_no_name_and_no_company_keeps_it_out():
    result = _parse(_with(_with(PRIVATE, "Vorname", ""), "Nachname", ""))
    assert result.registration is None
    assert "Weder Firma noch Name" in result.problems[0]


def test_the_same_number_for_both_directions_is_a_logical_error():
    result = _parse(_with(PRIVATE, "Messpunktnummer Einspeisung", BEZUG))
    assert result.registration is None
    assert "dieselbe Messpunktnummer" in result.problems[0]


# --- What is only shown -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "value", "warning"),
    [
        ("Messpunktnummer Bezug", "CH10000000000000000000000000010", "Messpunktnummer Bezug"),
        ("IBAN", "CH93 0076 2011 6238 5295 8", "IBAN"),
        ("IBAN", "", "Einspeisung ohne IBAN"),
        ("Ich", "beziehe Strom aus der LEG", "passt nicht"),
        ("PLZ", "365", "vier Ziffern"),
        ("BKW-Kundennummer", "9000-0001", "nicht nur Ziffern"),
        ("Datum", "gestern", "nicht lesbar"),
        ("Datum", "", "Kein Datum"),
    ],
)
def test_doubtful_details_are_shown_but_do_not_keep_it_out(label, value, warning):
    result = _parse(_with(PRIVATE, label, value))
    assert result.problems == []
    assert result.registration is not None
    assert any(warning in w for w in result.warnings), result.warnings


def test_a_missing_date_becomes_today():
    assert _parse(_with(PRIVATE, "Datum", "")).registration.submitted_at == "2026-12-01"


def test_ja_with_a_different_connection_address_uses_the_connection_address_and_says_so():
    result = _parse(_with(PRIVATE, "Anschluss Nr.", "29"))
    assert result.registration.site_address == ("Seeweg", "29", "3652", "Musterwil")
    assert any("Anschlussadresse weicht ab" in w for w in result.warnings)


def test_nein_with_the_same_address_is_the_postal_address():
    reg = _parse(_with(PRIVATE, "Anschluss an der Postadresse", "nein")).registration
    assert not reg.has_own_site_address
