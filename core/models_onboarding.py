import uuid
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def default_invitation_expiry():
    return timezone.now() + timedelta(hours=72)


class CandidateInvitation(models.Model):
    """
    Time-expiring pre-onboarding invitation issued to a prospective new hire.
    Allows candidate to set up credentials and submit compliance data before their start date.
    """
    STATUS_INVITED = "INVITED"
    STATUS_ACCEPTED = "ACCEPTED"
    STATUS_EXPIRED = "EXPIRED"
    STATUS_REVOKED = "REVOKED"

    STATUS_CHOICES = [
        (STATUS_INVITED, "Invited"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_REVOKED, "Revoked"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="candidate_invitations"
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Cryptographically secure unique access token"
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(help_text="Primary candidate personal/work email")
    phone = models.CharField(max_length=20, blank=True)
    department = models.ForeignKey(
        "core.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="candidate_invitations"
    )
    position = models.ForeignKey(
        "core.JobPosition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="candidate_invitations"
    )
    proposed_joining_date = models.DateField(help_text="Expected employment start date")
    expires_at = models.DateTimeField(
        default=default_invitation_expiry,
        help_text="Token expiration timestamp (default: 72 hours from issue)"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_INVITED
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    employee_profile = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations"
    )

    class Meta:
        db_table = "candidate_invitation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(status="INVITED"),
                name="uniq_active_candidate_invite_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["token", "status"], name="candidate_invite_token_idx"),
            models.Index(fields=["organization", "status"], name="candidate_invite_org_stat_idx"),
        ]

    def __str__(self):
        return f"Invitation for {self.full_name} <{self.email}> ({self.get_status_display()})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def is_valid(self):
        """Checks if the invitation is active and has not expired."""
        if self.status != self.STATUS_INVITED:
            return False
        if timezone.now() > self.expires_at:
            # Mark expired in memory if accessed past expiration
            return False
        return True

    def accept(self, employee=None):
        """Marks the invitation as accepted and optionally links to the created employee."""
        if not self.is_valid():
            raise ValidationError("Cannot accept an invalid or expired invitation.")
        self.status = self.STATUS_ACCEPTED
        self.accepted_at = timezone.now()
        if employee:
            self.employee_profile = employee
        self.save(update_fields=["status", "accepted_at", "employee_profile"])

    def revoke(self):
        """Revokes an outstanding invitation."""
        if self.status != self.STATUS_INVITED:
            raise ValidationError(f"Cannot revoke an invitation in status {self.status}.")
        self.status = self.STATUS_REVOKED
        self.save(update_fields=["status"])
