from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.models import (
    Department,
    Employee,
    ExpenseCategory,
    ExpenseClaim,
    JobPosition,
    LeaveEntitlement,
    OrganizationStatutoryConfig,
    EmployeeStatutoryProfile,
)
from core.models_offboarding import ExitRequest, ExitSettlement
from core.services.offboarding import (
    approve_fnf_settlement,
    calculate_fnf_settlement,
    review_exit_request,
    submit_resignation,
)

User = get_user_model()


class OffboardingPhase14FnFTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Stark Industries", code="STARK", is_active=True)
        self.dept = Department.objects.create(name="R&D", code="STARK_RD", organization=self.org)
        self.pos = JobPosition.objects.create(title="Lead Engineer", department=self.dept, organization=self.org)

        # Statutory config
        OrganizationStatutoryConfig.objects.create(
            organization=self.org,
            epf_enabled=True,
            epf_wage_ceiling=Decimal("15000.00"),
            epf_employee_rate=Decimal("12.00"),
            esi_enabled=True,
            esi_wage_ceiling=Decimal("21000.00"),
            esi_employee_rate=Decimal("0.75"),
            pt_enabled=True,
            pt_state="KARNATAKA",
        )

        # HR Admin
        self.hr_user = User.objects.create_user(username="pepper_potts", password="password123", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_user, is_active=True)

        # Employee (Resigning - 2 years tenure)
        self.emp_user = User.objects.create_user(username="peter_parker", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.emp_user, is_active=True)
        self.employee = Employee.objects.create(
            user=self.emp_user,
            name="Peter Parker",
            email="peter@example.com",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=Decimal("60000.00"),
            joined_on=date(2024, 6, 1),
            employment_status="ACTIVE",
        )

        EmployeeStatutoryProfile.objects.create(
            employee=self.employee,
            epf_enrolled=True,
            epf_restrict_to_ceiling=True,
            esi_enrolled=False,
            pt_enrolled=True,
            pt_state="KARNATAKA",
            monthly_tds=Decimal("1000.00"),
        )

        # Resignation request with official LWD = 15th of June (30 day month)
        self.exit_req = submit_resignation(
            employee=self.employee,
            resignation_date=date(2026, 5, 16),
            requested_lwd=date(2026, 6, 15),
            reason_category="CAREER_GROWTH",
            reason_detail="Independent research internship.",
        )
        self.exit_req.official_lwd = date(2026, 6, 15)
        self.exit_req.status = ExitRequest.STATUS_APPROVED
        self.exit_req.save()

    def test_prorated_salary_calculation(self):
        """Prorated salary calculates exact worked days over total month calendar days."""
        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)

        # June has 30 days. LWD is June 15 -> worked 15 days
        self.assertEqual(settlement.days_in_final_month, 30)
        self.assertEqual(settlement.worked_days_in_final_month, 15)
        self.assertEqual(settlement.last_drawn_basic, Decimal("60000.00"))
        self.assertEqual(settlement.daily_basic_rate, Decimal("2000.00"))

        # Prorated = 60,000 * 15 / 30 = 30,000.00
        self.assertEqual(settlement.prorated_salary, Decimal("30000.00"))

    def test_leave_encashment_calculation(self):
        """Unused leave balance is encashed using the daily basic pay rate."""
        # Create entitlement with 10 days
        LeaveEntitlement.objects.create(
            employee=self.employee,
            leave_type="PL",
            year=2026,
            annual_days=Decimal("10.00"),
            carryover_days=Decimal("0.00"),
            monthly_accrual=False,
        )

        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertEqual(settlement.leave_encashment_days, Decimal("10.00"))
        # 10 days * 2000.00 daily rate = 20,000.00
        self.assertEqual(settlement.leave_encashment_amount, Decimal("20000.00"))

    def test_approved_unpaid_expenses_reimbursement(self):
        """Approved but unreimbursed expense claims are credited to final settlement earnings."""
        cat = ExpenseCategory.objects.create(organization=self.org, name="Lab Supplies", code="LAB")
        ExpenseClaim.objects.create(
            employee=self.employee,
            category=cat,
            claim_number="EXP-001",
            title="Oscilloscope probes",
            amount=Decimal("4500.00"),
            incurred_date=date(2026, 5, 20),
            status=ExpenseClaim.STATUS_APPROVED,
        )

        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertEqual(settlement.reimbursable_expenses_amount, Decimal("4500.00"))

    def test_gratuity_calculation_eligible_vs_ineligible(self):
        """Gratuity is 0 for tenure < 5 years, and (15 * basic * tenure)/26 when tenure >= 5 years."""
        # Case A: Ineligible (joined June 2024, LWD June 2026 -> 2 years)
        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertFalse(settlement.is_gratuity_eligible)
        self.assertEqual(settlement.gratuity_amount, Decimal("0.00"))

        # Case B: Eligible (joined June 2020 -> 6.04 years tenure)
        self.employee.joined_on = date(2020, 6, 1)
        self.employee.save(update_fields=["joined_on"])

        settlement_eligible = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertTrue(settlement_eligible.is_gratuity_eligible)
        # Tenure = 6 years. Gratuity = (15 * 60000 * 6) / 26 = 5400000 / 26 = 207692.31
        self.assertEqual(settlement_eligible.gratuity_amount, Decimal("207692.31"))

    def test_notice_shortfall_deduction(self):
        """Notice period shortfall days are recovered at the daily basic rate unless waived."""
        # Resignation date May 16, LWD June 15 = 30 days notice -> 0 shortfall
        self.exit_req.calculate_shortfall()
        self.assertEqual(self.exit_req.notice_shortfall_days, 0)

        # Early release: LWD is May 31 -> 15 shortfall days
        self.exit_req.official_lwd = date(2026, 5, 31)
        self.exit_req.shortfall_waived = False
        self.exit_req.calculate_shortfall()
        self.exit_req.save()
        self.assertEqual(self.exit_req.notice_shortfall_days, 15)

        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        # 15 days * 2000.00 = 30,000.00
        self.assertEqual(settlement.notice_shortfall_recovery, Decimal("30000.00"))

        # When waived: recovery is 0
        self.exit_req.shortfall_waived = True
        self.exit_req.calculate_shortfall()
        self.exit_req.save()

        settlement_waived = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertEqual(settlement_waived.notice_shortfall_recovery, Decimal("0.00"))

    def test_net_payable_vs_recoverable_balance(self):
        """Calculates accurate net payable and switches to recoverable if deductions exceed earnings."""
        # Regular case: Earnings > Deductions
        settlement = calculate_fnf_settlement(self.exit_req, user=self.hr_user)
        self.assertFalse(settlement.is_recoverable)
        self.assertGreater(settlement.net_amount, Decimal("0.00"))
        self.assertIn("Payable", settlement.payment_type_label)

        # Recoverable case: heavy asset damage & advance recovery
        overrides = {
            "asset_damage_recovery": Decimal("50000.00"),
            "salary_advance_recovery": Decimal("20000.00"),
        }
        settlement_rec = calculate_fnf_settlement(self.exit_req, user=self.hr_user, overrides=overrides)
        self.assertTrue(settlement_rec.is_recoverable)
        self.assertLess(settlement_rec.net_amount, Decimal("0.00"))
        self.assertEqual(settlement_rec.abs_net_amount, abs(settlement_rec.net_amount))
        self.assertIn("Recoverable", settlement_rec.payment_type_label)

    def test_settlement_views_and_actions(self):
        """F&F settlement console detail, recalculation, and approval views work properly."""
        self.client.force_login(self.hr_user)

        # 1. Detail console
        url = reverse("offboarding_settlement_detail", kwargs={"pk": self.exit_req.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Peter Parker")
        self.assertContains(resp, "Payable Earnings")
        self.assertContains(resp, "Deductible Recoveries")

        # 2. Recalculate action with overrides
        action_url = reverse("offboarding_settlement_action", kwargs={"pk": self.exit_req.pk})
        post_data = {
            "action": "recalculate",
            "worked_days_in_final_month": "14",
            "leave_encashment_days": "5.0",
            "additional_earnings": "5000.00",
            "additional_earnings_notes": "Project completion bonus",
            "asset_damage_recovery": "1500.00",
            "remarks": "Audited by Finance team.",
        }
        resp = self.client.post(action_url, post_data)
        self.assertEqual(resp.status_code, 302)

        settlement = ExitSettlement.objects.get(exit_request=self.exit_req)
        self.assertEqual(settlement.worked_days_in_final_month, 14)
        self.assertEqual(settlement.leave_encashment_days, Decimal("5.00"))
        self.assertEqual(settlement.additional_earnings, Decimal("5000.00"))
        self.assertEqual(settlement.asset_damage_recovery, Decimal("1500.00"))

        # 3. Approve action
        resp = self.client.post(action_url, {"action": "approve", "remarks": "Approved for disbursement."})
        self.assertEqual(resp.status_code, 302)

        settlement.refresh_from_db()
        self.assertEqual(settlement.status, ExitSettlement.STATUS_APPROVED)
        self.assertEqual(settlement.finalized_by, self.hr_user)

        # 4. Settlements list view
        list_url = reverse("offboarding_settlements_list")
        list_resp = self.client.get(list_url)
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Peter Parker")

    def test_cross_tenant_isolation(self):
        """Users from other tenants cannot view or action settlements of foreign organizations."""
        other_org = Organization.objects.create(name="Oscorp", code="OSCORP", is_active=True)
        other_user = User.objects.create_user(username="norman_osborn", password="password123")
        OrganizationMembership.objects.create(organization=other_org, user=other_user, is_active=True)

        self.client.force_login(other_user)
        url = reverse("offboarding_settlement_detail", kwargs={"pk": self.exit_req.pk})
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [403, 404])
