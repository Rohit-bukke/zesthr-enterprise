from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core.models import (
    Department,
    Employee,
    EmploymentHistory,
    JobPosition,
    OrganizationStatutoryConfig,
    EmployeeStatutoryProfile,
    SecurityEvent,
)
from core.models_banking import EmployeeBankAccount
from core.models_offboarding import ExitRequest, ExitSettlement
from core.services.offboarding import (
    calculate_fnf_settlement,
    finalize_fnf_and_deactivate_employee,
    review_exit_request,
    submit_resignation,
)

User = get_user_model()


class OffboardingPhase15StatementTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Stark Industries", code="STARK", is_active=True)
        self.org2 = Organization.objects.create(name="Wayne Enterprises", code="WAYNE", is_active=True)

        self.dept = Department.objects.create(name="Engineering", code="ENG", organization=self.org1)
        self.pos = JobPosition.objects.create(title="Software Engineer", department=self.dept, organization=self.org1)

        # Statutory config
        OrganizationStatutoryConfig.objects.create(
            organization=self.org1,
            epf_enabled=True,
            epf_wage_ceiling=Decimal("15000.00"),
            epf_employee_rate=Decimal("12.00"),
            esi_enabled=False,
            pt_enabled=True,
            pt_state="KARNATAKA",
        )

        # Users
        self.hr_user = User.objects.create_user(
            username="pepper",
            email="pepper@stark.com",
            password="testpassword123",
            first_name="Pepper",
            last_name="Potts",
            is_superuser=True,
        )
        OrganizationMembership.objects.create(organization=self.org1, user=self.hr_user, is_active=True)

        self.emp_user = User.objects.create_user(
            username="peter",
            email="peter@stark.com",
            password="testpassword123",
            first_name="Peter",
            last_name="Parker",
        )
        OrganizationMembership.objects.create(organization=self.org1, user=self.emp_user, is_active=True)

        self.emp = Employee.objects.create(
            user=self.emp_user,
            name="Peter Parker",
            email="peter@stark.com",
            organization=self.org1,
            department=self.dept,
            position=self.pos,
            employment_status="ACTIVE",
            joined_on=date(2021, 6, 1),
            basic_salary=Decimal("60000.00"),
        )

        EmployeeStatutoryProfile.objects.create(
            employee=self.emp,
            pan_number="ABCDE1234F",
            uan_number="100900800700",
            epf_enrolled=True,
            epf_restrict_to_ceiling=True,
            esi_enrolled=False,
            pt_enrolled=True,
            pt_state="KARNATAKA",
            monthly_tds=Decimal("1000.00"),
        )

        EmployeeBankAccount.objects.create(
            employee=self.emp,
            bank_name="Avengers Bank",
            account_number="987654321012",
            ifsc_code="AVEN0001234",
            account_holder_name="Peter Parker",
            account_type="SALARY",
        )

        # Org 2 User (Staff member belonging to Org 2, non-superuser)
        self.org2_user = User.objects.create_user(
            username="bruce",
            email="bruce@wayne.com",
            password="testpassword123",
            is_staff=True,
        )
        OrganizationMembership.objects.create(organization=self.org2, user=self.org2_user, is_active=True)

        # Prepare approved exit request
        self.resignation_date = date(2026, 5, 1)
        self.lwd = date(2026, 5, 31)
        self.exit_req = submit_resignation(
            employee=self.emp,
            requested_lwd=self.lwd,
            reason_category="BETTER_OPPORTUNITY",
            reason_detail="Independent superhero freelance duties",
            resignation_date=self.resignation_date,
        )
        self.exit_req.official_lwd = self.lwd
        self.exit_req.status = ExitRequest.STATUS_APPROVED
        self.exit_req.save()

        # Calculate settlement
        self.settlement = calculate_fnf_settlement(
            self.exit_req,
            user=self.hr_user,
            overrides={"gratuity_override": Decimal("0.00")},
        )

    def test_settlement_statement_view_renders_correctly(self):
        """Test accessing /offboarding/requests/<pk>/settlement/statement/ renders credentials and ledger."""
        self.client.login(username="pepper", password="testpassword123")
        url = reverse("offboarding_settlement_statement", kwargs={"pk": self.exit_req.pk})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Full &amp; Final Settlement Statement")
        self.assertContains(resp, "Peter Parker")
        self.assertContains(resp, "ABCDE1234F")
        self.assertContains(resp, "100900800700")
        self.assertContains(resp, "Avengers Bank")
        self.assertContains(resp, "Prorated Salary")
        self.assertContains(resp, "Total Deductions")
        self.assertContains(resp, "Net Payable")

    def test_final_hr_sign_off_action_and_lock(self):
        """Test final HR sign-off locks settlement and marks request as SETTLED."""
        self.client.login(username="pepper", password="testpassword123")
        action_url = reverse("offboarding_settlement_action", kwargs={"pk": self.exit_req.pk})

        resp = self.client.post(action_url, {
            "action": "sign_off",
            "sign_off_notes": "All clearances cleared and verified. Ready for accounts release.",
        })
        self.assertEqual(resp.status_code, 302)

        self.settlement.refresh_from_db()
        self.exit_req.refresh_from_db()

        self.assertTrue(self.settlement.is_locked)
        self.assertEqual(self.settlement.status, ExitSettlement.STATUS_SETTLED)
        self.assertEqual(self.settlement.signed_off_by, self.hr_user)
        self.assertIsNotNone(self.settlement.signed_off_at)
        self.assertIn("STARK-", self.settlement.statement_number)
        self.assertEqual(self.exit_req.status, ExitRequest.STATUS_SETTLED)

    def test_employee_and_user_account_deactivation(self):
        """Test that signing off F&F deactivates employee and their login user account."""
        self.emp.refresh_from_db()
        self.emp_user.refresh_from_db()
        self.assertTrue(self.emp.is_active)
        self.assertTrue(self.emp_user.is_active)
        self.assertEqual(self.emp.employment_status, "ACTIVE")

        # Execute sign-off
        finalize_fnf_and_deactivate_employee(
            settlement_id=self.settlement.id,
            user=self.hr_user,
            sign_off_notes="Final release",
        )

        self.emp.refresh_from_db()
        self.emp_user.refresh_from_db()

        self.assertEqual(self.emp.employment_status, "RESIGNED")
        self.assertFalse(self.emp.is_active)
        self.assertEqual(self.emp.separation_date, self.lwd)
        self.assertFalse(self.emp_user.is_active)

    def test_immutable_audit_logging_on_deactivation(self):
        """Test that deactivation produces records in EmploymentHistory and SecurityEvent."""
        finalize_fnf_and_deactivate_employee(
            settlement_id=self.settlement.id,
            user=self.hr_user,
            sign_off_notes="Final settlement signed off",
        )

        # Check EmploymentHistory records
        status_hist = EmploymentHistory.objects.filter(
            employee=self.emp,
            field="employment_status",
            new_value="RESIGNED",
        ).first()
        self.assertIsNotNone(status_hist)
        self.assertEqual(status_hist.old_value, "ACTIVE")

        sep_hist = EmploymentHistory.objects.filter(
            employee=self.emp,
            field="separation",
        ).first()
        self.assertIsNotNone(sep_hist)
        self.assertIn("F&F Settled", sep_hist.new_value)

        # Check SecurityEvent
        sec_event = SecurityEvent.objects.filter(
            user=self.emp_user,
            event_type=SecurityEvent.ACCOUNT_DEACTIVATED,
        ).first()
        self.assertIsNotNone(sec_event)

    def test_double_sign_off_prevention(self):
        """Test that calling finalize_fnf_and_deactivate_employee on an already locked settlement raises ValidationError."""
        finalize_fnf_and_deactivate_employee(
            settlement_id=self.settlement.id,
            user=self.hr_user,
            sign_off_notes="First sign off",
        )

        with self.assertRaises(ValidationError) as ctx:
            finalize_fnf_and_deactivate_employee(
                settlement_id=self.settlement.id,
                user=self.hr_user,
                sign_off_notes="Attempted duplicate sign off",
            )
        self.assertIn("already been signed off and locked", str(ctx.exception))

    def test_cross_tenant_isolation(self):
        """Test that HR user from Organization B cannot view or sign off statement from Organization A."""
        self.client.login(username="bruce", password="testpassword123")

        # Statement view
        statement_url = reverse("offboarding_settlement_statement", kwargs={"pk": self.exit_req.pk})
        resp = self.client.get(statement_url)
        self.assertEqual(resp.status_code, 404)

        # Action endpoint
        action_url = reverse("offboarding_settlement_action", kwargs={"pk": self.exit_req.pk})
        resp2 = self.client.post(action_url, {"action": "sign_off"})
        self.assertEqual(resp2.status_code, 404)
