"""
Freight rating: what a shipment *should* cost under a given rate card.

This module is deliberately free of database access. It takes plain values and
returns a result object. That separation means the pricing rules can be unit
tested at speed without fixtures, and the same function serves both
reconciliation and any future rate-shopping feature.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

PAISA = Decimal("0.01")


def money(value):
    """Round to two decimal places, half-up.

    Python's default rounding is banker's rounding (ROUND_HALF_EVEN), which
    rounds 0.125 to 0.12. Indian invoicing convention -- and every courier we
    reconcile against -- uses half-up. Getting this wrong produces one-paisa
    variances on thousands of rows, which drowns the real discrepancies in noise.
    """
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass
class FreightBreakdown:
    """Itemised cost, so a dispute email can show its working."""

    chargeable_weight_kg: Decimal
    slabs_charged: int
    base_freight: Decimal = Decimal("0.00")
    increment_freight: Decimal = Decimal("0.00")
    rto_freight: Decimal = Decimal("0.00")
    fuel_surcharge: Decimal = Decimal("0.00")
    cod_fee: Decimal = Decimal("0.00")
    notes: list = field(default_factory=list)

    @property
    def freight_before_extras(self):
        return money(self.base_freight + self.increment_freight + self.rto_freight)

    @property
    def total(self):
        return money(self.freight_before_extras + self.fuel_surcharge + self.cod_fee)

    def as_dict(self):
        return {
            "chargeable_weight_kg": str(self.chargeable_weight_kg),
            "slabs_charged": self.slabs_charged,
            "base_freight": str(self.base_freight),
            "increment_freight": str(self.increment_freight),
            "rto_freight": str(self.rto_freight),
            "fuel_surcharge": str(self.fuel_surcharge),
            "cod_fee": str(self.cod_fee),
            "total": str(self.total),
            "notes": list(self.notes),
        }


def slabs_above_base(chargeable_weight, base_weight, increment_weight):
    """How many increment slabs are charged beyond the base weight.

    Couriers charge per *started* slab. At a 0.5 kg increment, a parcel 0.6 kg
    over the base costs two increments, not 1.2. Implemented with integer
    ceiling division on Decimals rather than math.ceil(float) -- converting to
    float here reintroduces exactly the representation error that makes a
    parcel sitting precisely on a slab boundary bill one slab too high.
    """
    if chargeable_weight <= base_weight:
        return 0
    excess = chargeable_weight - base_weight
    whole, remainder = divmod(excess, increment_weight)
    return int(whole) + (1 if remainder > 0 else 0)


def rate_shipment(
    *,
    chargeable_weight_kg,
    base_weight_kg,
    base_rate,
    increment_weight_kg,
    increment_rate,
    fuel_surcharge_pct=Decimal("0"),
    cod_fee_flat=Decimal("0"),
    cod_fee_pct=Decimal("0"),
    order_value=Decimal("0"),
    is_cod=False,
    is_rto=False,
    rto_multiplier=Decimal("1"),
):
    """Compute expected freight for one shipment.

    Order of operations matters and mirrors the contract wording:
      1. Forward freight from the weight slabs.
      2. Return leg, if the parcel came back (a multiple of forward freight).
      3. Fuel surcharge, applied to freight *including* the return leg.
      4. COD fee, applied only on the order value and never surcharged.
    """
    breakdown = FreightBreakdown(
        chargeable_weight_kg=chargeable_weight_kg,
        slabs_charged=slabs_above_base(
            chargeable_weight_kg, base_weight_kg, increment_weight_kg
        ),
    )

    breakdown.base_freight = money(base_rate)
    breakdown.increment_freight = money(breakdown.slabs_charged * increment_rate)

    forward = breakdown.freight_before_extras

    if is_rto:
        # The return leg is charged as a multiple of the forward journey.
        breakdown.rto_freight = money(forward * (rto_multiplier - Decimal("1")))
        breakdown.notes.append(f"RTO leg at {rto_multiplier}x forward freight")

    if fuel_surcharge_pct:
        breakdown.fuel_surcharge = money(
            breakdown.freight_before_extras * fuel_surcharge_pct / Decimal("100")
        )

    if is_cod:
        # Contracts say "the higher of a flat fee or a percentage of order value".
        pct_fee = money(order_value * cod_fee_pct / Decimal("100"))
        breakdown.cod_fee = max(money(cod_fee_flat), pct_fee)
        breakdown.notes.append("COD handling fee applied")

    return breakdown


def rate_from_slab(slab, rate_card, *, chargeable_weight_kg, is_cod, is_rto, order_value):
    """Thin adapter binding ORM objects to the pure ``rate_shipment`` above."""
    return rate_shipment(
        chargeable_weight_kg=chargeable_weight_kg,
        base_weight_kg=slab.base_weight_kg,
        base_rate=slab.base_rate,
        increment_weight_kg=slab.increment_weight_kg,
        increment_rate=slab.increment_rate,
        fuel_surcharge_pct=rate_card.fuel_surcharge_pct,
        cod_fee_flat=rate_card.cod_fee_flat,
        cod_fee_pct=rate_card.cod_fee_pct,
        order_value=order_value,
        is_cod=is_cod,
        is_rto=is_rto,
        rto_multiplier=rate_card.rto_multiplier,
    )
