from decimal import Decimal
from django.contrib.auth.models import User, Permission
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.organizations.models import Organization, OrganizationMembership
from core.models import Employee, Department, WorkLocation, PayrollRun
from core.models_banking import EmployeeBankAccount
from core.services import payroll as payroll_service


class EmployeeBankAccountTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="TechBank Corp", code="TBANK", default_currency="INR")
        self.dept = Department.objects.create(organization=self.org, name="Operations", code="OPS")
        self.location = WorkLocation.objects.create(organization=self.org, name="Mumbai Office", time_zone="Asia/Kolkata")

        self.user = User.objects.create_user(username="hr_officer", password="password123")
        OrganizationMembership.objects.create(user=self.user, organization=self.org, is_active=True)
        self.user.user_permissions.add(
            Permission.objects.get(codename="generate_payroll"),
            Permission.objects.get(codename="approve_payroll"),
            Permission.objects.get(codename="finalize_payroll"),
            Permission.objects.get(codename="view_payroll_report"),
            Permission.objects.get(codename="view_employee_pay"),
            Permission.objects.get(codename="change_employee_pay"),
        )

        self.employee = Employee.objects.create(
            organization=self.org,
            name="Vikram Seth",
            employee_number="TB001",
            basic_salary=Decimal("45000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )

    def test_valid_bank_account_creation_and_masking(self):
        """Valid bank account details are accepted and correctly masked."""
        account = EmployeeBankAccount(
            employee=self.employee,
            bank_name="HDFC Bank",
            branch_name="Koramangala, Bangalore",
            account_holder_name="Vikram Seth",
            account_number="50100234567890",
            ifsc_code="HDFC0001234",
            account_type="SALARY",
            payment_method="NEFT",
            upi_id="vikram@okhdfcbank",
        )
        account.clean()
        account.save()

        self.assertEqual(account.masked_account_number, "XXXXXXXXXX7890")
        self.assertEqual(str(account), "Vikram Seth - HDFC Bank (XXXXXXXXXX7890)")
        self.assertFalse(account.is_verified)

    def test_ifsc_code_validation(self):
        """IFSC code must strictly match 4 letters, '0', and 6 alphanumeric characters."""
        # Invalid: 5th char not '0'
        account = EmployeeBankAccount(
            employee=self.employee,
            bank_name="ICICI Bank",
            account_holder_name="Vikram Seth",
            account_number="123456789012",
            ifsc_code="ICIC1000123",  # '1' instead of '0'
        )
        with self.assertRaises(ValidationError):
            account.clean()

        # Invalid: too short
        account.ifsc_code = "ICIC001"
        with self.assertRaises(ValidationError):
            account.clean()

        # Valid: lowercase should be auto-uppercased
        account.ifsc_code = "icic0001234"
        account.clean()
        self.assertEqual(account.ifsc_code, "ICIC0001234")

    def test_account_number_validation(self):
        """Account number must be numeric and between 9 and 18 digits."""
        account = EmployeeBankAccount(
            employee=self.employee,
            bank_name="State Bank of India",
            account_holder_name="Vikram Seth",
            account_number="12345",  # Too short (< 9 digits)
            ifsc_code="SBIN0000456",
        )
        with self.assertRaises(ValidationError):
            account.clean()

        account.account_number = "12345678ABCD"  # Non-numeric
        with self.assertRaises(ValidationError):
            account.clean()

        account.account_number = "20012345678"  # 11 digits, valid
        account.clean()
        account.save()
        self.assertEqual(account.masked_account_number, "XXXXXXX5678")

    def test_upi_validation(self):
        """UPI ID format must follow username@bank syntax."""
        account = EmployeeBankAccount(
            employee=self.employee,
            bank_name="Axis Bank",
            account_holder_name="Vikram Seth",
            account_number="912345678901",
            ifsc_code="UTIB0000123",
            upi_id="invalid_upi_without_at",
        )
        with self.assertRaises(ValidationError):
            account.clean()

        account.upi_id = "vikram.seth@axisbank"
        account.clean()
        self.assertEqual(account.upi_id, "vikram.seth@axisbank")

    def test_mark_verified_action(self):
        """Authorized user can mark a bank account as verified."""
        account = EmployeeBankAccount.objects.create(
            employee=self.employee,
            bank_name="HDFC Bank",
            account_holder_name="Vikram Seth",
            account_number="50100234567890",
            ifsc_code="HDFC0001234",
        )
        self.assertFalse(account.is_verified)
        account.mark_verified(self.user)

        account.refresh_from_db()
        self.assertTrue(account.is_verified)
        self.assertEqual(account.verified_by, self.user)
        self.assertIsNotNone(account.verified_at)

    def test_bank_details_captured_in_payroll_snapshot(self):
        """Payroll calculation snapshot captures frozen bank payment details."""
        EmployeeBankAccount.objects.create(
            employee=self.employee,
            bank_name="Kotak Mahindra Bank",
            branch_name="MG Road",
            account_holder_name="Vikram Seth",
            account_number="601234567890",
            ifsc_code="KKBK0000123",
            payment_method="NEFT",
            is_verified=True,
        )

        run = payroll_service.create_payroll_run(
            month=5,
            year=2026,
            user=self.user,
            organization=self.org,
        )
        payroll_service.submit_payroll_for_approval(run, user=self.user)
        run.refresh_from_db()

        self.assertEqual(len(run.calculation_snapshot), 1)
        row = run.calculation_snapshot[0]
        self.assertIn("bank_details", row)
        bank_details = row["bank_details"]
        self.assertEqual(bank_details["bank_name"], "Kotak Mahindra Bank")
        self.assertEqual(bank_details["account_number"], "XXXXXXXX7890")
        self.assertEqual(bank_details["raw_account_number"], "601234567890")
        self.assertEqual(bank_details["ifsc_code"], "KKBK0000123")
        self.assertEqual(bank_details["payment_method"], "NEFT")
        self.assertTrue(bank_details["is_verified"])
