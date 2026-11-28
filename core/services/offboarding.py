"""
Offboarding & Separation Service Module.
Handles resignation submissions, notice period and shortfall calculations, and manager review workflows.
"""
from datetime import date, timedelta
from typing import Optional
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Employee
from core.models_offboarding import ExitRequest


def submit_resignation(
    employee: Employee,
    requested_lwd: date,
    reason_category: str,
    reason_detail: str = "",
    notice_period_days: int = 30,
    resignation_date: Optional[date] = None,
) -> ExitRequest:
    """
    Submits a formal employee resignation and initiates notice period tracking.
    """
    active_statuses = [
        ExitRequest.STATUS_SUBMITTED,
        ExitRequest.STATUS_APPROVED,
        ExitRequest.STATUS_CLEARANCE_IN_PROGRESS,
    ]
    if ExitRequest.objects.filter(employee=employee, status__in=active_statuses).exists():
        raise ValidationError("An active resignation / exit request is already in progress for this employee.")

    effective_resignation_date = resignation_date or timezone.localdate()

    if requested_lwd < effective_resignation_date:
        raise ValidationError("Requested last working day cannot be earlier than resignation date.")

    with transaction.atomic():
        exit_request = ExitRequest(
            organization=employee.organization,
            employee=employee,
            resignation_date=effective_resignation_date,
            requested_lwd=requested_lwd,
            reason_category=reason_category,
            reason_detail=reason_detail,
            notice_period_days=notice_period_days,
            status=ExitRequest.STATUS_SUBMITTED,
        )
        exit_request.calculate_shortfall()
        exit_request.save()
        return exit_request


def review_exit_request(
    exit_request_id: int,
    reviewer,
    action: str,
    official_lwd: Optional[date] = None,
    waive_shortfall: bool = False,
    review_notes: str = "",
) -> ExitRequest:
    """
    Processes manager / HR decision on an exit request (Approve or Reject).
    Computes official last working day and notice shortfall days.
    """
    action = action.lower().strip()
    if action not in ("approve", "reject"):
        raise ValidationError(f"Invalid review action '{action}'. Must be 'approve' or 'reject'.")

    with transaction.atomic():
        exit_request = ExitRequest.objects.select_for_update().get(pk=exit_request_id)

        if exit_request.status != ExitRequest.STATUS_SUBMITTED:
            raise ValidationError(f"Cannot review an exit request in status {exit_request.status}.")

        now = timezone.now()
        exit_request.reviewed_by = reviewer
        exit_request.reviewed_at = now
        exit_request.review_notes = review_notes

        if action == "approve":
            final_lwd = official_lwd or exit_request.requested_lwd
            if final_lwd < exit_request.resignation_date:
                raise ValidationError("Official last working day cannot be before resignation date.")

            exit_request.official_lwd = final_lwd
            exit_request.status = ExitRequest.STATUS_APPROVED

            if waive_shortfall:
                exit_request.shortfall_waived = True
                exit_request.notice_shortfall_days = 0
            else:
                exit_request.shortfall_waived = False
                exit_request.calculate_shortfall()

            # Record pending separation date on employee
            emp = exit_request.employee
            emp.separation_date = final_lwd
            emp.separation_reason = f"Resignation: {exit_request.get_reason_category_display()}"
            # Save employee (leaving status active until final clearance / settlement)
            emp.save(update_fields=["separation_date", "separation_reason"])

        elif action == "reject":
            exit_request.status = ExitRequest.STATUS_REJECTED

        exit_request.save()
        return exit_request
