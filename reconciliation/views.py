"""
Views.

Mix of function-based views (where the flow is procedural, like upload) and
class-based generic views (where it is plain listing). Two endpoints return
JSON for the jQuery front end rather than re-rendering the page.
"""

from decimal import Decimal

from django.contrib import messages
from django.db.models import Case, Count, DecimalField, F, Q, Sum, When
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import ListView
from django.utils.decorators import method_decorator

from .demo import blocked_in_demo, demo_aware_login_required
from .forms import InvoiceUploadForm
from .models import Courier, CourierInvoice, Discrepancy, InvoiceLine, Shipment
from .services.ingest import IngestError, import_lines, parse_invoice_csv
from .services.reconciler import reconcile_invoice

ZERO = Decimal("0.00")
DECIMAL_FIELD = DecimalField(max_digits=14, decimal_places=2)


@demo_aware_login_required
def dashboard(request):
    """Portfolio-level view of recovery exposure.

    All figures come from database aggregates rather than Python loops over
    querysets -- the summing happens in PostgreSQL, and the view transfers a
    handful of numbers instead of every row.
    """
    invoices = CourierInvoice.objects.all()

    totals = InvoiceLine.objects.aggregate(
        billed=Coalesce(Sum("billed_amount"), ZERO, output_field=DECIMAL_FIELD),
        expected=Coalesce(Sum("expected_amount"), ZERO, output_field=DECIMAL_FIELD),
    )

    open_recovery = Discrepancy.objects.filter(
        status=Discrepancy.Status.OPEN
    ).aggregate(total=Coalesce(Sum("amount_impact"), ZERO, output_field=DECIMAL_FIELD))["total"]

    recovered = Discrepancy.objects.filter(
        status=Discrepancy.Status.RECOVERED
    ).aggregate(total=Coalesce(Sum("amount_impact"), ZERO, output_field=DECIMAL_FIELD))["total"]

    by_kind = list(
        Discrepancy.objects.values("kind")
        .annotate(
            count=Count("id"),
            impact=Coalesce(Sum("amount_impact"), ZERO, output_field=DECIMAL_FIELD),
        )
        .order_by("-impact")
    )
    kind_labels = dict(Discrepancy.Kind.choices)
    for row in by_kind:
        row["label"] = kind_labels.get(row["kind"], row["kind"])

    # Per-courier leaderboard. Conditional aggregation counts discrepancies and
    # sums impact in the same pass rather than querying once per courier.
    by_courier = (
        Courier.objects.annotate(
            invoice_count=Count("invoices", distinct=True),
            billed=Coalesce(
                Sum("invoices__lines__billed_amount"), ZERO, output_field=DECIMAL_FIELD
            ),
            impact=Coalesce(
                Sum(
                    Case(
                        When(
                            invoices__lines__discrepancies__status__in=[
                                Discrepancy.Status.OPEN,
                                Discrepancy.Status.DISPUTED,
                            ],
                            then=F("invoices__lines__discrepancies__amount_impact"),
                        ),
                        output_field=DECIMAL_FIELD,
                    )
                ),
                ZERO,
                output_field=DECIMAL_FIELD,
            ),
        )
        .filter(invoice_count__gt=0)
        .order_by("-impact")
    )

    context = {
        "invoice_count": invoices.count(),
        "shipment_count": Shipment.objects.count(),
        "total_billed": totals["billed"],
        "total_expected": totals["expected"],
        "total_variance": totals["billed"] - totals["expected"],
        "open_recovery": open_recovery,
        "recovered": recovered,
        "open_count": Discrepancy.objects.filter(status=Discrepancy.Status.OPEN).count(),
        "by_kind": by_kind,
        "by_courier": by_courier,
        "recent_invoices": invoices.select_related("courier")[:8],
    }
    return render(request, "reconciliation/dashboard.html", context)


@method_decorator(demo_aware_login_required, name="dispatch")
class InvoiceListView(ListView):
    model = CourierInvoice
    template_name = "reconciliation/invoice_list.html"
    context_object_name = "invoices"
    paginate_by = 20

    def get_queryset(self):
        # select_related on the FK avoids one extra query per row when the
        # template prints courier.name -- the N+1 that quietly kills list pages.
        qs = CourierInvoice.objects.select_related("courier").annotate(
            line_count=Count("lines", distinct=True),
            billed=Coalesce(Sum("lines__billed_amount"), ZERO, output_field=DECIMAL_FIELD),
            open_impact=Coalesce(
                Sum(
                    "lines__discrepancies__amount_impact",
                    filter=Q(lines__discrepancies__status=Discrepancy.Status.OPEN),
                ),
                ZERO,
                output_field=DECIMAL_FIELD,
            ),
        )

        status = self.request.GET.get("status")
        if status in CourierInvoice.Status.values:
            qs = qs.filter(status=status)

        courier = self.request.GET.get("courier")
        if courier:
            qs = qs.filter(courier__code=courier)

        # Explicit ordering: annotate() adds a GROUP BY that drops Meta.ordering,
        # and paginating an unordered queryset can repeat or skip rows between
        # pages because the database is free to return them in any order.
        return qs.order_by("-invoice_date", "-id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["couriers"] = Courier.objects.all()
        ctx["status_choices"] = CourierInvoice.Status.choices
        ctx["current_status"] = self.request.GET.get("status", "")
        ctx["current_courier"] = self.request.GET.get("courier", "")
        return ctx


@demo_aware_login_required
@blocked_in_demo
def invoice_upload(request):
    """Create an invoice, import its CSV, and reconcile it in one request."""
    if request.method != "POST":
        return render(
            request, "reconciliation/invoice_upload.html", {"form": InvoiceUploadForm()}
        )

    form = InvoiceUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, "reconciliation/invoice_upload.html", {"form": form})

    try:
        rows, errors = parse_invoice_csv(form.cleaned_data["csv_file"])
    except IngestError as exc:
        form.add_error("csv_file", str(exc))
        return render(request, "reconciliation/invoice_upload.html", {"form": form})

    if not rows:
        form.add_error("csv_file", "No usable rows found in the file.")
        return render(request, "reconciliation/invoice_upload.html", {"form": form})

    invoice = form.save()
    imported = import_lines(invoice, rows)
    result = reconcile_invoice(invoice)

    messages.success(
        request,
        f"Imported {imported} lines. Found {result.discrepancy_count} discrepancies "
        f"worth Rs {result.net_impact}.",
    )
    # Surface parse failures without blocking the import -- finance would rather
    # reconcile 3,998 good rows now and fix 2 by hand than reconcile nothing.
    for err in errors[:10]:
        messages.warning(request, err)
    if len(errors) > 10:
        messages.warning(request, f"...and {len(errors) - 10} more row errors.")

    return redirect("invoice_detail", pk=invoice.pk)


@demo_aware_login_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(
        CourierInvoice.objects.select_related("courier"), pk=pk
    )
    # prefetch_related pulls all discrepancies in one extra query rather than
    # one per line as the template iterates.
    lines = (
        invoice.lines.select_related("shipment")
        .prefetch_related("discrepancies")
        .all()
    )

    discrepancies = Discrepancy.objects.filter(line__invoice=invoice).select_related(
        "line", "line__shipment"
    )

    context = {
        "invoice": invoice,
        "lines": lines,
        "discrepancies": discrepancies,
        "total_billed": invoice.total_billed,
        "total_expected": invoice.total_expected,
        "total_variance": invoice.total_variance,
        "kind_choices": Discrepancy.Kind.choices,
        "status_choices": Discrepancy.Status.choices,
    }
    return render(request, "reconciliation/invoice_detail.html", context)


@demo_aware_login_required
@require_POST
@blocked_in_demo
def invoice_reconcile(request, pk):
    """Re-run reconciliation. Useful after correcting a rate card."""
    invoice = get_object_or_404(CourierInvoice, pk=pk)
    result = reconcile_invoice(invoice)
    messages.success(
        request,
        f"Reconciled {result.lines_checked} lines: {result.lines_matched} clean, "
        f"{result.discrepancy_count} flagged, net Rs {result.net_impact}.",
    )
    return redirect("invoice_detail", pk=invoice.pk)


# ---------------------------------------------------------------------------
# JSON endpoints consumed by jQuery
# ---------------------------------------------------------------------------


@demo_aware_login_required
def api_discrepancies(request, pk):
    """Filtered discrepancy rows for an invoice, as JSON.

    Backs the live filter on the detail page: jQuery re-queries this instead of
    reloading the page, so filtering a few thousand rows stays instant.
    """
    invoice = get_object_or_404(CourierInvoice, pk=pk)
    qs = Discrepancy.objects.filter(line__invoice=invoice).select_related("line")

    kind = request.GET.get("kind")
    if kind:
        qs = qs.filter(kind=kind)

    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)

    search = (request.GET.get("q") or "").strip()
    if search:
        qs = qs.filter(line__awb__icontains=search)

    min_impact = request.GET.get("min_impact")
    if min_impact:
        try:
            qs = qs.filter(amount_impact__gte=Decimal(min_impact))
        except (TypeError, ValueError, ArithmeticError):
            pass  # Ignore an unparseable filter rather than 500 on it.

    kind_labels = dict(Discrepancy.Kind.choices)
    status_labels = dict(Discrepancy.Status.choices)

    rows = [
        {
            "id": d.id,
            "awb": d.line.awb,
            "kind": d.kind,
            "kind_label": kind_labels.get(d.kind, d.kind),
            "status": d.status,
            "status_label": status_labels.get(d.status, d.status),
            "detail": d.detail,
            "billed": str(d.line.billed_amount),
            "expected": str(d.line.expected_amount or "-"),
            "impact": str(d.amount_impact),
        }
        for d in qs[:500]
    ]

    total = qs.aggregate(
        t=Coalesce(Sum("amount_impact"), ZERO, output_field=DECIMAL_FIELD)
    )["t"]

    return JsonResponse(
        {
            "count": qs.count(),
            # quantize so the payload always carries two decimal places --
            # the aggregate returns a bare Decimal('200') on some backends,
            # and the front end should not have to normalise currency.
            "total_impact": str(total.quantize(Decimal("0.01"))),
            "rows": rows,
        }
    )


@demo_aware_login_required
@require_POST
@blocked_in_demo
def api_update_discrepancy(request, pk):
    """Update a single discrepancy's status from the detail page."""
    discrepancy = get_object_or_404(Discrepancy, pk=pk)
    status = request.POST.get("status")

    if status not in Discrepancy.Status.values:
        return JsonResponse({"ok": False, "error": "Invalid status"}, status=400)

    discrepancy.status = status
    discrepancy.save(update_fields=["status"])

    return JsonResponse(
        {
            "ok": True,
            "id": discrepancy.id,
            "status": discrepancy.status,
            "status_label": discrepancy.get_status_display(),
        }
    )
