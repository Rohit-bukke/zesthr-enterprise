from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Employee, Department, WorkLocation, PayrollRun, Salary
from core.models_statutory import OrganizationStatutoryConfig, EmployeeStatutoryProfile
from core.services import payroll as payroll_service


class PayrollStatutoryIntegrationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp India", code="ACME", default_currency="INR")
        self.dept = Department.objects.create(organization=self.org, name="Finance", code="FIN")
        self.location = WorkLocation.objects.create(organization=self.org, name="Bangalore Office", time_zone="Asia/Kolkata")

        self.creator = User.objects.create_user(username="payroll_creator", password="password123")
        self.approver = User.objects.create_user(username="payroll_approver", password="password123")

        # Assign permissions
        from django.contrib.auth.models import Permission
        for user in (self.creator, self.approver):
            OrganizationMembership.objects.create(user=user, organization=self.org, is_active=True)
            user.user_permissions.add(
                Permission.objects.get(codename="generate_payroll"),
                Permission.objects.get(codename="approve_payroll"),
                Permission.objects.get(codename="finalize_payroll"),
                Permission.objects.get(codename="view_payroll_report"),
            )

        self.employee = Employee.objects.create(
            organization=self.org,
            name="Priya Sharma",
            employee_number="AC001",
            basic_salary=Decimal("20000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )

        self.stat_config = OrganizationStatutoryConfig.objects.create(
            organization=self.org,
            epf_enabled=True,
            epf_wage_ceiling=Decimal("15000.00"),
            epf_restrict_to_ceiling_default=True,
            esi_enabled=True,
            esi_wage_ceiling=Decimal("21000.00"),
            pt_enabled=True,
            pt_state="KARNATAKA",
        )

        self.profile = EmployeeStatutoryProfile.objects.create(
            employee=self.employee,
            pan_number="ABCDE9999Z",
            uan_number="100987654321",
            epf_enrolled=True,
            epf_restrict_to_ceiling=True,
            esi_enrolled=True,
            pt_enrolled=True,
            monthly_tds=Decimal("500.00"),
        )

    def test_payroll_calculation_includes_statutory_deductions(self):
        """calculate_employee_payroll applies EPF, ESI, PT, and TDS accurately."""
        row = payroll_service.calculate_employee_payroll(
            employee=self.employee,
            month=5,
            year=2026,
        )

        # Basic: 20000, Gross: 20000
        self.assertEqual(row.basic_salary, Decimal("20000.00"))
        self.assertEqual(row.gross_pay, Decimal("20000.00"))

        # EPF: capped on 15,000 -> 12% = 1800.00
        # ESI: gross <= 21,000 -> 0.75% of 20,000 = 150.00
        # PT: Karnataka gross > 15,000 = 200.00
        # TDS: 500.00
        # Total statutory: 1800 + 150 + 200 + 500 = 2650.00
        self.assertEqual(row.statutory_deductions, Decimal("2650.00"))
        self.assertEqual(row.total_deduction, Decimal("2650.00"))
        self.assertEqual(row.net_pay, Decimal("17350.00"))

        # Employer contributions:
        # EPF Employer: 1800.00
        # ESI Employer: 3.25% of 20000 = 650.00
        # Total employer: 1800 + 650 = 2450.00
        self.assertEqual(row.employer_contributions, Decimal("2450.00"))

        # Components breakdown check
        component_names = [name for name, _, _ in row.components]
        self.assertIn("Provident Fund (EPF)", component_names)
        self.assertIn("Employee State Insurance (ESIC)", component_names)
        self.assertIn("Professional Tax (KARNATAKA)", component_names)
        self.assertIn("Tax Deducted at Source (TDS)", component_names)

    def test_payroll_run_lifecycle_persists_statutory_snapshot(self):
        """Submitting and finalizing a payroll run freezes statutory calculation details."""
        run = payroll_service.create_payroll_run(
            month=5,
            year=2026,
            user=self.creator,
            organization=self.org,
        )

        # Submit
        payroll_service.submit_payroll_for_approval(run, user=self.creator)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.PENDING_APPROVAL)
        self.assertTrue(len(run.calculation_snapshot) > 0)

        snapshot_row = run.calculation_snapshot[0]
        self.assertEqual(snapshot_row["statutory_deductions"], "2650.00")
        self.assertEqual(snapshot_row["employer_contributions"], "2450.00")
        self.assertIn("statutory_details", snapshot_row)
        self.assertEqual(snapshot_row["statutory_details"]["epf_employee"], "1800.00")
        self.assertEqual(snapshot_row["statutory_details"]["esi_employee"], "150.00")
        self.assertEqual(snapshot_row["statutory_details"]["pt_amount"], "200.00")
        self.assertEqual(snapshot_row["statutory_details"]["tds_amount"], "500.00")

        # Approve and Finalize
        payroll_service.approve_payroll(run, user=self.approver)
        payroll_service.finalize_payroll(run, user=self.approver)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.FINALIZED)

        # Verify persisted Salary object
        salary = Salary.objects.get(emp=self.employee, month=5, year=2026)
        self.assertEqual(salary.gross, Decimal("20000.00"))
        self.assertEqual(salary.deduction, Decimal("2650.00"))
        self.assertEqual(salary.net, Decimal("17350.00"))
        self.assertEqual(salary.calculation_snapshot["statutory_deductions"], "2650.00")

    def test_organization_without_statutory_config_remains_unaffected(self):
        """Organizations without statutory config maintain zero statutory deductions."""
        plain_org = Organization.objects.create(name="Global Non-Indian Org", code="GLOBAL", default_currency="USD")
        plain_emp = Employee.objects.create(
            organization=plain_org,
            name="John Doe",
            employee_number="GL001",
            basic_salary=Decimal("5000.00"),
            joined_on="2024-01-01",
        )
        row = payroll_service.calculate_employee_payroll(
            employee=plain_emp,
            month=5,
            year=2026,
        )
        self.assertEqual(row.statutory_deductions, Decimal("0.00"))
        self.assertEqual(row.total_deduction, Decimal("0.00"))
        self.assertEqual(row.net_pay, Decimal("5000.00"))
