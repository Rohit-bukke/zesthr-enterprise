from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.organizations.models import Organization
from core.models import Employee, Department, WorkLocation
from core.models_statutory import OrganizationStatutoryConfig, EmployeeStatutoryProfile
from core.services.statutory import (
    calculate_professional_tax,
    calculate_statutory_deductions,
)


class StatutoryEngineTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="TechCorp India", default_currency="INR")
        self.dept = Department.objects.create(organization=self.org, name="Engineering", code="ENG")
        self.location = WorkLocation.objects.create(organization=self.org, name="Bangalore HQ", time_zone="Asia/Kolkata")

        self.employee = Employee.objects.create(
            organization=self.org,
            name="Rohan Verma",
            employee_number="TC001",
            basic_salary=Decimal("30000.00"),
            department=self.dept,
            work_location=self.location,
            joined_on="2024-01-01",
        )

        self.config = OrganizationStatutoryConfig.objects.create(
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
            pan_number="ABCDE1234F",
            uan_number="100123456789",
            epf_enrolled=True,
            epf_restrict_to_ceiling=True,
            esi_enrolled=True,
            pt_enrolled=True,
            monthly_tds=Decimal("1500.00"),
        )

    def test_epf_restricted_to_ceiling(self):
        """When basic salary exceeds ceiling (30k > 15k) and restrict is True, PF is capped at 15k."""
        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("30000.00"),
            gross_pay=Decimal("45000.00"),
            month=5,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertEqual(res.epf_wage, Decimal("15000.00"))
        # 12% of 15000 = 1800.00
        self.assertEqual(res.epf_employee, Decimal("1800.00"))
        self.assertEqual(res.epf_employer_total, Decimal("1800.00"))
        # EPS = 15000 * 8.33% = 1249.50
        self.assertEqual(res.epf_employer_eps, Decimal("1249.50"))
        # EPF = 1800 - 1249.50 = 550.50
        self.assertEqual(res.epf_employer_epf, Decimal("550.50"))

    def test_epf_unrestricted(self):
        """When restrict is False, PF calculates on full basic."""
        self.profile.epf_restrict_to_ceiling = False
        self.profile.save()

        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("20000.00"),
            gross_pay=Decimal("25000.00"),
            month=5,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertEqual(res.epf_wage, Decimal("20000.00"))
        # 12% of 20000 = 2400.00
        self.assertEqual(res.epf_employee, Decimal("2400.00"))
        self.assertEqual(res.epf_employer_total, Decimal("2400.00"))
        # EPS is always capped at 1250 max
        self.assertEqual(res.epf_employer_eps, Decimal("1249.50"))
        self.assertEqual(res.epf_employer_epf, Decimal("1150.50"))

    def test_epf_opt_out(self):
        """When employee is not enrolled in EPF, contribution is 0."""
        self.profile.epf_enrolled = False
        self.profile.save()

        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("15000.00"),
            gross_pay=Decimal("20000.00"),
            month=5,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertEqual(res.epf_employee, Decimal("0.00"))
        self.assertEqual(res.epf_employer_total, Decimal("0.00"))

    def test_esi_under_ceiling(self):
        """Gross pay <= 21,000 is eligible for ESI (0.75% emp, 3.25% employer)."""
        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("12000.00"),
            gross_pay=Decimal("18000.00"),
            month=5,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertTrue(res.esi_eligible)
        # 18,000 * 0.75% = 135.00
        self.assertEqual(res.esi_employee, Decimal("135.00"))
        # 18,000 * 3.25% = 585.00
        self.assertEqual(res.esi_employer, Decimal("585.00"))

    def test_esi_above_ceiling(self):
        """Gross pay > 21,000 is exempt from ESI."""
        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("15000.00"),
            gross_pay=Decimal("25000.00"),
            month=5,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertFalse(res.esi_eligible)
        self.assertEqual(res.esi_employee, Decimal("0.00"))
        self.assertEqual(res.esi_employer, Decimal("0.00"))

    def test_professional_tax_slabs(self):
        """Verify PT calculations across states."""
        # Karnataka
        self.assertEqual(calculate_professional_tax(Decimal("14000.00"), "KARNATAKA"), Decimal("0.00"))
        self.assertEqual(calculate_professional_tax(Decimal("25000.00"), "KARNATAKA"), Decimal("200.00"))

        # Maharashtra (February charges 300, others charge 200)
        self.assertEqual(calculate_professional_tax(Decimal("7000.00"), "MAHARASHTRA", month=5), Decimal("0.00"))
        self.assertEqual(calculate_professional_tax(Decimal("9000.00"), "MAHARASHTRA", month=5), Decimal("175.00"))
        self.assertEqual(calculate_professional_tax(Decimal("25000.00"), "MAHARASHTRA", month=5), Decimal("200.00"))
        self.assertEqual(calculate_professional_tax(Decimal("25000.00"), "MAHARASHTRA", month=2), Decimal("300.00"))

        # Telangana
        self.assertEqual(calculate_professional_tax(Decimal("14000.00"), "TELANGANA"), Decimal("0.00"))
        self.assertEqual(calculate_professional_tax(Decimal("18000.00"), "TELANGANA"), Decimal("150.00"))
        self.assertEqual(calculate_professional_tax(Decimal("30000.00"), "TELANGANA"), Decimal("200.00"))

        # West Bengal
        self.assertEqual(calculate_professional_tax(Decimal("10000.00"), "WEST_BENGAL"), Decimal("0.00"))
        self.assertEqual(calculate_professional_tax(Decimal("12000.00"), "WEST_BENGAL"), Decimal("110.00"))
        self.assertEqual(calculate_professional_tax(Decimal("20000.00"), "WEST_BENGAL"), Decimal("130.00"))
        self.assertEqual(calculate_professional_tax(Decimal("30000.00"), "WEST_BENGAL"), Decimal("150.00"))
        self.assertEqual(calculate_professional_tax(Decimal("50000.00"), "WEST_BENGAL"), Decimal("200.00"))

    def test_total_deductions_and_breakdown(self):
        """Ensure all employee deductions and employer contributions sum correctly."""
        # basic=12,000, gross=18,000 (Karnataka)
        # EPF = 12000 * 12% = 1440.00
        # ESI = 18000 * 0.75% = 135.00
        # PT (Karnataka > 15k) = 200.00
        # TDS = 1500.00
        # Total Employee Deductions = 1440 + 135 + 200 + 1500 = 3275.00
        res = calculate_statutory_deductions(
            employee=self.employee,
            basic_salary=Decimal("12000.00"),
            gross_pay=Decimal("18000.00"),
            month=6,
            year=2026,
            config=self.config,
            profile=self.profile,
        )
        self.assertEqual(res.epf_employee, Decimal("1440.00"))
        self.assertEqual(res.esi_employee, Decimal("135.00"))
        self.assertEqual(res.pt_amount, Decimal("200.00"))
        self.assertEqual(res.tds_amount, Decimal("1500.00"))
        self.assertEqual(res.total_employee_deductions, Decimal("3275.00"))

        breakdown_names = [item[0] for item in res.breakdown]
        self.assertIn("Provident Fund (EPF)", breakdown_names)
        self.assertIn("Employee State Insurance (ESIC)", breakdown_names)
        self.assertIn("Professional Tax (KARNATAKA)", breakdown_names)
        self.assertIn("Tax Deducted at Source (TDS)", breakdown_names)

    def test_pan_uan_validation(self):
        """Validate format checks on PAN and UAN."""
        profile = EmployeeStatutoryProfile(
            employee=self.employee,
            pan_number="invalid_pan",
        )
        with self.assertRaises(ValidationError):
            profile.clean()

        profile.pan_number = "ABCDE1234F"
        profile.uan_number = "12345"  # not 12 digits
        with self.assertRaises(ValidationError):
            profile.clean()

        profile.uan_number = "123456789012"
        profile.clean()  # Should pass cleanly
