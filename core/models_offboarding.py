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

    @property
    def total_clearance_items(self):
        return self.clearance_items.count()

    @property
    def cleared_clearance_items(self):
        return self.clearance_items.filter(status=ExitClearanceItem.STATUS_CLEARED).count()

    @property
    def clearance_percentage(self):
        total = self.total_clearance_items
        if total == 0:
            return 0
        return int((self.cleared_clearance_items / total) * 100)

    @property
    def is_clearance_complete(self):
        total = self.total_clearance_items
        return total > 0 and self.cleared_clearance_items == total

    def check_clearance_completion(self):
        """Update overall status if all items are cleared."""
        if self.is_clearance_complete and self.status == self.STATUS_CLEARANCE_IN_PROGRESS:
            # All items cleared - ready for final settlement
            pass

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


class ExitClearanceItem(models.Model):
    """
    Departmental exit clearance gates (IT, Finance, HR, Facilities) required before official employee release.
    """
    ROLE_IT = "IT"
    ROLE_FINANCE = "FINANCE"
    ROLE_HR = "HR"
    ROLE_FACILITIES = "FACILITIES"

    DEPARTMENT_ROLE_CHOICES = [
        (ROLE_IT, "Information Technology (IT)"),
        (ROLE_FINANCE, "Finance & Accounts"),
        (ROLE_HR, "Human Resources (HR)"),
        (ROLE_FACILITIES, "Admin & Facilities"),
    ]

    STATUS_PENDING = "PENDING"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_CLEARED = "CLEARED"
    STATUS_BLOCKED = "BLOCKED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_CLEARED, "Cleared (No Dues)"),
        (STATUS_BLOCKED, "Blocked / Action Required"),
    ]

    exit_request = models.ForeignKey(
        ExitRequest,
        on_delete=models.CASCADE,
        related_name="clearance_items"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="exit_clearance_items"
    )
    department_role = models.CharField(
        max_length=20,
        choices=DEPARTMENT_ROLE_CHOICES,
        default=ROLE_HR
    )
    item_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING
    )
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cleared_exit_items"
    )
    cleared_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Inspection notes, recovery deductions, or return receipts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "exit_clearance_item"
        ordering = ["department_role", "id"]

    def __str__(self):
        return f"[{self.department_role}] {self.item_name} ({self.get_status_display()})"

    def mark_cleared(self, user=None, notes=""):
        self.status = self.STATUS_CLEARED
        self.cleared_at = timezone.now()
        if user:
            self.cleared_by = user
        if notes:
            self.notes = notes
        self.save(update_fields=["status", "cleared_at", "cleared_by", "notes", "updated_at"])
        self.exit_request.check_clearance_completion()

    def mark_blocked(self, user=None, notes=""):
        self.status = self.STATUS_BLOCKED
        if user:
            self.cleared_by = user
        if notes:
            self.notes = notes
        self.save(update_fields=["status", "cleared_by", "notes", "updated_at"])
        self.exit_request.check_clearance_completion()

