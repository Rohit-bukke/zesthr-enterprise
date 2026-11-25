from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.organizations.models import Organization
from core.models import Employee, EmploymentHistory
from core.models_statutory import OrganizationStatutoryConfig
from core.models_compensation import CompensationBand, SalaryRevision
from core.services import compensation


class CompensationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Corp", code="ACME_COMP")
        OrganizationStatutoryConfig.objects.create(
            organization=self.org,
            epf_enabled=True,
        )
        self.user = User.objects.create_user(username="test_emp", password="password")
        self.admin = User.objects.create_superuser(username="admin_user", password="password")
        self.employee = Employee.objects.create(
            organization=self.org,
            user=self.user,
            employee_number="EMP-COMP-01",
            name="John Doe",
            email="john@example.com",
            basic_salary=Decimal("50000.00"),
            joined_on=date(2025, 1, 1),
        )

    def test_compensation_band_creation_and_validation(self):
        band = CompensationBand.objects.create(
            organization=self.org,
            code="ENG-L3",
            name="Senior Engineer",
            min_base_salary=Decimal("60000.00"),
            max_base_salary=Decimal("90000.00"),
            currency="INR",
        )
        self.assertEqual(band.code, "ENG-L3")
        self.assertEqual(band.min_base_salary, Decimal("60000.00"))

        # Max cannot be less than min
        with self.assertRaises(ValidationError):
            invalid_band = CompensationBand(
                organization=self.org,
                code="INV",
                name="Invalid",
                min_base_salary=Decimal("50000.00"),
                max_base_salary=Decimal("30000.00"),
            )
            invalid_band.clean()

    def test_propose_and_approve_salary_revision(self):
        today = date.today()
        revision = compensation.propose_salary_revision(
            employee=self.employee,
            revised_salary=Decimal("60000.00"),
            reason=SalaryRevision.REASON_ANNUAL_APPRAISAL,
            effective_date=today,
            proposed_by=self.admin,
            review_notes="Excellent annual performance",
        )
        self.assertEqual(revision.status, SalaryRevision.STATUS_PROPOSED)
        self.assertEqual(revision.increment_percentage, Decimal("20.00"))
        self.assertEqual(self.employee.basic_salary, Decimal("50000.00"))

        # Approve revision
        approved = compensation.approve_salary_revision(revision.pk, reviewer=self.admin)
        self.assertEqual(approved.status, SalaryRevision.STATUS_APPLIED)

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("60000.00"))

        # Verify employment history was captured
        history = EmploymentHistory.objects.filter(employee=self.employee).last()
        self.assertIsNotNone(history)
        self.assertEqual(history.field, "basic_salary")
        self.assertEqual(history.new_value, "60000.00")

    def test_reject_salary_revision(self):
        today = date.today()
        revision = compensation.propose_salary_revision(
            employee=self.employee,
            revised_salary=Decimal("70000.00"),
            reason=SalaryRevision.REASON_PROMOTION,
            effective_date=today + timedelta(days=30),
            proposed_by=self.admin,
        )
        rejected = compensation.reject_salary_revision(revision.pk, reviewer=self.admin, review_notes="Budget freeze")
        self.assertEqual(rejected.status, SalaryRevision.STATUS_REJECTED)

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("50000.00"))

    def test_future_dated_revision_applied_on_schedule(self):
        tomorrow = date.today() + timedelta(days=1)
        revision = compensation.propose_salary_revision(
            employee=self.employee,
            revised_salary=Decimal("55000.00"),
            reason=SalaryRevision.REASON_MARKET_CORRECTION,
            effective_date=tomorrow,
            proposed_by=self.admin,
        )
        approved = compensation.approve_salary_revision(revision.pk, reviewer=self.admin)
        self.assertEqual(approved.status, SalaryRevision.STATUS_APPROVED)

        # Still old salary today
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("50000.00"))

        # Run scheduler as of tomorrow
        applied_count = compensation.apply_pending_effective_revisions(as_of_date=tomorrow)
        self.assertEqual(applied_count, 1)

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.basic_salary, Decimal("55000.00"))

    def test_total_rewards_calculation(self):
        rewards = compensation.get_employee_total_rewards(self.employee)
        self.assertEqual(rewards["monthly_base"], Decimal("50000.00"))
        self.assertEqual(rewards["annual_base"], Decimal("600000.00"))
        self.assertGreater(rewards["total_annual_ctc"], Decimal("600000.00"))
