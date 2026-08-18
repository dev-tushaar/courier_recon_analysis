"""
Tests for the pricing engine.

These are plain SimpleTestCase -- no database, no fixtures -- because
``rating.py`` deliberately takes values rather than model instances. The whole
file runs in milliseconds, which means it actually gets run.

The boundary cases below are the ones that produced real disputes: a parcel
sitting exactly on a slab edge, and a parcel one gram over it.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from reconciliation.services.rating import money, rate_shipment, slabs_above_base

D = Decimal


class SlabCountingTests(SimpleTestCase):
    """Slab arithmetic is where over-billing hides, so it gets its own tests."""

    def test_under_base_weight_charges_no_increments(self):
        self.assertEqual(slabs_above_base(D("0.300"), D("0.500"), D("0.500")), 0)

    def test_exactly_at_base_weight_charges_no_increments(self):
        # The classic off-by-one: 0.5 kg on a 0.5 kg base is *included*.
        self.assertEqual(slabs_above_base(D("0.500"), D("0.500"), D("0.500")), 0)

    def test_one_gram_over_base_charges_a_full_slab(self):
        self.assertEqual(slabs_above_base(D("0.501"), D("0.500"), D("0.500")), 1)

    def test_exactly_on_a_slab_boundary_does_not_round_up(self):
        # 1.5 kg = base 0.5 + exactly two 0.5 kg slabs. Charging three here is
        # the single most common courier overbill.
        self.assertEqual(slabs_above_base(D("1.500"), D("0.500"), D("0.500")), 2)

    def test_partial_slab_is_charged_in_full(self):
        self.assertEqual(slabs_above_base(D("1.600"), D("0.500"), D("0.500")), 3)


class RateShipmentTests(SimpleTestCase):
    BASE = {
        "base_weight_kg": D("0.500"),
        "base_rate": D("50.00"),
        "increment_weight_kg": D("0.500"),
        "increment_rate": D("40.00"),
    }

    def test_base_weight_only(self):
        result = rate_shipment(chargeable_weight_kg=D("0.400"), **self.BASE)
        self.assertEqual(result.total, D("50.00"))
        self.assertEqual(result.slabs_charged, 0)

    def test_increments_are_added(self):
        result = rate_shipment(chargeable_weight_kg=D("1.500"), **self.BASE)
        # 50 base + 2 slabs x 40
        self.assertEqual(result.total, D("130.00"))

    def test_fuel_surcharge_is_a_percentage_of_freight(self):
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            fuel_surcharge_pct=D("20.00"),
            **self.BASE,
        )
        self.assertEqual(result.fuel_surcharge, D("10.00"))
        self.assertEqual(result.total, D("60.00"))

    def test_cod_takes_the_higher_of_flat_or_percentage(self):
        # 2% of 5000 = 100, which beats the 35 flat fee.
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            cod_fee_flat=D("35.00"),
            cod_fee_pct=D("2.00"),
            order_value=D("5000.00"),
            is_cod=True,
            **self.BASE,
        )
        self.assertEqual(result.cod_fee, D("100.00"))

    def test_cod_falls_back_to_flat_fee_on_small_orders(self):
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            cod_fee_flat=D("35.00"),
            cod_fee_pct=D("2.00"),
            order_value=D("500.00"),
            is_cod=True,
            **self.BASE,
        )
        self.assertEqual(result.cod_fee, D("35.00"))

    def test_prepaid_shipment_pays_no_cod_fee(self):
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            cod_fee_flat=D("35.00"),
            order_value=D("5000.00"),
            is_cod=False,
            **self.BASE,
        )
        self.assertEqual(result.cod_fee, D("0.00"))

    def test_rto_adds_the_return_leg(self):
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            is_rto=True,
            rto_multiplier=D("1.50"),
            **self.BASE,
        )
        # 50 forward + 25 return leg
        self.assertEqual(result.total, D("75.00"))

    def test_fuel_surcharge_applies_to_the_return_leg_too(self):
        result = rate_shipment(
            chargeable_weight_kg=D("0.400"),
            is_rto=True,
            rto_multiplier=D("2.00"),
            fuel_surcharge_pct=D("10.00"),
            **self.BASE,
        )
        # freight 100 (50 forward + 50 return), surcharge 10
        self.assertEqual(result.freight_before_extras, D("100.00"))
        self.assertEqual(result.fuel_surcharge, D("10.00"))
        self.assertEqual(result.total, D("110.00"))


class MoneyRoundingTests(SimpleTestCase):
    def test_rounds_half_up_not_bankers(self):
        # Python's default would give 0.12 here. Invoices round up.
        self.assertEqual(money(D("0.125")), D("0.13"))
        self.assertEqual(money(D("0.135")), D("0.14"))

    def test_quantizes_to_two_places(self):
        self.assertEqual(money(D("10.999")), D("11.00"))
