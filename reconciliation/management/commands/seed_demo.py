"""
Seed realistic demo data.

Generates shipments, rate cards and invoices where a controlled proportion of
lines contain *deliberate* billing errors -- so the reconciler has something to
find and the numbers on the dashboard mean something.

    python manage.py seed_demo --shipments 800

The random seed is fixed by default so runs are reproducible; pass --random for
fresh data.
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from reconciliation.models import (
    Courier,
    CourierInvoice,
    InvoiceLine,
    PaymentMode,
    RateCard,
    RateSlab,
    Shipment,
    ShipmentStatus,
    Zone,
)
from reconciliation.services.rating import money, rate_from_slab
from reconciliation.services.reconciler import reconcile_invoice

COURIERS = [
    ("Delhivery", "DLVRY", Decimal("18.00"), Decimal("1.60")),
    ("Blue Dart", "BLUDT", Decimal("22.00"), Decimal("1.75")),
    ("Xpressbees", "XPRSB", Decimal("15.00"), Decimal("1.50")),
]

# zone -> (base_weight, base_rate, increment_weight, increment_rate)
ZONE_PRICING = {
    Zone.A: (Decimal("0.500"), Decimal("32.00"), Decimal("0.500"), Decimal("28.00")),
    Zone.B: (Decimal("0.500"), Decimal("42.00"), Decimal("0.500"), Decimal("36.00")),
    Zone.C: (Decimal("0.500"), Decimal("58.00"), Decimal("0.500"), Decimal("48.00")),
    Zone.D: (Decimal("0.500"), Decimal("72.00"), Decimal("0.500"), Decimal("62.00")),
    Zone.E: (Decimal("0.500"), Decimal("96.00"), Decimal("0.500"), Decimal("84.00")),
}


class Command(BaseCommand):
    help = "Create demo couriers, rate cards, shipments and invoices."

    def add_arguments(self, parser):
        parser.add_argument("--shipments", type=int, default=600)
        parser.add_argument("--random", action="store_true",
                            help="Do not fix the RNG seed.")
        parser.add_argument("--flush", action="store_true",
                            help="Delete existing demo data first.")

    @transaction.atomic
    def handle(self, *args, **options):
        if not options["random"]:
            random.seed(42)

        if options["flush"]:
            CourierInvoice.objects.all().delete()
            Shipment.objects.all().delete()
            Courier.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing data deleted."))

        couriers = self._create_couriers()
        shipments = self._create_shipments(couriers, options["shipments"])
        self.stdout.write(self.style.SUCCESS(f"Created {len(shipments)} shipments."))

        total_errors = 0
        for courier in couriers:
            invoice, planted = self._create_invoice(courier)
            result = reconcile_invoice(invoice)
            total_errors += planted
            self.stdout.write(
                f"  {courier.code}: {result.lines_checked} lines, "
                f"{planted} errors planted, {result.discrepancy_count} detected, "
                f"net impact Rs {result.net_impact}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {total_errors} billing errors planted across "
            f"{len(couriers)} invoices."
        ))
        self.stdout.write("Create a login with: python manage.py createsuperuser")

    # -- builders ---------------------------------------------------------

    def _create_couriers(self):
        couriers = []
        for name, code, fuel, rto in COURIERS:
            courier, _ = Courier.objects.get_or_create(
                code=code, defaults={"name": name}
            )
            card, created = RateCard.objects.get_or_create(
                courier=courier,
                name="FY26 contract",
                defaults={
                    "effective_from": date.today() - timedelta(days=365),
                    "effective_to": None,
                    "fuel_surcharge_pct": fuel,
                    "cod_fee_flat": Decimal("35.00"),
                    "cod_fee_pct": Decimal("1.50"),
                    "rto_multiplier": rto,
                },
            )
            if created:
                for zone, (bw, br, iw, ir) in ZONE_PRICING.items():
                    # Each courier prices slightly differently around a common base.
                    jitter = Decimal(random.randint(-4, 6))
                    RateSlab.objects.create(
                        rate_card=card, zone=zone,
                        base_weight_kg=bw, base_rate=br + jitter,
                        increment_weight_kg=iw, increment_rate=ir + jitter,
                    )
            couriers.append(courier)
        return couriers

    def _create_shipments(self, couriers, count):
        zones = list(ZONE_PRICING.keys())
        # Weighted so most parcels are metro-to-metro, as in a real D2C mix.
        zone_weights = [0.15, 0.25, 0.35, 0.20, 0.05]
        shipments = []

        for i in range(count):
            courier = random.choice(couriers)
            zone = random.choices(zones, weights=zone_weights)[0]

            # Bimodal weights: lots of small accessories, some bulky items.
            if random.random() < 0.7:
                weight = Decimal(str(round(random.uniform(0.2, 1.5), 3)))
                dims = [random.randint(10, 25) for _ in range(3)]
            else:
                weight = Decimal(str(round(random.uniform(1.5, 8.0), 3)))
                dims = [random.randint(25, 55) for _ in range(3)]

            status = random.choices(
                [ShipmentStatus.DELIVERED, ShipmentStatus.RTO, ShipmentStatus.IN_TRANSIT],
                weights=[0.86, 0.11, 0.03],
            )[0]
            payment = random.choices(
                [PaymentMode.PREPAID, PaymentMode.COD], weights=[0.65, 0.35]
            )[0]

            shipments.append(
                Shipment(
                    awb=f"{courier.code}{100000 + i}",
                    order_ref=f"FR-{20000 + i}",
                    courier=courier,
                    zone=zone,
                    status=status,
                    payment_mode=payment,
                    order_value=Decimal(str(random.randint(499, 4999))),
                    actual_weight_kg=weight,
                    length_cm=Decimal(dims[0]),
                    width_cm=Decimal(dims[1]),
                    height_cm=Decimal(dims[2]),
                    shipped_on=date.today() - timedelta(days=random.randint(31, 60)),
                )
            )

        Shipment.objects.bulk_create(shipments, batch_size=500)
        return shipments

    def _create_invoice(self, courier):
        """Build an invoice, mostly correct, with planted errors."""
        period_end = date.today() - timedelta(days=30)
        period_start = period_end - timedelta(days=30)

        invoice = CourierInvoice.objects.create(
            courier=courier,
            invoice_number=f"{courier.code}/{period_end.strftime('%Y%m')}/001",
            invoice_date=period_end + timedelta(days=3),
            period_start=period_start,
            period_end=period_end,
        )

        rate_card = courier.current_rate_card(period_end)
        shipments = list(courier.shipments.all())
        lines = []
        planted = 0
        row = 2

        for shipment in shipments:
            slab = rate_card.slab_for_zone(shipment.zone)
            correct = rate_from_slab(
                slab, rate_card,
                chargeable_weight_kg=shipment.chargeable_weight_kg(
                    rate_card.volumetric_divisor
                ),
                is_cod=shipment.payment_mode == PaymentMode.COD,
                is_rto=shipment.status == ShipmentStatus.RTO,
                order_value=shipment.order_value,
            )

            billed_weight = shipment.chargeable_weight_kg(rate_card.volumetric_divisor)
            billed_zone = shipment.zone
            billed_amount = correct.total

            roll = random.random()

            if roll < 0.08:
                # Weight inflation -- the most common real overbill.
                planted += 1
                billed_weight = (billed_weight + Decimal(
                    str(round(random.uniform(0.25, 1.2), 3))
                )).quantize(Decimal("0.001"))
                inflated = rate_from_slab(
                    slab, rate_card,
                    chargeable_weight_kg=billed_weight,
                    is_cod=shipment.payment_mode == PaymentMode.COD,
                    is_rto=shipment.status == ShipmentStatus.RTO,
                    order_value=shipment.order_value,
                )
                billed_amount = inflated.total

            elif roll < 0.12:
                # Zone upgrade -- billed as a farther, pricier zone.
                planted += 1
                worse = {Zone.A: Zone.C, Zone.B: Zone.D, Zone.C: Zone.D,
                         Zone.D: Zone.E, Zone.E: Zone.E}[shipment.zone]
                billed_zone = worse
                worse_slab = rate_card.slab_for_zone(worse)
                upgraded = rate_from_slab(
                    worse_slab, rate_card,
                    chargeable_weight_kg=billed_weight,
                    is_cod=shipment.payment_mode == PaymentMode.COD,
                    is_rto=shipment.status == ShipmentStatus.RTO,
                    order_value=shipment.order_value,
                )
                billed_amount = upgraded.total

            elif roll < 0.15:
                # Silent rate error: right weight and zone, wrong arithmetic.
                planted += 1
                billed_amount = money(correct.total * Decimal("1.18"))

            lines.append(InvoiceLine(
                invoice=invoice, awb=shipment.awb, billed_zone=billed_zone,
                billed_weight_kg=billed_weight, billed_amount=billed_amount,
                row_number=row,
            ))
            row += 1

        # Plant a handful of duplicate rows and phantom AWBs.
        for dup in random.sample(lines, min(4, len(lines))):
            planted += 1
            lines.append(InvoiceLine(
                invoice=invoice, awb=dup.awb, billed_zone=dup.billed_zone,
                billed_weight_kg=dup.billed_weight_kg,
                billed_amount=dup.billed_amount, row_number=row,
            ))
            row += 1

        for n in range(3):
            planted += 1
            lines.append(InvoiceLine(
                invoice=invoice, awb=f"GHOST{courier.code}{n:03d}",
                billed_zone=Zone.C, billed_weight_kg=Decimal("1.000"),
                billed_amount=Decimal(str(random.randint(80, 260))), row_number=row,
            ))
            row += 1

        InvoiceLine.objects.bulk_create(lines, batch_size=500)
        return invoice, planted
