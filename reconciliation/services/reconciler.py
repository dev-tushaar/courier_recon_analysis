"""
The reconciliation engine.

For each invoice line it answers: does what the courier billed match what the
contract says they should have billed, and if not, precisely why?

Design notes
------------
* The whole run is wrapped in a single transaction. A partially reconciled
  invoice is worse than an unreconciled one, because it looks finished.
* Shipments are fetched once into a dict keyed by AWB rather than queried per
  line. On a 20,000-row invoice the naive version issues 20,000 queries; this
  issues one. Classic N+1, and the first thing that bites at real volume.
* Every finding carries the arithmetic in ``detail``. Disputes get sent to the
  courier's finance team, who will not accept "your system says so".
"""

from collections import Counter
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import Discrepancy, InvoiceLine, Shipment, ShipmentStatus
from .rating import money, rate_from_slab

# Below this rupee value a variance is treated as rounding noise and ignored.
# Without a tolerance, half-paisa differences in surcharge rounding would raise
# thousands of meaningless discrepancies and nobody would read the report.
MATERIALITY_THRESHOLD = Decimal("1.00")

# Couriers weigh on their own machines; a small delta is normal and not worth
# disputing. Beyond this, it is a systematic overbill rather than instrument drift.
WEIGHT_TOLERANCE_KG = Decimal("0.050")


class ReconciliationResult:
    """Summary of one reconciliation run."""

    def __init__(self):
        self.lines_checked = 0
        self.lines_matched = 0
        self.discrepancy_counts = Counter()
        self.total_overcharge = Decimal("0.00")
        self.total_undercharge = Decimal("0.00")

    @property
    def discrepancy_count(self):
        return sum(self.discrepancy_counts.values())

    @property
    def net_impact(self):
        return self.total_overcharge - self.total_undercharge

    def as_dict(self):
        return {
            "lines_checked": self.lines_checked,
            "lines_matched": self.lines_matched,
            "discrepancy_count": self.discrepancy_count,
            "by_kind": dict(self.discrepancy_counts),
            "total_overcharge": str(self.total_overcharge),
            "total_undercharge": str(self.total_undercharge),
            "net_impact": str(self.net_impact),
        }


@transaction.atomic
def reconcile_invoice(invoice):
    """Re-run reconciliation for a whole invoice and return a result summary."""
    result = ReconciliationResult()

    lines = list(invoice.lines.all())

    # Clear prior findings so a re-run is idempotent rather than additive.
    Discrepancy.objects.filter(line__invoice=invoice).delete()

    # --- Resolve everything the loop needs, up front -----------------------
    # Three bulk reads replace three-per-line reads. On a 20,000-line invoice
    # that is the difference between ~6 queries and ~60,000.
    awbs = [line.awb for line in lines]
    shipment_by_awb = {
        s.awb: s
        for s in Shipment.objects.filter(awb__in=awbs).select_related("courier")
    }

    rate_card = invoice.courier.current_rate_card(invoice.period_end)
    slab_by_zone = (
        {slab.zone: slab for slab in rate_card.slabs.all()} if rate_card else {}
    )

    # An AWB appearing on more than one line of the same invoice is a duplicate
    # bill. Counted up front so we can flag every occurrence after the first.
    awb_counts = Counter(awbs)
    seen = Counter()

    all_findings = []

    for line in lines:
        result.lines_checked += 1
        findings = []

        shipment = shipment_by_awb.get(line.awb)
        line.shipment = shipment

        seen[line.awb] += 1
        is_repeat = seen[line.awb] > 1

        if is_repeat:
            findings.append(
                Discrepancy(
                    line=line,
                    kind=Discrepancy.Kind.DUPLICATE,
                    detail=(
                        f"AWB {line.awb} appears {awb_counts[line.awb]} times on this "
                        f"invoice. This is occurrence {seen[line.awb]}; the full billed "
                        f"amount of Rs {line.billed_amount} is claimed in duplicate."
                    ),
                    amount_impact=line.billed_amount,
                )
            )
            line.expected_amount = Decimal("0.00")

        elif shipment is None:
            findings.append(
                Discrepancy(
                    line=line,
                    kind=Discrepancy.Kind.UNKNOWN_AWB,
                    detail=(
                        f"AWB {line.awb} was billed Rs {line.billed_amount} but does not "
                        f"exist in our shipment records. Either the courier has billed us "
                        f"for another client's parcel, or the AWB was never handed over."
                    ),
                    amount_impact=line.billed_amount,
                )
            )
            line.expected_amount = Decimal("0.00")

        elif rate_card is None:
            findings.append(
                Discrepancy(
                    line=line,
                    kind=Discrepancy.Kind.NO_RATE_CARD,
                    detail=(
                        f"No rate card is effective for {invoice.courier.name} on "
                        f"{invoice.period_end}. Expected cost cannot be computed."
                    ),
                    amount_impact=Decimal("0.00"),
                )
            )
            line.expected_amount = None

        else:
            findings.extend(
                _check_priced_line(line, shipment, rate_card, slab_by_zone)
            )

        if findings:
            all_findings.extend(findings)
            for f in findings:
                result.discrepancy_counts[f.kind] += 1
                if f.amount_impact > 0:
                    result.total_overcharge += f.amount_impact
                elif f.amount_impact < 0:
                    result.total_undercharge += abs(f.amount_impact)
        else:
            result.lines_matched += 1

    # One write for all lines and one for all findings, rather than two per line.
    InvoiceLine.objects.bulk_update(
        lines, ["shipment", "expected_amount"], batch_size=1000
    )
    if all_findings:
        Discrepancy.objects.bulk_create(all_findings, batch_size=1000)

    invoice.status = (
        invoice.Status.DISPUTED if result.discrepancy_count else invoice.Status.RECONCILED
    )
    invoice.reconciled_at = timezone.now()
    invoice.save(update_fields=["status", "reconciled_at"])

    return result


def _check_priced_line(line, shipment, rate_card, slab_by_zone):
    """Compare one matched line against the contract. Returns Discrepancy objects."""
    findings = []

    divisor = rate_card.volumetric_divisor
    our_chargeable = shipment.chargeable_weight_kg(divisor)

    # --- Weight ------------------------------------------------------------
    weight_delta = line.billed_weight_kg - our_chargeable
    if weight_delta > WEIGHT_TOLERANCE_KG:
        volumetric = shipment.volumetric_weight_kg(divisor)
        findings.append(
            Discrepancy(
                line=line,
                kind=Discrepancy.Kind.WEIGHT,
                detail=(
                    f"Billed weight {line.billed_weight_kg} kg exceeds our chargeable "
                    f"weight {our_chargeable} kg by {weight_delta} kg. "
                    f"Our measurement: actual {shipment.actual_weight_kg} kg, "
                    f"volumetric {volumetric} kg "
                    f"({shipment.length_cm}x{shipment.width_cm}x{shipment.height_cm} cm "
                    f"/ {divisor})."
                ),
                # Priced below once we know the rupee effect of the whole line.
                amount_impact=Decimal("0.00"),
            )
        )

    # --- Zone --------------------------------------------------------------
    zone_for_pricing = shipment.zone
    if line.billed_zone and line.billed_zone != shipment.zone:
        findings.append(
            Discrepancy(
                line=line,
                kind=Discrepancy.Kind.ZONE,
                detail=(
                    f"Billed as zone {line.billed_zone} but the delivery pincode places "
                    f"this shipment in zone {shipment.zone}."
                ),
                amount_impact=Decimal("0.00"),
            )
        )

    # --- Expected price ----------------------------------------------------
    slab = slab_by_zone.get(zone_for_pricing)
    if slab is None:
        findings.append(
            Discrepancy(
                line=line,
                kind=Discrepancy.Kind.NO_RATE_CARD,
                detail=(
                    f"Rate card '{rate_card.name}' has no slab defined for zone "
                    f"{zone_for_pricing}."
                ),
                amount_impact=Decimal("0.00"),
            )
        )
        line.expected_amount = None
        return findings

    breakdown = rate_from_slab(
        slab,
        rate_card,
        chargeable_weight_kg=our_chargeable,
        is_cod=shipment.payment_mode == "COD",
        is_rto=shipment.status == ShipmentStatus.RTO,
        order_value=shipment.order_value,
    )
    line.expected_amount = breakdown.total

    variance = money(line.billed_amount - breakdown.total)

    if abs(variance) >= MATERIALITY_THRESHOLD:
        if findings:
            # The weight or zone error already explains the variance. Attribute
            # the rupee impact to that root cause rather than raising a second,
            # double-counted RATE finding for the same money.
            findings[0].amount_impact = variance
        else:
            findings.append(
                Discrepancy(
                    line=line,
                    kind=Discrepancy.Kind.RATE,
                    detail=(
                        f"Weight and zone agree, but the amount does not. "
                        f"Billed Rs {line.billed_amount}, expected Rs {breakdown.total} "
                        f"(base Rs {breakdown.base_freight} + "
                        f"{breakdown.slabs_charged} increment slab(s) "
                        f"Rs {breakdown.increment_freight} + RTO Rs {breakdown.rto_freight} "
                        f"+ fuel Rs {breakdown.fuel_surcharge} + COD Rs {breakdown.cod_fee}). "
                        f"Variance Rs {variance}."
                    ),
                    amount_impact=variance,
                )
            )
    elif findings:
        # Weight or zone is wrong but the rupee effect is immaterial -- worth
        # flagging to the courier as a data-quality issue, not as a claim.
        findings[0].amount_impact = Decimal("0.00")

    return findings
