from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core import roles
from core.models import Department, Employee, JobPosition
from core.models_offboarding import ExitClearanceItem, ExitRequest
from core.services.offboarding import (
    action_clearance_item,
    generate_exit_clearance_items,
    review_exit_request,
    submit_resignation,
)

User = get_user_model()


class OffboardingPhase13ClearanceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Wayne Enterprises", code="WAYNE", is_active=True)
        self.dept = Department.objects.create(name="Applied Sciences", code="WAYNE_SCI", organization=self.org)
        self.pos = JobPosition.objects.create(title="Defense Analyst", department=self.dept, organization=self.org)

        # HR Admin
        self.hr_user = User.objects.create_user(username="lucius_fox", password="password123", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_user, is_active=True)

        # Manager
        self.manager_user = User.objects.create_user(username="alfred_p", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.manager_user, is_active=True)
        self.manager_emp = Employee.objects.create(
            user=self.manager_user,
            name="Alfred Pennyworth",
            email="alfred@example.com",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=Decimal("80000.00"),
            joined_on=timezone.localdate() - timedelta(days=365),
            employment_status="ACTIVE",
        )

        # Employee (Resigning)
        self.emp_user = User.objects.create_user(username="dick_grayson", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.emp_user, is_active=True)
        self.employee = Employee.objects.create(
            user=self.emp_user,
            name="Dick Grayson",
            email="dick@example.com",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            manager=self.manager_emp,
            basic_salary=Decimal("50000.00"),
            joined_on=timezone.localdate() - timedelta(days=200),
            employment_status="ACTIVE",
        )

        # Create active exit request for testing
        self.exit_req = submit_resignation(
            employee=self.employee,
            requested_lwd=timezone.localdate() + timedelta(days=30),
            reason_category="CAREER_GROWTH",
            reason_detail="Moving to Bludhaven operations.",
        )

    def test_auto_generation_of_clearance_checklist_on_approval(self):
        """Exit approval automatically instantiates 12 clearance items across 4 departments."""
        self.assertEqual(self.exit_req.clearance_items.count(), 0)

        reviewed = review_exit_request(
            exit_request_id=self.exit_req.pk,
            reviewer=self.hr_user,
            action="approve",
        )

        self.assertEqual(reviewed.status, ExitRequest.STATUS_APPROVED)
        items = reviewed.clearance_items.all()
        self.assertEqual(items.count(), 12)

        # Department distribution check
        dept_counts = {}
        for item in items:
            dept_counts[item.department_role] = dept_counts.get(item.department_role, 0) + 1
            self.assertEqual(item.status, ExitClearanceItem.STATUS_PENDING)
            self.assertEqual(item.organization, self.org)

        self.assertEqual(dept_counts[ExitClearanceItem.ROLE_IT], 3)
        self.assertEqual(dept_counts[ExitClearanceItem.ROLE_FINANCE], 3)
        self.assertEqual(dept_counts[ExitClearanceItem.ROLE_HR], 3)
        self.assertEqual(dept_counts[ExitClearanceItem.ROLE_FACILITIES], 3)

    def test_idempotent_clearance_generation(self):
        """Calling generate_exit_clearance_items multiple times avoids creating duplicate records."""
        items_first = generate_exit_clearance_items(self.exit_req)
        self.assertEqual(len(items_first), 12)

        items_second = generate_exit_clearance_items(self.exit_req)
        self.assertEqual(len(items_second), 0)
        self.assertEqual(self.exit_req.clearance_items.count(), 12)

    def test_action_clearance_item_service(self):
        """Action service allows clearing, blocking, reopening, and annotating clearance checkpoints."""
        items = generate_exit_clearance_items(self.exit_req)
        item = items[0]

        # 1. Clear item
        cleared = action_clearance_item(item.pk, user=self.hr_user, action="clear", notes="All credentials revoked")
        self.assertEqual(cleared.status, ExitClearanceItem.STATUS_CLEARED)
        self.assertEqual(cleared.cleared_by, self.hr_user)
        self.assertIsNotNone(cleared.cleared_at)
        self.assertEqual(cleared.notes, "All credentials revoked")

        # 2. Block item
        blocked = action_clearance_item(item.pk, user=self.hr_user, action="block", notes="Pending laptop charger")
        self.assertEqual(blocked.status, ExitClearanceItem.STATUS_BLOCKED)
        self.assertEqual(blocked.notes, "Pending laptop charger")

        # 3. Reopen item
        reopened = action_clearance_item(item.pk, user=self.hr_user, action="reopen")
        self.assertEqual(reopened.status, ExitClearanceItem.STATUS_PENDING)
        self.assertIsNone(reopened.cleared_at)
        self.assertIsNone(reopened.cleared_by)

        # 4. Invalid action raises error
        with self.assertRaises(ValidationError):
            action_clearance_item(item.pk, user=self.hr_user, action="destroy")

    def test_exit_request_clearance_progress_properties(self):
        """ExitRequest clearance calculation properties compute totals and completion accurately."""
        items = generate_exit_clearance_items(self.exit_req)
        self.assertEqual(self.exit_req.total_clearance_items, 12)
        self.assertEqual(self.exit_req.cleared_clearance_items, 0)
        self.assertEqual(self.exit_req.clearance_percentage, 0)
        self.assertFalse(self.exit_req.is_clearance_complete)

        # Clear half of the items (6 items)
        for i in range(6):
            action_clearance_item(items[i].pk, user=self.hr_user, action="clear")

        self.assertEqual(self.exit_req.cleared_clearance_items, 6)
        self.assertEqual(self.exit_req.clearance_percentage, 50)
        self.assertFalse(self.exit_req.is_clearance_complete)

        # Clear the remaining 6 items
        for i in range(6, 12):
            action_clearance_item(items[i].pk, user=self.hr_user, action="clear")

        self.assertEqual(self.exit_req.cleared_clearance_items, 12)
        self.assertEqual(self.exit_req.clearance_percentage, 100)
        self.assertTrue(self.exit_req.is_clearance_complete)

    def test_clearance_console_view(self):
        """Console view renders properly with metrics, filters, and gate badges."""
        review_exit_request(self.exit_req.pk, reviewer=self.hr_user, action="approve")

        self.client.force_login(self.hr_user)
        response = self.client.get(reverse("offboarding_clearance_console"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Multi-Departmental Exit Clearance Console")
        self.assertContains(response, "Dick Grayson")
        self.assertContains(response, "IT Systems")
        self.assertContains(response, "Finance / Payroll")

        # Test filtering by role
        it_response = self.client.get(reverse("offboarding_clearance_console") + "?role=IT")
        self.assertEqual(it_response.status_code, 200)

        # Test filtering by status
        pending_response = self.client.get(reverse("offboarding_clearance_console") + "?status=PENDING")
        self.assertEqual(pending_response.status_code, 200)

    def test_clearance_action_endpoint(self):
        """Action endpoint processes POST requests and updates item state."""
        items = generate_exit_clearance_items(self.exit_req)
        item = items[0]

        self.client.force_login(self.hr_user)
        url = reverse("offboarding_clearance_action", kwargs={"pk": item.pk})

        resp = self.client.post(url, {"action": "clear", "notes": "Asset returned in pristine condition."})
        self.assertEqual(resp.status_code, 302)

        item.refresh_from_db()
        self.assertEqual(item.status, ExitClearanceItem.STATUS_CLEARED)
        self.assertEqual(item.notes, "Asset returned in pristine condition.")

    def test_cross_tenant_isolation(self):
        """Clearance item from another organization cannot be modified by external users."""
        other_org = Organization.objects.create(name="LexCorp", code="LEX", is_active=True)
        other_user = User.objects.create_user(username="lex_luthor", password="password123")
        OrganizationMembership.objects.create(organization=other_org, user=other_user, is_active=True)

        items = generate_exit_clearance_items(self.exit_req)
        item = items[0]

        self.client.force_login(other_user)
        url = reverse("offboarding_clearance_action", kwargs={"pk": item.pk})

        # Regular employee in other org does not have permission or access
        resp = self.client.post(url, {"action": "clear"})
        self.assertIn(resp.status_code, [403, 404])
