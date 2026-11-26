import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.forms_onboarding import CandidateRegistrationForm
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation

User = get_user_model()


class OnboardingPhase3LandingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="NexGen Corp", code="NEXGEN", is_active=True)
        self.dept = Department.objects.create(name="Engineering", code="NEX_ENG", organization=self.org)
        self.pos = JobPosition.objects.create(title="Site Reliability Engineer", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="recruiter", password="password123")

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Priya",
            last_name="Nair",
            email="priya.nair@example.com",
            phone="9876543210",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=21),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_INVITED,
            expires_at=timezone.now() + timedelta(days=3),
        )

    def test_invalid_token_returns_404(self):
        random_token = uuid.uuid4()
        response = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": random_token}))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Invitation Unavailable", status_code=404)

    def test_inactive_organization_returns_403(self):
        self.org.is_active = False
        self.org.save()
        response = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "inactive", status_code=403)

    def test_expired_invitation_displays_error(self):
        self.invitation.expires_at = timezone.now() - timedelta(hours=1)
        self.invitation.save()
        response = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has expired")

    def test_revoked_invitation_displays_error(self):
        self.invitation.status = CandidateInvitation.STATUS_REVOKED
        self.invitation.save()
        response = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has been revoked")

    def test_valid_token_get_renders_form_with_prefilled_data(self):
        response = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome, Priya!")
        self.assertContains(response, "Site Reliability Engineer")
        self.assertContains(response, "priya.nair@example.com")
        self.assertContains(response, 'value="priya.nair"')

    def test_candidate_registration_form_validation(self):
        # Mismatched passwords
        form_data = {
            "username": "priyanair",
            "first_name": "Priya",
            "last_name": "Nair",
            "phone": "9876543210",
            "password": "SecurePassword123!",
            "password_confirm": "MismatchPass123!",
        }
        form = CandidateRegistrationForm(data=form_data, invitation=self.invitation)
        self.assertFalse(form.is_valid())
        self.assertIn("password_confirm", form.errors)

        # Existing username taken
        User.objects.create_user(username="priyanair", password="password123")
        form_data["password_confirm"] = "SecurePassword123!"
        form_taken = CandidateRegistrationForm(data=form_data, invitation=self.invitation)
        self.assertFalse(form_taken.is_valid())
        self.assertIn("username", form_taken.errors)

    def test_candidate_successful_acceptance_and_account_creation(self):
        post_data = {
            "username": "pnair_sre",
            "first_name": "Priya",
            "last_name": "Nair",
            "phone": "9876543210",
            "password": "SuperSecretPass123!",
            "password_confirm": "SuperSecretPass123!",
        }
        response = self.client.post(
            reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}),
            data=post_data,
            follow=True
        )
        self.assertEqual(response.status_code, 200)

        # Check User created and authenticated
        user = User.objects.get(username="pnair_sre")
        self.assertEqual(user.email, "priya.nair@example.com")
        self.assertEqual(user.first_name, "Priya")
        self.assertEqual(user.last_name, "Nair")
        self.assertTrue(user.check_password("SuperSecretPass123!"))

        # Check OrganizationMembership created
        membership = OrganizationMembership.objects.get(user=user, organization=self.org)
        self.assertTrue(membership.is_active)

        # Check Employee created
        employee = Employee.objects.get(user=user)
        self.assertEqual(employee.organization, self.org)
        self.assertEqual(employee.name, "Priya Nair")
        self.assertEqual(employee.designation, "Site Reliability Engineer")
        self.assertEqual(employee.department, self.dept)
        self.assertEqual(employee.position, self.pos)
        self.assertEqual(employee.employment_status, "PROBATION")
        self.assertEqual(employee.basic_salary, Decimal("0.00"))

        # Check invitation accepted
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, CandidateInvitation.STATUS_ACCEPTED)
        self.assertIsNotNone(self.invitation.accepted_at)
        self.assertEqual(self.invitation.employee_profile, employee)

        # Log out candidate to test public revisit of accepted invite
        self.client.logout()
        revisit_resp = self.client.get(reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token}))
        self.assertEqual(revisit_resp.status_code, 200)
        self.assertContains(revisit_resp, "already been accepted")
