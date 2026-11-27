import uuid
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.forms_onboarding import OnboardingStep1PersonalForm
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingProfile

User = get_user_model()


class OnboardingPhase4WizardStep1Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Quantum Corp", code="QUANTUM", is_active=True)
        self.dept = Department.objects.create(name="Data Engineering", code="QUANT_DATA", organization=self.org)
        self.pos = JobPosition.objects.create(title="Data Platform Engineer", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="hr_lead", password="password123")

        self.candidate_user = User.objects.create_user(
            username="arjun_sharma",
            email="arjun.sharma@example.com",
            first_name="Arjun",
            last_name="Sharma",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate_user, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Arjun",
            last_name="Sharma",
            email="arjun.sharma@example.com",
            phone="9876543210",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=14),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate_user,
            name="Arjun Sharma",
            email="arjun.sharma@example.com",
            designation="Data Platform Engineer",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=1000,
            joined_on=date.today() + timedelta(days=14),
            employment_status="PROBATION",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        self.onboarding_profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=self.candidate_user,
            organization=self.org,
            current_step=OnboardingProfile.STEP_1_PERSONAL,
        )

    def test_step1_form_validations(self):
        # Under 18 years old should fail validation
        underage_dob = date.today() - timedelta(days=16 * 365)
        form_data = {
            "date_of_birth": underage_dob,
            "gender": "M",
            "blood_group": "O+",
            "current_address": "123 Tech Park, Bengaluru",
            "same_as_current_address": True,
            "emergency_contact_name": "Ramesh Sharma",
            "emergency_contact_relationship": "Father",
            "emergency_contact_phone": "+91 9988776655",
        }
        form = OnboardingStep1PersonalForm(data=form_data, instance=self.onboarding_profile)
        self.assertFalse(form.is_valid())
        self.assertIn("date_of_birth", form.errors)

        # Valid adult DOB with same permanent address
        valid_dob = date(1995, 5, 20)
        form_data["date_of_birth"] = valid_dob
        valid_form = OnboardingStep1PersonalForm(data=form_data, instance=self.onboarding_profile)
        self.assertTrue(valid_form.is_valid())
        saved = valid_form.save()
        self.assertEqual(saved.permanent_address, "123 Tech Park, Bengaluru")

    def test_wizard_step1_get_authenticated(self):
        self.client.login(username="arjun_sharma", password="password123")
        response = self.client.get(reverse("onboarding_wizard_step1"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Personal &amp; Emergency Details")
        self.assertContains(response, "Data Platform Engineer")
        self.assertContains(response, "Quantum Corp")

    def test_wizard_step1_post_submission_advances_wizard(self):
        self.client.login(username="arjun_sharma", password="password123")
        post_data = {
            "date_of_birth": "1994-08-15",
            "gender": "M",
            "blood_group": "B+",
            "current_address": "404 Cyber City, Hyderabad, 500081",
            "same_as_current_address": True,
            "emergency_contact_name": "Sunita Sharma",
            "emergency_contact_relationship": "Mother",
            "emergency_contact_phone": "+91 9123456789",
        }
        response = self.client.post(reverse("onboarding_wizard_step1"), data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.onboarding_profile.refresh_from_db()
        self.assertTrue(self.onboarding_profile.step_1_completed)
        self.assertEqual(self.onboarding_profile.current_step, OnboardingProfile.STEP_2_STATUTORY)
        self.assertEqual(self.onboarding_profile.blood_group, "B+")
        self.assertEqual(self.onboarding_profile.emergency_contact_name, "Sunita Sharma")

        # Verify syncing to Employee profile
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.date_of_birth, date(1994, 8, 15))
        self.assertEqual(self.employee.address, "404 Cyber City, Hyderabad, 500081")
        self.assertEqual(self.employee.emergency_contact_name, "Sunita Sharma")
        self.assertEqual(self.employee.emergency_contact_phone, "+91 9123456789")

    def test_unauthenticated_redirects_to_login(self):
        response = self.client.get(reverse("onboarding_wizard_step1"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
