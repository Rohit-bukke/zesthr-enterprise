"""Statutory compliance and deduction engine.

Handles Provident Fund (EPF/EPS), Employee State Insurance (ESIC),
State-specific Professional Tax (PT), and Tax Deducted at Source (TDS).
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class StatutoryCalculation:
    """Itemized statutory calculation results."""
    basic_salary: Decimal
    gross_pay: Decimal
    month: int
    year: int

    # EPF
    epf_wage: Decimal
    epf_employee: Decimal
    epf_employer_eps: Decimal
    epf_employer_epf: Decimal
    epf_employer_total: Decimal

    # ESI
    esi_eligible: bool
    esi_employee: Decimal
    esi_employer: Decimal

    # Professional Tax & TDS
    pt_amount: Decimal
    tds_amount: Decimal

    # Totals
    total_employee_deductions: Decimal
    total_employer_contributions: Decimal

    # Breakdown ready for payroll snapshot
    breakdown: tuple  # tuple of (name, type, amount_str)


def calculate_professional_tax(gross_pay: Decimal, state: str, month: int = 1) -> Decimal:
    """Calculate Professional Tax based on state statutory slabs and gross monthly wage."""
    gross = Decimal(gross_pay)
    state = (state or "FLAT").upper().strip()

    if state == "KARNATAKA":
        if gross > Decimal("15000.00"):
            return Decimal("200.00")
        return Decimal("0.00")

    elif state == "MAHARASHTRA":
        if gross <= Decimal("7500.00"):
            return Decimal("0.00")
        elif gross <= Decimal("10000.00"):
            return Decimal("175.00")
        else:
            # Maharashtra charges INR 300 in February, INR 200 in all other months
            return Decimal("300.00") if month == 2 else Decimal("200.00")

    elif state in ("TELANGANA", "ANDHRA_PRADESH"):
        if gross <= Decimal("15000.00"):
            return Decimal("0.00")
        elif gross <= Decimal("20000.00"):
            return Decimal("150.00")
        else:
            return Decimal("200.00")

    elif state == "WEST_BENGAL":
        if gross <= Decimal("10000.00"):
            return Decimal("0.00")
        elif gross <= Decimal("15000.00"):
            return Decimal("110.00")
        elif gross <= Decimal("25000.00"):
            return Decimal("130.00")
        elif gross <= Decimal("40000.00"):
            return Decimal("150.00")
        else:
            return Decimal("200.00")

    elif state == "GUJARAT":
        if gross <= Decimal("12000.00"):
            return Decimal("0.00")
        return Decimal("200.00")

    elif state == "TAMIL_NADU":
        if gross <= Decimal("21000.00"):
            return Decimal("0.00")
        elif gross <= Decimal("30000.00"):
            return Decimal("100.00")
        elif gross <= Decimal("45000.00"):
            return Decimal("235.00")
        else:
            return Decimal("208.00")

    else:  # FLAT fallback
        if gross > Decimal("15000.00"):
            return Decimal("200.00")
        return Decimal("0.00")


def calculate_statutory_deductions(
    employee,
    basic_salary: Decimal,
    gross_pay: Decimal,
    month: int,
    year: int,
    config=None,
    profile=None,
) -> StatutoryCalculation:
    """Calculate all statutory deductions and employer contributions for an employee."""
    basic = money(basic_salary)
    gross = money(gross_pay)

    # Resolve organization configuration
    if config is None:
        from ..models_statutory import OrganizationStatutoryConfig
        try:
            config = employee.organization.statutory_config
        except (AttributeError, OrganizationStatutoryConfig.DoesNotExist):
            config = None

    # Resolve employee profile
    if profile is None:
        try:
            profile = employee.statutory_profile
        except Exception:
            profile = None

    # 1. EPF (Employees' Provident Fund)
    epf_enabled = (config.epf_enabled if config else True) and (profile.epf_enrolled if profile else True)
    epf_wage_ceiling = config.epf_wage_ceiling if config else Decimal("15000.00")
    restrict_ceiling = profile.epf_restrict_to_ceiling if profile else (config.epf_restrict_to_ceiling_default if config else True)

    if epf_enabled and basic > 0:
        epf_wage = min(basic, epf_wage_ceiling) if restrict_ceiling else basic
        emp_rate = (config.epf_employee_rate if config else Decimal("12.00")) / Decimal("100")
        employer_rate = (config.epf_employer_rate if config else Decimal("12.00")) / Decimal("100")
        eps_rate = (config.epf_employer_eps_rate if config else Decimal("8.33")) / Decimal("100")

        epf_employee = money(epf_wage * emp_rate)

        # Employer EPS is capped on statutory wage ceiling (max 15000 * 8.33% = 1250)
        eps_wage = min(basic, epf_wage_ceiling)
        epf_employer_eps = money(min(eps_wage * eps_rate, Decimal("1250.00")))
        epf_employer_total = money(epf_wage * employer_rate)
        epf_employer_epf = money(epf_employer_total - epf_employer_eps)
    else:
        epf_wage = Decimal("0.00")
        epf_employee = Decimal("0.00")
        epf_employer_eps = Decimal("0.00")
        epf_employer_epf = Decimal("0.00")
        epf_employer_total = Decimal("0.00")

    # 2. ESIC (Employees' State Insurance)
    esi_enabled = (config.esi_enabled if config else True) and (profile.esi_enrolled if profile else True)
    esi_wage_ceiling = config.esi_wage_ceiling if config else Decimal("21000.00")
    esi_eligible = esi_enabled and (gross <= esi_wage_ceiling) and (gross > 0)

    if esi_eligible:
        esi_emp_rate = (config.esi_employee_rate if config else Decimal("0.75")) / Decimal("100")
        esi_er_rate = (config.esi_employer_rate if config else Decimal("3.25")) / Decimal("100")
        esi_employee = money(gross * esi_emp_rate)
        esi_employer = money(gross * esi_er_rate)
    else:
        esi_employee = Decimal("0.00")
        esi_employer = Decimal("0.00")

    # 3. Professional Tax (PT)
    pt_enabled = (config.pt_enabled if config else True) and (profile.pt_enrolled if profile else True)
    pt_state = (profile.pt_state if profile and profile.pt_state else (config.pt_state if config else "KARNATAKA"))

    if pt_enabled and gross > 0:
        pt_amount = money(calculate_professional_tax(gross, pt_state, month=month))
    else:
        pt_amount = Decimal("0.00")

    # 4. Tax Deducted at Source (TDS)
    tds_amount = money(profile.monthly_tds) if profile and profile.monthly_tds else Decimal("0.00")

    total_employee_deductions = money(epf_employee + esi_employee + pt_amount + tds_amount)
    total_employer_contributions = money(epf_employer_total + esi_employer)

    # Itemized list of employee deductions for snapshot/payslip
    breakdown = []
    if epf_employee > 0:
        breakdown.append(("Provident Fund (EPF)", "DEDUCTION", str(epf_employee)))
    if esi_employee > 0:
        breakdown.append(("Employee State Insurance (ESIC)", "DEDUCTION", str(esi_employee)))
    if pt_amount > 0:
        breakdown.append((f"Professional Tax ({pt_state})", "DEDUCTION", str(pt_amount)))
    if tds_amount > 0:
        breakdown.append(("Tax Deducted at Source (TDS)", "DEDUCTION", str(tds_amount)))

    return StatutoryCalculation(
        basic_salary=basic,
        gross_pay=gross,
        month=month,
        year=year,
        epf_wage=epf_wage,
        epf_employee=epf_employee,
        epf_employer_eps=epf_employer_eps,
        epf_employer_epf=epf_employer_epf,
        epf_employer_total=epf_employer_total,
        esi_eligible=esi_eligible,
        esi_employee=esi_employee,
        esi_employer=esi_employer,
        pt_amount=pt_amount,
        tds_amount=tds_amount,
        total_employee_deductions=total_employee_deductions,
        total_employer_contributions=total_employer_contributions,
        breakdown=tuple(breakdown),
    )
