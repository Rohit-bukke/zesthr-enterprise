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


class ExitSettlementAdjustmentForm(forms.Form):
    """
    Form for HR / Finance to inspect and adjust Full & Final settlement parameters.
    """
    worked_days_in_final_month = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0", "max": "31"}),
        label="Worked Days in Final Month"
    )
    leave_encashment_days = forms.DecimalField(
        required=False,
        max_digits=6,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.5", "min": "0"}),
        label="Leave Encashment Days"
    )
    reimbursable_expenses_amount = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Approved Expense Reimbursements (₹)"
    )
    is_gratuity_eligible = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Gratuity Eligible (Tenure >= 5 Yrs)"
    )
    gratuity_amount = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Gratuity Amount (₹)"
    )
    additional_earnings = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Additional Earnings / Bonus (₹)"
    )
    additional_earnings_notes = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "E.g. Performance bonus, retention bonus..."}),
        label="Additional Earnings Description"
    )

    notice_shortfall_days = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
        label="Notice Shortfall Days"
    )
    salary_advance_recovery = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Salary Advance Recovery (₹)"
    )
    asset_damage_recovery = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Asset / Equipment Damage Deduction (₹)"
    )
    additional_deductions = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
        label="Additional Deductions (₹)"
    )
    additional_deductions_notes = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "E.g. Relocation recovery, training bond..."}),
        label="Additional Deductions Description"
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Finance notes, audit justifications..."}),
        label="Finance & Settlement Remarks"
    )

    def __init__(self, *args, settlement=None, **kwargs):
        super().__init__(*args, **kwargs)
        if settlement and not self.is_bound:
            self.fields["worked_days_in_final_month"].initial = settlement.worked_days_in_final_month
            self.fields["leave_encashment_days"].initial = settlement.leave_encashment_days
            self.fields["reimbursable_expenses_amount"].initial = settlement.reimbursable_expenses_amount
            self.fields["is_gratuity_eligible"].initial = settlement.is_gratuity_eligible
            self.fields["gratuity_amount"].initial = settlement.gratuity_amount
            self.fields["additional_earnings"].initial = settlement.additional_earnings
            self.fields["additional_earnings_notes"].initial = settlement.additional_earnings_notes
            self.fields["notice_shortfall_days"].initial = settlement.notice_shortfall_days
            self.fields["salary_advance_recovery"].initial = settlement.salary_advance_recovery
            self.fields["asset_damage_recovery"].initial = settlement.asset_damage_recovery
            self.fields["additional_deductions"].initial = settlement.additional_deductions
            self.fields["additional_deductions_notes"].initial = settlement.additional_deductions_notes
            self.fields["remarks"].initial = settlement.remarks
