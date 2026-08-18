"""Django admin configuration.

Rate cards are maintained here rather than in a custom UI -- the admin is
genuinely the right tool for low-traffic reference data, and building a bespoke
CRUD screen for it would be wasted effort.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Courier,
    CourierInvoice,
    Discrepancy,
    InvoiceLine,
    RateCard,
    RateSlab,
    Shipment,
)


class RateSlabInline(admin.TabularInline):
    model = RateSlab
    extra = 5
    max_num = 5


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = ("name", "courier", "effective_from", "effective_to",
                    "fuel_surcharge_pct", "rto_multiplier")
    list_filter = ("courier",)
    inlines = [RateSlabInline]


@admin.register(Courier)
class CourierAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "shipment_count")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}

    def get_queryset(self, request):
        # Annotate once instead of counting per row in the list column.
        from django.db.models import Count
        return super().get_queryset(request).annotate(_shipments=Count("shipments"))

    @admin.display(description="Shipments", ordering="_shipments")
    def shipment_count(self, obj):
        return obj._shipments


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("awb", "order_ref", "courier", "zone", "status",
                    "actual_weight_kg", "shipped_on")
    list_filter = ("courier", "zone", "status", "payment_mode")
    search_fields = ("awb", "order_ref")
    date_hierarchy = "shipped_on"
    list_select_related = ("courier",)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ("awb", "billed_zone", "billed_weight_kg", "billed_amount", "expected_amount")
    readonly_fields = ("expected_amount",)
    show_change_link = True


@admin.register(CourierInvoice)
class CourierInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "courier", "invoice_date", "status", "variance_display")
    list_filter = ("courier", "status")
    search_fields = ("invoice_number",)
    date_hierarchy = "invoice_date"
    inlines = [InvoiceLineInline]

    @admin.display(description="Variance")
    def variance_display(self, obj):
        variance = obj.total_variance
        colour = "#b91c1c" if variance > 0 else "#15803d"
        return format_html('<b style="color:{}">Rs {}</b>', colour, variance)


@admin.register(Discrepancy)
class DiscrepancyAdmin(admin.ModelAdmin):
    list_display = ("awb", "kind", "status", "amount_impact", "detected_at")
    list_filter = ("kind", "status", "line__invoice__courier")
    search_fields = ("line__awb",)
    list_select_related = ("line",)
    list_editable = ("status",)

    @admin.display(description="AWB", ordering="line__awb")
    def awb(self, obj):
        return obj.line.awb
