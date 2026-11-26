from decimal import Decimal
from django.contrib.auth.models import User, Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, Client

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Employee, Department, WorkLocation, PayrollRun, PayrollEvent
from core.models_banking import EmployeeBankAccount
from core.services import payroll as payroll_service
from core.services.bank_payout import generate_payout_file


class BankPayoutFileTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Apex Enterprises", code="APEX", default_currency="INR")
        self.dept = Department.objects.create(organization=self.org, name="Accounts", code="ACC")
        self.location = WorkLocation.objects.create(organization=self.org, name="Bangalore Hub", time_zone="Asia/Kolkata")

        self.user = User.objects.create_user(username="finance_officer", password="password123")
        OrganizationMembership.objects.create(user=self.user, organization=self.org, is_active=True)
        self.user.user_permissions.add(
            Permission.objects.get(codename="generate_payroll"),
            Permission.objects.get(codename="approve_payroll"),
            Permission.objects.get(codename="finalize_payroll"),
            Permission.objects.get(codename="view_payroll_report"),
        )

        self.approver = User.objects.create_user(username="director_approver", password="password123")
        OrganizationMembership.objects.create(user=self.approver, organization=self.org, is_active=True)
        self.approver.user_permissions.add(
            Permission.objects.get(codename="approve_payroll"),
            Permission.objects.get(codename="finalize_payroll"),
        )

        # Employee 1: Standard salary
        self.emp1 = Employee.objects.create(
            organization=self.org,
            name="Ananya Roy",
            employee_number="AP001",
            basic_salary=Decimal("50000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )
        EmployeeBankAccount.objects.create(
            employee=self.emp1,
            bank_name="HDFC Bank",
            account_holder_name="Ananya Roy",
            account_number="50100987654321",
            ifsc_code="HDFC0001234",
            payment_method="NEFT",
            is_verified=True,
        )

        # Employee 2: High earner (>= 200k -> RTGS)
        self.emp2 = Employee.objects.create(
            organization=self.org,
            name="Rajesh Khanna",
            employee_number="AP002",
            basic_salary=Decimal("250000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )
        EmployeeBankAccount.objects.create(
            employee=self.emp2,
            bank_name="ICICI Bank",
            account_holder_name="Rajesh Khanna",
            account_number="12345678901234",
            ifsc_code="ICIC0000567",
            payment_method="NEFT",
            is_verified=True,
        )

        # Employee 3: Cash payment (should be skipped from electronic batch)
        self.emp3 = Employee.objects.create(
            organization=self.org,
            name="Sunil Cashier",
            employee_number="AP003",
            basic_salary=Decimal("15000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )
        EmployeeBankAccount.objects.create(
            employee=self.emp3,
            bank_name="Cash Payment",
            account_holder_name="Sunil",
            account_number="9999999999",
            ifsc_code="SBIN0000001",
            payment_method="CASH",
        )

    def _create_finalized_run(self):
        run = payroll_service.create_payroll_run(month=5, year=2026, user=self.user, organization=self.org)
        payroll_service.submit_payroll_for_approval(run, user=self.user)
        payroll_service.approve_payroll(run, user=self.approver)
        payroll_service.finalize_payroll(run, user=self.approver)
        run.refresh_from_db()
        return run

    def test_unfinalized_run_raises_validation_error(self):
        """Payout files cannot be exported for unfinalized draft/pending runs."""
        run = payroll_service.create_payroll_run(month=5, year=2026, user=self.user, organization=self.org)
        with self.assertRaises(ValidationError) as ctx:
            generate_payout_file(run, format_type="standard")
        self.assertIn("only be generated for finalized", str(ctx.exception))

    def test_standard_neft_payout_file(self):
        """Verify standard CSV format, RTGS/NEFT threshold, and skipping cash workers."""
        run = self._create_finalized_run()
        payout = generate_payout_file(run, format_type="standard")

        self.assertEqual(payout.record_count, 2)
        self.assertEqual(payout.skipped_count, 1)  # Sunil Cashier skipped
        self.assertEqual(payout.total_amount, Decimal("300000.00"))  # 50,000 + 250,000
        self.assertTrue(payout.filename.startswith("payout_run_"))
        self.assertTrue(payout.filename.endswith(".csv"))

        lines = payout.content.strip().split("\n")
        self.assertEqual(lines[0], "Payment Mode,Beneficiary Name,Account Number,IFSC Code,Amount,Currency,Transaction Ref,Narrative,Employee ID")
        # Line 1: Ananya (NEFT, 50,000)
        self.assertIn("NEFT,Ananya Roy,50100987654321,HDFC0001234,50000.00,INR", lines[1])
        # Line 2: Rajesh (RTGS >= 200,000)
        self.assertIn("RTGS,Rajesh Khanna,12345678901234,ICIC0000567,250000.00,INR", lines[2])

    def test_hdfc_cms_format(self):
        """Verify HDFC CMS format with 'N' and 'R' transaction types."""
        run = self._create_finalized_run()
        payout = generate_payout_file(run, format_type="hdfc")

        self.assertEqual(payout.record_count, 2)
        lines = payout.content.strip().split("\n")
        self.assertEqual(lines[0], "Transaction Type,Beneficiary Code,Account Number,Amount,Beneficiary Name,Payment Details,IFSC Code")
        self.assertTrue(lines[1].startswith("N,"))
        self.assertTrue(lines[2].startswith("R,"))
        self.assertIn("HDFC0001234", lines[1])
        self.assertIn("ICIC0000567", lines[2])

    def test_icici_cib_format(self):
        """Verify ICICI CIB format with corporate debit account specification."""
        run = self._create_finalized_run()
        payout = generate_payout_file(run, format_type="icici", debit_account="9988776655")

        self.assertEqual(payout.record_count, 2)
        lines = payout.content.strip().split("\n")
        self.assertEqual(lines[0], "Debit Account No,Beneficiary Account No,Beneficiary Name,Amount,Payment Mode,Date,IFSC,Payment Ref")
        self.assertTrue(lines[1].startswith("9988776655,"))
        self.assertIn("50100987654321,Ananya Roy,50000.00,NEFT", lines[1])

    def test_http_payout_download_view_and_audit(self):
        """HTTP download view returns CSV file attachment and records an audit event."""
        run = self._create_finalized_run()
        client = Client()
        client.force_login(self.user)

        url = f"/payroll/runs/{run.pk}/payout/standard/"
        response = client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")

        # Verify audit trail
        event = PayrollEvent.objects.filter(run=run, action="PAYOUT_EXPORTED").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, self.user)
        self.assertIn("Exported STANDARD payout file", event.reason)
