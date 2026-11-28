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


class OnboardingProfile(models.Model):
    """
    Candidate onboarding progress, draft data, and multi-step wizard state persistence.
    Created when candidate registers; tracks steps 1 to 4 before final submission.
    """
    STEP_1_PERSONAL = 1
    STEP_2_STATUTORY = 2
    STEP_3_BANKING = 3
    STEP_4_POLICIES = 4
    STEP_5_REVIEW = 5

    WIZARD_STEP_CHOICES = [
        (STEP_1_PERSONAL, "Personal & Emergency Details"),
        (STEP_2_STATUTORY, "Statutory Compliance"),
        (STEP_3_BANKING, "Banking Details"),
        (STEP_4_POLICIES, "Company Policies"),
        (STEP_5_REVIEW, "Submission & Verification"),
    ]

    STATUS_DRAFT = "DRAFT"
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_VERIFIED = "VERIFIED"
    STATUS_REJECTED = "REJECTED"

    ONBOARDING_STATUS_CHOICES = [
        (STATUS_DRAFT, "In Progress (Draft)"),
        (STATUS_SUBMITTED, "Submitted for Verification"),
        (STATUS_VERIFIED, "Verified & Completed"),
        (STATUS_REJECTED, "Changes Requested"),
    ]

    GENDER_CHOICES = [
        ("M", "Male"),
        ("F", "Female"),
        ("O", "Other"),
        ("N", "Prefer not to say"),
    ]

    BLOOD_GROUP_CHOICES = [
        ("A+", "A+"),
        ("A-", "A-"),
        ("B+", "B+"),
        ("B-", "B-"),
        ("AB+", "AB+"),
        ("AB-", "AB-"),
        ("O+", "O+"),
        ("O-", "O-"),
    ]

    invitation = models.OneToOneField(
        CandidateInvitation,
        on_delete=models.CASCADE,
        related_name="onboarding_profile"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_profiles"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="onboarding_profiles"
    )
    current_step = models.PositiveSmallIntegerField(
        default=STEP_1_PERSONAL,
        choices=WIZARD_STEP_CHOICES
    )
    status = models.CharField(
        max_length=20,
        choices=ONBOARDING_STATUS_CHOICES,
        default=STATUS_DRAFT
    )

    # Step 1: Personal Profile
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=2, choices=GENDER_CHOICES, blank=True)
    blood_group = models.CharField(max_length=5, choices=BLOOD_GROUP_CHOICES, blank=True)
    current_address = models.TextField(blank=True, help_text="Current residential address")
    permanent_address = models.TextField(blank=True, help_text="Permanent residential address")
    same_as_current_address = models.BooleanField(
        default=False,
        help_text="Whether permanent address is the same as current address"
    )

    # Step 1: Emergency Contact
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_relationship = models.CharField(max_length=50, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    emergency_contact_alt_phone = models.CharField(max_length=20, blank=True)

    # Step 2: Statutory Compliance & Identity Details
    pan_number = models.CharField(
        max_length=10,
        blank=True,
        help_text="10-character Permanent Account Number (e.g. ABCDE1234F)"
    )
    pan_document = models.FileField(
        upload_to="onboarding/documents/%Y/%m/",
        blank=True,
        null=True,
        help_text="Uploaded PAN card image or PDF proof"
    )
    aadhaar_number = models.CharField(
        max_length=12,
        blank=True,
        help_text="12-digit Aadhaar unique identification number"
    )
    aadhaar_document = models.FileField(
        upload_to="onboarding/documents/%Y/%m/",
        blank=True,
        null=True,
        help_text="Uploaded Aadhaar card image or PDF proof"
    )
    has_existing_uan = models.BooleanField(
        default=False,
        help_text="Whether candidate already has an existing EPFO UAN"
    )
    uan_number = models.CharField(
        max_length=12,
        blank=True,
        help_text="12-digit Universal Account Number (if existing PF member)"
    )

    # Step 3: Banking Details & Cheque Proof
    bank_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Name of the bank (e.g. HDFC Bank, SBI, ICICI)"
    )
    branch_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Bank branch name or city"
    )
    account_holder_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Beneficiary name as registered with the bank"
    )
    account_number = models.CharField(
        max_length=35,
        blank=True,
        help_text="Bank account number (9 to 18 digits)"
    )
    ifsc_code = models.CharField(
        max_length=11,
        blank=True,
        help_text="11-character IFSC code (e.g. HDFC0001234)"
    )
    account_type = models.CharField(
        max_length=20,
        default="SALARY",
        choices=[
            ("SALARY", "Salary Account"),
            ("SAVINGS", "Savings Account"),
            ("CURRENT", "Current Account"),
        ]
    )
    cheque_document = models.FileField(
        upload_to="onboarding/documents/%Y/%m/",
        blank=True,
        null=True,
        help_text="Uploaded cancelled cheque or bank passbook copy"
    )

    # Step 4: Company Policies Acknowledgment & Digital Sign-Off
    agreed_code_of_conduct = models.BooleanField(
        default=False,
        help_text="Candidate agreement to Company Code of Conduct & Workplace Ethics"
    )
    agreed_security_policy = models.BooleanField(
        default=False,
        help_text="Candidate agreement to Information Security & Acceptable Use Policy"
    )
    agreed_nda = models.BooleanField(
        default=False,
        help_text="Candidate agreement to Non-Disclosure Agreement (NDA) & Confidentiality"
    )
    policy_signed_full_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="Digital signature name matching legal declaration"
    )
    policy_accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Exact timestamp of electronic acceptance"
    )
    policy_signer_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="IP address of device executing policy sign-off"
    )

    # Step completion markers
    step_1_completed = models.BooleanField(default=False)
    step_2_completed = models.BooleanField(default=False)
    step_3_completed = models.BooleanField(default=False)
    step_4_completed = models.BooleanField(default=False)

    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_onboardings"
    )
    verification_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "onboarding_profile"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Onboarding: {self.invitation.full_name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.same_as_current_address and self.current_address:
            self.permanent_address = self.current_address
        super().save(*args, **kwargs)


class OnboardingChecklist(models.Model):
    """
    Checklist coordinating provisioning and orientation tasks across IT, HR, Facilities, and Manager.
    """
    STATUS_PENDING = "PENDING"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_COMPLETED = "COMPLETED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_COMPLETED, "Completed"),
    ]

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="onboarding_checklists"
    )
    invitation = models.OneToOneField(
        CandidateInvitation,
        on_delete=models.CASCADE,
        related_name="onboarding_checklist"
    )
    employee = models.ForeignKey(
        "core.Employee",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="onboarding_checklists"
    )
    title = models.CharField(max_length=200, help_text="e.g. Onboarding Checklist: Jane Doe")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING
    )
    target_date = models.DateField(null=True, blank=True, help_text="Expected completion deadline")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "onboarding_checklist"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def total_tasks(self):
        return self.tasks.count()

    @property
    def completed_tasks(self):
        return self.tasks.filter(status=OnboardingTask.STATUS_COMPLETED).count()

    @property
    def progress_percentage(self):
        total = self.total_tasks
        if total == 0:
            return 0
        return int((self.completed_tasks / total) * 100)

    def update_overall_status(self):
        """Recomputes and saves overall status based on child task completion."""
        total = self.total_tasks
        if total == 0:
            self.status = self.STATUS_PENDING
        else:
            completed = self.completed_tasks
            if completed == total:
                self.status = self.STATUS_COMPLETED
            elif completed > 0 or self.tasks.filter(status=OnboardingTask.STATUS_IN_PROGRESS).exists():
                self.status = self.STATUS_IN_PROGRESS
            else:
                self.status = self.STATUS_PENDING
        self.save(update_fields=["status", "updated_at"])


class OnboardingTask(models.Model):
    """
    Individual departmental onboarding task (e.g., IT hardware, HR paperwork, Facilities desk, Manager 1-on-1).
    """
    ROLE_IT = "IT"
    ROLE_HR = "HR"
    ROLE_FACILITIES = "FACILITIES"
    ROLE_MANAGER = "MANAGER"

    DEPARTMENT_ROLE_CHOICES = [
        (ROLE_IT, "IT / Tech Provisioning"),
        (ROLE_HR, "Human Resources"),
        (ROLE_FACILITIES, "Admin / Facilities"),
        (ROLE_MANAGER, "Hiring Manager"),
    ]

    STATUS_PENDING = "PENDING"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_COMPLETED = "COMPLETED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_COMPLETED, "Completed"),
    ]

    checklist = models.ForeignKey(
        OnboardingChecklist,
        on_delete=models.CASCADE,
        related_name="tasks"
    )
    department_role = models.CharField(
        max_length=20,
        choices=DEPARTMENT_ROLE_CHOICES,
        default=ROLE_HR
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_onboarding_tasks"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Fulfillment notes (e.g. Asset tag, Slack handle)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "onboarding_task"
        ordering = ["due_date", "id"]

    def __str__(self):
        return f"[{self.department_role}] {self.title} ({self.get_status_display()})"

    def mark_completed(self, user=None, notes=""):
        self.status = self.STATUS_COMPLETED
        self.completed_at = timezone.now()
        if user:
            self.completed_by = user
        if notes:
            self.notes = notes
        self.save(update_fields=["status", "completed_at", "completed_by", "notes", "updated_at"])
        self.checklist.update_overall_status()


