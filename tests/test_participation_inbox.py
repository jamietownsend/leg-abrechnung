"""A pasted Teilnahmeerklärung in the Web-Registrierungen inbox: stored, matched, taken over, deleted."""

import sqlite3
from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest
from nicegui import Client, ui

from app.db.connection import connection_scope
from app.db.schema import initialize_database
from app.domain.registration_matching import load_matches
from app.importers.participation_email import parse_participation_email
from app.models import assignment as assignment_repo
from app.models import metering_point as metering_point_repo
from app.models import person as person_repo
from app.models import site as site_repo
from app.models import web_registration as web_registration_repo
from app.models.assignment import Assignment
from app.models.metering_point import DIRECTION_CONSUMPTION, DIRECTION_FEED_IN, MeteringPoint
from app.models.person import Person
from app.models.site import Site
from app.models.web_registration import SAVED_CREATED, SAVED_UNCHANGED, SAVED_UPDATED, SOURCE_EMAIL
from tests.test_participation_email import BEZUG, BUSINESS, EINSPEISUNG, PRIVATE, _with


def _pasted(text: str = PRIVATE):
    return parse_participation_email(text, today=date(2026, 12, 1)).registration


def _person(db, email="anna.muster@example.invalid", first_name="Anna") -> int:
    return person_repo.create(
        db,
        Person(
            id=None,
            salutation="",
            company="",
            first_name=first_name,
            last_name="Muster",
            contact_email=email,
            contact_phone="",
            billing_street="",
            billing_house_number="",
            billing_postal_code="",
            billing_city="",
            billing_country="CH",
            iban="",
            customer_number=None,
            bkw_customer_number=None,
            paper_invoice=False,
            active=True,
            created_at="",
        ),
    )


def _site(db, street="Seeweg", house_number="27") -> int:
    return site_repo.create(
        db,
        Site(
            id=None,
            street=street,
            house_number=house_number,
            postal_code="3652",
            municipality="Musterwil",
            address_detail="",
            substation_area_id=None,
            created_at="",
        ),
    )


def _metering_point(db, designation, direction, site_id) -> int:
    return metering_point_repo.create(
        db,
        MeteringPoint(
            id=None,
            designation=designation,
            direction=direction,
            site_id=site_id,
            leg_id=None,
            pv_capacity_kwp=None,
            battery_capacity_kwh=None,
            created_at="",
        ),
    )


# --- The migration --------------------------------------------------------------------------------


def test_migration_61_keeps_existing_rows_and_their_progress():
    """Every leg-ittigen.ch row survives the table rebuild, and the meters stay attached."""
    from app.db import migrations, schema

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    with patch.object(schema, "MIGRATIONS", [m for m in migrations.MIGRATIONS if m.version <= 60]):
        initialize_database(connection)
    connection.execute(
        "INSERT INTO web_registration (cloudflare_id, first_name, email, submitted_at, imported_at, "
        "person_taken_over) VALUES (7, 'Anna', 'a@example.invalid', '2026-01-01', '2026-01-01', 1)"
    )
    connection.execute(
        "INSERT INTO web_registration_meter (web_registration_id, meter_number, note, metering_point_taken_over) "
        "VALUES (1, 'CH1', 'Bezug', 1)"
    )
    connection.commit()

    assert schema.migrate_to_latest(connection) >= 61
    (reg,) = web_registration_repo.list_all(connection)
    assert (reg.cloudflare_id, reg.source, reg.first_name, reg.person_taken_over) == (
        7,
        "leg_ittigen",
        "Anna",
        True,
    )
    assert not reg.has_own_site_address
    (meter,) = reg.meters
    assert (meter.meter_number, meter.note, meter.metering_point_taken_over, meter.direction) == (
        "CH1",
        "Bezug",
        True,
        None,
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_leg_ittigen_row_still_needs_its_cloudflare_id(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO web_registration (source, submitted_at, imported_at) VALUES ('leg_ittigen', 'x', 'x')"
        )


# --- Storing a paste ------------------------------------------------------------------------------


def test_a_paste_goes_into_the_inbox_with_everything_the_email_said(db):
    reg_id, outcome = web_registration_repo.save_from_email(db, _pasted(BUSINESS))
    assert outcome == SAVED_CREATED
    stored = web_registration_repo.get(db, reg_id)
    assert (stored.source, stored.cloudflare_id, stored.company) == (
        SOURCE_EMAIL,
        None,
        "Bootsverleih Muster",
    )
    assert stored.site_address == ("Uferstrasse", "41", "3652", "Musterwil")
    assert [(m.meter_number, m.direction) for m in stored.meters] == [(BEZUG, DIRECTION_CONSUMPTION)]
    assert stored.submitted_at == "2026-11-27"


def test_a_paste_is_a_new_row_unless_an_update_is_asked_for(db):
    """Whoever pastes decides; without being told to update, nothing is overwritten."""
    first_id, _ = web_registration_repo.save_from_email(db, _pasted())
    second_id, outcome = web_registration_repo.save_from_email(db, _pasted())
    assert outcome == SAVED_CREATED and second_id != first_id
    assert len(web_registration_repo.list_all(db)) == 2


def test_updating_with_the_same_text_changes_nothing(db):
    first_id, _ = web_registration_repo.save_from_email(db, _pasted())
    assert web_registration_repo.save_from_email(db, _pasted(), update_id=first_id) == (
        first_id,
        SAVED_UNCHANGED,
    )
    assert len(web_registration_repo.list_all(db)) == 1


def test_an_update_replaces_the_content_and_keeps_what_was_taken_over(db):
    reg_id, _ = web_registration_repo.save_from_email(db, _pasted())
    stored = web_registration_repo.get(db, reg_id)
    web_registration_repo.mark_person_taken_over(db, reg_id)
    web_registration_repo.mark_metering_point_taken_over(db, stored.meters[0].id)

    corrected = _with(PRIVATE, "Telefon", "079 000 00 09")
    assert web_registration_repo.save_from_email(db, _pasted(corrected), update_id=reg_id) == (
        reg_id,
        SAVED_UPDATED,
    )

    updated = web_registration_repo.get(db, reg_id)
    assert updated.phone == "079 000 00 09"
    assert updated.person_taken_over
    assert [m.metering_point_taken_over for m in updated.meters] == [True, False]
    assert len(web_registration_repo.list_all(db)) == 1


def test_only_a_pasted_row_can_be_updated_by_a_paste(db):
    """A leg-ittigen.ch row belongs to its sync."""
    db.execute(
        "INSERT INTO web_registration (cloudflare_id, email, submitted_at, imported_at) "
        "VALUES (9, 'anna.muster@example.invalid', 'x', 'x')"
    )
    ittigen_id = db.execute("SELECT id FROM web_registration WHERE cloudflare_id = 9").fetchone()[0]
    with pytest.raises(ValueError):
        web_registration_repo.save_from_email(db, _pasted(), update_id=ittigen_id)
    with pytest.raises(ValueError):
        web_registration_repo.save_from_email(db, _pasted(), update_id=12345)


def _stranger(text: str = PRIVATE) -> str:
    """The same email from somebody else: other address, name, Firma and BKW customer number."""
    for label, value in [
        ("Email", "beat@example.invalid"),
        ("Vorname", "Beat"),
        ("Nachname", "Beispiel"),
        ("Firma", ""),
        ("BKW-Kundennummer", "90000099"),
    ]:
        text = _with(text, label, value)
    return text


def test_another_senders_rows_and_leg_ittigen_rows_are_no_candidates(db):
    web_registration_repo.save_from_email(db, _pasted(_stranger()))
    db.execute(
        "INSERT INTO web_registration (cloudflare_id, email, first_name, last_name, submitted_at, imported_at) "
        "VALUES (9, 'anna.muster@example.invalid', 'Anna', 'Muster', 'x', 'x')"
    )
    assert web_registration_repo.list_update_candidates(db, _pasted()) == []


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("Email", " Anna.Muster@Example.INVALID "),
        ("Vorname", "anna"),
        ("BKW-Kundennummer", "90000001"),
    ],
)
def test_one_shared_detail_makes_a_candidate(db, label, value):
    """Email (case aside), name (case and accents aside) or BKW customer number -- any one is enough."""
    mine, _ = web_registration_repo.save_from_email(db, _pasted())
    other = _stranger()
    if label == "Vorname":
        other = _with(other, "Nachname", "Müster")
    other = _with(other, label, value)
    assert [c.id for c in web_registration_repo.list_update_candidates(db, _pasted(other))] == [mine]


def test_the_same_person_from_another_address_is_a_candidate(db):
    """The business email: same name, other address, other BKW customer number."""
    mine, _ = web_registration_repo.save_from_email(db, _pasted())
    assert [c.id for c in web_registration_repo.list_update_candidates(db, _pasted(BUSINESS))] == [mine]


def test_the_same_firma_is_a_candidate(db):
    mine, _ = web_registration_repo.save_from_email(db, _pasted(BUSINESS))
    other = _with(_stranger(), "Firma", "BOOTSVERLEIH MUSTER")
    assert [c.id for c in web_registration_repo.list_update_candidates(db, _pasted(other))] == [mine]


def test_the_suggestion_is_the_row_for_the_same_connection(db):
    first, _ = web_registration_repo.save_from_email(db, _pasted())
    other_connection = _with(_with(PRIVATE, "Anschluss an der Postadresse", "nein"), "Anschluss Nr.", "99")
    web_registration_repo.save_from_email(db, _pasted(other_connection))
    candidates = web_registration_repo.list_update_candidates(db, _pasted())

    assert (
        web_registration_repo.suggested_update(candidates, _pasted(_with(PRIVATE, "Telefon", "x"))).id
        == first
    )
    third_connection = _with(other_connection, "Anschluss Nr.", "100")
    assert web_registration_repo.suggested_update(candidates, _pasted(third_connection)) is None


# --- Matching against what exists -------------------------------------------------------------


def test_the_standort_is_found_by_the_connection_address_not_the_postal_one(db):
    connection_site = _site(db, "Uferstrasse", "41")
    _site(db, "Uferstrasse", "28")
    reg_id, _ = web_registration_repo.save_from_email(db, _pasted(BUSINESS))
    match = load_matches(db, [web_registration_repo.get(db, reg_id)])[reg_id]
    assert match.site.id == connection_site


def test_existing_matching_records_are_no_conflict(db):
    """What already exists and agrees is offered for linking, not reported as a problem."""
    person_id = _person(db)
    site_id = _site(db)
    consumption = _metering_point(db, BEZUG, DIRECTION_CONSUMPTION, site_id)
    _metering_point(db, EINSPEISUNG, DIRECTION_FEED_IN, site_id)
    assignment_repo.create(db, Assignment(None, person_id, consumption, date(2026, 1, 1), None, ""))
    reg_id, _ = web_registration_repo.save_from_email(db, _pasted())
    match = load_matches(db, [web_registration_repo.get(db, reg_id)])[reg_id]
    assert match.person.id == person_id and match.site.id == site_id
    assert all(meter is not None for meter in match.metering_points.values())
    assert all(conflicts == [] for conflicts in match.meter_conflicts.values())


def test_contradictions_with_existing_records_are_named(db):
    """Reported as Einspeisung but recorded as Bezug, at another Standort, and assigned to someone else."""
    other_site = _site(db, "Bergweg", "3")
    feed_in_recorded_as_consumption = _metering_point(db, EINSPEISUNG, DIRECTION_CONSUMPTION, other_site)
    someone_else = _person(db, email="beat@example.invalid", first_name="Beat")
    assignment_repo.create(
        db, Assignment(None, someone_else, feed_in_recorded_as_consumption, date(2026, 1, 1), None, "")
    )
    reg_id, _ = web_registration_repo.save_from_email(db, _pasted())
    reg = web_registration_repo.get(db, reg_id)
    match = load_matches(db, [reg])[reg_id]
    feed_in_meter = next(m for m in reg.meters if m.direction == DIRECTION_FEED_IN)
    conflicts = " | ".join(match.meter_conflicts[feed_in_meter.id])
    assert "Als Einspeisung gemeldet, aber als Bezug erfasst." in conflicts
    assert "Bergweg 3" in conflicts
    assert "Zugeordnet zu Beat Muster." in conflicts


def test_a_leg_ittigen_row_still_matches_on_its_postal_address(db):
    """Unchanged behaviour: no connection address means the postal one."""
    site_id = _site(db)
    reg = replace(_pasted(), source="leg_ittigen", cloudflare_id=5, id=1)
    assert load_matches(db, [reg])[1].site.id == site_id


def test_a_leg_ittigen_row_gets_no_conflict_hints(db):
    """The same contradictions that are named for a pasted email stay unsaid for a synced row."""
    other_site = _site(db, "Bergweg", "3")
    _metering_point(db, EINSPEISUNG, DIRECTION_CONSUMPTION, other_site)
    reg_id, _ = web_registration_repo.save_from_email(db, _pasted())
    reg = replace(web_registration_repo.get(db, reg_id), source="leg_ittigen", cloudflare_id=5)
    match = load_matches(db, [reg])[reg_id]
    assert any(meter is not None for meter in match.metering_points.values())
    assert match.meter_conflicts == {}


# --- The page ---------------------------------------------------------------------------------------


def _render_inbox(probe: str) -> Client:
    from app.gui.pages.web_registrations import web_registrations_page

    client = Client(ui.page(probe)(lambda: None), request=None)
    with client:
        web_registrations_page()
    return client


def _elements(client: Client, kind: str) -> list:
    return [element for element in client.elements.values() if element.__class__.__name__ == kind]


def _click(client: Client, text: str, index: int = 0) -> None:
    buttons = [b for b in _elements(client, "Button") if b.text == text]
    handler = next(
        listener.handler for listener in buttons[index]._event_listeners.values() if listener.type == "click"
    )
    handler(None)


def _inbox() -> list:
    """What the page's own database holds (the page reads `connection_scope`, not the `db` fixture)."""
    with connection_scope() as connection:
        return web_registration_repo.list_all(connection)


def test_pasting_on_the_page_previews_then_puts_it_into_the_inbox():
    client = _render_inbox("/probe-participation-paste")
    with client:
        _click(client, "Teilnahmeerklärung einfügen")
        (textarea,) = _elements(client, "Textarea")
        (save,) = [b for b in _elements(client, "Button") if b.text == "In den Eingang übernehmen"]
        assert not save.enabled
        textarea.value = BUSINESS
        preview = " ".join(
            label.text
            for label in _elements(client, "Label")
            if label.text.startswith(("Name:", "Anschluss:", "Bezug:"))
        )
        assert "Bootsverleih Muster (Anna Muster)" in preview
        assert "Uferstrasse 41, 3652 Musterwil" in preview
        assert save.enabled
        _click(client, "In den Eingang übernehmen")
    (stored,) = _inbox()
    assert stored.is_from_email and stored.company == "Bootsverleih Muster"


def test_a_text_with_a_problem_cannot_be_put_into_the_inbox():
    client = _render_inbox("/probe-participation-problem")
    with client:
        _click(client, "Teilnahmeerklärung einfügen")
        (textarea,) = _elements(client, "Textarea")
        textarea.value = _with(PRIVATE, "Email", "")
        (save,) = [b for b in _elements(client, "Button") if b.text == "In den Eingang übernehmen"]
        assert not save.enabled
        assert any("Keine Email-Adresse" in label.text for label in _elements(client, "Label"))
    assert _inbox() == []


def test_taking_over_the_standort_prefills_the_connection_address_not_the_postal_one():
    with connection_scope() as connection:
        web_registration_repo.save_from_email(connection, _pasted(BUSINESS))
    client = _render_inbox("/probe-participation-takeover-site")
    with patch("app.gui.pages.web_registrations.open_site_form") as site_form, client:
        _click(client, "Standort übernehmen")
    assert site_form.call_args.kwargs["prefill"] == {
        "street": "Uferstrasse",
        "house_number": "41",
        "postal_code": "3652",
        "municipality": "Musterwil",
    }


def test_taking_over_a_meter_prefills_its_direction_and_the_connections_standort():
    with connection_scope() as connection:
        web_registration_repo.save_from_email(connection, _pasted(BUSINESS))
        connection_site = _site(connection, "Uferstrasse", "41")
    client = _render_inbox("/probe-participation-takeover-meter")
    with patch("app.gui.pages.web_registrations.open_metering_point_form") as meter_form, client:
        # The Standort at the connection address exists, so it is offered for linking.
        assert [b for b in _elements(client, "Button") if b.text == "Vorhandenen Standort verknüpfen"]
        _click(client, "Messpunkt übernehmen")
    prefill = meter_form.call_args.kwargs["prefill"]
    assert prefill["site_id"] == connection_site
    assert prefill["direction"] == DIRECTION_CONSUMPTION


def test_deleting_a_pasted_row_never_reaches_for_the_leg_ittigen_database():
    with connection_scope() as connection:
        web_registration_repo.save_from_email(connection, _pasted())
    client = _render_inbox("/probe-participation-delete")
    with patch("app.gui.pages.web_registrations.delete_submissions") as remote_delete, client:
        _click(client, "Löschen")
        texts = " ".join(label.text for label in _elements(client, "Label"))
        assert "die Email in Ihrem Postfach bleibt" in texts
        assert "Web-Datenbank" not in texts
        _click(client, "Löschen", index=1)
    remote_delete.assert_not_called()
    assert _inbox() == []


def test_the_reason_a_text_is_refused_stands_beside_the_disabled_button():
    """However long the pasted email, why it cannot go in is readable without scrolling."""
    client = _render_inbox("/probe-participation-reason")
    with client:
        _click(client, "Teilnahmeerklärung einfügen")
        (textarea,) = _elements(client, "Textarea")
        textarea.value = _with(PRIVATE, "Email", "")
        (save,) = [b for b in _elements(client, "Button") if b.text == "In den Eingang übernehmen"]
        row_labels = [label.text for label in save.parent_slot.parent.descendants() if hasattr(label, "text")]
        assert any("Keine Email-Adresse" in text for text in row_labels)


def test_the_printout_names_the_connection_address_too():
    from app.gui.pages.web_registrations import _print_row

    row = _print_row(_pasted(BUSINESS))
    assert row["address"] == "Uferstrasse 28, 3652 Musterwil / Anschluss: Uferstrasse 41, 3652 Musterwil"
    assert row["meters"] == f"{BEZUG} (Bezug)"


# --- Update or add: the user decides ----------------------------------------------------------


def _paste_on_page(client: Client, text: str) -> None:
    _click(client, "Teilnahmeerklärung einfügen")
    (textarea,) = _elements(client, "Textarea")
    textarea.value = text


def _choice(client: Client):
    radios = [r for r in _elements(client, "Radio") if "participation-update-choice" in r._markers]
    return radios[0] if radios else None


def _save_on_page(client: Client) -> None:
    _click(client, "In den Eingang übernehmen")


def test_without_an_earlier_paste_from_the_address_there_is_nothing_to_decide():
    client = _render_inbox("/probe-choice-none")
    with client:
        _paste_on_page(client, PRIVATE)
        assert _choice(client) is None
        _save_on_page(client)
    assert len(_inbox()) == 1


def test_a_correction_for_the_same_connection_is_suggested_as_an_update():
    with connection_scope() as connection:
        first_id, _ = web_registration_repo.save_from_email(connection, _pasted())
    client = _render_inbox("/probe-choice-update")
    with client:
        _paste_on_page(client, _with(PRIVATE, "Telefon", "079 000 00 09"))
        choice = _choice(client)
        assert choice.value == first_id
        assert set(choice.options) == {first_id, 0}
        assert choice.options[0] == "Als neue Anmeldung hinzufügen"
        _save_on_page(client)
    (stored,) = _inbox()
    assert (stored.id, stored.phone) == (first_id, "079 000 00 09")


def test_another_connection_is_suggested_as_an_addition():
    with connection_scope() as connection:
        web_registration_repo.save_from_email(connection, _pasted())
    other_connection = _with(_with(PRIVATE, "Anschluss an der Postadresse", "nein"), "Anschluss Nr.", "99")
    client = _render_inbox("/probe-choice-add")
    with client:
        _paste_on_page(client, other_connection)
        assert _choice(client).value == 0
        _save_on_page(client)
    assert len(_inbox()) == 2


def test_the_user_can_overrule_the_suggestion_and_the_choice_survives_editing_the_text():
    with connection_scope() as connection:
        first_id, _ = web_registration_repo.save_from_email(connection, _pasted())
    client = _render_inbox("/probe-choice-overrule")
    with client:
        _paste_on_page(client, PRIVATE)
        assert _choice(client).value == first_id
        _choice(client).value = 0
        (textarea,) = _elements(client, "Textarea")
        textarea.value = _with(PRIVATE, "Bemerkung", "zweites Gebäude")
        assert _choice(client).value == 0
        _save_on_page(client)
    assert len(_inbox()) == 2


def test_an_identical_text_says_so_in_its_option():
    with connection_scope() as connection:
        first_id, _ = web_registration_repo.save_from_email(connection, _pasted())
    client = _render_inbox("/probe-choice-identical")
    with client:
        _paste_on_page(client, PRIVATE)
        assert "identisch mit diesem Text" in _choice(client).options[first_id]
