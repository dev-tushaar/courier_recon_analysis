"""Forms for invoice upload and discrepancy filtering."""

from django import forms

from .models import Courier, CourierInvoice, Discrepancy

CSS = "form-input"


class InvoiceUploadForm(forms.ModelForm):
    """Create an invoice and attach its CSV in one step."""

    csv_file = forms.FileField(
        label="Invoice CSV",
        help_text="Required columns: awb, weight_kg, amount. Optional: zone.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv", "class": CSS}),
    )

    class Meta:
        model = CourierInvoice
        fields = ["courier", "invoice_number", "invoice_date", "period_start", "period_end"]
        widgets = {
            "courier": forms.Select(attrs={"class": CSS}),
            "invoice_number": forms.TextInput(
                attrs={"class": CSS, "placeholder": "e.g. INV-2026-0417"}
            ),
            "invoice_date": forms.DateInput(attrs={"type": "date", "class": CSS}),
            "period_start": forms.DateInput(attrs={"type": "date", "class": CSS}),
            "period_end": forms.DateInput(attrs={"type": "date", "class": CSS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["courier"].queryset = Courier.objects.filter(is_active=True)

    def clean_csv_file(self):
        f = self.cleaned_data["csv_file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file.")
        if f.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File is larger than 10 MB.")
        return f

    def clean(self):
        """Cross-field validation the individual field cleans cannot express."""
        cleaned = super().clean()
        start, end = cleaned.get("period_start"), cleaned.get("period_end")
        if start and end and start > end:
            raise forms.ValidationError("Billing period ends before it starts.")

        invoice_date = cleaned.get("invoice_date")
        if invoice_date and end and invoice_date < end:
            self.add_error(
                "invoice_date",
                "Invoice is dated before the end of the period it bills for.",
            )
        return cleaned


class DiscrepancyStatusForm(forms.ModelForm):
    """Inline status update, posted over AJAX from the detail page."""

    class Meta:
        model = Discrepancy
        fields = ["status"]
