"""App URL routing. Named routes so templates never hardcode paths."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("invoices/", views.InvoiceListView.as_view(), name="invoice_list"),
    path("invoices/upload/", views.invoice_upload, name="invoice_upload"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/reconcile/", views.invoice_reconcile, name="invoice_reconcile"),

    # JSON endpoints for the jQuery front end
    path("api/invoices/<int:pk>/discrepancies/", views.api_discrepancies,
         name="api_discrepancies"),
    path("api/discrepancies/<int:pk>/status/", views.api_update_discrepancy,
         name="api_update_discrepancy"),
]
