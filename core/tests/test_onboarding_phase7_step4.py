from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.organizations.models import Organization, OrganizationMembership
from core.forms_onboarding import OnboardingStep4PolicyForm
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingProfile

User = get_user_model()


class OnboardingPhase7WizardStep4Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Aura Labs", code="AURA", is_active=True)
        self.dept = Department.objects.create(name="Product Engineering", code="AURA_ENG", organization=self.org)
        self.pos = JobPosition.objects.create(title="Frontend Architect", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="aura_hr", password="password123")

        self.candidate = User.objects.create_user(
            username="neha_verma",
            email="neha.verma@example.com",
            first_name="Neha",
            last_name="Verma",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Neha",
            last_name="Verma",
            email="neha.verma@example.com",
            phone="9876543210",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=15),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate,
            name="Neha Verma",
            email="neha.verma@example.com",
            designation="Frontend Architect",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=3000,
            joined_on=date.today() + timedelta(days=15),
            employment_status="PROBATION",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        self.onboarding_profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=self.candidate,
            organization=self.org,
            current_step=OnboardingProfile.STEP_4_POLICIES,
            step_1_completed=True,
            step_2_completed=True,
            step_3_completed=True,
            date_of_birth=date(1991, 3, 15),
            current_address="Sector 62, Noida",
            permanent_address="Sector 62, Noida",
            emergency_contact_name="Amit Verma",
            emergency_contact_relationship="Spouse",
            emergency_contact_phone="+91 9999988888",
            pan_number="ABCDE1111A",
            aadhaar_number="123412341234",
            bank_name="ICICI Bank",
            account_number="001122334455",
            ifsc_code="ICIC0000001",
        )

    def test_step4_form_validations_require_all_policies(self):
        # Missing NDA agreement
        form_data = {
            "agreed_code_of_conduct": True,
            "agreed_security_policy": True,
            "agreed_nda": False,  # Missing
            "policy_signed_full_name": "Neha Verma",
        }
        form = OnboardingStep4PolicyForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form.is_valid())
        self.assertIn("agreed_nda", form.errors)

        # Missing digital signature name
        form_data["agreed_nda"] = True
        form_data["policy_signed_full_name"] = ""
        form2 = OnboardingStep4PolicyForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form2.is_valid())
        self.assertIn("policy_signed_full_name", form2.errors)

    def test_wizard_step4_get_authenticated(self):
        self.client.login(username="neha_verma", password="password123")
        response = self.client.get(reverse("onboarding_wizard_step4"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company Policies &amp; Digital Sign-Off")
        self.assertContains(response, "Code of Conduct")
        self.assertContains(response, "InfoSec Policy")
        self.assertContains(response, "Confidentiality / NDA")

    def test_wizard_step4_successful_submission_and_audit_capture(self):
        self.client.login(username="neha_verma", password="password123")
        post_data = {
            "agreed_code_of_conduct": True,
            "agreed_security_policy": True,
            "agreed_nda": True,
            "policy_signed_full_name": "Neha Verma",
        }
        response = self.client.post(
            reverse("onboarding_wizard_step4"),
            data=post_data,
            follow=True,
            REMOTE_ADDR="192.168.1.100"
        )
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertTrue(self.onboarding_profile.step_4_completed)
        self.assertEqual(self.onboarding_profile.current_step, OnboardingProfile.STEP_5_REVIEW)
        self.assertTrue(self.onboarding_profile.agreed_code_of_conduct)
        self.assertTrue(self.onboarding_profile.agreed_security_policy)
        self.assertTrue(self.onboarding_profile.agreed_nda)
        self.assertEqual(self.onboarding_profile.policy_signed_full_name, "Neha Verma")
        self.assertIsNotNone(self.onboarding_profile.policy_accepted_at)
        self.assertEqual(self.onboarding_profile.policy_signer_ip, "192.168.1.100")
