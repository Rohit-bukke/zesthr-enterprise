from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models_expenses import ExpenseCategory, ExpenseClaim
from core.models import Employee, PayrollRun


def submit_expense_claim(
    employee: Employee,
    category: ExpenseCategory,
    title: str,
    amount: Decimal,
    incurred_date: date,
    receipt=None,
    description: str = "",
) -> ExpenseClaim:
    """
    Submits an employee expense reimbursement claim.
    """
    if not title:
        raise ValidationError({"title": "Expense title is required."})

    if amount is None or amount <= 0:
        raise ValidationError({"amount": "Expense amount must be greater than zero."})

    if category.organization_id != employee.organization_id:
        raise ValidationError("Category belongs to another organization.")

    if category.requires_receipt and not receipt:
        raise ValidationError({"receipt": f"Receipt attachment is mandatory for {category.name} claims."})

    if category.max_amount_per_claim and amount > category.max_amount_per_claim:
        raise ValidationError({"amount": f"Amount exceeds category limit of {category.max_amount_per_claim}."})

    claim = ExpenseClaim(
        employee=employee,
        category=category,
        title=title,
        amount=amount,
        incurred_date=incurred_date,
        receipt=receipt,
        description=description,
        status=ExpenseClaim.STATUS_SUBMITTED,
    )
    claim.full_clean()
    claim.save()
    return claim


def approve_expense_claim(claim_id: int, reviewer=None, decision_notes: str = "") -> ExpenseClaim:
    """
    Approves an expense claim for reimbursement in the next payroll cycle.
    """
    claim = ExpenseClaim.objects.get(pk=claim_id)
    if claim.status not in (ExpenseClaim.STATUS_SUBMITTED, ExpenseClaim.STATUS_REJECTED):
        raise ValidationError(f"Cannot approve claim in status {claim.status}.")

    claim.status = ExpenseClaim.STATUS_APPROVED
    claim.decided_by = reviewer
    claim.decided_at = timezone.now()
    if decision_notes:
        claim.decision_notes = decision_notes
    claim.save()
    return claim


def reject_expense_claim(claim_id: int, reviewer=None, decision_notes: str = "") -> ExpenseClaim:
    """
    Rejects an expense claim.
    """
    claim = ExpenseClaim.objects.get(pk=claim_id)
    if claim.status != ExpenseClaim.STATUS_SUBMITTED:
        raise ValidationError(f"Cannot reject claim in status {claim.status}.")

    claim.status = ExpenseClaim.STATUS_REJECTED
    claim.decided_by = reviewer
    claim.decided_at = timezone.now()
    if decision_notes:
        claim.decision_notes = decision_notes
    claim.save()
    return claim


def get_unpaid_approved_expenses(employee: Employee):
    """
    Returns approved claims waiting to be reimbursed through payroll.
    """
    return ExpenseClaim.objects.filter(
        employee=employee,
        status=ExpenseClaim.STATUS_APPROVED,
        payroll_run__isnull=True,
    ).select_related("category")


def mark_expenses_reimbursed_for_run(run: PayrollRun):
    """
    Marks all unpaid approved expense claims for active employees as PAID and links them to the PayrollRun.
    """
    with transaction.atomic():
        claims = ExpenseClaim.objects.filter(
            employee__organization=run.organization,
            status=ExpenseClaim.STATUS_APPROVED,
            payroll_run__isnull=True,
        )
        claims.update(status=ExpenseClaim.STATUS_PAID, payroll_run=run)
