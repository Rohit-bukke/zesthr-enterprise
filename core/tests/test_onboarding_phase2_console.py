import uuid
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Department, JobPosition
from core.models_onboarding import CandidateInvitation
from core.forms_onboarding import CandidateInvitationForm

User = get_user_model()


class OnboardingPhase2ConsoleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp", code="ACME")
        self.other_org = Organization.objects.create(name="Globex Corp", code="GLOBEX")

        self.dept = Department.objects.create(name="Engineering", code="ENG", organization=self.org)
        self.desig = JobPosition.objects.create(title="Backend Engineer", department=self.dept, organization=self.org)

        # HR User with ADD_EMPLOYEE perm
        self.hr_user = User.objects.create_user(username="hr_user", email="hr@acme.com", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_user, is_active=True)
        add_emp_perm = Permission.objects.get(codename="add_employee")
        self.hr_user.user_permissions.add(add_emp_perm)

        # Normal User without perms
        self.normal_user = User.objects.create_user(username="regular_emp", email="user@acme.com", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.normal_user, is_active=True)

    def test_invitation_form_validation(self):
        # Target date in past should raise validation error
        form_data = {
            "first_name": "Rohan",
            "last_name": "Sharma",
            "email": "rohan@example.com",
            "phone": "9876543210",
            "department": self.dept.pk,
            "position": self.desig.pk,
            "proposed_joining_date": date.today() - timedelta(days=2),
            "expiry_days": 3,
        }
        form = CandidateInvitationForm(data=form_data, organization=self.org)
        self.assertFalse(form.is_valid())
        self.assertIn("proposed_joining_date", form.errors)

        # Valid form data
        form_data["proposed_joining_date"] = date.today() + timedelta(days=14)
        valid_form = CandidateInvitationForm(data=form_data, organization=self.org)
        self.assertTrue(valid_form.is_valid())

    def test_invitations_list_permission_denied_for_regular_user(self):
        self.client.login(username="regular_emp", password="password123")
        response = self.client.get(reverse("onboarding_invitations"))
        self.assertEqual(response.status_code, 403)

    def test_invitations_list_access_and_filtering(self):
        self.client.login(username="hr_user", password="password123")

        # Create two invitations
        inv1 = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Alice",
            last_name="Smith",
            email="alice@example.com",
            department=self.dept,
            position=self.desig,
            proposed_joining_date=date.today() + timedelta(days=7),
            invited_by=self.hr_user,
            status=CandidateInvitation.STATUS_INVITED,
        )
        inv2 = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Bob",
            last_name="Johnson",
            email="bob@example.com",
            department=self.dept,
            position=self.desig,
            proposed_joining_date=date.today() + timedelta(days=10),
            invited_by=self.hr_user,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        response = self.client.get(reverse("onboarding_invitations"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alice Smith")
        self.assertContains(response, "Bob Johnson")

        # Test status filter
        filter_resp = self.client.get(reverse("onboarding_invitations") + "?status=INVITED")
        self.assertEqual(filter_resp.status_code, 200)
        self.assertContains(filter_resp, "Alice Smith")
        self.assertNotContains(filter_resp, "Bob Johnson")

        # Test search
        search_resp = self.client.get(reverse("onboarding_invitations") + "?q=Bob")
        self.assertEqual(search_resp.status_code, 200)
        self.assertContains(search_resp, "Bob Johnson")
        self.assertNotContains(search_resp, "Alice Smith")

    def test_invitation_create_post(self):
        self.client.login(username="hr_user", password="password123")

        post_data = {
            "first_name": "Charlie",
            "last_name": "Brown",
            "email": "charlie@example.com",
            "phone": "9998887776",
            "department": self.dept.pk,
            "position": self.desig.pk,
            "proposed_joining_date": (date.today() + timedelta(days=20)).isoformat(),
            "expiry_days": 5,
        }
        response = self.client.post(reverse("onboarding_invitation_create"), data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        inv = CandidateInvitation.objects.get(email="charlie@example.com")
        self.assertEqual(inv.first_name, "Charlie")
        self.assertEqual(inv.organization, self.org)
        self.assertEqual(inv.invited_by, self.hr_user)
        self.assertEqual(inv.status, CandidateInvitation.STATUS_INVITED)
        self.assertTrue(inv.is_valid())

    def test_invitation_revoke_and_resend_action(self):
        self.client.login(username="hr_user", password="password123")
        inv = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Dana",
            last_name="Scully",
            email="dana@example.com",
            department=self.dept,
            position=self.desig,
            proposed_joining_date=date.today() + timedelta(days=15),
            invited_by=self.hr_user,
            status=CandidateInvitation.STATUS_INVITED,
        )

        # Revoke action
        revoke_url = reverse("onboarding_invitation_action", kwargs={"pk": inv.pk})
        resp = self.client.post(revoke_url, data={"action": "revoke"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CandidateInvitation.STATUS_REVOKED)

        # Resend action (re-activates and extends expiry)
        resp2 = self.client.post(revoke_url, data={"action": "resend"}, follow=True)
        self.assertEqual(resp2.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CandidateInvitation.STATUS_INVITED)
        self.assertTrue(inv.expires_at > timezone.now())
