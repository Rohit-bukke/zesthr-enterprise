from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.organizations.models import Organization, OrganizationMembership
from core.forms_onboarding import OnboardingStep2StatutoryForm
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingProfile
from core.models_statutory import EmployeeStatutoryProfile

User = get_user_model()


class OnboardingPhase5WizardStep2Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Apex Systems", code="APEX", is_active=True)
        self.dept = Department.objects.create(name="Cloud Platform", code="APEX_CLOUD", organization=self.org)
        self.pos = JobPosition.objects.create(title="Cloud Architect", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="apex_hr", password="password123")

        self.candidate = User.objects.create_user(
            username="kavita_krishnan",
            email="kavita.krishnan@example.com",
            first_name="Kavita",
            last_name="Krishnan",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Kavita",
            last_name="Krishnan",
            email="kavita.krishnan@example.com",
            phone="9876543210",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=10),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate,
            name="Kavita Krishnan",
            email="kavita.krishnan@example.com",
            designation="Cloud Architect",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=1500,
            joined_on=date.today() + timedelta(days=10),
            employment_status="PROBATION",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        self.onboarding_profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=self.candidate,
            organization=self.org,
            current_step=OnboardingProfile.STEP_2_STATUTORY,
            step_1_completed=True,
            date_of_birth=date(1992, 4, 12),
            current_address="Flat 102, Sunrise Apts, Pune",
            permanent_address="Flat 102, Sunrise Apts, Pune",
            emergency_contact_name="Mohan Krishnan",
            emergency_contact_relationship="Brother",
            emergency_contact_phone="+91 9888877777",
        )

    def test_step2_form_format_validations(self):
        # Invalid PAN format
        form_data = {
            "pan_number": "12345ABCDE",  # Incorrect format
            "aadhaar_number": "123456789012",
            "has_existing_uan": False,
        }
        form = OnboardingStep2StatutoryForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form.is_valid())
        self.assertIn("pan_number", form.errors)

        # Invalid Aadhaar format (non-12 digits)
        form_data["pan_number"] = "ABCDE1234F"
        form_data["aadhaar_number"] = "12345"
        form2 = OnboardingStep2StatutoryForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form2.is_valid())
        self.assertIn("aadhaar_number", form2.errors)

        # Missing UAN when has_existing_uan checked
        form_data["aadhaar_number"] = "987654321098"
        form_data["has_existing_uan"] = True
        form_data["uan_number"] = ""
        form3 = OnboardingStep2StatutoryForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form3.is_valid())
        self.assertIn("uan_number", form3.errors)

    def test_wizard_step2_get_authenticated(self):
        self.client.login(username="kavita_krishnan", password="password123")
        response = self.client.get(reverse("onboarding_wizard_step2"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Statutory Compliance &amp; Document Collection")
        self.assertContains(response, "Permanent Account Number (PAN)")
        self.assertContains(response, "Unique Identification (Aadhaar)")

    def test_wizard_step2_successful_submission_and_statutory_profile_sync(self):
        self.client.login(username="kavita_krishnan", password="password123")

        dummy_pan = SimpleUploadedFile("pan.pdf", b"%PDF-1.4 dummy pan content", content_type="application/pdf")
        dummy_aadhaar = SimpleUploadedFile("aadhaar.png", b"fake image bytes", content_type="image/png")

        post_data = {
            "pan_number": "ABCDE5678G",
            "aadhaar_number": "112233445566",
            "has_existing_uan": True,
            "uan_number": "100987654321",
            "pan_document": dummy_pan,
            "aadhaar_document": dummy_aadhaar,
        }
        response = self.client.post(reverse("onboarding_wizard_step2"), data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertTrue(self.onboarding_profile.step_2_completed)
        self.assertEqual(self.onboarding_profile.current_step, OnboardingProfile.STEP_3_BANKING)
        self.assertEqual(self.onboarding_profile.pan_number, "ABCDE5678G")
        self.assertEqual(self.onboarding_profile.aadhaar_number, "112233445566")
        self.assertEqual(self.onboarding_profile.uan_number, "100987654321")
        self.assertTrue(bool(self.onboarding_profile.pan_document))
        self.assertTrue(bool(self.onboarding_profile.aadhaar_document))

        # Check EmployeeStatutoryProfile automatically synced and integrated
        stat_profile = EmployeeStatutoryProfile.objects.get(employee=self.employee)
        self.assertEqual(stat_profile.pan_number, "ABCDE5678G")
        self.assertEqual(stat_profile.uan_number, "100987654321")
        self.assertTrue(stat_profile.epf_enrolled)
        self.assertTrue(stat_profile.esi_enrolled)
