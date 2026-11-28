from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core import roles
from core.models import Department, Employee, ExpenseCategory, JobPosition
from core.models_offboarding import ExitRequest, ExitSettlement
from core.roles import sync_roles
from core.services.offboarding import (
    approve_fnf_settlement,
    calculate_fnf_settlement,
    finalize_fnf_and_deactivate_employee,
    generate_exit_clearance_items,
    submit_resignation,
)

User = get_user_model()


class ProjectVerificationFixesTests(TestCase):
    def setUp(self):
        sync_roles()
        self.org = Organization.objects.create(name="ZestHR Corp", code="ZEST", is_active=True)
        self.dept = Department.objects.create(name="Engineering", code="ENG", organization=self.org)
        self.pos = JobPosition.objects.create(title="Engineer", department=self.dept, organization=self.org)

        # HR Admin
        self.hr_admin = User.objects.create_user(username="hr_admin", password="pw", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_admin, is_active=True)

        # HR Officer (has CHANGE_EMPLOYEE, NO salary permissions)
        self.hr_officer = User.objects.create_user(username="hr_off", password="pw")
        self.hr_officer.groups.add(Group.objects.get(name=roles.HR_OFFICER))
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_officer, is_active=True)

        # Payroll Officer (has VIEW_EMPLOYEE_PAY, GENERATE_PAYROLL, NO CHANGE_EMPLOYEE)
        self.payroll_officer = User.objects.create_user(username="pay_off", password="pw")
        self.payroll_officer.groups.add(Group.objects.get(name=roles.PAYROLL_OFFICER))
        OrganizationMembership.objects.create(organization=self.org, user=self.payroll_officer, is_active=True)

        # Read-Only Admin (VIEW_EMPLOYEE only)
        self.ro_admin = User.objects.create_user(username="ro_admin", password="pw")
        self.ro_admin.groups.add(Group.objects.get(name=roles.READ_ONLY_ADMIN))
        OrganizationMembership.objects.create(organization=self.org, user=self.ro_admin, is_active=True)

        # Manager A (regular Employee with direct_reports, no RBAC role)
        self.mgr_a_user = User.objects.create_user(username="mgr_a", password="pw")
        OrganizationMembership.objects.create(organization=self.org, user=self.mgr_a_user, is_active=True)
        self.mgr_a = Employee.objects.create(
            user=self.mgr_a_user,
            name="Manager A",
            email="mgra@zest.local",
            organization=self.org,
            department=self.dept,
            position=self.pos,
            basic_salary=Decimal("80000.00"),
            joined_on=date(2023, 1, 1),
            employment_status="ACTIVE",
        )

        # Report under Manager A
        self.emp_a_user = User.objects.create_user(username="emp_a", password="pw")
        OrganizationMembership.objects.create(organization=self.org, user=self.emp_a_user, is_active=True)
        self.emp_a = Employee.objects.create(
            user=self.emp_a_user,
            name="Report Under A",
            email="empa@zest.local",
            organization=self.org,
            department=self.dept,
            position=self.pos,
            manager=self.mgr_a,
            basic_salary=Decimal("50000.00"),
            joined_on=date(2024, 1, 1),
            employment_status="ACTIVE",
        )

        # Unrelated Employee (not under Manager A)
        self.emp_b_user = User.objects.create_user(username="emp_b", password="pw")
        OrganizationMembership.objects.create(organization=self.org, user=self.emp_b_user, is_active=True)
        self.emp_b = Employee.objects.create(
            user=self.emp_b_user,
            name="Unrelated B",
            email="empb@zest.local",
            organization=self.org,
            department=self.dept,
            position=self.pos,
            basic_salary=Decimal("55000.00"),
            joined_on=date(2024, 1, 1),
            employment_status="ACTIVE",
        )

        res_date = date(2026, 5, 1)
        lwd = date(2026, 5, 31)
        self.exit_a = submit_resignation(
            employee=self.emp_a,
            requested_lwd=lwd,
            reason_category="CAREER_GROWTH",
            resignation_date=res_date,
        )
        self.exit_b = submit_resignation(
            employee=self.emp_b,
            requested_lwd=lwd,
            reason_category="CAREER_GROWTH",
            resignation_date=res_date,
        )
        self.exit_b.status = ExitRequest.STATUS_APPROVED
        self.exit_b.official_lwd = lwd
        self.exit_b.save()
        self.clearance_items_b = generate_exit_clearance_items(self.exit_b)
        self.settlement_b = calculate_fnf_settlement(self.exit_b, user=self.hr_admin)

    def test_manager_cannot_access_non_reports_or_clearance_or_settlements(self):
        """A reporting manager may only see direct reports' exit requests and never clearance/F&F."""
        self.client.force_login(self.mgr_a_user)

        # List shows emp_a only, not emp_b
        list_resp = self.client.get(reverse("offboarding_requests_list"))
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Report Under A")
        self.assertNotContains(list_resp, "Unrelated B")

        # Detail allowed for direct report, 404 for non-report
        self.assertEqual(
            self.client.get(reverse("offboarding_request_detail", kwargs={"pk": self.exit_a.pk})).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("offboarding_request_detail", kwargs={"pk": self.exit_b.pk})).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("offboarding_request_review", kwargs={"pk": self.exit_b.pk}), {"action": "approve"}).status_code,
            404,
        )

        # Clearance and F&F settlements strictly forbidden (403)
        self.assertEqual(self.client.get(reverse("offboarding_clearance_console")).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("offboarding_clearance_action", kwargs={"pk": self.clearance_items_b[0].pk}), {"action": "clear"}).status_code,
            403,
        )
        self.assertEqual(self.client.get(reverse("offboarding_settlements_list")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("offboarding_settlement_detail", kwargs={"pk": self.exit_b.pk})).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("offboarding_settlement_statement", kwargs={"pk": self.exit_b.pk})).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("offboarding_settlement_action", kwargs={"pk": self.exit_b.pk}), {"action": "sign_off"}).status_code,
            403,
        )

    def test_role_separation_hr_officer_payroll_officer_and_readonly_admin(self):
        """Enforce privilege separation across HR Officer, Payroll Officer, and Read-Only Admin."""
        # Read-Only Admin cannot manage exit workflows or F&F
        self.client.force_login(self.ro_admin)
        self.assertEqual(self.client.get(reverse("offboarding_requests_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("offboarding_clearance_console")).status_code, 403)
        self.assertEqual(self.client.get(reverse("offboarding_settlements_list")).status_code, 403)

        # HR Officer has exit requests & clearance, NOT F&F Settlements (no salary access)
        self.client.force_login(self.hr_officer)
        self.assertEqual(self.client.get(reverse("offboarding_requests_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("offboarding_clearance_console")).status_code, 200)
        self.assertEqual(self.client.get(reverse("offboarding_settlements_list")).status_code, 403)

        # Payroll Officer can access F&F Settlements and see the sidebar link
        self.client.force_login(self.payroll_officer)
        resp = self.client.get(reverse("offboarding_settlements_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "F&amp;F Settlements")

    def test_locked_settlement_cannot_be_recalculated_or_reapproved(self):
        """Once signed off and locked, settlement cannot be overwritten or un-settled."""
        finalize_fnf_and_deactivate_employee(self.settlement_b.pk, user=self.hr_admin, sign_off_notes="Final")

        with self.assertRaises(ValidationError):
            calculate_fnf_settlement(self.exit_b, user=self.hr_admin, overrides={"additional_earnings": Decimal("9999.00")})

        with self.assertRaises(ValidationError):
            approve_fnf_settlement(self.settlement_b.pk, user=self.hr_admin, remarks="Re-approving")

        # HTTP endpoint redirects with error rather than crashing or mutating
        self.client.force_login(self.hr_admin)
        resp = self.client.post(
            reverse("offboarding_settlement_action", kwargs={"pk": self.exit_b.pk}),
            {"action": "approve"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.settlement_b.refresh_from_db()
        self.assertEqual(self.settlement_b.status, ExitSettlement.STATUS_SETTLED)
        self.assertTrue(self.settlement_b.is_locked)

    def test_clearance_console_pending_count_renders_correctly(self):
        """Clearance console displays accurate integer pending count instead of broken template math."""
        self.client.force_login(self.hr_admin)
        resp = self.client.get(reverse("offboarding_clearance_console"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "0 cleared, 1 pending")

    def test_invalid_decimal_input_on_compensation_and_expense_views(self):
        """Malformed numeric amounts yield validation errors instead of unhandled InvalidOperation 500."""
        self.client.force_login(self.hr_admin)
        resp1 = self.client.post(
            reverse("salary_revision_propose"),
            {
                "employee_id": self.emp_a.pk,
                "revised_salary": "not-a-number",
                "reason": "ANNUAL_APPRAISAL",
                "effective_date": timezone.localdate().strftime("%Y-%m-%d"),
            },
        )
        self.assertEqual(resp1.status_code, 200)

        cat = ExpenseCategory.objects.create(organization=self.org, name="Travel", code="TRV")
        self.client.force_login(self.emp_a_user)
        resp2 = self.client.post(
            reverse("expense_submit"),
            {
                "category_id": cat.pk,
                "title": "Taxi",
                "amount": "abc",
                "incurred_date": timezone.localdate().strftime("%Y-%m-%d"),
            },
        )
        self.assertEqual(resp2.status_code, 200)

    @override_settings(DEBUG=True)
    def test_seed_demo_assigns_organization_memberships_to_staff_accounts(self):
        """seed_demo grants OrganizationMembership to admin, hr.officer, and payroll.officer."""
        call_command("seed_demo")
        for uname in ("admin", "hr.officer", "payroll.officer"):
            u = User.objects.get(username=uname)
            self.assertTrue(
                OrganizationMembership.objects.filter(user=u, is_active=True).exists(),
                f"{uname} is missing an active OrganizationMembership",
            )
