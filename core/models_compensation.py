from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class CompensationBand(models.Model):
    """
    Standard salary/compensation band for job positions or levels within an organization.
    """
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="compensation_bands")
    name = models.CharField(max_length=100, help_text="e.g. Senior Software Engineer - Band L4")
    code = models.CharField(max_length=30, help_text="e.g. ENG-L4")
    department = models.ForeignKey("core.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="compensation_bands")
    min_base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    max_base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=10, default="INR")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "compensation_band"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="comp_band_org_code_uniq"),
            models.CheckConstraint(condition=models.Q(min_base_salary__gte=0), name="comp_band_min_non_neg"),
            models.CheckConstraint(condition=models.Q(max_base_salary__gte=models.F("min_base_salary")), name="comp_band_max_gte_min"),
        ]

    def __str__(self):
        return f"{self.code} - {self.name} ({self.currency} {self.min_base_salary} - {self.max_base_salary})"

    def clean(self):
        if self.min_base_salary is not None and self.max_base_salary is not None:
            if self.max_base_salary < self.min_base_salary:
                raise ValidationError({"max_base_salary": "Maximum salary must be greater than or equal to minimum salary."})


class SalaryRevision(models.Model):
    """
    Audit-trailed and effective-dated compensation revisions (appraisals, promotions, adjustments).
    """
    REASON_ANNUAL_APPRAISAL = "ANNUAL_APPRAISAL"
    REASON_PROMOTION = "PROMOTION"
    REASON_MARKET_CORRECTION = "MARKET_CORRECTION"
    REASON_PROBATION_PASS = "PROBATION_PASS"
    REASON_RETENTION = "RETENTION"
    REASON_OTHER = "OTHER"

    REASON_CHOICES = [
        (REASON_ANNUAL_APPRAISAL, "Annual Appraisal"),
        (REASON_PROMOTION, "Promotion"),
        (REASON_MARKET_CORRECTION, "Market Correction"),
        (REASON_PROBATION_PASS, "Probation Completion"),
        (REASON_RETENTION, "Retention"),
        (REASON_OTHER, "Other Adjustment"),
    ]

    STATUS_PROPOSED = "PROPOSED"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_APPLIED = "APPLIED"

    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Proposed"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_APPLIED, "Applied"),
    ]

    employee = models.ForeignKey("core.Employee", on_delete=models.PROTECT, related_name="salary_revisions")
    previous_salary = models.DecimalField(max_digits=12, decimal_places=2)
    revised_salary = models.DecimalField(max_digits=12, decimal_places=2)
    increment_percentage = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    reason = models.CharField(max_length=30, choices=REASON_CHOICES, default=REASON_ANNUAL_APPRAISAL)
    effective_date = models.DateField(help_text="Date from which the revised salary applies")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PROPOSED)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    review_notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "salary_revision"
        ordering = ["-effective_date", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(previous_salary__gte=0), name="sal_rev_prev_non_neg"),
            models.CheckConstraint(condition=models.Q(revised_salary__gt=0), name="sal_rev_revised_pos"),
        ]

    def __str__(self):
        return f"{self.employee.name} - {self.previous_salary} -> {self.revised_salary} ({self.get_status_display()})"

    def clean(self):
        if self.previous_salary is not None and self.revised_salary is not None:
            if self.previous_salary > 0:
                diff = self.revised_salary - self.previous_salary
                self.increment_percentage = round((diff / self.previous_salary) * Decimal("100.00"), 2)
            else:
                self.increment_percentage = Decimal("0.00")
