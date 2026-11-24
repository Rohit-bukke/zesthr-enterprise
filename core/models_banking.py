import re
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# Standard Indian Financial System Code (IFSC) regex: 4 letters, 0, 6 alphanumeric chars
IFSC_REGEX = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# Standard Indian bank account number: 9 to 18 digits
ACCOUNT_NUMBER_REGEX = re.compile(r"^[0-9]{9,18}$")
# Standard UPI VPA regex: username@bank
UPI_REGEX = re.compile(r"^[\w.\-]+@[\w\-]+$")


class EmployeeBankAccount(models.Model):
    """
    Employee banking and payment disbursement information.
    Used for corporate payroll disbursements (NEFT, RTGS, IMPS, NACH).
    """

    ACCOUNT_TYPE_CHOICES = [
        ("SALARY", "Salary Account"),
        ("SAVINGS", "Savings Account"),
        ("CURRENT", "Current Account"),
    ]

    PAYMENT_METHOD_CHOICES = [
        ("NEFT", "NEFT / RTGS (Batch Transfer)"),
        ("IMPS", "IMPS (Instant Transfer)"),
        ("NACH", "NACH / Direct Debit"),
        ("CHEQUE", "Cheque"),
        ("CASH", "Cash"),
    ]

    employee = models.OneToOneField(
        "core.Employee",
        on_delete=models.CASCADE,
        related_name="bank_account",
    )

    bank_name = models.CharField(
        max_length=100,
        help_text="Name of the bank (e.g. HDFC Bank, State Bank of India, ICICI Bank)",
    )

    branch_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Branch name or city",
    )

    account_holder_name = models.CharField(
        max_length=100,
        help_text="Beneficiary name as registered with the bank",
    )

    account_number = models.CharField(
        max_length=35,
        help_text="Bank account number (9 to 18 digits)",
    )

    ifsc_code = models.CharField(
        max_length=11,
        help_text="11-character IFSC code (e.g. HDFC0001234, SBIN0000456)",
    )

    account_type = models.CharField(
        max_length=20,
        choices=ACCOUNT_TYPE_CHOICES,
        default="SALARY",
    )

    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default="NEFT",
    )

    upi_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional UPI ID (e.g. employee@okhdfcbank)",
    )

    is_verified = models.BooleanField(
        default=False,
        help_text="Whether bank details have been verified against cancelled cheque / statement",
    )

    verified_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Employee Bank Account"
        verbose_name_plural = "Employee Bank Accounts"
        ordering = ["employee__name"]

    def __str__(self):
        return f"{self.employee.name} - {self.bank_name} ({self.masked_account_number})"

    @property
    def masked_account_number(self):
        """Return masked account number displaying only last 4 digits (e.g. XXXXXX1234)."""
        num = (self.account_number or "").strip()
        if len(num) > 4:
            return "X" * (len(num) - 4) + num[-4:]
        return num

    def clean(self):
        super().clean()
        if self.account_number:
            acc = self.account_number.strip()
            if not ACCOUNT_NUMBER_REGEX.match(acc):
                raise ValidationError({"account_number": "Invalid account number. Must contain 9 to 18 digits."})
            self.account_number = acc

        if self.ifsc_code:
            ifsc = self.ifsc_code.strip().upper()
            if not IFSC_REGEX.match(ifsc):
                raise ValidationError({"ifsc_code": "Invalid IFSC code format. Must be 4 letters, '0', and 6 alphanumeric characters (e.g. HDFC0001234)."})
            self.ifsc_code = ifsc

        if self.upi_id:
            upi = self.upi_id.strip().lower()
            if not UPI_REGEX.match(upi):
                raise ValidationError({"upi_id": "Invalid UPI ID format (e.g. user@bank)."})
            self.upi_id = upi

        if self.account_holder_name:
            self.account_holder_name = self.account_holder_name.strip()

    def mark_verified(self, user):
        """Mark this bank account as verified by an authorized user."""
        self.is_verified = True
        self.verified_at = timezone.now()
        self.verified_by = user
        self.save(update_fields=["is_verified", "verified_at", "verified_by"])
