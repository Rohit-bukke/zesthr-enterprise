from datetime import timedelta
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.organizations.tenancy import scope_queryset
from core.models import Department, JobPosition
from core.models_onboarding import CandidateInvitation


class CandidateInvitationForm(forms.ModelForm):
    expiry_days = forms.IntegerField(
        initial=3,
        min_value=1,
        max_value=30,
        label="Invitation Expiry (Days)",
        help_text="Number of days before the candidate's invitation token expires (1-30 days, default 3)."
    )

    class Meta:
        model = CandidateInvitation
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "department",
            "position",
            "proposed_joining_date",
        ]
        widgets = {
            "proposed_joining_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, user=None, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.organization = organization

        # Scope department and position dropdowns
        if self.user:
            self.fields["department"].queryset = scope_queryset(
                Department.objects.filter(is_active=True), self.user
            )
            self.fields["position"].queryset = scope_queryset(
                JobPosition.objects.all(), self.user
            )

    def clean_proposed_joining_date(self):
        joining_date = self.cleaned_data.get("proposed_joining_date")
        if joining_date and joining_date < timezone.localdate():
            raise ValidationError("Proposed joining date cannot be in the past.")
        return joining_date

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email is required.")

        # Check for active invitation in the organization
        if self.organization:
            active_invite = CandidateInvitation.objects.filter(
                organization=self.organization,
                email__iexact=email,
                status=CandidateInvitation.STATUS_INVITED,
                expires_at__gt=timezone.now(),
            )
            if self.instance.pk:
                active_invite = active_invite.exclude(pk=self.instance.pk)
            if active_invite.exists():
                raise ValidationError("An active invitation has already been sent to this email address.")

        return email

    def save(self, commit=True):
        invitation = super().save(commit=False)
        if self.organization and not invitation.organization_id:
            invitation.organization = self.organization
        if self.user and not invitation.invited_by_id:
            invitation.invited_by = self.user

        expiry_days = self.cleaned_data.get("expiry_days", 3)
        invitation.expires_at = timezone.now() + timedelta(days=expiry_days)

        if commit:
            invitation.save()
        return invitation
