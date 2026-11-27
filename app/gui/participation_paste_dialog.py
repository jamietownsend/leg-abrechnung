"""Dialog: paste a Teilnahmeerklärung from an email into the Web-Registrierungen inbox."""

from typing import Callable

from nicegui import ui

from app.db.connection import connection_scope
from app.domain.iban_validation import format_iban
from app.gui.form_dialog import form_guard
from app.gui.safe_notify import safe_notify
from app.importers.participation_email import ParsedParticipation, parse_participation_email
from app.models import web_registration as web_registration_repo
from app.models.metering_point import DIRECTION_FEED_IN
from app.models.web_registration import SAVED_CREATED, SAVED_UNCHANGED, SAVED_UPDATED, WebRegistration

#: What the inbox says after a paste, by what `save_from_email` did.
_OUTCOME_TEXT = {
    SAVED_CREATED: "Teilnahmeerklärung von {name} in den Eingang übernommen.",
    SAVED_UPDATED: "Teilnahmeerklärung von {name} im Eingang aktualisiert.",
    SAVED_UNCHANGED: "Teilnahmeerklärung von {name} ist unverändert bereits im Eingang.",
}

#: The choice "add as a new row" among the rows a paste could update.
ADD_NEW = 0


def candidate_label(candidate: WebRegistration, registration: WebRegistration) -> str:
    """One existing row as an update option: who, which connection, when, and how far it got."""
    street, number, postal_code, city = candidate.site_address
    label = (
        f"Aktualisieren: {candidate.display_name}, Anschluss {street} {number}, {postal_code} {city}, "
        f"eingegangen {candidate.submitted_at}"
    )
    if web_registration_repo.is_unchanged(candidate, registration):
        label += " -- identisch mit diesem Text"
    elif (
        candidate.person_taken_over
        or candidate.site_taken_over
        or any(m.metering_point_taken_over for m in candidate.meters)
    ):
        label += " -- teilweise übernommen; Übernommenes bleibt übernommen"
    return label


def summary_lines(registration: WebRegistration) -> list[str]:
    """What will go into the inbox, one line per thing, as the preview shows it."""
    postal = (
        f"{registration.street} {registration.house_number}, {registration.postal_code} {registration.city}"
    )
    site = "wie Postadresse"
    if registration.has_own_site_address:
        street, number, postal_code, city = registration.site_address
        site = f"{street} {number}, {postal_code} {city}"
    lines = [
        f"Name: {registration.display_name}",
        f"Email: {registration.email}",
        f"Telefon: {registration.phone or '-'}",
        f"Postadresse: {postal}",
        f"Anschluss: {site}",
    ]
    lines += [
        f"{'Einspeisung' if meter.direction == DIRECTION_FEED_IN else 'Bezug'}: {meter.meter_number}"
        for meter in registration.meters
    ]
    lines += [
        f"IBAN: {format_iban(registration.iban) if registration.iban else '-'}",
        f"BKW-Kundennummer: {registration.bkw_customer_number or '-'}",
        f"Datum: {registration.submitted_at}",
    ]
    if registration.message:
        lines.append(f"Bemerkung: {registration.message}")
    return lines


def open_participation_paste_dialog(on_saved: Callable[[], None]) -> None:
    """Paste, check, and take a Teilnahmeerklärung into the inbox; nothing is created beyond the inbox row."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label("Teilnahmeerklärung einfügen").classes("text-lg font-bold")
        ui.label(
            "Den ganzen Text der Email einfügen. Gelesen werden die Zeilen des Formulars "
            "(„Vorname: …“, „Messpunktnummer Bezug: …“); Gruss und Signatur werden übergangen. "
            "Danach erscheint die Anmeldung im Eingang und wird dort wie jede Web-Registrierung "
            "übernommen."
        ).classes("text-body2 text-grey-8")
        # A fixed height with its own scrollbar: a long email must not push the preview out of sight.
        text = ui.textarea("Text der Email").props("outlined rows=10").classes("w-full")
        preview = ui.column().classes("w-full gap-0").mark("participation-preview")
        #: The update-or-add choice, kept across re-renders while the same rows are on offer.
        choice: dict[str, object] = {"value": ADD_NEW, "offered": ()}

        def parsed() -> ParsedParticipation:
            return parse_participation_email(text.value or "")

        def render_preview() -> None:
            """Show what the text says and what stands in the way, as it is pasted or edited."""
            preview.clear()
            error_label.text = ""
            if not (text.value or "").strip():
                save_button.disable()
                return
            result = parsed()
            with preview:
                for problem in result.problems:
                    ui.label(f"✖ {problem}").classes("text-negative text-body2")
                if result.registration is not None:
                    render_update_choice(result.registration)
                    for line in summary_lines(result.registration):
                        ui.label(line).classes("text-body2")
                for warning in result.warnings:
                    ui.label(f"⚠ {warning}").classes("text-warning text-body2")
            save_button.set_enabled(not result.problems)
            # Also beside the disabled button, so why it is disabled never needs scrolling to read.
            error_label.text = "; ".join(result.problems)

        def render_update_choice(registration: WebRegistration) -> None:
            """When pasted rows from this sender exist: update one of them, or add a new row -- the user's call."""
            with connection_scope() as connection:
                candidates = web_registration_repo.list_update_candidates(connection, registration)
            offered = tuple(c.id for c in candidates)
            if not candidates:
                choice.update(value=ADD_NEW, offered=())
                return
            if offered != choice["offered"]:
                # New rows on offer: start from the suggestion, the row for the same connection.
                suggestion = web_registration_repo.suggested_update(candidates, registration)
                choice.update(value=suggestion.id if suggestion else ADD_NEW, offered=offered)
            ui.label(
                "Von dieser Email-Adresse ist schon eine Anmeldung im Eingang. Ist das eine Korrektur "
                "oder eine weitere Anmeldung (z. B. ein zweiter Anschluss)?"
            ).classes("text-body2 font-bold mt-2")
            options = {c.id: candidate_label(c, registration) for c in candidates}
            options[ADD_NEW] = "Als neue Anmeldung hinzufügen"
            ui.radio(
                options,
                value=choice["value"],
                on_change=lambda event: choice.update(value=event.value),
            ).props("dense").classes("text-body2 mb-2").mark("participation-update-choice")

        def save() -> None:
            """Put the pasted Teilnahmeerklärung into the inbox."""
            result = parsed()
            if result.registration is None:
                error_label.text = "; ".join(result.problems) or "Kein Text eingefügt."
                return
            update_id = choice["value"] if choice["offered"] and choice["value"] != ADD_NEW else None
            with connection_scope() as connection:
                _id, outcome = web_registration_repo.save_from_email(
                    connection, result.registration, update_id=update_id
                )
            dialog.close()
            safe_notify(
                _OUTCOME_TEXT[outcome].format(name=result.registration.display_name),
                type="info" if outcome == SAVED_UNCHANGED else "positive",
            )
            on_saved()

        with ui.row().classes("w-full justify-end items-center gap-2 mt-2"):
            error_label = ui.label("").classes("text-negative mr-auto")
            ui.button("Abbrechen", on_click=dialog.close).props("flat")
            save_button = ui.button("In den Eingang übernehmen", on_click=save)
            save_button.disable()

        text.on_value_change(lambda _: render_preview())
    form_guard(dialog, on_save=save)
    dialog.open()
