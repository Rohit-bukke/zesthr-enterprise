from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingProfile

User = get_user_model()


class OnboardingPhase8ReviewQueueTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Starlight Tech", code="STARLIGHT", is_active=True)
        self.dept = Department.objects.create(name="AI Systems", code="STAR_AI", organization=self.org)
        self.pos = JobPosition.objects.create(title="Machine Learning Engineer", department=self.dept, organization=self.org)

        # HR Lead with add_employee perm
        self.hr = User.objects.create_user(username="starlight_hr", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.hr, is_active=True)
        add_perm = Permission.objects.get(codename="add_employee")
        self.hr.user_permissions.add(add_perm)

        # Candidate user
        self.candidate = User.objects.create_user(
            username="kunal_verma",
            email="kunal.verma@example.com",
            first_name="Kunal",
            last_name="Verma",
            password="password123",
            is_active=True,
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Kunal",
            last_name="Verma",
            email="kunal.verma@example.com",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=7),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate,
            name="Kunal Verma",
            email="kunal.verma@example.com",
            designation="Machine Learning Engineer",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=4000,
            joined_on=date.today() + timedelta(days=7),
            employment_status="PROBATION",
            is_active=False,
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        self.onboarding_profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=self.candidate,
            organization=self.org,
            current_step=OnboardingProfile.STEP_5_REVIEW,
            step_1_completed=True,
            step_2_completed=True,
            step_3_completed=True,
            step_4_completed=True,
            status=OnboardingProfile.STATUS_DRAFT,
            date_of_birth=date(1995, 8, 20),
            current_address="100 Innovation Way, Hyderabad",
            permanent_address="100 Innovation Way, Hyderabad",
            emergency_contact_name="Ravi Verma",
            emergency_contact_relationship="Father",
            emergency_contact_phone="+91 9988776655",
            pan_number="ABCDE8888P",
            aadhaar_number="888877776666",
            bank_name="Axis Bank",
            account_number="918020011223344",
            ifsc_code="UTIB0000001",
            agreed_code_of_conduct=True,
            agreed_security_policy=True,
            agreed_nda=True,
            policy_signed_full_name="Kunal Verma",
        )

    def test_candidate_final_submission(self):
        self.client.login(username="kunal_verma", password="password123")
        response = self.client.post(reverse("onboarding_wizard_submit"), follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertEqual(self.onboarding_profile.status, OnboardingProfile.STATUS_SUBMITTED)
        self.assertIsNotNone(self.onboarding_profile.submitted_at)

    def test_review_queue_permission_denied_for_candidate(self):
        self.client.login(username="kunal_verma", password="password123")
        response = self.client.get(reverse("onboarding_review_queue"))
        self.assertEqual(response.status_code, 403)

    def test_review_queue_and_detail_for_hr(self):
        self.onboarding_profile.status = OnboardingProfile.STATUS_SUBMITTED
        self.onboarding_profile.save()

        self.client.login(username="starlight_hr", password="password123")
        response = self.client.get(reverse("onboarding_review_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kunal Verma")
        self.assertContains(response, "Awaiting Review")

        # Detail inspect
        detail_resp = self.client.get(reverse("onboarding_review_detail", kwargs={"pk": self.onboarding_profile.pk}))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, "Candidate Review: Kunal Verma")
        self.assertContains(detail_resp, "ABCDE8888P")
        self.assertContains(detail_resp, "Axis Bank")

    def test_hr_approve_and_activate_employee_action(self):
        self.onboarding_profile.status = OnboardingProfile.STATUS_SUBMITTED
        self.onboarding_profile.save()

        self.client.login(username="starlight_hr", password="password123")
        action_url = reverse("onboarding_review_action", kwargs={"pk": self.onboarding_profile.pk})
        post_data = {
            "action": "approve",
            "notes": "All identity and banking documents verified against originals.",
        }
        response = self.client.post(action_url, data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertEqual(self.onboarding_profile.status, OnboardingProfile.STATUS_VERIFIED)
        self.assertIsNotNone(self.onboarding_profile.verified_at)
        self.assertEqual(self.onboarding_profile.verified_by, self.hr)
        self.assertEqual(self.onboarding_profile.verification_notes, "All identity and banking documents verified against originals.")

        # Check Employee model activated
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.employment_status, "ACTIVE")
        self.assertTrue(self.employee.is_active)
        self.assertEqual(self.employee.address, "100 Innovation Way, Hyderabad")
        self.assertEqual(self.employee.emergency_contact_name, "Ravi Verma")
        self.assertEqual(self.employee.emergency_contact_phone, "+91 9988776655")

    def test_hr_reject_action_requests_changes(self):
        self.onboarding_profile.status = OnboardingProfile.STATUS_SUBMITTED
        self.onboarding_profile.save()

        self.client.login(username="starlight_hr", password="password123")
        action_url = reverse("onboarding_review_action", kwargs={"pk": self.onboarding_profile.pk})
        post_data = {
            "action": "reject",
            "notes": "Aadhaar photo is blurry. Please re-upload.",
        }
        response = self.client.post(action_url, data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertEqual(self.onboarding_profile.status, OnboardingProfile.STATUS_REJECTED)
        self.assertEqual(self.onboarding_profile.verification_notes, "Aadhaar photo is blurry. Please re-upload.")
