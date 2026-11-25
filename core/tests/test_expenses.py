from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.organizations.models import Organization
from core.models import Employee, PayrollRun
from core.models_expenses import ExpenseCategory, ExpenseClaim
from core.services import expenses, payroll


class ExpenseTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp", code="ACME_EXP")
        self.user = User.objects.create_user(username="test_emp_exp", password="password")
        self.admin = User.objects.create_superuser(username="admin_exp", password="password")
        self.employee = Employee.objects.create(
            organization=self.org,
            user=self.user,
            employee_number="EMP-EXP-01",
            name="Alice Smith",
            email="alice@example.com",
            basic_salary=Decimal("50000.00"),
            joined_on=date(2025, 1, 1),
        )
        self.category = ExpenseCategory.objects.create(
            organization=self.org,
            code="TRAVEL",
            name="Travel & Conveyance",
            requires_receipt=False,
            max_amount_per_claim=Decimal("10000.00"),
        )

    def test_submit_and_approve_expense_claim(self):
        claim = expenses.submit_expense_claim(
            employee=self.employee,
            category=self.category,
            title="Taxi to Client Office",
            amount=Decimal("1500.00"),
            incurred_date=date.today(),
            description="Client presentation visit",
        )
        self.assertEqual(claim.status, ExpenseClaim.STATUS_SUBMITTED)
        self.assertTrue(claim.claim_number.startswith("EXP-"))

        # Approve claim
        approved = expenses.approve_expense_claim(claim.pk, reviewer=self.admin, decision_notes="Approved")
        self.assertEqual(approved.status, ExpenseClaim.STATUS_APPROVED)
        self.assertEqual(approved.decided_by, self.admin)

    def test_reject_expense_claim(self):
        claim = expenses.submit_expense_claim(
            employee=self.employee,
            category=self.category,
            title="Luxury Dinner",
            amount=Decimal("4500.00"),
            incurred_date=date.today(),
        )
        rejected = expenses.reject_expense_claim(claim.pk, reviewer=self.admin, decision_notes="Not business related")
        self.assertEqual(rejected.status, ExpenseClaim.STATUS_REJECTED)

    def test_claim_amount_exceeding_category_limit_fails(self):
        with self.assertRaises(ValidationError):
            expenses.submit_expense_claim(
                employee=self.employee,
                category=self.category,
                title="International Flight",
                amount=Decimal("15000.00"),  # max is 10000
                incurred_date=date.today(),
            )

    def test_expense_reimbursement_in_payroll_calculation(self):
        # Submit and approve an expense claim
        claim = expenses.submit_expense_claim(
            employee=self.employee,
            category=self.category,
            title="Client Lunch",
            amount=Decimal("2000.00"),
            incurred_date=date.today(),
        )
        expenses.approve_expense_claim(claim.pk, reviewer=self.admin)

        # Calculate payroll for the employee
        calc = payroll.calculate_employee_payroll(self.employee, month=date.today().month, year=date.today().year)
        self.assertEqual(calc.expense_reimbursements, Decimal("2000.00"))

        # Net pay must reflect: (gross - total_deductions) + expense_reimbursements
        expected_net = (calc.gross_pay - calc.total_deduction) + Decimal("2000.00")
        self.assertEqual(calc.net_pay, expected_net)
