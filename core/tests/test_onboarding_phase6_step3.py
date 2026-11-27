from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.organizations.models import Organization, OrganizationMembership
from core.forms_onboarding import OnboardingStep3BankingForm
from core.models import Department, Employee, JobPosition
from core.models_banking import EmployeeBankAccount
from core.models_onboarding import CandidateInvitation, OnboardingProfile

User = get_user_model()


class OnboardingPhase6WizardStep3Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Vertex Dynamics", code="VERTEX", is_active=True)
        self.dept = Department.objects.create(name="FinTech Engineering", code="VERTEX_FIN", organization=self.org)
        self.pos = JobPosition.objects.create(title="Payment Systems Specialist", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="vertex_hr", password="password123")

        self.candidate = User.objects.create_user(
            username="tarun_mehta",
            email="tarun.mehta@example.com",
            first_name="Tarun",
            last_name="Mehta",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Tarun",
            last_name="Mehta",
            email="tarun.mehta@example.com",
            phone="9876543210",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=12),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate,
            name="Tarun Mehta",
            email="tarun.mehta@example.com",
            designation="Payment Systems Specialist",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=2000,
            joined_on=date.today() + timedelta(days=12),
            employment_status="PROBATION",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        self.onboarding_profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=self.candidate,
            organization=self.org,
            current_step=OnboardingProfile.STEP_3_BANKING,
            step_1_completed=True,
            step_2_completed=True,
            date_of_birth=date(1993, 7, 24),
            current_address="12 MG Road, Gurgaon",
            permanent_address="12 MG Road, Gurgaon",
            emergency_contact_name="Deepak Mehta",
            emergency_contact_relationship="Brother",
            emergency_contact_phone="+91 9777766666",
            pan_number="ABCDE9999Z",
            aadhaar_number="998877665544",
        )

    def test_step3_form_validations(self):
        # Invalid IFSC regex
        form_data = {
            "account_holder_name": "Tarun Mehta",
            "bank_name": "HDFC Bank",
            "branch_name": "Cyber Hub",
            "account_number": "123456789012",
            "account_number_confirm": "123456789012",
            "ifsc_code": "INVALID_IFSC",  # Invalid
            "account_type": "SALARY",
        }
        form = OnboardingStep3BankingForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form.is_valid())
        self.assertIn("ifsc_code", form.errors)

        # Mismatched account numbers
        form_data["ifsc_code"] = "HDFC0001234"
        form_data["account_number_confirm"] = "999999999999"
        form2 = OnboardingStep3BankingForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form2.is_valid())
        self.assertIn("account_number_confirm", form2.errors)

        # Account number too short (< 9 digits)
        form_data["account_number"] = "12345"
        form_data["account_number_confirm"] = "12345"
        form3 = OnboardingStep3BankingForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form3.is_valid())
        self.assertIn("account_number", form3.errors)

    def test_wizard_step3_get_authenticated(self):
        self.client.login(username="tarun_mehta", password="password123")
        response = self.client.get(reverse("onboarding_wizard_step3"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Banking Details &amp; Disbursement Setup")
        self.assertContains(response, "Beneficiary / Account Holder Name")
        self.assertContains(response, "11-Character IFSC Code")

    def test_wizard_step3_successful_submission_and_employee_bank_account_population(self):
        self.client.login(username="tarun_mehta", password="password123")

        dummy_cheque = SimpleUploadedFile("cheque.pdf", b"%PDF-1.4 dummy cancelled cheque", content_type="application/pdf")

        post_data = {
            "account_holder_name": "Tarun Mehta",
            "bank_name": "HDFC Bank",
            "branch_name": "Cyber City Gurgaon",
            "account_number": "50100223344556",
            "account_number_confirm": "50100223344556",
            "ifsc_code": "HDFC0000240",
            "account_type": "SALARY",
            "cheque_document": dummy_cheque,
        }
        response = self.client.post(reverse("onboarding_wizard_step3"), data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertTrue(self.onboarding_profile.step_3_completed)
        self.assertEqual(self.onboarding_profile.current_step, OnboardingProfile.STEP_4_POLICIES)
        self.assertEqual(self.onboarding_profile.bank_name, "HDFC Bank")
        self.assertEqual(self.onboarding_profile.account_number, "50100223344556")
        self.assertEqual(self.onboarding_profile.ifsc_code, "HDFC0000240")
        self.assertTrue(bool(self.onboarding_profile.cheque_document))

        # Check EmployeeBankAccount populated and integrated directly
        bank_record = EmployeeBankAccount.objects.get(employee=self.employee)
        self.assertEqual(bank_record.bank_name, "HDFC Bank")
        self.assertEqual(bank_record.account_holder_name, "Tarun Mehta")
        self.assertEqual(bank_record.account_number, "50100223344556")
        self.assertEqual(bank_record.ifsc_code, "HDFC0000240")
        self.assertEqual(bank_record.account_type, "SALARY")
        self.assertEqual(bank_record.payment_method, "NEFT")
