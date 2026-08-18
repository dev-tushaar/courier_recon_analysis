"""
CSV ingestion for courier invoices.

Courier invoice exports are messy in predictable ways: BOM-prefixed headers from
Excel, currency symbols and thousands separators inside numeric columns, blank
trailing rows, inconsistent header casing. This module normalises all of that
and reports per-row errors instead of failing the whole upload on row 4,000.
"""

import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..models import InvoiceLine, Zone

REQUIRED_COLUMNS = {"awb", "weight_kg", "amount"}
OPTIONAL_COLUMNS = {"zone"}

# Real exports use whatever header the courier's billing system emits. Rather
# than demand one format, map the common variants onto our canonical names.
COLUMN_ALIASES = {
    "awb": "awb",
    "awb_no": "awb",
    "awbnumber": "awb",
    "waybill": "awb",
    "tracking_id": "awb",
    "weight": "weight_kg",
    "weight_kg": "weight_kg",
    "charged_weight": "weight_kg",
    "billed_weight": "weight_kg",
    "amount": "amount",
    "billed_amount": "amount",
    "total": "amount",
    "freight": "amount",
    "zone": "zone",
    "billed_zone": "zone",
}


class IngestError(Exception):
    """Raised when the file is unusable as a whole (bad headers, not CSV)."""


def _normalise_header(name):
    return name.strip().lstrip("\ufeff").lower().replace(" ", "_").replace("-", "_")


def _parse_decimal(raw, field_name, row_number):
    """Coerce a spreadsheet cell into a Decimal.

    Strips currency symbols, thousands separators and stray whitespace. Parsing
    via str -> Decimal rather than float keeps the exact decimal value the
    courier printed; float('1234.55') is already not 1234.55.
    """
    if raw is None:
        raise ValueError(f"Row {row_number}: '{field_name}' is missing")

    cleaned = str(raw).strip()
    for junk in ("\u20b9", "Rs.", "Rs", "INR", ",", " "):
        cleaned = cleaned.replace(junk, "")

    if not cleaned:
        raise ValueError(f"Row {row_number}: '{field_name}' is empty")

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Row {row_number}: '{field_name}' value {raw!r} is not a number")


def parse_invoice_csv(file_obj):
    """Parse an uploaded CSV into a list of row dicts plus a list of errors.

    Returns ``(rows, errors)``. Rows that fail validation are skipped and
    reported; the caller decides whether a partial import is acceptable.
    """
    try:
        raw = file_obj.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        raise IngestError("File is not valid UTF-8. Re-export it as CSV UTF-8.")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise IngestError("The file appears to be empty.")

    header_map = {}
    for original in reader.fieldnames:
        canonical = COLUMN_ALIASES.get(_normalise_header(original))
        if canonical:
            header_map[original] = canonical

    missing = REQUIRED_COLUMNS - set(header_map.values())
    if missing:
        raise IngestError(
            f"Missing required column(s): {', '.join(sorted(missing))}. "
            f"Found: {', '.join(reader.fieldnames)}"
        )

    valid_zones = {z.value for z in Zone}
    rows, errors = [], []

    for row_number, raw_row in enumerate(reader, start=2):  # row 1 is the header
        # Skip fully blank rows -- Excel loves to append a few.
        if not any((v or "").strip() for v in raw_row.values()):
            continue

        record = {
            canonical: raw_row.get(original)
            for original, canonical in header_map.items()
        }

        try:
            awb = (record.get("awb") or "").strip()
            if not awb:
                raise ValueError(f"Row {row_number}: AWB is blank")

            weight = _parse_decimal(record.get("weight_kg"), "weight_kg", row_number)
            amount = _parse_decimal(record.get("amount"), "amount", row_number)

            if weight < 0 or amount < 0:
                raise ValueError(f"Row {row_number}: negative weight or amount")

            zone = (record.get("zone") or "").strip().upper()
            if zone and zone not in valid_zones:
                raise ValueError(f"Row {row_number}: unknown zone {zone!r}")

            rows.append(
                {
                    "awb": awb,
                    "billed_weight_kg": weight.quantize(Decimal("0.001")),
                    "billed_amount": amount.quantize(Decimal("0.01")),
                    "billed_zone": zone,
                    "row_number": row_number,
                }
            )
        except ValueError as exc:
            errors.append(str(exc))

    return rows, errors


@transaction.atomic
def import_lines(invoice, rows, replace_existing=True):
    """Persist parsed rows as invoice lines.

    ``bulk_create`` issues a handful of INSERTs instead of one per row, which is
    the difference between a two-second and a two-minute import on a large file.
    """
    if replace_existing:
        invoice.lines.all().delete()

    InvoiceLine.objects.bulk_create(
        [InvoiceLine(invoice=invoice, **row) for row in rows],
        batch_size=1000,
    )
    return len(rows)
