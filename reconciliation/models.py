"""
Domain models for courier invoice reconciliation.

The model layer is split into three groups:

1. Reference data   -- Courier, RateCard, RateSlab
                       What a shipment *should* cost, per the contract.
2. Operational data -- Shipment
                       What we actually shipped, measured on our side.
3. Billing data     -- CourierInvoice, InvoiceLine, Discrepancy
                       What the courier claims we owe, and where the two disagree.

Money is stored as DecimalField throughout. Floats are never used for currency:
0.1 + 0.2 != 0.3 in binary floating point, and reconciliation is precisely the
place where sub-paisa drift accumulates into a wrong dispute total.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Sum


class Zone(models.TextChoices):
    """Delivery zones. Courier contracts price by zone, not by distance."""

    A = "A", "Zone A - Intra-city"
    B = "B", "Zone B - Intra-state"
    C = "C", "Zone C - Metro to metro"
    D = "D", "Zone D - Rest of India"
    E = "E", "Zone E - Special (NE, J&K, islands)"


class PaymentMode(models.TextChoices):
    PREPAID = "PREPAID", "Prepaid"
    COD = "COD", "Cash on delivery"


class ShipmentStatus(models.TextChoices):
    IN_TRANSIT = "IN_TRANSIT", "In transit"
    DELIVERED = "DELIVERED", "Delivered"
    RTO = "RTO", "Returned to origin"
    LOST = "LOST", "Lost / damaged"


# ---------------------------------------------------------------------------
# 1. Reference data
# ---------------------------------------------------------------------------


class Courier(models.Model):
    """A courier partner we ship with."""

    name = models.CharField(max_length=100, unique=True)
    code = models.SlugField(max_length=20, unique=True, help_text="Short code, e.g. 'DLVRY'")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def current_rate_card(self, on_date):
        """Return the rate card in force on ``on_date``, or None.

        Contracts get renegotiated, so rate cards are versioned by date rather
        than overwritten. An invoice from March must be checked against March's
        pricing even if we re-signed in June.
        """
        return (
            self.rate_cards.filter(effective_from__lte=on_date)
            .filter(models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=on_date))
            .order_by("-effective_from")
            .first()
        )


class RateCard(models.Model):
    """A versioned pricing contract with a courier."""

    courier = models.ForeignKey(Courier, on_delete=models.CASCADE, related_name="rate_cards")
    name = models.CharField(max_length=120)
    effective_from = models.DateField()
    effective_to = models.DateField(
        null=True, blank=True, help_text="Leave blank while this card is current."
    )

    # Volumetric divisor: chargeable weight considers parcel bulk, not just mass.
    # A pillow weighs little but occupies a lot of van.
    volumetric_divisor = models.PositiveIntegerField(
        default=5000, help_text="cm3 per kg. Industry standard is 5000."
    )
    fuel_surcharge_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        help_text="Percentage applied on top of freight.",
    )
    cod_fee_flat = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    cod_fee_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        help_text="Percent of order value. The higher of flat/pct applies.",
    )
    rto_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("1.00"),
        help_text="Return leg cost as a multiple of forward freight.",
    )

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=F("effective_from")),
                name="ratecard_valid_date_range",
            )
        ]

    def __str__(self):
        return f"{self.courier.code} · {self.name}"

    def slab_for_zone(self, zone):
        return self.slabs.filter(zone=zone).first()


class RateSlab(models.Model):
    """Per-zone pricing within a rate card.

    Couriers price as: a base rate covering the first ``base_weight_kg``, then a
    fixed charge for each additional slab of ``increment_weight_kg``. The
    increment is charged per *started* slab, not pro-rata -- 0.1 kg over the
    base costs a full increment. That rounding is where a lot of disputes live.
    """

    rate_card = models.ForeignKey(RateCard, on_delete=models.CASCADE, related_name="slabs")
    zone = models.CharField(max_length=1, choices=Zone.choices)

    base_weight_kg = models.DecimalField(
        max_digits=6, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    base_rate = models.DecimalField(max_digits=8, decimal_places=2)
    increment_weight_kg = models.DecimalField(
        max_digits=6, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    increment_rate = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        ordering = ["zone"]
        constraints = [
            models.UniqueConstraint(
                fields=["rate_card", "zone"], name="unique_zone_per_rate_card"
            )
        ]

    def __str__(self):
        return f"{self.rate_card} · Zone {self.zone}"


# ---------------------------------------------------------------------------
# 2. Operational data
# ---------------------------------------------------------------------------


class Shipment(models.Model):
    """A parcel we dispatched. This is our side of the truth.

    Weight and dimensions here come from our own packing station, which is what
    gives us standing to dispute a courier's billed weight.
    """

    awb = models.CharField(
        "AWB number", max_length=50, unique=True, db_index=True,
        help_text="Air waybill / tracking number. The join key against courier invoices.",
    )
    order_ref = models.CharField(max_length=50, db_index=True)
    courier = models.ForeignKey(Courier, on_delete=models.PROTECT, related_name="shipments")

    zone = models.CharField(max_length=1, choices=Zone.choices)
    status = models.CharField(
        max_length=12, choices=ShipmentStatus.choices, default=ShipmentStatus.IN_TRANSIT
    )
    payment_mode = models.CharField(
        max_length=8, choices=PaymentMode.choices, default=PaymentMode.PREPAID
    )
    order_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    actual_weight_kg = models.DecimalField(max_digits=6, decimal_places=3)
    length_cm = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal("0.0"))
    width_cm = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal("0.0"))
    height_cm = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal("0.0"))

    shipped_on = models.DateField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-shipped_on", "awb"]
        indexes = [
            # Reconciliation filters by courier + date window on every run, and
            # the dashboard groups by zone. Composite indexes here keep those
            # queries off a sequential scan as the table grows.
            models.Index(fields=["courier", "shipped_on"], name="ship_courier_date_idx"),
            models.Index(fields=["zone", "status"], name="ship_zone_status_idx"),
        ]

    def __str__(self):
        return f"{self.awb} ({self.courier.code})"

    def volumetric_weight_kg(self, divisor):
        """Bulk expressed as weight: L x W x H / divisor."""
        volume = self.length_cm * self.width_cm * self.height_cm
        if not volume:
            return Decimal("0.000")
        return (volume / Decimal(divisor)).quantize(Decimal("0.001"))

    def chargeable_weight_kg(self, divisor):
        """Couriers bill on whichever is greater: dead weight or volumetric."""
        return max(self.actual_weight_kg, self.volumetric_weight_kg(divisor))


# ---------------------------------------------------------------------------
# 3. Billing data
# ---------------------------------------------------------------------------


class CourierInvoice(models.Model):
    """An invoice document received from a courier."""

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        RECONCILED = "RECONCILED", "Reconciled"
        DISPUTED = "DISPUTED", "Disputed"
        CLOSED = "CLOSED", "Closed"

    courier = models.ForeignKey(Courier, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=60)
    invoice_date = models.DateField()
    period_start = models.DateField()
    period_end = models.DateField()

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UPLOADED)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-invoice_date"]
        constraints = [
            # The same invoice number can legitimately recur across couriers,
            # so uniqueness is scoped to the pair. Enforced in the database
            # rather than in application code, because a re-upload race would
            # slip past a view-level check.
            models.UniqueConstraint(
                fields=["courier", "invoice_number"], name="unique_invoice_per_courier"
            )
        ]

    def __str__(self):
        return f"{self.courier.code} · {self.invoice_number}"

    @property
    def total_billed(self):
        return self.lines.aggregate(t=Sum("billed_amount"))["t"] or Decimal("0.00")

    @property
    def total_expected(self):
        return self.lines.aggregate(t=Sum("expected_amount"))["t"] or Decimal("0.00")

    @property
    def total_variance(self):
        return self.total_billed - self.total_expected


class InvoiceLine(models.Model):
    """One billed row from a courier invoice, keyed by AWB.

    ``shipment`` is nullable on purpose: couriers routinely bill for AWBs we
    have no record of. Refusing to import those rows would hide the problem;
    storing them unmatched surfaces it as a discrepancy.
    """

    invoice = models.ForeignKey(CourierInvoice, on_delete=models.CASCADE, related_name="lines")
    shipment = models.ForeignKey(
        Shipment, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoice_lines"
    )

    awb = models.CharField(max_length=50, db_index=True)
    billed_zone = models.CharField(max_length=1, choices=Zone.choices, blank=True)
    billed_weight_kg = models.DecimalField(max_digits=6, decimal_places=3)
    billed_amount = models.DecimalField(max_digits=10, decimal_places=2)

    expected_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Computed by the reconciler from the rate card.",
    )
    row_number = models.PositiveIntegerField(
        default=0, help_text="Source row in the uploaded CSV, for traceability."
    )

    class Meta:
        ordering = ["row_number", "awb"]
        indexes = [models.Index(fields=["invoice", "awb"], name="line_invoice_awb_idx")]

    def __str__(self):
        return f"{self.awb} @ {self.billed_amount}"

    @property
    def variance(self):
        if self.expected_amount is None:
            return None
        return self.billed_amount - self.expected_amount


class Discrepancy(models.Model):
    """A single reconciliation finding against an invoice line."""

    class Kind(models.TextChoices):
        WEIGHT = "WEIGHT", "Weight mismatch"
        ZONE = "ZONE", "Zone mismatch"
        RATE = "RATE", "Rate applied incorrectly"
        DUPLICATE = "DUPLICATE", "Duplicate AWB billed"
        UNKNOWN_AWB = "UNKNOWN_AWB", "AWB not in our system"
        NO_RATE_CARD = "NO_RATE_CARD", "No rate card covers this date"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DISPUTED = "DISPUTED", "Raised with courier"
        RECOVERED = "RECOVERED", "Credit note received"
        WAIVED = "WAIVED", "Waived internally"

    line = models.ForeignKey(InvoiceLine, on_delete=models.CASCADE, related_name="discrepancies")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    detail = models.TextField(help_text="Human-readable explanation for the dispute email.")
    amount_impact = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        help_text="Positive = courier overcharged us.",
    )
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-amount_impact"]
        verbose_name_plural = "discrepancies"
        indexes = [models.Index(fields=["kind", "status"], name="disc_kind_status_idx")]

    def __str__(self):
        return f"{self.get_kind_display()} on {self.line.awb}"
