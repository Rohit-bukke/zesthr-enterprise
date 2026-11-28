from datetime import timedelta
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models_offboarding import ExitRequest


class ResignationSubmissionForm(forms.ModelForm):
    """
    Form for employees to submit formal resignation with requested last working day and reasons.
    """
    requested_lwd = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="Proposed Last Working Day (LWD)",
        help_text="Standard contractual notice is 30 days from today."
    )

    class Meta:
        model = ExitRequest
        fields = ["requested_lwd", "reason_category", "reason_detail"]
        widgets = {
            "reason_category": forms.Select(attrs={"class": "form-control"}),
            "reason_detail": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Provide details regarding your resignation, handover plan, or feedback..."
            }),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        if not self.is_bound:
            today = timezone.localdate()
            default_lwd = today + timedelta(days=30)
            self.fields["requested_lwd"].initial = default_lwd

    def clean_requested_lwd(self):
        lwd = self.cleaned_data.get("requested_lwd")
        today = timezone.localdate()
        if lwd and lwd < today:
            raise ValidationError("Proposed last working day cannot be in the past.")
        return lwd


class ExitRequestReviewForm(forms.Form):
    """
    Form for managers and HR to decide on employee resignation (Approve or Reject).
    """
    ACTION_APPROVE = "approve"
    ACTION_REJECT = "reject"
    ACTION_CHOICES = [
        (ACTION_APPROVE, "Approve Resignation & Notice Period"),
        (ACTION_REJECT, "Reject / Decline Resignation"),
    ]

    action = forms.ChoiceField(
        choices=ACTION_CHOICES,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        initial=ACTION_APPROVE
    )
    official_lwd = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="Official Agreed Last Working Day (LWD)",
        help_text="Leave blank to accept the employee's requested LWD."
    )
    waive_shortfall = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Waive Notice Shortfall (No Recovery Deduction)",
        help_text="Check to waive notice penalty deductions for early release."
    )
    review_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Decision remarks, transition conditions, or reasons for rejection..."
        }),
        label="Decision Notes"
    )

    def __init__(self, *args, exit_request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.exit_request = exit_request
        if exit_request and not self.is_bound:
            self.fields["official_lwd"].initial = exit_request.requested_lwd

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get("action")
        official_lwd = cleaned_data.get("official_lwd")

        if action == self.ACTION_APPROVE and self.exit_request:
            effective_lwd = official_lwd or self.exit_request.requested_lwd
            if effective_lwd < self.exit_request.resignation_date:
                self.add_error("official_lwd", "Official last working day cannot be earlier than the resignation submission date.")

        return cleaned_data
