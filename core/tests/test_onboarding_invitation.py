import uuid
from datetime import date, timedelta
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation


class CandidateInvitationModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Tech", code="ACME_ONBOARD")
        self.dept = Department.objects.create(organization=self.org, name="Engineering", code="ENG_OB")
        self.position = JobPosition.objects.create(organization=self.org, title="DevOps Engineer", department=self.dept)
        self.hr_user = User.objects.create_user(username="hr_recruiter", password="password123")
        self.joining_date = date.today() + timedelta(days=14)

    def test_create_invitation_with_default_token_and_expiry(self):
        invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Rohan",
            last_name="Deshmukh",
            email="rohan.deshmukh@example.com",
            department=self.dept,
            position=self.position,
            proposed_joining_date=self.joining_date,
            invited_by=self.hr_user,
        )

        self.assertIsNotNone(invitation.token)
        self.assertIsInstance(invitation.token, uuid.UUID)
        self.assertEqual(invitation.status, CandidateInvitation.STATUS_INVITED)
        self.assertEqual(invitation.full_name, "Rohan Deshmukh")
        self.assertTrue(invitation.is_valid())

        # Expiration must be approximately 72 hours from now
        expected_expiry = timezone.now() + timedelta(hours=72)
        diff = abs((invitation.expires_at - expected_expiry).total_seconds())
        self.assertLess(diff, 10)  # within 10 seconds

    def test_invitation_is_invalid_when_expired(self):
        invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Sneha",
            last_name="Kulkarni",
            email="sneha.kulkarni@example.com",
            proposed_joining_date=self.joining_date,
            expires_at=timezone.now() - timedelta(minutes=5),  # expired 5 min ago
        )
        self.assertFalse(invitation.is_valid())

    def test_accept_invitation_success(self):
        invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Amit",
            last_name="Jadhav",
            email="amit.jadhav@example.com",
            proposed_joining_date=self.joining_date,
        )

        emp = Employee.objects.create(
            organization=self.org,
            name="Amit Jadhav",
            email="amit.jadhav@example.com",
            basic_salary=45000,
            department=self.dept,
        )

        invitation.accept(employee=emp)
        invitation.refresh_from_db()

        self.assertEqual(invitation.status, CandidateInvitation.STATUS_ACCEPTED)
        self.assertIsNotNone(invitation.accepted_at)
        self.assertEqual(invitation.employee_profile, emp)
        self.assertFalse(invitation.is_valid())  # accepted is no longer valid for re-acceptance

    def test_accept_expired_invitation_raises_validation_error(self):
        invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Pooja",
            last_name="Shinde",
            email="pooja.shinde@example.com",
            proposed_joining_date=self.joining_date,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        with self.assertRaises(ValidationError):
            invitation.accept()

    def test_revoke_invitation(self):
        invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Kiran",
            last_name="Mane",
            email="kiran.mane@example.com",
            proposed_joining_date=self.joining_date,
        )
        self.assertEqual(invitation.status, CandidateInvitation.STATUS_INVITED)

        invitation.revoke()
        invitation.refresh_from_db()

        self.assertEqual(invitation.status, CandidateInvitation.STATUS_REVOKED)
        self.assertFalse(invitation.is_valid())

        # Cannot revoke already revoked invitation
        with self.assertRaises(ValidationError):
            invitation.revoke()

    def test_unique_active_invitation_constraint_per_email(self):
        CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Vikram",
            last_name="Rao",
            email="vikram.rao@example.com",
            proposed_joining_date=self.joining_date,
            status=CandidateInvitation.STATUS_INVITED,
        )

        # Creating another active invitation for the same email in the same org must raise IntegrityError
        with self.assertRaises(IntegrityError):
            CandidateInvitation.objects.create(
                organization=self.org,
                first_name="Vikram",
                last_name="Rao",
                email="vikram.rao@example.com",
                proposed_joining_date=self.joining_date,
                status=CandidateInvitation.STATUS_INVITED,
            )
