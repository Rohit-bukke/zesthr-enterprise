from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class ExitRequest(models.Model):
    """
    Employee resignation submission, notice period calculation, and separation tracking.
    """
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CLEARANCE_IN_PROGRESS = "CLEARANCE_IN_PROGRESS"
    STATUS_SETTLED = "SETTLED"

    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted (Pending Review)"),
        (STATUS_APPROVED, "Approved (Serving Notice)"),
        (STATUS_REJECTED, "Rejected / Withdrawn"),
        (STATUS_CLEARANCE_IN_PROGRESS, "Clearance In Progress"),
        (STATUS_SETTLED, "Settled & Separated"),
    ]

    REASON_CAREER_GROWTH = "CAREER_GROWTH"
    REASON_HIGHER_EDUCATION = "HIGHER_EDUCATION"
    REASON_BETTER_COMPENSATION = "BETTER_COMPENSATION"
    REASON_PERSONAL_RELOCATION = "PERSONAL_RELOCATION"
    REASON_HEALTH = "HEALTH"
    REASON_WORK_ENVIRONMENT = "WORK_ENVIRONMENT"
    REASON_OTHER = "OTHER"

    REASON_CHOICES = [
        (REASON_CAREER_GROWTH, "Career Growth & Better Opportunities"),
        (REASON_HIGHER_EDUCATION, "Pursuing Higher Education"),
        (REASON_BETTER_COMPENSATION, "Better Compensation / Benefits"),
        (REASON_PERSONAL_RELOCATION, "Personal / Family Relocation"),
        (REASON_HEALTH, "Health & Medical Reasons"),
        (REASON_WORK_ENVIRONMENT, "Work-Life Balance / Environment"),
        (REASON_OTHER, "Other Reasons"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="exit_requests"
    )
    employee = models.ForeignKey(
        "core.Employee",
        on_delete=models.CASCADE,
        related_name="exit_requests"
    )
    resignation_date = models.DateField(
        help_text="Date on which resignation was submitted"
    )
    requested_lwd = models.DateField(
        help_text="Employee's proposed Last Working Day"
    )
    official_lwd = models.DateField(
        null=True,
        blank=True,
        help_text="Manager / HR agreed official Last Working Day"
    )
    reason_category = models.CharField(
        max_length=30,
        choices=REASON_CHOICES,
        default=REASON_CAREER_GROWTH
    )
    reason_detail = models.TextField(
        blank=True,
        help_text="Detailed resignation reasoning and handover notes"
    )
    notice_period_days = models.PositiveIntegerField(
        default=30,
        help_text="Standard contractual notice period in calendar days"
    )
    notice_shortfall_days = models.PositiveIntegerField(
        default=0,
        help_text="Days of notice shortfall subject to recovery or managerial waiver"
    )
    shortfall_waived = models.BooleanField(
        default=False,
        help_text="Whether notice period shortfall penalty is waived by management"
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_SUBMITTED
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_exit_requests"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(
        blank=True,
        help_text="Manager / HR decision notes, handover conditions, or waiver reasons"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "exit_request"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requested_lwd__gte=models.F("resignation_date")),
                name="exit_req_requested_lwd_gte_resignation"
            ),
        ]

    def __str__(self):
        return f"ExitRequest: {self.employee.name} ({self.get_status_display()})"

    @property
    def standard_lwd(self):
        return self.resignation_date + timedelta(days=self.notice_period_days)

    def calculate_shortfall(self):
        if self.shortfall_waived:
            self.notice_shortfall_days = 0
            return 0
        effective_lwd = self.official_lwd or self.requested_lwd
        if not effective_lwd or not self.resignation_date:
            self.notice_shortfall_days = 0
            return 0
        standard = self.standard_lwd
        if effective_lwd < standard:
            self.notice_shortfall_days = (standard - effective_lwd).days
        else:
            self.notice_shortfall_days = 0
        return self.notice_shortfall_days

    def clean(self):
        if self.requested_lwd and self.resignation_date and self.requested_lwd < self.resignation_date:
            raise ValidationError("Requested last working day cannot be earlier than resignation date.")
        if self.official_lwd and self.resignation_date and self.official_lwd < self.resignation_date:
            raise ValidationError("Official last working day cannot be earlier than resignation date.")

    def save(self, *args, **kwargs):
        if not self.resignation_date:
            self.resignation_date = timezone.localdate()
        self.calculate_shortfall()
        super().save(*args, **kwargs)
