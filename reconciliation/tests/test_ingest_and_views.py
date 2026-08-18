"""Tests for CSV parsing and the HTTP layer."""

import io
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import (
    Courier,
    CourierInvoice,
    Discrepancy,
    InvoiceLine,
    RateCard,
    RateSlab,
    Shipment,
    Zone,
)
from reconciliation.services.ingest import IngestError, parse_invoice_csv

D = Decimal


def csv_file(text):
    return io.StringIO(text)


class ParseInvoiceCsvTests(TestCase):
    def test_parses_a_clean_file(self):
        rows, errors = parse_invoice_csv(csv_file(
            "awb,zone,weight_kg,amount\n"
            "ABC123,C,1.250,148.50\n"
            "ABC124,B,0.480,92.00\n"
        ))
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["awb"], "ABC123")
        self.assertEqual(rows[0]["billed_amount"], D("148.50"))

    def test_accepts_alternative_header_names(self):
        rows, _ = parse_invoice_csv(csv_file(
            "AWB No,Charged Weight,Billed Amount\nXYZ1,2.5,300\n"
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["billed_weight_kg"], D("2.500"))

    def test_strips_currency_symbols_and_separators(self):
        rows, errors = parse_invoice_csv(csv_file(
            "awb,weight_kg,amount\nABC1,1.0,\"Rs 1,234.55\"\n"
        ))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["billed_amount"], D("1234.55"))

    def test_handles_excel_byte_order_mark(self):
        rows, _ = parse_invoice_csv(csv_file(
            "\ufeffawb,weight_kg,amount\nABC1,1.0,100\n"
        ))
        self.assertEqual(len(rows), 1)

    def test_skips_blank_trailing_rows(self):
        rows, errors = parse_invoice_csv(csv_file(
            "awb,weight_kg,amount\nABC1,1.0,100\n,,\n,,\n"
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])

    def test_reports_bad_rows_without_failing_the_file(self):
        rows, errors = parse_invoice_csv(csv_file(
            "awb,weight_kg,amount\n"
            "GOOD1,1.0,100\n"
            "BAD1,not-a-number,100\n"
            "GOOD2,2.0,200\n"
        ))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("Row 3", errors[0])

    def test_rejects_a_file_missing_required_columns(self):
        with self.assertRaises(IngestError) as ctx:
            parse_invoice_csv(csv_file("awb,zone\nABC1,C\n"))
        self.assertIn("Missing required column", str(ctx.exception))

    def test_rejects_an_empty_file(self):
        with self.assertRaises(IngestError):
            parse_invoice_csv(csv_file(""))

    def test_rejects_an_unknown_zone(self):
        _, errors = parse_invoice_csv(csv_file(
            "awb,zone,weight_kg,amount\nABC1,Z,1.0,100\n"
        ))
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown zone", errors[0])


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("auditor", password="test-pass-123")
        self.courier = Courier.objects.create(name="Test Courier", code="TEST")
        card = RateCard.objects.create(
            courier=self.courier, name="Card",
            effective_from=date.today() - timedelta(days=200),
        )
        RateSlab.objects.create(
            rate_card=card, zone=Zone.C,
            base_weight_kg=D("0.500"), base_rate=D("80.00"),
            increment_weight_kg=D("0.500"), increment_rate=D("40.00"),
        )
        self.invoice = CourierInvoice.objects.create(
            courier=self.courier, invoice_number="INV-1",
            invoice_date=date.today(),
            period_start=date.today() - timedelta(days=30),
            period_end=date.today() - timedelta(days=1),
        )
        self.line = InvoiceLine.objects.create(
            invoice=self.invoice, awb="GHOST1", billed_zone=Zone.C,
            billed_weight_kg=D("1.0"), billed_amount=D("200.00"), row_number=2,
        )
        self.discrepancy = Discrepancy.objects.create(
            line=self.line, kind=Discrepancy.Kind.UNKNOWN_AWB,
            detail="Not our shipment", amount_impact=D("200.00"),
        )

    def login(self):
        self.client.login(username="auditor", password="test-pass-123")

    # -- auth -------------------------------------------------------------

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_json_endpoint_requires_login(self):
        response = self.client.get(
            reverse("api_discrepancies", args=[self.invoice.pk])
        )
        self.assertEqual(response.status_code, 302)

    # -- pages ------------------------------------------------------------

    def test_dashboard_renders_for_logged_in_user(self):
        self.login()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recovery overview")

    def test_invoice_detail_renders(self):
        self.login()
        response = self.client.get(reverse("invoice_detail", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INV-1")

    def test_invoice_list_filters_by_courier(self):
        self.login()
        response = self.client.get(reverse("invoice_list"), {"courier": "TEST"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["invoices"]), 1)

    # -- JSON API ---------------------------------------------------------

    def test_discrepancy_api_returns_rows(self):
        self.login()
        response = self.client.get(
            reverse("api_discrepancies", args=[self.invoice.pk])
        )
        data = json.loads(response.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["awb"], "GHOST1")
        self.assertEqual(data["total_impact"], "200.00")

    def test_discrepancy_api_filters_by_kind(self):
        self.login()
        response = self.client.get(
            reverse("api_discrepancies", args=[self.invoice.pk]),
            {"kind": Discrepancy.Kind.WEIGHT},
        )
        self.assertEqual(json.loads(response.content)["count"], 0)

    def test_discrepancy_api_searches_by_awb(self):
        self.login()
        response = self.client.get(
            reverse("api_discrepancies", args=[self.invoice.pk]), {"q": "GHOST"}
        )
        self.assertEqual(json.loads(response.content)["count"], 1)

    def test_status_update_persists(self):
        self.login()
        response = self.client.post(
            reverse("api_update_discrepancy", args=[self.discrepancy.pk]),
            {"status": Discrepancy.Status.DISPUTED},
        )
        self.assertEqual(response.status_code, 200)
        self.discrepancy.refresh_from_db()
        self.assertEqual(self.discrepancy.status, Discrepancy.Status.DISPUTED)

    def test_status_update_rejects_an_invalid_value(self):
        self.login()
        response = self.client.post(
            reverse("api_update_discrepancy", args=[self.discrepancy.pk]),
            {"status": "NONSENSE"},
        )
        self.assertEqual(response.status_code, 400)
        self.discrepancy.refresh_from_db()
        self.assertEqual(self.discrepancy.status, Discrepancy.Status.OPEN)

    def test_status_update_rejects_get(self):
        self.login()
        response = self.client.get(
            reverse("api_update_discrepancy", args=[self.discrepancy.pk])
        )
        self.assertEqual(response.status_code, 405)

    # -- upload -----------------------------------------------------------

    def test_upload_imports_and_reconciles_in_one_request(self):
        self.login()
        Shipment.objects.create(
            awb="REAL001", order_ref="ORD-1", courier=self.courier,
            zone=Zone.C, actual_weight_kg=D("0.400"),
            shipped_on=date.today() - timedelta(days=15),
        )
        upload = io.BytesIO(b"awb,zone,weight_kg,amount\nREAL001,C,0.400,80.00\n")
        upload.name = "invoice.csv"

        response = self.client.post(reverse("invoice_upload"), {
            "courier": self.courier.pk,
            "invoice_number": "INV-2",
            "invoice_date": date.today().isoformat(),
            "period_start": (date.today() - timedelta(days=30)).isoformat(),
            "period_end": (date.today() - timedelta(days=1)).isoformat(),
            "csv_file": upload,
        })

        self.assertEqual(response.status_code, 302)
        invoice = CourierInvoice.objects.get(invoice_number="INV-2")
        self.assertEqual(invoice.lines.count(), 1)
        self.assertEqual(invoice.status, CourierInvoice.Status.RECONCILED)

    def test_upload_rejects_a_period_ending_before_it_starts(self):
        self.login()
        upload = io.BytesIO(b"awb,weight_kg,amount\nX,1,100\n")
        upload.name = "invoice.csv"

        response = self.client.post(reverse("invoice_upload"), {
            "courier": self.courier.pk,
            "invoice_number": "INV-3",
            "invoice_date": date.today().isoformat(),
            "period_start": date.today().isoformat(),
            "period_end": (date.today() - timedelta(days=10)).isoformat(),
            "csv_file": upload,
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CourierInvoice.objects.filter(invoice_number="INV-3").exists())

    def test_upload_rejects_a_non_csv_file(self):
        self.login()
        upload = io.BytesIO(b"not a csv")
        upload.name = "invoice.pdf"

        response = self.client.post(reverse("invoice_upload"), {
            "courier": self.courier.pk,
            "invoice_number": "INV-4",
            "invoice_date": date.today().isoformat(),
            "period_start": (date.today() - timedelta(days=30)).isoformat(),
            "period_end": (date.today() - timedelta(days=1)).isoformat(),
            "csv_file": upload,
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CourierInvoice.objects.filter(invoice_number="INV-4").exists())
