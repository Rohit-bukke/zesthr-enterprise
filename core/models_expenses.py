import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ExpenseCategory(models.Model):
    """
    Categories for employee expense reimbursement claims (Travel, Meals, Equipment, etc.).
    """
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="expense_categories")
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30)
    description = models.CharField(max_length=255, blank=True)
    requires_receipt = models.BooleanField(default=True)
    max_amount_per_claim = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "expense_category"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="exp_cat_org_code_uniq"),
            models.UniqueConstraint(fields=["organization", "name"], name="exp_cat_org_name_uniq"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class ExpenseClaim(models.Model):
    """
    Employee expense reimbursement request.
    """
    STATUS_DRAFT = "DRAFT"
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_PAID = "PAID"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_PAID, "Paid via Payroll"),
    ]

    employee = models.ForeignKey("core.Employee", on_delete=models.PROTECT, related_name="expense_claims")
    claim_number = models.CharField(max_length=40, unique=True, editable=False)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="claims")
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    incurred_date = models.DateField()
    receipt = models.FileField(upload_to="expense_receipts/", null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SUBMITTED)
    submitted_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_notes = models.CharField(max_length=255, blank=True)
    payroll_run = models.ForeignKey("core.PayrollRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="expense_claims")

    class Meta:
        db_table = "expense_claim"
        ordering = ["-submitted_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="exp_claim_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["employee", "status"], name="exp_claim_emp_status_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.claim_number:
            self.claim_number = f"EXP-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Expense amount must be greater than zero."})
        if self.category and self.category.max_amount_per_claim:
            if self.amount and self.amount > self.category.max_amount_per_claim:
                raise ValidationError({"amount": f"Amount exceeds maximum allowed limit ({self.category.max_amount_per_claim}) for {self.category.name}."})
        if self.category and self.category.requires_receipt and not self.receipt and not self.pk:
            # When submitting a new claim requiring receipt
            pass

    def __str__(self):
        return f"{self.claim_number}: {self.employee.name} - {self.currency} {self.amount} ({self.get_status_display()})"
