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
from core.models_offboarding import ExitClearanceItem, ExitRequest


# Default cross-departmental exit clearance checklist templates
DEFAULT_EXIT_CLEARANCE_TEMPLATES = {
    ExitClearanceItem.ROLE_IT: [
        {
            "item_name": "Laptop & Workstation Hardware Return",
            "description": "Inspect and recover company laptop, charger, monitors, cables, and external peripherals.",
        },
        {
            "item_name": "Google Workspace & Corporate Email De-provisioning",
            "description": "Forward incoming correspondence, set out-of-office autoreply, and suspend Google Workspace login.",
        },
        {
            "item_name": "Repository & Communication Access Revocation",
            "description": "Revoke access tokens, VPN profiles, GitHub organization membership, and Slack credentials.",
        },
    ],
    ExitClearanceItem.ROLE_FINANCE: [
        {
            "item_name": "Outstanding Expense Claims Settlement",
            "description": "Verify that all submitted expense claims and travel vouchers are finalized or reimbursed.",
        },
        {
            "item_name": "Company Credit Card & Petty Cash Closure",
            "description": "Recover physical corporate credit card, settle pending statements, and audit petty cash handovers.",
        },
        {
            "item_name": "Finance & Accounts No-Dues Clearance",
            "description": "Audit employee ledger for pending salary advances, travel advances, or equipment penalties.",
        },
    ],
    ExitClearanceItem.ROLE_HR: [
        {
            "item_name": "Formal Exit Interview Completion",
            "description": "Conduct confidential exit interview questionnaire, document departure feedback and reasons.",
        },
        {
            "item_name": "Company ID Badge & Access Card Surrender",
            "description": "Recover employee identity badge, business cards, and parking stickers.",
        },
        {
            "item_name": "Benefits & Insurance Policy Transition",
            "description": "Process health insurance de-enrollment, PF transfer documents, and gratuity advisory.",
        },
    ],
    ExitClearanceItem.ROLE_FACILITIES: [
        {
            "item_name": "Pedestal & Desk Space Handover",
            "description": "Inspect workstation, desk drawers, pedestal keys, and return office property.",
        },
        {
            "item_name": "Physical Access RFID Keycard Return",
            "description": "Deactivate and recover building entrance RFID card, server room keycards, and gym access.",
        },
        {
            "item_name": "Parking Permit & Locker Key Return",
            "description": "Surrender campus vehicle parking permit, physical locker keys, and visitor passes.",
        },
    ],
}


def get_clearance_templates(department_role: Optional[str] = None):
    """
    Returns exit clearance templates, optionally filtered by department role.
    """
    if department_role:
        return [
            {**tpl, "department_role": department_role}
            for tpl in DEFAULT_EXIT_CLEARANCE_TEMPLATES.get(department_role, [])
        ]
    templates = []
    for dept_role, tpl_list in DEFAULT_EXIT_CLEARANCE_TEMPLATES.items():
        for tpl in tpl_list:
            templates.append({**tpl, "department_role": dept_role})
    return templates


def generate_exit_clearance_items(exit_request: ExitRequest, custom_templates=None):
    """
    Instantiates departmental clearance items for an exit request and transitions status to CLEARANCE_IN_PROGRESS.
    """
    templates = custom_templates if custom_templates is not None else get_clearance_templates()

    existing_keys = set(
        exit_request.clearance_items.values_list("department_role", "item_name")
    )

    created_items = []
    with transaction.atomic():
        for tpl in templates:
            role = tpl.get("department_role", ExitClearanceItem.ROLE_HR)
            name = tpl["item_name"]
            if (role, name) in existing_keys:
                continue

            item = ExitClearanceItem(
                exit_request=exit_request,
                organization=exit_request.organization,
                department_role=role,
                item_name=name,
                description=tpl.get("description", ""),
                status=ExitClearanceItem.STATUS_PENDING,
            )
            created_items.append(item)

        if created_items:
            ExitClearanceItem.objects.bulk_create(created_items)

    return created_items


def action_clearance_item(item_id: int, user, action: str, notes: str = "") -> ExitClearanceItem:
    """
    Handles department clearance actions: 'clear', 'block', 'in_progress', 'reopen', or 'update_notes'.
    """
    action = action.lower().strip()
    item = ExitClearanceItem.objects.select_for_update().get(pk=item_id)

    with transaction.atomic():
        if action == "clear":
            item.mark_cleared(user=user, notes=notes)
        elif action == "block":
            item.mark_blocked(user=user, notes=notes)
        elif action == "in_progress":
            item.status = ExitClearanceItem.STATUS_IN_PROGRESS
            if notes:
                item.notes = notes
            item.save(update_fields=["status", "notes", "updated_at"])
        elif action == "reopen":
            item.status = ExitClearanceItem.STATUS_PENDING
            item.cleared_at = None
            item.cleared_by = None
            if notes:
                item.notes = notes
            item.save(update_fields=["status", "cleared_at", "cleared_by", "notes", "updated_at"])
        elif action == "update_notes":
            item.notes = notes
            item.save(update_fields=["notes", "updated_at"])
        else:
            raise ValidationError(f"Invalid clearance action '{action}'.")

        if item.exit_request.status == ExitRequest.STATUS_APPROVED:
            item.exit_request.status = ExitRequest.STATUS_CLEARANCE_IN_PROGRESS
            item.exit_request.save(update_fields=["status"])

        item.exit_request.check_clearance_completion()
        return item


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

            exit_request.save()

            # Instantiate departmental clearance gates
            generate_exit_clearance_items(exit_request)
            return exit_request
        elif action == "reject":
            exit_request.status = ExitRequest.STATUS_REJECTED
            exit_request.save()
            return exit_request
