import re
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models

PT_STATE_CHOICES = [
    ("KARNATAKA", "Karnataka"),
    ("MAHARASHTRA", "Maharashtra"),
    ("TELANGANA", "Telangana"),
    ("ANDHRA_PRADESH", "Andhra Pradesh"),
    ("TAMIL_NADU", "Tamil Nadu"),
    ("WEST_BENGAL", "West Bengal"),
    ("GUJARAT", "Gujarat"),
    ("FLAT", "Flat Rate (Default)"),
]

PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
UAN_REGEX = re.compile(r"^[0-9]{12}$")


class OrganizationStatutoryConfig(models.Model):
    """Organization-level statutory compliance configuration."""

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="statutory_config",
    )

    # EPF (Employees' Provident Fund)
    epf_enabled = models.BooleanField(
        default=True,
        help_text="Enable Employees' Provident Fund deductions",
    )
    epf_employee_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("12.00"),
        help_text="Employee PF deduction rate in % (default 12%)",
    )
    epf_employer_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("12.00"),
        help_text="Total employer PF contribution rate in % (default 12%)",
    )
    epf_employer_eps_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("8.33"),
        help_text="Employer Pension Scheme (EPS) share in % (default 8.33%)",
    )
    epf_employer_epf_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.67"),
        help_text="Employer EPF share in % (default 3.67%)",
    )
    epf_wage_ceiling = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("15000.00"),
        help_text="Statutory monthly wage ceiling for PF (INR 15,000)",
    )
    epf_restrict_to_ceiling_default = models.BooleanField(
        default=True,
        help_text="Default to capping wage at ceiling (15k) for employees",
    )

    # ESIC (Employees' State Insurance Corporation)
    esi_enabled = models.BooleanField(
        default=True,
        help_text="Enable Employee State Insurance contributions",
    )
    esi_wage_ceiling = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("21000.00"),
        help_text="Statutory monthly gross wage ceiling for ESI eligibility (INR 21,000)",
    )
    esi_employee_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.75"),
        help_text="Employee ESI rate in % (default 0.75%)",
    )
    esi_employer_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.25"),
        help_text="Employer ESI rate in % (default 3.25%)",
    )

    # Professional Tax (PT)
    pt_enabled = models.BooleanField(
        default=True,
        help_text="Enable Professional Tax deduction",
    )
    pt_state = models.CharField(
        max_length=50,
        choices=PT_STATE_CHOICES,
        default="KARNATAKA",
        help_text="Default state for Professional Tax rules",
    )

    class Meta:
        verbose_name = "Organization Statutory Configuration"
        verbose_name_plural = "Organization Statutory Configurations"

    def __str__(self):
        return f"Statutory Config: {self.organization.name}"

    @classmethod
    def get_or_create_default(cls, organization):
        config, _ = cls.objects.get_or_create(organization=organization)
        return config


class EmployeeStatutoryProfile(models.Model):
    """Employee-specific statutory profile, tax identification, and overrides."""

    employee = models.OneToOneField(
        "core.Employee",
        on_delete=models.CASCADE,
        related_name="statutory_profile",
    )

    pan_number = models.CharField(
        max_length=10,
        blank=True,
        help_text="Permanent Account Number (10 alphanumeric chars)",
    )
    uan_number = models.CharField(
        max_length=12,
        blank=True,
        help_text="Universal Account Number (12 digits)",
    )
    pf_number = models.CharField(
        max_length=50,
        blank=True,
        help_text="PF Member ID / Account number",
    )
    esi_number = models.CharField(
        max_length=17,
        blank=True,
        help_text="ESIC Insurance Number (17 digits)",
    )

    epf_enrolled = models.BooleanField(
        default=True,
        help_text="Whether this employee is enrolled in EPF",
    )
    epf_restrict_to_ceiling = models.BooleanField(
        default=True,
        help_text="Calculate PF on wage ceiling (INR 15,000 max) even if basic exceeds ceiling",
    )
    esi_enrolled = models.BooleanField(
        default=True,
        help_text="Whether this employee is enrolled in ESIC",
    )
    pt_enrolled = models.BooleanField(
        default=True,
        help_text="Whether this employee is subject to Professional Tax",
    )
    pt_state = models.CharField(
        max_length=50,
        choices=PT_STATE_CHOICES,
        blank=True,
        help_text="Override state for PT calculation (if blank, organization default applies)",
    )
    monthly_tds = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Fixed monthly Income Tax (TDS) deduction",
    )

    class Meta:
        verbose_name = "Employee Statutory Profile"
        verbose_name_plural = "Employee Statutory Profiles"

    def __str__(self):
        return f"Statutory Profile: {self.employee.name}"

    def clean(self):
        super().clean()
        if self.pan_number:
            pan = self.pan_number.strip().upper()
            if not PAN_REGEX.match(pan):
                raise ValidationError({"pan_number": "Invalid PAN format. Must be 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F)."})
            self.pan_number = pan

        if self.uan_number:
            uan = self.uan_number.strip()
            if not UAN_REGEX.match(uan):
                raise ValidationError({"uan_number": "Invalid UAN format. Must be exactly 12 digits."})
            self.uan_number = uan

        if self.esi_number:
            self.esi_number = self.esi_number.strip()
