"""
Tests for the reconciliation engine.

Each test plants exactly one kind of billing error and asserts that the engine
finds that error and no other. Building the fixture through a helper keeps each
test focused on the single thing it is checking.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from reconciliation.models import (
    Courier,
    CourierInvoice,
    Discrepancy,
    InvoiceLine,
    PaymentMode,
    RateCard,
    RateSlab,
    Shipment,
    ShipmentStatus,
    Zone,
)
from reconciliation.services.reconciler import reconcile_invoice

D = Decimal


class ReconcilerTestCase(TestCase):
    def setUp(self):
        self.courier = Courier.objects.create(name="Test Courier", code="TEST")
        self.card = RateCard.objects.create(
            courier=self.courier,
            name="Test card",
            effective_from=date.today() - timedelta(days=365),
            volumetric_divisor=5000,
            fuel_surcharge_pct=D("0.00"),
            cod_fee_flat=D("0.00"),
            cod_fee_pct=D("0.00"),
            rto_multiplier=D("1.00"),
        )
        for zone, base_rate in [
            (Zone.A, D("50.00")), (Zone.B, D("60.00")), (Zone.C, D("80.00")),
            (Zone.D, D("100.00")), (Zone.E, D("140.00")),
        ]:
            RateSlab.objects.create(
                rate_card=self.card, zone=zone,
                base_weight_kg=D("0.500"), base_rate=base_rate,
                increment_weight_kg=D("0.500"), increment_rate=D("40.00"),
            )

        self.invoice = CourierInvoice.objects.create(
            courier=self.courier,
            invoice_number="TEST-001",
            invoice_date=date.today(),
            period_start=date.today() - timedelta(days=30),
            period_end=date.today() - timedelta(days=1),
        )

    def make_shipment(self, awb="TEST0001", **kwargs):
        defaults = {
            "order_ref": "ORD-1",
            "courier": self.courier,
            "zone": Zone.C,
            "status": ShipmentStatus.DELIVERED,
            "payment_mode": PaymentMode.PREPAID,
            "order_value": D("1000.00"),
            "actual_weight_kg": D("0.400"),
            "length_cm": D("10.0"), "width_cm": D("10.0"), "height_cm": D("10.0"),
            "shipped_on": date.today() - timedelta(days=15),
        }
        defaults.update(kwargs)
        return Shipment.objects.create(awb=awb, **defaults)

    def make_line(self, awb="TEST0001", weight=D("0.400"), amount=D("80.00"),
                  zone=Zone.C, row=2):
        return InvoiceLine.objects.create(
            invoice=self.invoice, awb=awb, billed_zone=zone,
            billed_weight_kg=weight, billed_amount=amount, row_number=row,
        )

    # -- happy path -------------------------------------------------------

    def test_correctly_billed_line_produces_no_discrepancy(self):
        self.make_shipment()
        self.make_line()

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.lines_checked, 1)
        self.assertEqual(result.lines_matched, 1)
        self.assertEqual(result.discrepancy_count, 0)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, CourierInvoice.Status.RECONCILED)

    def test_sub_rupee_variance_is_ignored_as_rounding_noise(self):
        self.make_shipment()
        self.make_line(amount=D("80.40"))

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.discrepancy_count, 0)

    # -- each error kind --------------------------------------------------

    def test_detects_weight_inflation(self):
        self.make_shipment(actual_weight_kg=D("0.400"))
        # Billed at 1.2 kg -> base + 2 slabs = 160 instead of 80.
        self.make_line(weight=D("1.200"), amount=D("160.00"))

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.discrepancy_count, 1)
        finding = Discrepancy.objects.get()
        self.assertEqual(finding.kind, Discrepancy.Kind.WEIGHT)
        self.assertEqual(finding.amount_impact, D("80.00"))

    def test_small_weight_difference_within_tolerance_is_not_flagged(self):
        # Courier scales differ slightly from ours; 30 g is not a dispute.
        self.make_shipment(actual_weight_kg=D("0.400"))
        self.make_line(weight=D("0.430"), amount=D("80.00"))

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.discrepancy_count, 0)

    def test_detects_zone_upgrade(self):
        self.make_shipment(zone=Zone.C)
        # Billed as zone E (140) rather than zone C (80).
        self.make_line(zone=Zone.E, amount=D("140.00"))

        result = reconcile_invoice(self.invoice)

        finding = Discrepancy.objects.get()
        self.assertEqual(finding.kind, Discrepancy.Kind.ZONE)
        self.assertEqual(finding.amount_impact, D("60.00"))

    def test_detects_rate_error_when_weight_and_zone_are_correct(self):
        self.make_shipment()
        self.make_line(amount=D("95.00"))  # should be 80

        finding = Discrepancy.objects.none()
        reconcile_invoice(self.invoice)

        finding = Discrepancy.objects.get()
        self.assertEqual(finding.kind, Discrepancy.Kind.RATE)
        self.assertEqual(finding.amount_impact, D("15.00"))

    def test_detects_duplicate_awb(self):
        self.make_shipment()
        self.make_line(row=2)
        self.make_line(row=3)  # same AWB billed twice

        result = reconcile_invoice(self.invoice)

        kinds = list(
            Discrepancy.objects.values_list("kind", flat=True)
        )
        self.assertEqual(kinds, [Discrepancy.Kind.DUPLICATE])
        # The first occurrence is legitimate; only the repeat is claimed back.
        self.assertEqual(result.total_overcharge, D("80.00"))

    def test_detects_unknown_awb(self):
        self.make_line(awb="NOTOURS999", amount=D("120.00"))

        reconcile_invoice(self.invoice)

        finding = Discrepancy.objects.get()
        self.assertEqual(finding.kind, Discrepancy.Kind.UNKNOWN_AWB)
        self.assertEqual(finding.amount_impact, D("120.00"))

    def test_flags_missing_rate_card(self):
        self.card.effective_to = date.today() - timedelta(days=200)
        self.card.save()
        self.make_shipment()
        self.make_line()

        reconcile_invoice(self.invoice)

        finding = Discrepancy.objects.get()
        self.assertEqual(finding.kind, Discrepancy.Kind.NO_RATE_CARD)

    # -- pricing rules flowing through ------------------------------------

    def test_volumetric_weight_drives_pricing_for_bulky_parcels(self):
        # 40x40x40 = 64000 cm3 / 5000 = 12.8 kg volumetric vs 0.4 kg actual.
        self.make_shipment(
            actual_weight_kg=D("0.400"),
            length_cm=D("40.0"), width_cm=D("40.0"), height_cm=D("40.0"),
        )
        # base 80 + ceil((12.8 - 0.5)/0.5)=25 slabs x 40 = 1080
        self.make_line(weight=D("12.800"), amount=D("1080.00"))

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.discrepancy_count, 0)

    def test_rto_shipment_expects_the_return_leg(self):
        self.card.rto_multiplier = D("1.50")
        self.card.save()
        self.make_shipment(status=ShipmentStatus.RTO)
        self.make_line(amount=D("120.00"))  # 80 x 1.5

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.discrepancy_count, 0)

    def test_undercharge_is_reported_separately_from_overcharge(self):
        self.make_shipment()
        self.make_line(amount=D("60.00"))  # courier billed less than contract

        result = reconcile_invoice(self.invoice)

        self.assertEqual(result.total_undercharge, D("20.00"))
        self.assertEqual(result.total_overcharge, D("0.00"))
        self.assertEqual(result.net_impact, D("-20.00"))

    # -- engine behaviour --------------------------------------------------

    def test_reconciliation_is_idempotent(self):
        """Re-running must not double up findings."""
        self.make_shipment()
        self.make_line(weight=D("1.200"), amount=D("160.00"))

        reconcile_invoice(self.invoice)
        first = Discrepancy.objects.count()
        reconcile_invoice(self.invoice)
        second = Discrepancy.objects.count()

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    def test_invoice_is_marked_disputed_when_findings_exist(self):
        self.make_shipment()
        self.make_line(amount=D("999.00"))

        reconcile_invoice(self.invoice)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, CourierInvoice.Status.DISPUTED)

    def test_matched_lines_are_linked_back_to_their_shipment(self):
        shipment = self.make_shipment()
        line = self.make_line()

        reconcile_invoice(self.invoice)

        line.refresh_from_db()
        self.assertEqual(line.shipment, shipment)
        self.assertEqual(line.expected_amount, D("80.00"))

    def test_query_count_does_not_grow_with_invoice_size(self):
        """Guards against reintroducing an N+1 in the reconciler.

        Shipments, the rate card and its slabs are all resolved in bulk before
        the loop, and writes are batched afterwards, so the query count is a
        constant. Asserting a fixed number would be brittle; asserting that
        four times the data costs the same number of queries is the property
        that actually matters. If someone moves a lookup back inside the loop,
        this fails immediately rather than being discovered on a 20,000-row
        invoice in production.
        """
        def build(prefix, n, invoice):
            for i in range(n):
                self.make_shipment(awb=f"{prefix}{i:04d}", order_ref=f"ORD-{prefix}{i}")
                InvoiceLine.objects.create(
                    invoice=invoice, awb=f"{prefix}{i:04d}", billed_zone=Zone.C,
                    billed_weight_kg=D("0.400"), billed_amount=D("80.00"),
                    row_number=i + 2,
                )

        build("SMALL", 5, self.invoice)
        with self.assertNumQueries(9):
            reconcile_invoice(self.invoice)

        big = CourierInvoice.objects.create(
            courier=self.courier, invoice_number="TEST-002",
            invoice_date=date.today(),
            period_start=date.today() - timedelta(days=30),
            period_end=date.today() - timedelta(days=1),
        )
        build("BIG", 20, big)
        with self.assertNumQueries(9):
            reconcile_invoice(big)
