from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models_compensation import CompensationBand, SalaryRevision
from core.models import Employee, EmploymentHistory


def propose_salary_revision(
    employee: Employee,
    revised_salary: Decimal,
    reason: str,
    effective_date: date,
    proposed_by=None,
    review_notes: str = "",
) -> SalaryRevision:
    """
    Propose an effective-dated salary revision for an employee.
    """
    if revised_salary is None or revised_salary <= 0:
        raise ValidationError({"revised_salary": "Revised salary must be a positive amount."})

    if effective_date is None:
        raise ValidationError({"effective_date": "Effective date is required."})

    previous_salary = employee.basic_salary or Decimal("0.00")

    diff = revised_salary - previous_salary
    increment_pct = round((diff / previous_salary) * Decimal("100.00"), 2) if previous_salary > 0 else Decimal("0.00")

    revision = SalaryRevision.objects.create(
        employee=employee,
        previous_salary=previous_salary,
        revised_salary=revised_salary,
        increment_percentage=increment_pct,
        reason=reason,
        effective_date=effective_date,
        proposed_by=proposed_by,
        review_notes=review_notes,
        status=SalaryRevision.STATUS_PROPOSED,
    )
    return revision


def approve_salary_revision(revision_id: int, reviewer=None, review_notes: str = "") -> SalaryRevision:
    """
    Approve a proposed salary revision and automatically apply it if effective_date <= today.
    """
    with transaction.atomic():
        revision = SalaryRevision.objects.select_for_update().get(pk=revision_id)
        if revision.status not in (SalaryRevision.STATUS_PROPOSED, SalaryRevision.STATUS_REJECTED):
            raise ValidationError(f"Cannot approve revision in status {revision.status}.")

        revision.status = SalaryRevision.STATUS_APPROVED
        revision.reviewed_by = reviewer
        revision.decided_at = timezone.now()
        if review_notes:
            revision.review_notes = review_notes
        revision.save()

        # If effective date has arrived or passed, apply immediately
        if revision.effective_date <= timezone.now().date():
            apply_salary_revision(revision)

        return revision


def reject_salary_revision(revision_id: int, reviewer=None, review_notes: str = "") -> SalaryRevision:
    """
    Reject a proposed salary revision.
    """
    revision = SalaryRevision.objects.get(pk=revision_id)
    if revision.status != SalaryRevision.STATUS_PROPOSED:
        raise ValidationError(f"Cannot reject revision in status {revision.status}.")

    revision.status = SalaryRevision.STATUS_REJECTED
    revision.reviewed_by = reviewer
    revision.decided_at = timezone.now()
    if review_notes:
        revision.review_notes = review_notes
    revision.save()
    return revision


def apply_salary_revision(revision: SalaryRevision) -> None:
    """
    Applies the approved revision to the employee's base salary and logs employment history.
    """
    with transaction.atomic():
        emp = revision.employee
        old_salary = emp.basic_salary
        emp.basic_salary = revision.revised_salary
        emp.save(update_fields=["basic_salary"])

        revision.status = SalaryRevision.STATUS_APPLIED
        revision.save(update_fields=["status"])

        # Record in employment history
        EmploymentHistory.objects.create(
            employee=emp,
            field="basic_salary",
            old_value=str(old_salary),
            new_value=str(revision.revised_salary),
            sensitive=True,
            changed_by=revision.reviewed_by or revision.proposed_by,
        )


def apply_pending_effective_revisions(as_of_date: date = None) -> int:
    """
    Batch job to apply all APPROVED revisions whose effective_date is <= as_of_date.
    """
    if as_of_date is None:
        as_of_date = timezone.now().date()

    pending = SalaryRevision.objects.filter(
        status=SalaryRevision.STATUS_APPROVED,
        effective_date__lte=as_of_date,
    ).select_related("employee", "employee__department", "employee__position")

    count = 0
    for rev in pending:
        apply_salary_revision(rev)
        count += 1
    return count


def get_employee_total_rewards(employee: Employee) -> dict:
    """
    Computes a comprehensive Total Rewards / CTC statement for an employee.
    """
    monthly_base = employee.basic_salary or Decimal("0.00")
    annual_base = monthly_base * Decimal("12.00")

    # Active recurring components
    active_components = employee.payroll_components.select_related("component").filter(
        active=True,
        component__active=True,
    )
    monthly_allowances = sum(
        (c.amount for c in active_components if c.component.component_type == "EARNING"),
        Decimal("0.00")
    )
    annual_allowances = monthly_allowances * Decimal("12.00")

    # Employer statutory benefits
    from core.services.statutory import calculate_statutory_deductions
    gross_est = monthly_base + monthly_allowances
    stat_res = calculate_statutory_deductions(
        employee=employee,
        basic_salary=monthly_base,
        gross_pay=gross_est,
        month=timezone.now().month,
        year=timezone.now().year,
        config=getattr(employee.organization, "statutory_config", None),
        profile=getattr(employee, "statutory_profile", None),
    )
    monthly_employer_benefits = stat_res.total_employer_contributions
    annual_employer_benefits = monthly_employer_benefits * Decimal("12.00")

    total_monthly_ctc = monthly_base + monthly_allowances + monthly_employer_benefits
    total_annual_ctc = annual_base + annual_allowances + annual_employer_benefits

    # Check against compensation band if assigned
    band = None
    if employee.position:
        band = CompensationBand.objects.filter(
            organization=employee.organization,
            department=employee.department,
            is_active=True,
        ).first()

    return {
        "monthly_base": monthly_base,
        "annual_base": annual_base,
        "monthly_allowances": monthly_allowances,
        "annual_allowances": annual_allowances,
        "monthly_employer_benefits": monthly_employer_benefits,
        "annual_employer_benefits": annual_employer_benefits,
        "total_monthly_ctc": total_monthly_ctc,
        "total_annual_ctc": total_annual_ctc,
        "statutory_breakdown": stat_res,
        "band": band,
        "components": active_components,
        "revisions": employee.salary_revisions.all().order_by("-effective_date"),
    }
