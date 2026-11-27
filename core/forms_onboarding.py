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


class OnboardingStep1PersonalForm(forms.ModelForm):
    """
    Onboarding Wizard Step 1: Personal profile & emergency contacts.
    """
    class Meta:
        from core.models_onboarding import OnboardingProfile
        model = OnboardingProfile
        fields = [
            "date_of_birth",
            "gender",
            "blood_group",
            "current_address",
            "permanent_address",
            "same_as_current_address",
            "emergency_contact_name",
            "emergency_contact_relationship",
            "emergency_contact_phone",
            "emergency_contact_alt_phone",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "gender": forms.Select(attrs={"class": "form-control"}),
            "blood_group": forms.Select(attrs={"class": "form-control"}),
            "current_address": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Flat / House No., Street, City, State, PIN"}),
            "permanent_address": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Permanent residential address"}),
            "same_as_current_address": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "emergency_contact_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Full name of emergency contact"}),
            "emergency_contact_relationship": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Spouse, Parent, Sibling, Friend"}),
            "emergency_contact_phone": forms.TextInput(attrs={"class": "form-control", "placeholder": "+91 9876543210"}),
            "emergency_contact_alt_phone": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional secondary phone"}),
        }

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        if dob:
            today = timezone.localdate()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                raise ValidationError("Candidate must be at least 18 years of age.")
            if age > 100:
                raise ValidationError("Please provide a valid date of birth.")
        else:
            raise ValidationError("Date of birth is required.")
        return dob

    def clean(self):
        cleaned_data = super().clean()
        current_addr = cleaned_data.get("current_address", "").strip()
        same_as = cleaned_data.get("same_as_current_address", False)
        perm_addr = cleaned_data.get("permanent_address", "").strip()

        if not current_addr:
            self.add_error("current_address", "Current residential address is required.")

        if same_as:
            cleaned_data["permanent_address"] = current_addr
        elif not perm_addr:
            self.add_error("permanent_address", "Permanent address is required or select 'Same as current address'.")

        # Emergency contact requirements
        if not cleaned_data.get("emergency_contact_name", "").strip():
            self.add_error("emergency_contact_name", "Emergency contact name is required.")
        if not cleaned_data.get("emergency_contact_relationship", "").strip():
            self.add_error("emergency_contact_relationship", "Relationship to emergency contact is required.")
        if not cleaned_data.get("emergency_contact_phone", "").strip():
            self.add_error("emergency_contact_phone", "Primary emergency contact phone number is required.")

        return cleaned_data


class OnboardingStep2StatutoryForm(forms.ModelForm):
    """
    Onboarding Wizard Step 2: Statutory compliance & document collection (PAN, Aadhaar, UAN).
    """
    class Meta:
        from core.models_onboarding import OnboardingProfile
        model = OnboardingProfile
        fields = [
            "pan_number",
            "pan_document",
            "aadhaar_number",
            "aadhaar_document",
            "has_existing_uan",
            "uan_number",
        ]
        widgets = {
            "pan_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. ABCDE1234F",
                "maxlength": 10,
                "style": "text-transform: uppercase;",
            }),
            "pan_document": forms.FileInput(attrs={
                "class": "form-control",
                "accept": ".pdf,.jpg,.jpeg,.png",
            }),
            "aadhaar_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "12-digit Aadhaar number",
                "maxlength": 12,
            }),
            "aadhaar_document": forms.FileInput(attrs={
                "class": "form-control",
                "accept": ".pdf,.jpg,.jpeg,.png",
            }),
            "has_existing_uan": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
            "uan_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "12-digit existing UAN",
                "maxlength": 12,
            }),
        }

    def clean_pan_number(self):
        pan = self.cleaned_data.get("pan_number", "").strip().upper()
        if not pan:
            raise ValidationError("Permanent Account Number (PAN) is mandatory for statutory compliance.")
        import re
        pan_regex = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
        if not pan_regex.match(pan):
            raise ValidationError("Invalid PAN format. Must be 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).")
        return pan

    def clean_aadhaar_number(self):
        aadhaar = self.cleaned_data.get("aadhaar_number", "").strip()
        if not aadhaar:
            raise ValidationError("Aadhaar number is mandatory for statutory identity verification.")
        import re
        if not re.match(r"^\d{12}$", aadhaar):
            raise ValidationError("Invalid Aadhaar number. Must be exactly 12 digits.")
        return aadhaar

    def clean_uan_number(self):
        has_uan = self.cleaned_data.get("has_existing_uan", False)
        uan = self.cleaned_data.get("uan_number", "").strip()
        if has_uan:
            if not uan:
                raise ValidationError("Please provide your 12-digit existing UAN, or uncheck 'I have an existing UAN'.")
            import re
            if not re.match(r"^\d{12}$", uan):
                raise ValidationError("Invalid UAN format. Must be exactly 12 digits.")
            return uan
        return ""

    def clean(self):
        cleaned_data = super().clean()
        pan_doc = cleaned_data.get("pan_document")
        aadhaar_doc = cleaned_data.get("aadhaar_document")

        # Require document upload if not already present on instance
        if not pan_doc and not (self.instance and self.instance.pan_document):
            self.add_error("pan_document", "Please upload a copy of your PAN Card (PDF, PNG, JPG).")
        if not aadhaar_doc and not (self.instance and self.instance.aadhaar_document):
            self.add_error("aadhaar_document", "Please upload a copy of your Aadhaar Card (PDF, PNG, JPG).")

        return cleaned_data


class OnboardingStep3BankingForm(forms.ModelForm):
    """
    Onboarding Wizard Step 3: Banking details & cancelled cheque / passbook upload.
    """
    account_number_confirm = forms.CharField(
        max_length=35,
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Re-enter bank account number",
            "autocomplete": "off",
        }),
        help_text="Confirm account number to prevent typo in salary disbursement.",
    )

    class Meta:
        from core.models_onboarding import OnboardingProfile
        model = OnboardingProfile
        fields = [
            "account_holder_name",
            "bank_name",
            "branch_name",
            "account_number",
            "account_number_confirm",
            "ifsc_code",
            "account_type",
            "cheque_document",
        ]
        widgets = {
            "account_holder_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Beneficiary name matching bank records",
            }),
            "bank_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. HDFC Bank, State Bank of India, ICICI Bank",
            }),
            "branch_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Branch name or city",
            }),
            "account_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "9 to 18 digits account number",
                "autocomplete": "off",
            }),
            "ifsc_code": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "11-character IFSC (e.g. HDFC0001234)",
                "maxlength": 11,
                "style": "text-transform: uppercase;",
            }),
            "account_type": forms.Select(attrs={
                "class": "form-control",
            }),
            "cheque_document": forms.FileInput(attrs={
                "class": "form-control",
                "accept": ".pdf,.jpg,.jpeg,.png",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.account_number:
            self.fields["account_number_confirm"].initial = self.instance.account_number

    def clean_ifsc_code(self):
        ifsc = self.cleaned_data.get("ifsc_code", "").strip().upper()
        if not ifsc:
            raise ValidationError("IFSC Code is required for bank transfers.")
        import re
        if not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc):
            raise ValidationError("Invalid IFSC format. Must be 11 characters (4 letters, 0, 6 alphanumeric digits, e.g. HDFC0001234).")
        return ifsc

    def clean_account_number(self):
        acc = self.cleaned_data.get("account_number", "").strip()
        if not acc:
            raise ValidationError("Bank account number is required.")
        import re
        if not re.match(r"^[0-9]{9,18}$", acc):
            raise ValidationError("Invalid account number. Must contain between 9 and 18 digits.")
        return acc

    def clean(self):
        cleaned_data = super().clean()
        acc = cleaned_data.get("account_number")
        acc_confirm = cleaned_data.get("account_number_confirm")
        cheque_doc = cleaned_data.get("cheque_document")

        if acc and acc_confirm and acc != acc_confirm:
            self.add_error("account_number_confirm", "Account numbers do not match.")

        # Require cancelled cheque document if not already uploaded
        if not cheque_doc and not (self.instance and self.instance.cheque_document):
            self.add_error("cheque_document", "Please upload a copy of your cancelled cheque or passbook front page (PDF, PNG, JPG).")

        return cleaned_data


class OnboardingStep4PolicyForm(forms.ModelForm):
    """
    Onboarding Wizard Step 4: Company Policies Acknowledgment & Digital Sign-Off.
    """
    class Meta:
        from core.models_onboarding import OnboardingProfile
        model = OnboardingProfile
        fields = [
            "agreed_code_of_conduct",
            "agreed_security_policy",
            "agreed_nda",
            "policy_signed_full_name",
        ]
        widgets = {
            "agreed_code_of_conduct": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "agreed_security_policy": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "agreed_nda": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "policy_signed_full_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Type your full legal name as your electronic signature",
            }),
        }

    def clean_agreed_code_of_conduct(self):
        agreed = self.cleaned_data.get("agreed_code_of_conduct")
        if not agreed:
            raise ValidationError("You must read and agree to the Code of Conduct & Workplace Ethics.")
        return agreed

    def clean_agreed_security_policy(self):
        agreed = self.cleaned_data.get("agreed_security_policy")
        if not agreed:
            raise ValidationError("You must read and agree to the Information Security & Acceptable Use Policy.")
        return agreed

    def clean_agreed_nda(self):
        agreed = self.cleaned_data.get("agreed_nda")
        if not agreed:
            raise ValidationError("You must read and agree to the Non-Disclosure Agreement (NDA) & Confidentiality Terms.")
        return agreed

    def clean_policy_signed_full_name(self):
        sig = self.cleaned_data.get("policy_signed_full_name", "").strip()
        if not sig:
            raise ValidationError("Please provide your full legal signature name.")
        if len(sig) < 3:
            raise ValidationError("Signature name is too short. Please type your full name.")
        return sig





