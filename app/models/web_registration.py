"""WebRegistration: an inbox row for one registration -- submitted through the public form on leg-
ittigen.ch (see `app.importers.registration_sync`), or a Teilnahmeerklärung pasted from an email
(see `app.importers.participation_email`)."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.sort_keys import fold_for_sort

#: Where a row came from: the leg-ittigen.ch form, or a Teilnahmeerklärung pasted from an email.
SOURCE_LEG_ITTIGEN = "leg_ittigen"
SOURCE_EMAIL = "email"


@dataclass
class WebRegistrationMeter:
    """One reported Zählernummer within a `WebRegistration`."""

    id: Optional[int]
    web_registration_id: Optional[int]
    meter_number: str
    note: str
    metering_point_taken_over: bool = False
    #: `DIRECTION_CONSUMPTION` / `DIRECTION_FEED_IN` when the sender said which; None otherwise.
    direction: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "WebRegistrationMeter":
        """Build a `WebRegistrationMeter` from a `sqlite3.Row`."""
        return WebRegistrationMeter(
            id=row["id"],
            web_registration_id=row["web_registration_id"],
            meter_number=row["meter_number"],
            note=row["note"],
            metering_point_taken_over=bool(row["metering_point_taken_over"]),
            direction=row["direction"],
        )


@dataclass
class WebRegistration:
    """One registration, from the leg-ittigen.ch form or a pasted Teilnahmeerklärung."""

    id: Optional[int]
    #: None for a pasted email, which has no Cloudflare row behind it.
    cloudflare_id: Optional[int]
    company: str
    salutation: str
    first_name: str
    last_name: str
    street: str
    house_number: str
    postal_code: str
    city: str
    email: str
    phone: str
    bkw_customer_number: str
    iban: str
    message: str
    submitted_at: str
    imported_at: str
    person_taken_over: bool = False
    site_taken_over: bool = False
    meters: list[WebRegistrationMeter] = field(default_factory=list)
    source: str = SOURCE_LEG_ITTIGEN
    #: The building the meters are in, when it is not the postal address; all empty means the same.
    site_street: str = ""
    site_house_number: str = ""
    site_postal_code: str = ""
    site_city: str = ""

    @property
    def is_from_email(self) -> bool:
        """Whether this row was pasted from an email rather than fetched from leg-ittigen.ch."""
        return self.source == SOURCE_EMAIL

    @property
    def has_own_site_address(self) -> bool:
        """Whether the connection is at an address of its own, apart from the postal address."""
        return any((self.site_street, self.site_house_number, self.site_postal_code, self.site_city))

    @property
    def site_address(self) -> tuple[str, str, str, str]:
        """Street, number, PLZ and place of the connection -- the postal address unless one was given."""
        if self.has_own_site_address:
            return (self.site_street, self.site_house_number, self.site_postal_code, self.site_city)
        return (self.street, self.house_number, self.postal_code, self.city)

    @property
    def display_name(self) -> str:
        """Single-line display name, mirroring `Person.display_name`."""
        full_name = " ".join(p for p in (self.first_name, self.last_name) if p)
        if self.company and full_name:
            return f"{self.company} ({full_name})"
        return self.company or full_name

    @property
    def is_fully_processed(self) -> bool:
        """Whether there is nothing left to take over from this registration."""
        return (
            self.person_taken_over
            and self.site_taken_over
            and all(m.metering_point_taken_over for m in self.meters)
        )

    @staticmethod
    def from_row(row: sqlite3.Row, meters: list[WebRegistrationMeter]) -> "WebRegistration":
        """Build a `WebRegistration` from a `sqlite3.Row` and its meters."""
        return WebRegistration(
            id=row["id"],
            cloudflare_id=row["cloudflare_id"],
            company=row["company"],
            salutation=row["salutation"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            street=row["street"],
            house_number=row["house_number"],
            postal_code=row["postal_code"],
            city=row["city"],
            email=row["email"],
            phone=row["phone"],
            bkw_customer_number=row["bkw_customer_number"],
            iban=row["iban"],
            message=row["message"],
            submitted_at=row["submitted_at"],
            imported_at=row["imported_at"],
            person_taken_over=bool(row["person_taken_over"]),
            site_taken_over=bool(row["site_taken_over"]),
            meters=meters,
            source=row["source"],
            site_street=row["site_street"],
            site_house_number=row["site_house_number"],
            site_postal_code=row["site_postal_code"],
            site_city=row["site_city"],
        )


def _load_meters(connection: sqlite3.Connection, web_registration_id: int) -> list[WebRegistrationMeter]:
    """Load all meters reported with one registration."""
    rows = connection.execute(
        "SELECT * FROM web_registration_meter WHERE web_registration_id = ? ORDER BY id",
        (web_registration_id,),
    ).fetchall()
    return [WebRegistrationMeter.from_row(row) for row in rows]


def list_all(connection: sqlite3.Connection) -> list[WebRegistration]:
    """List all registrations, most recently submitted first."""
    rows = connection.execute("SELECT * FROM web_registration ORDER BY submitted_at DESC").fetchall()
    return [WebRegistration.from_row(row, _load_meters(connection, row["id"])) for row in rows]


def get(connection: sqlite3.Connection, web_registration_id: int) -> Optional[WebRegistration]:
    """Fetch a single registration by id."""
    row = connection.execute("SELECT * FROM web_registration WHERE id = ?", (web_registration_id,)).fetchone()
    return WebRegistration.from_row(row, _load_meters(connection, row["id"])) if row else None


def get_by_email(connection: sqlite3.Connection, email: str) -> Optional[WebRegistration]:
    """Fetch a single registration by its email address."""
    row = connection.execute("SELECT * FROM web_registration WHERE email = ?", (email,)).fetchone()
    return WebRegistration.from_row(row, _load_meters(connection, row["id"])) if row else None


def upsert_from_submission(connection: sqlite3.Connection, registration: WebRegistration) -> int:
    """Insert a new registration, or update the existing one for the same email in place, replacing its..."""
    existing = get_by_email(connection, registration.email)
    previously_taken_over_by_meter = (
        {m.meter_number: m.metering_point_taken_over for m in existing.meters} if existing else {}
    )
    now = datetime.now(timezone.utc).isoformat()

    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO web_registration
                (cloudflare_id, company, salutation, first_name, last_name, street, house_number,
                 postal_code, city, email, phone, bkw_customer_number, iban, message,
                 submitted_at, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                registration.cloudflare_id,
                registration.company,
                registration.salutation,
                registration.first_name,
                registration.last_name,
                registration.street,
                registration.house_number,
                registration.postal_code,
                registration.city,
                registration.email,
                registration.phone,
                registration.bkw_customer_number,
                registration.iban,
                registration.message,
                registration.submitted_at,
                now,
            ),
        )
        web_registration_id = cursor.lastrowid
    else:
        connection.execute(
            """
            UPDATE web_registration SET
                cloudflare_id = ?, company = ?, salutation = ?, first_name = ?, last_name = ?,
                street = ?, house_number = ?, postal_code = ?, city = ?, email = ?, phone = ?,
                bkw_customer_number = ?, iban = ?, message = ?, submitted_at = ?,
                imported_at = ?
            WHERE id = ?
            """,
            (
                registration.cloudflare_id,
                registration.company,
                registration.salutation,
                registration.first_name,
                registration.last_name,
                registration.street,
                registration.house_number,
                registration.postal_code,
                registration.city,
                registration.email,
                registration.phone,
                registration.bkw_customer_number,
                registration.iban,
                registration.message,
                registration.submitted_at,
                now,
                existing.id,
            ),
        )
        web_registration_id = existing.id
        connection.execute(
            "DELETE FROM web_registration_meter WHERE web_registration_id = ?",
            (web_registration_id,),
        )

    for meter in registration.meters:
        connection.execute(
            "INSERT INTO web_registration_meter (web_registration_id, meter_number, note, metering_point_taken_over) "
            "VALUES (?, ?, ?, ?)",
            (
                web_registration_id,
                meter.meter_number,
                meter.note,
                previously_taken_over_by_meter.get(meter.meter_number, False),
            ),
        )

    connection.commit()
    return web_registration_id


#: What `save_from_email` did with a pasted Teilnahmeerklärung.
SAVED_CREATED = "created"
SAVED_UPDATED = "updated"
SAVED_UNCHANGED = "unchanged"

#: What a pasted row consists of -- written on save, compared to tell a repeat from a change.
_EMAIL_COMPARED_FIELDS = (
    "company",
    "first_name",
    "last_name",
    "street",
    "house_number",
    "postal_code",
    "city",
    "site_street",
    "site_house_number",
    "site_postal_code",
    "site_city",
    "email",
    "phone",
    "bkw_customer_number",
    "iban",
    "message",
    "submitted_at",
)


def _site_key(registration: WebRegistration) -> tuple[str, ...]:
    """The connection address, compared without case or surrounding blanks."""
    street, house_number, postal_code, _city = registration.site_address
    return tuple(part.strip().lower() for part in (street, house_number, postal_code))


def _same_sender(row: sqlite3.Row, registration: WebRegistration) -> bool:
    """Same email address, same name, same Firma or same BKW customer number -- any one is enough."""
    pairs = [
        (row["email"], registration.email),
        (row["bkw_customer_number"], registration.bkw_customer_number),
        (row["company"], registration.company),
    ]
    if fold_for_sort(row["first_name"]) and fold_for_sort(row["last_name"]):
        pairs.append(
            (f"{row['first_name']} {row['last_name']}", f"{registration.first_name} {registration.last_name}")
        )
    return any(fold_for_sort(stored) and fold_for_sort(stored) == fold_for_sort(new) for stored, new in pairs)


def list_update_candidates(
    connection: sqlite3.Connection, registration: WebRegistration
) -> list[WebRegistration]:
    """Pasted rows from the same sender, newest first -- what a new paste might update."""
    rows = connection.execute(
        "SELECT * FROM web_registration WHERE source = ? ORDER BY submitted_at DESC, id DESC",
        (SOURCE_EMAIL,),
    ).fetchall()
    return [
        WebRegistration.from_row(row, _load_meters(connection, row["id"]))
        for row in rows
        if _same_sender(row, registration)
    ]


def suggested_update(
    candidates: list[WebRegistration], registration: WebRegistration
) -> Optional[WebRegistration]:
    """Which row a new paste most likely corrects: the one for the same connection. Only a suggestion."""
    return next((c for c in candidates if _site_key(c) == _site_key(registration)), None)


def is_unchanged(existing: WebRegistration, registration: WebRegistration) -> bool:
    """Whether a paste says exactly what the row already holds."""
    return all(
        getattr(existing, name) == getattr(registration, name) for name in _EMAIL_COMPARED_FIELDS
    ) and {(m.meter_number, m.direction) for m in existing.meters} == {
        (m.meter_number, m.direction) for m in registration.meters
    }


def save_from_email(
    connection: sqlite3.Connection, registration: WebRegistration, *, update_id: Optional[int] = None
) -> tuple[int, str]:
    """Add a pasted Teilnahmeerklärung as a new row, or update row `update_id` -- whoever pastes decides.

    An update replaces the row's content and keeps what was already taken over, per meter number.
    """
    existing = None
    if update_id is not None:
        existing = get(connection, update_id)
        if existing is None or not existing.is_from_email:
            raise ValueError(f"Keine eingefügte Anmeldung #{update_id} zum Aktualisieren.")
        if is_unchanged(existing, registration):
            return existing.id, SAVED_UNCHANGED

    now = datetime.now(timezone.utc).isoformat()
    values = tuple(getattr(registration, name) for name in _EMAIL_COMPARED_FIELDS)
    if existing is None:
        columns = ", ".join(_EMAIL_COMPARED_FIELDS)
        cursor = connection.execute(
            f"INSERT INTO web_registration (source, {columns}, imported_at) "  # nosec B608 -- fixed names
            f"VALUES (?, {', '.join('?' for _ in _EMAIL_COMPARED_FIELDS)}, ?)",
            (SOURCE_EMAIL, *values, now),
        )
        web_registration_id = cursor.lastrowid
        outcome = SAVED_CREATED
        taken_over_before: dict[str, bool] = {}
    else:
        assignments = ", ".join(f"{name} = ?" for name in _EMAIL_COMPARED_FIELDS)
        connection.execute(
            f"UPDATE web_registration SET {assignments}, imported_at = ? WHERE id = ?",  # nosec B608 -- fixed names
            (*values, now, existing.id),
        )
        web_registration_id = existing.id
        outcome = SAVED_UPDATED
        taken_over_before = {m.meter_number: m.metering_point_taken_over for m in existing.meters}
        connection.execute("DELETE FROM web_registration_meter WHERE web_registration_id = ?", (existing.id,))

    for meter in registration.meters:
        connection.execute(
            "INSERT INTO web_registration_meter "
            "(web_registration_id, meter_number, note, metering_point_taken_over, direction) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                web_registration_id,
                meter.meter_number,
                meter.note,
                taken_over_before.get(meter.meter_number, False),
                meter.direction,
            ),
        )
    connection.commit()
    return web_registration_id, outcome


def mark_person_taken_over(connection: sqlite3.Connection, web_registration_id: int) -> None:
    """Record that this registration's Person needs no further action."""
    connection.execute(
        "UPDATE web_registration SET person_taken_over = 1 WHERE id = ?",
        (web_registration_id,),
    )
    connection.commit()


def mark_site_taken_over(connection: sqlite3.Connection, web_registration_id: int) -> None:
    """Record that this registration's site needs no further action."""
    connection.execute(
        "UPDATE web_registration SET site_taken_over = 1 WHERE id = ?",
        (web_registration_id,),
    )
    connection.commit()


def mark_metering_point_taken_over(connection: sqlite3.Connection, web_registration_meter_id: int) -> None:
    """Record that one reported meter needs no further action."""
    connection.execute(
        "UPDATE web_registration_meter SET metering_point_taken_over = 1 WHERE id = ?",
        (web_registration_meter_id,),
    )
    connection.commit()


def unmark_person_taken_over(connection: sqlite3.Connection, web_registration_id: int) -> None:
    """Reopen this registration's Person item."""
    connection.execute(
        "UPDATE web_registration SET person_taken_over = 0 WHERE id = ?",
        (web_registration_id,),
    )
    connection.commit()


def unmark_site_taken_over(connection: sqlite3.Connection, web_registration_id: int) -> None:
    """Reopen this registration's site item."""
    connection.execute(
        "UPDATE web_registration SET site_taken_over = 0 WHERE id = ?",
        (web_registration_id,),
    )
    connection.commit()


def unmark_metering_point_taken_over(connection: sqlite3.Connection, web_registration_meter_id: int) -> None:
    """Reopen one reported meter."""
    connection.execute(
        "UPDATE web_registration_meter SET metering_point_taken_over = 0 WHERE id = ?",
        (web_registration_meter_id,),
    )
    connection.commit()


def delete(connection: sqlite3.Connection, web_registration_id: int) -> None:
    """Delete a registration and its reported meters (cascade)."""
    connection.execute("DELETE FROM web_registration WHERE id = ?", (web_registration_id,))
    connection.commit()
