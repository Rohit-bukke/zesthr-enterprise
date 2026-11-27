from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Department, Employee, JobPosition
from core.models_onboarding import CandidateInvitation, OnboardingChecklist, OnboardingTask

User = get_user_model()


class OnboardingPhase9TaskModelsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Cyberdyne Corp", code="CYBER", is_active=True)
        self.dept = Department.objects.create(name="Robotics", code="CYBER_ROB", organization=self.org)
        self.pos = JobPosition.objects.create(title="Hardware Systems Engineer", department=self.dept, organization=self.org)
        self.hr = User.objects.create_user(username="cyber_hr", password="password123")
        self.it_admin = User.objects.create_user(username="cyber_it", password="password123")

        self.candidate = User.objects.create_user(
            username="sarah_connor",
            email="sarah.connor@example.com",
            first_name="Sarah",
            last_name="Connor",
            password="password123",
        )
        OrganizationMembership.objects.create(organization=self.org, user=self.candidate, is_active=True)

        self.invitation = CandidateInvitation.objects.create(
            organization=self.org,
            first_name="Sarah",
            last_name="Connor",
            email="sarah.connor@example.com",
            department=self.dept,
            position=self.pos,
            proposed_joining_date=date.today() + timedelta(days=7),
            invited_by=self.hr,
            status=CandidateInvitation.STATUS_ACCEPTED,
        )

        self.employee = Employee.objects.create(
            user=self.candidate,
            name="Sarah Connor",
            email="sarah.connor@example.com",
            designation="Hardware Systems Engineer",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=5000,
            joined_on=date.today() + timedelta(days=7),
            employment_status="ACTIVE",
        )
        self.invitation.employee_profile = self.employee
        self.invitation.save()

    def test_checklist_and_task_creation(self):
        checklist = OnboardingChecklist.objects.create(
            organization=self.org,
            invitation=self.invitation,
            employee=self.employee,
            title="Onboarding Checklist: Sarah Connor",
            target_date=date.today() + timedelta(days=7),
        )
        self.assertEqual(checklist.status, OnboardingChecklist.STATUS_PENDING)
        self.assertEqual(checklist.total_tasks, 0)
        self.assertEqual(checklist.progress_percentage, 0)

        # Create tasks across departments
        task_it = OnboardingTask.objects.create(
            checklist=checklist,
            department_role=OnboardingTask.ROLE_IT,
            title="Provision Developer Laptop & Access Badges",
            due_date=date.today() + timedelta(days=5),
        )
        task_hr = OnboardingTask.objects.create(
            checklist=checklist,
            department_role=OnboardingTask.ROLE_HR,
            title="Verify PF & Statutory Registration",
            due_date=date.today() + timedelta(days=6),
        )
        task_mgr = OnboardingTask.objects.create(
            checklist=checklist,
            department_role=OnboardingTask.ROLE_MANAGER,
            title="Conduct Team Intro & Assign Onboarding Buddy",
            due_date=date.today() + timedelta(days=7),
        )

        self.assertEqual(checklist.total_tasks, 3)
        self.assertEqual(checklist.completed_tasks, 0)
        self.assertEqual(checklist.progress_percentage, 0)

        # Mark 1st task in progress
        task_it.status = OnboardingTask.STATUS_IN_PROGRESS
        task_it.save()
        checklist.update_overall_status()
        self.assertEqual(checklist.status, OnboardingChecklist.STATUS_IN_PROGRESS)

        # Mark 1st task completed
        task_it.mark_completed(user=self.it_admin, notes="MacBook Pro M3 Max - Asset Tag #CYB-9988")
        self.assertEqual(task_it.status, OnboardingTask.STATUS_COMPLETED)
        self.assertEqual(task_it.completed_by, self.it_admin)
        self.assertIsNotNone(task_it.completed_at)
        self.assertEqual(checklist.completed_tasks, 1)
        self.assertEqual(checklist.progress_percentage, 33)

        # Mark remaining tasks completed
        task_hr.mark_completed(user=self.hr, notes="All statutory documentation approved.")
        task_mgr.mark_completed(user=self.hr, notes="Buddy assigned.")

        checklist.refresh_from_db()
        self.assertEqual(checklist.completed_tasks, 3)
        self.assertEqual(checklist.progress_percentage, 100)
        self.assertEqual(checklist.status, OnboardingChecklist.STATUS_COMPLETED)
