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


class CandidateRegistrationForm(forms.Form):
    """
    Candidate self-registration form on token verification.
    Sets up password credentials and confirms contact information.
    """
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Choose a username", "autocomplete": "username"})
    )
    first_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "First name"})
    )
    last_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Last name"})
    )
    phone = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Mobile phone number"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Create strong password", "autocomplete": "new-password"}),
        help_text="Must be at least 8 characters."
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm password", "autocomplete": "new-password"})
    )

    def __init__(self, *args, invitation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invitation = invitation
        if invitation:
            self.fields["first_name"].initial = invitation.first_name
            self.fields["last_name"].initial = invitation.last_name
            self.fields["phone"].initial = invitation.phone
            if not self.is_bound:
                suggested_username = invitation.email.split("@")[0].lower()
                from django.contrib.auth import get_user_model
                User = get_user_model()
                base_username = suggested_username
                idx = 1
                while User.objects.filter(username=suggested_username).exists():
                    suggested_username = f"{base_username}{idx}"
                    idx += 1
                self.fields["username"].initial = suggested_username

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        from django.contrib.auth import get_user_model
        User = get_user_model()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken. Please choose another.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm and password != password_confirm:
            self.add_error("password_confirm", "Passwords do not match.")

        if password:
            from django.contrib.auth.password_validation import validate_password
            try:
                validate_password(password)
            except ValidationError as error:
                self.add_error("password", error)

        return cleaned_data

