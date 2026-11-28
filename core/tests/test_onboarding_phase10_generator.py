from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingChecklist, OnboardingProfile, OnboardingTask
from core.services.onboarding import (
    DEFAULT_DEPARTMENT_TEMPLATES,
    create_or_get_onboarding_checklist,
    generate_provisioning_tasks,
    get_default_templates,
    instantiate_onboarding_workflow,
)

User = get_user_model()


class OnboardingPhase10GeneratorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Stark Industries", code="STARK", is_active=True)
        self.dept = Department.objects.create(name="Arc Reactor Lab", code="STARK_ARC", organization=self.org)
        self.pos = JobPosition.objects.create(title="Propulsion Specialist", department=self.dept, organization=self.org)

        self.hr_user = User.objects.create_user(username="pepper_potts", password="password123", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_user, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Peter",
            last_name="Parker",
            email="peter.parker@example.com",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=10),
            invited_by=self.hr_user,
            status=CandidateInvitation.STATUS_INVITED,
        )

    def test_default_templates_structure(self):
        """Verify default department templates have all required roles and keys."""
        roles = [
            OnboardingTask.ROLE_IT,
            OnboardingTask.ROLE_HR,
            OnboardingTask.ROLE_FACILITIES,
            OnboardingTask.ROLE_MANAGER,
        ]
        for role in roles:
            self.assertIn(role, DEFAULT_DEPARTMENT_TEMPLATES)
            templates = DEFAULT_DEPARTMENT_TEMPLATES[role]
            self.assertGreater(len(templates), 0)
            for tpl in templates:
                self.assertIn("title", tpl)
                self.assertIn("description", tpl)
                self.assertIn("days_due", tpl)
                self.assertIsInstance(tpl["days_due"], int)

    def test_get_default_templates_filtering(self):
        """Verify template retrieval by department role vs full list."""
        it_templates = get_default_templates(department_role=OnboardingTask.ROLE_IT)
        self.assertEqual(len(it_templates), len(DEFAULT_DEPARTMENT_TEMPLATES[OnboardingTask.ROLE_IT]))
        for tpl in it_templates:
            self.assertEqual(tpl["department_role"], OnboardingTask.ROLE_IT)

        all_templates = get_default_templates()
        total_expected = sum(len(v) for v in DEFAULT_DEPARTMENT_TEMPLATES.values())
        self.assertEqual(len(all_templates), total_expected)

    def test_create_or_get_onboarding_checklist(self):
        """Verify checklist creation and idempotent retrieval."""
        checklist1 = create_or_get_onboarding_checklist(self.invitation)
        self.assertIsNotNone(checklist1.pk)
        self.assertEqual(checklist1.organization, self.org)
        self.assertEqual(checklist1.invitation, self.invitation)
        self.assertEqual(checklist1.title, f"Onboarding Checklist: {self.invitation.full_name}")

        # Idempotent retrieval
        checklist2 = create_or_get_onboarding_checklist(self.invitation)
        self.assertEqual(checklist1.pk, checklist2.pk)

    def test_generate_provisioning_tasks(self):
        """Verify default provisioning tasks instantiation across departments."""
        checklist = create_or_get_onboarding_checklist(self.invitation)
        tasks = generate_provisioning_tasks(checklist)

        total_expected = sum(len(v) for v in DEFAULT_DEPARTMENT_TEMPLATES.values())
        self.assertEqual(len(tasks), total_expected)
        self.assertEqual(checklist.tasks.count(), total_expected)

        # Check due dates offset against proposed joining date
        joining = self.invitation.proposed_joining_date
        it_tasks = checklist.tasks.filter(department_role=OnboardingTask.ROLE_IT)
        self.assertTrue(it_tasks.exists())
        for task in it_tasks:
            self.assertGreaterEqual(task.due_date, joining)

        # Check idempotency: calling generate_provisioning_tasks again creates 0 new tasks
        tasks_again = generate_provisioning_tasks(checklist)
        self.assertEqual(len(tasks_again), 0)
        self.assertEqual(checklist.tasks.count(), total_expected)

    def test_custom_templates_provisioning(self):
        """Verify provisioning with custom template list."""
        checklist = create_or_get_onboarding_checklist(self.invitation)
        custom_tpls = [
            {
                "department_role": OnboardingTask.ROLE_IT,
                "title": "Custom Quantum Computing Cluster Access",
                "description": "Configure quantum simulator keys",
                "days_due": 3,
            }
        ]
        created = generate_provisioning_tasks(checklist, custom_templates=custom_tpls)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].title, "Custom Quantum Computing Cluster Access")

    def test_instantiate_onboarding_workflow_helper(self):
        """Verify the full instantiate_onboarding_workflow service helper."""
        checklist, tasks = instantiate_onboarding_workflow(self.invitation)
        self.assertIsNotNone(checklist.pk)
        self.assertGreater(len(tasks), 0)
        self.assertEqual(checklist.total_tasks, len(tasks))
        self.assertEqual(checklist.status, OnboardingChecklist.STATUS_PENDING)

    def test_candidate_registration_triggers_task_generation(self):
        """Verify that accepting invitation via candidate landing generates onboarding tasks."""
        url = reverse("onboarding_candidate_landing", kwargs={"token": self.invitation.token})
        post_data = {
            "first_name": "Peter",
            "last_name": "Parker",
            "username": "spidey_pete",
            "phone": "9876543210",
            "password": "Password123!@#",
            "password_confirm": "Password123!@#",
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, CandidateInvitation.STATUS_ACCEPTED)

        # Checklist and tasks must be created automatically
        checklist = getattr(self.invitation, "onboarding_checklist", None)
        self.assertIsNotNone(checklist)
        self.assertGreater(checklist.tasks.count(), 0)

        # Verify roles generated
        roles_in_checklist = set(checklist.tasks.values_list("department_role", flat=True))
        self.assertIn(OnboardingTask.ROLE_IT, roles_in_checklist)
        self.assertIn(OnboardingTask.ROLE_HR, roles_in_checklist)
        self.assertIn(OnboardingTask.ROLE_FACILITIES, roles_in_checklist)
        self.assertIn(OnboardingTask.ROLE_MANAGER, roles_in_checklist)

    def test_onboarding_review_approve_ensures_checklist(self):
        """Verify that HR review approval ensures checklist and tasks are present."""
        candidate_user = User.objects.create_user(
            username="peter_parker_emp",
            email="peter.parker.emp@example.com",
            first_name="Peter",
            last_name="Parker",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=candidate_user, is_active=True)

        self.invitation.status = CandidateInvitation.STATUS_ACCEPTED
        self.invitation.save()

        profile = OnboardingProfile.objects.create(
            invitation=self.invitation,
            user=candidate_user,
            organization=self.org,
            current_step=OnboardingProfile.STEP_5_REVIEW,
            status=OnboardingProfile.STATUS_SUBMITTED,
        )

        self.client.force_login(self.hr_user)
        url = reverse("onboarding_review_action", kwargs={"pk": profile.pk})
        response = self.client.post(url, {"action": "approve", "notes": "Approved by Pepper"})
        self.assertEqual(response.status_code, 302)

        profile.refresh_from_db()
        self.assertEqual(profile.status, OnboardingProfile.STATUS_VERIFIED)

        # Checklist must exist
        checklist = OnboardingChecklist.objects.get(invitation=self.invitation)
        self.assertGreater(checklist.tasks.count(), 0)
