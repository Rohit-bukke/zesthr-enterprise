from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core import roles
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingChecklist, OnboardingTask
from core.services.onboarding import instantiate_onboarding_workflow

User = get_user_model()


class OnboardingPhase11TrackerTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Stark Industries", code="STARK_IND", is_active=True)
        self.org2 = Organization.objects.create(name="Wayne Enterprises", code="WAYNE_ENT", is_active=True)

        self.dept = Department.objects.create(name="Robotics", code="STARK_ROB", organization=self.org1)
        self.pos = JobPosition.objects.create(title="Robotics Engineer", department=self.dept, organization=self.org1)

        # Admin user
        self.admin_user = User.objects.create_user(username="tony_stark", password="password123", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org1, user=self.admin_user, is_active=True)

        # Candidate user (unauthorized for tracker)
        self.candidate_user = User.objects.create_user(username="peter_parker", password="password123")
        OrganizationMembership.objects.create(organization=self.org1, user=self.candidate_user, is_active=True)

        # Candidate invitation and workflow instantiation
        today = timezone.localdate()
        self.invitation = CandidateInvitation.objects.create(
            organization=self.org1,
            first_name="Peter",
            last_name="Parker",
            email="peter.parker@example.com",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=today + timedelta(days=7),
            invited_by=self.admin_user,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate_user,
            name="Peter Parker",
            email="peter.parker@example.com",
            designation="Robotics Engineer",
            department=self.dept,
            position=self.pos,
            organization=self.org1,
            basic_salary=Decimal("60000.00"),
            joined_on=today + timedelta(days=7),
            employment_status="PROBATION",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

        # Instantiate checklist and default tasks
        self.checklist, self.tasks = instantiate_onboarding_workflow(self.invitation, employee=self.employee)

    def test_anonymous_redirects_to_login(self):
        """Anonymous user is redirected to login."""
        response = self.client.get(reverse("onboarding_tracker"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_candidate_forbidden_from_tracker(self):
        """Candidate without staff/admin privileges receives 403 Forbidden."""
        self.client.force_login(self.candidate_user)
        response = self.client.get(reverse("onboarding_tracker"))
        self.assertEqual(response.status_code, 403)

    def test_tracker_view_renders_for_admin(self):
        """Admin can view the tracker with candidate checklist and metrics."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("onboarding_tracker"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "onboarding/tracker.html")
        self.assertContains(response, "Departmental Onboarding Task Tracker")
        self.assertContains(response, "Peter Parker")
        self.assertContains(response, "Robotics Engineer")
        self.assertContains(response, "Robotics")

    def test_filter_by_department_role(self):
        """Filtering by department role returns only tasks belonging to that department."""
        self.client.force_login(self.admin_user)

        # Filter IT
        response_it = self.client.get(reverse("onboarding_tracker") + "?role=IT")
        self.assertEqual(response_it.status_code, 200)
        items = response_it.context["page_obj"].object_list
        self.assertEqual(len(items), 1)
        it_tasks = items[0]["tasks"]
        self.assertTrue(all(t.department_role == OnboardingTask.ROLE_IT for t in it_tasks))
        self.assertGreater(len(it_tasks), 0)

        # Filter Facilities
        response_fac = self.client.get(reverse("onboarding_tracker") + "?role=FACILITIES")
        self.assertEqual(response_fac.status_code, 200)
        fac_tasks = response_fac.context["page_obj"].object_list[0]["tasks"]
        self.assertTrue(all(t.department_role == OnboardingTask.ROLE_FACILITIES for t in fac_tasks))
        self.assertGreater(len(fac_tasks), 0)

    def test_search_candidates(self):
        """Search query matches candidate full name and position."""
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("onboarding_tracker") + "?q=Parker")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)

        response_nomatch = self.client.get(reverse("onboarding_tracker") + "?q=BruceWayneNonExistent")
        self.assertEqual(response_nomatch.status_code, 200)
        self.assertEqual(len(response_nomatch.context["page_obj"]), 0)

    def test_task_action_start_complete_and_reopen(self):
        """Verify one-click start, complete, and reopen actions with notes update."""
        self.client.force_login(self.admin_user)
        task = self.tasks[0]
        url = reverse("onboarding_task_action", kwargs={"pk": task.pk})

        # 1. Start task (move to IN_PROGRESS)
        resp1 = self.client.post(url, {"action": "in_progress", "notes": "Configuring M3 Max"})
        self.assertEqual(resp1.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.STATUS_IN_PROGRESS)
        self.assertEqual(task.notes, "Configuring M3 Max")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, OnboardingChecklist.STATUS_IN_PROGRESS)

        # 2. Complete task
        resp2 = self.client.post(url, {"action": "complete", "notes": "Asset Tag #STARK-1002"})
        self.assertEqual(resp2.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.STATUS_COMPLETED)
        self.assertEqual(task.completed_by, self.admin_user)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.notes, "Asset Tag #STARK-1002")

        # 3. Reopen task
        resp3 = self.client.post(url, {"action": "reopen", "notes": "Pending security review"})
        self.assertEqual(resp3.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.STATUS_PENDING)
        self.assertIsNone(task.completed_at)
        self.assertIsNone(task.completed_by)

    def test_all_tasks_completion_marks_checklist_completed(self):
        """When every task is completed, checklist status automatically transitions to COMPLETED."""
        self.client.force_login(self.admin_user)

        for task in self.tasks:
            url = reverse("onboarding_task_action", kwargs={"pk": task.pk})
            self.client.post(url, {"action": "complete", "notes": "Fitted"})

        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, OnboardingChecklist.STATUS_COMPLETED)
        self.assertEqual(self.checklist.progress_percentage, 100)

    def test_task_action_cross_tenant_forbidden(self):
        """User cannot modify tasks of a checklist belonging to a different organization."""
        other_user = User.objects.create_user(username="bruce_wayne", password="password123")
        OrganizationMembership.objects.create(organization=self.org2, user=other_user, is_active=True)

        self.client.force_login(other_user)
        task = self.tasks[0]
        url = reverse("onboarding_task_action", kwargs={"pk": task.pk})
        response = self.client.post(url, {"action": "complete"})
        self.assertEqual(response.status_code, 403)
