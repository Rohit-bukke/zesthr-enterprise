"""
Offboarding & Separation Service Module.
Handles resignation submissions, notice period and shortfall calculations,
multi-departmental clearance checklist tracking, and automated Full & Final (F&F) settlement calculations.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Employee, EmploymentHistory, ExpenseClaim, LeaveEntitlement, SecurityEvent
from core.models_offboarding import ExitClearanceItem, ExitRequest, ExitSettlement
from core.services.accounts import record_event
from core.services.leave_balances import balance
from core.services.statutory import calculate_statutory_deductions


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


def _clean_decimal_override(val, default=None):
    if val is not None and str(val).strip() != "":
        try:
            return Decimal(str(val)).quantize(Decimal("0.01"))
        except Exception:
            return default
    return default


def calculate_fnf_settlement(
    exit_request: ExitRequest,
    user=None,
    overrides: Optional[dict] = None
) -> ExitSettlement:
    """
    Automated Full & Final (F&F) Financial Settlement Calculation Engine.
    Computes prorated salary, leave encashment, approved expenses, statutory gratuity,
    notice shortfall recovery, statutory deductions (EPF, ESI, PT, TDS), and net balance.
    """
    overrides = overrides or {}
    employee = exit_request.employee
    lwd = exit_request.official_lwd or exit_request.requested_lwd or timezone.localdate()

    # 1. Base Compensation & Daily Rate
    last_drawn_basic = employee.basic_salary or Decimal("0.00")
    daily_basic_rate = (last_drawn_basic / Decimal("30.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 2. Prorated Final Month Salary
    days_in_month = calendar.monthrange(lwd.year, lwd.month)[1]
    worked_days = min(lwd.day, days_in_month)
    if overrides.get("worked_days_in_final_month") is not None and str(overrides.get("worked_days_in_final_month")).strip() != "":
        worked_days = int(overrides["worked_days_in_final_month"])

    prorated_salary = (
        (last_drawn_basic * Decimal(worked_days)) / Decimal(days_in_month)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    over_salary = _clean_decimal_override(overrides.get("prorated_salary"))
    if over_salary is not None:
        prorated_salary = over_salary

    # 3. Unused Leave Encashment
    over_leave_days = _clean_decimal_override(overrides.get("leave_encashment_days"))
    if over_leave_days is not None:
        leave_encashment_days = over_leave_days
    else:
        # Sum positive balance from LeaveEntitlements for the separation year
        entitlements = LeaveEntitlement.objects.filter(employee=employee, year=lwd.year)
        total_remaining = Decimal("0.00")
        for ent in entitlements:
            try:
                b = balance(ent, as_of=lwd)
                if b.remaining > 0:
                    total_remaining += b.remaining
            except Exception:
                pass
        leave_encashment_days = total_remaining.quantize(Decimal("0.01"))

    leave_encashment_amount = (daily_basic_rate * leave_encashment_days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    over_leave_amt = _clean_decimal_override(overrides.get("leave_encashment_amount"))
    if over_leave_amt is not None:
        leave_encashment_amount = over_leave_amt

    # 4. Approved Unpaid Expense Claim Reimbursements
    over_exp = _clean_decimal_override(overrides.get("reimbursable_expenses_amount"))
    if over_exp is not None:
        reimbursable_expenses_amount = over_exp
    else:
        unpaid_claims = ExpenseClaim.objects.filter(
            employee=employee,
            status=ExpenseClaim.STATUS_APPROVED,
            payroll_run__isnull=True
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        reimbursable_expenses_amount = unpaid_claims.quantize(Decimal("0.01"))

    # 5. Gratuity Calculation (Statutory requirement: tenure >= 5 years under Payment of Gratuity Act)
    if employee.joined_on and lwd >= employee.joined_on:
        tenure_days = (lwd - employee.joined_on).days
        tenure_years = (Decimal(tenure_days) / Decimal("365.25")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        tenure_years = Decimal("0.00")

    # In Indian law, 4 years and 240 days (approx 4.8 years) qualifies for gratuity
    is_gratuity_eligible = tenure_years >= Decimal("4.80")
    if overrides.get("is_gratuity_eligible") is not None and str(overrides.get("is_gratuity_eligible")).strip() != "":
        is_gratuity_eligible = bool(overrides["is_gratuity_eligible"])

    if is_gratuity_eligible and last_drawn_basic > 0:
        completed_years = int(round(tenure_years))
        # Gratuity formula: (15 * Last Basic * Tenure) / 26
        gratuity_amount = (
            (Decimal("15") * last_drawn_basic * Decimal(completed_years)) / Decimal("26")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        gratuity_amount = Decimal("0.00")

    over_grat = _clean_decimal_override(overrides.get("gratuity_amount"))
    if over_grat is not None:
        gratuity_amount = over_grat

    # 6. Additional custom earnings
    additional_earnings = _clean_decimal_override(overrides.get("additional_earnings"), default=Decimal("0.00"))
    additional_earnings_notes = overrides.get("additional_earnings_notes") or ""

    # 7. Notice Period Shortfall Recovery
    if overrides.get("notice_shortfall_days") is not None and str(overrides.get("notice_shortfall_days")).strip() != "":
        notice_shortfall_days = int(overrides["notice_shortfall_days"])
    else:
        notice_shortfall_days = exit_request.notice_shortfall_days if not exit_request.shortfall_waived else 0

    notice_shortfall_recovery = (daily_basic_rate * Decimal(notice_shortfall_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    over_notice = _clean_decimal_override(overrides.get("notice_shortfall_recovery"))
    if over_notice is not None:
        notice_shortfall_recovery = over_notice

    # 8. Salary Advance and Asset Damage Recoveries
    salary_advance_recovery = _clean_decimal_override(overrides.get("salary_advance_recovery"), default=Decimal("0.00"))
    asset_damage_recovery = _clean_decimal_override(overrides.get("asset_damage_recovery"), default=Decimal("0.00"))

    # 9. Final Statutory Deductions (EPF, ESI, Professional Tax, TDS on prorated salary)
    try:
        stat_calc = calculate_statutory_deductions(
            employee=employee,
            basic_salary=prorated_salary,
            gross_pay=prorated_salary,
            month=lwd.month,
            year=lwd.year,
        )
        epf_deduction = stat_calc.epf_employee
        esi_deduction = stat_calc.esi_employee
        professional_tax = stat_calc.pt_amount
        tds_deduction = stat_calc.tds_amount
    except Exception:
        epf_deduction = Decimal("0.00")
        esi_deduction = Decimal("0.00")
        professional_tax = Decimal("0.00")
        tds_deduction = Decimal("0.00")

    over_epf = _clean_decimal_override(overrides.get("epf_deduction"))
    if over_epf is not None:
        epf_deduction = over_epf
    over_esi = _clean_decimal_override(overrides.get("esi_deduction"))
    if over_esi is not None:
        esi_deduction = over_esi
    over_pt = _clean_decimal_override(overrides.get("professional_tax"))
    if over_pt is not None:
        professional_tax = over_pt
    over_tds = _clean_decimal_override(overrides.get("tds_deduction"))
    if over_tds is not None:
        tds_deduction = over_tds

    # 10. Additional custom deductions
    additional_deductions = _clean_decimal_override(overrides.get("additional_deductions"), default=Decimal("0.00"))
    additional_deductions_notes = overrides.get("additional_deductions_notes") or ""
    remarks = overrides.get("remarks") or ""

    # Create or update ExitSettlement instance
    with transaction.atomic():
        settlement, created = ExitSettlement.objects.select_for_update().get_or_create(
            exit_request=exit_request,
            defaults={
                "employee": employee,
                "organization": exit_request.organization,
                "status": ExitSettlement.STATUS_CALCULATED,
                "calculated_by": user,
            }
        )
        if not created and settlement.is_locked:
            raise ValidationError("This Full & Final settlement has been signed off and locked and cannot be recalculated.")

        settlement.last_drawn_basic = last_drawn_basic
        settlement.daily_basic_rate = daily_basic_rate
        settlement.worked_days_in_final_month = worked_days
        settlement.days_in_final_month = days_in_month
        settlement.prorated_salary = prorated_salary
        settlement.leave_encashment_days = leave_encashment_days
        settlement.leave_encashment_amount = leave_encashment_amount
        settlement.reimbursable_expenses_amount = reimbursable_expenses_amount
        settlement.tenure_years = tenure_years
        settlement.is_gratuity_eligible = is_gratuity_eligible
        settlement.gratuity_amount = gratuity_amount
        settlement.additional_earnings = additional_earnings
        settlement.additional_earnings_notes = additional_earnings_notes

        settlement.notice_shortfall_days = notice_shortfall_days
        settlement.notice_shortfall_recovery = notice_shortfall_recovery
        settlement.salary_advance_recovery = salary_advance_recovery
        settlement.asset_damage_recovery = asset_damage_recovery
        settlement.epf_deduction = epf_deduction
        settlement.esi_deduction = esi_deduction
        settlement.professional_tax = professional_tax
        settlement.tds_deduction = tds_deduction
        settlement.additional_deductions = additional_deductions
        settlement.additional_deductions_notes = additional_deductions_notes
        if remarks:
            settlement.remarks = remarks

        if user and not settlement.calculated_by:
            settlement.calculated_by = user

        settlement.status = ExitSettlement.STATUS_CALCULATED
        settlement.recalculate_totals()
        settlement.save()

    return settlement


def approve_fnf_settlement(settlement_id: int, user, remarks: str = "") -> ExitSettlement:
    """
    Finalizes and approves an F&F settlement by Finance / Management.
    """
    with transaction.atomic():
        settlement = ExitSettlement.objects.select_for_update().get(pk=settlement_id)
        if settlement.is_locked:
            raise ValidationError("This Full & Final settlement has been signed off and locked.")
        settlement.status = ExitSettlement.STATUS_APPROVED
        settlement.finalized_by = user
        settlement.finalized_at = timezone.now()
        if remarks:
            settlement.remarks = f"{settlement.remarks}\n{remarks}".strip()
        settlement.save(update_fields=["status", "finalized_by", "finalized_at", "remarks", "updated_at"])

        # If clearance is already complete, update exit_request status to SETTLED
        exit_req = settlement.exit_request
        if exit_req.is_clearance_complete:
            exit_req.status = ExitRequest.STATUS_SETTLED
            exit_req.save(update_fields=["status", "updated_at"])

    return settlement


def finalize_fnf_and_deactivate_employee(
    settlement_id: int,
    user,
    sign_off_notes: str = ""
) -> ExitSettlement:
    """
    Executes final HR sign-off on Full & Final Settlement:
    1. Locks the settlement snapshot (status = SETTLED, is_locked = True).
    2. Ensures immutable statement reference number is assigned.
    3. Deactivates employee employment status to RESIGNED and sets is_active = False.
    4. Revokes web login access by deactivating User account (User.is_active = False).
    5. Records immutable audit trails in EmploymentHistory and SecurityEvent.
    6. Marks ExitRequest status as SETTLED.
    """
    with transaction.atomic():
        settlement = ExitSettlement.objects.select_for_update().get(pk=settlement_id)
        if settlement.is_locked:
            raise ValidationError("This Full & Final settlement has already been signed off and locked.")

        exit_req = settlement.exit_request
        employee = settlement.employee
        lwd = exit_req.official_lwd or exit_req.requested_lwd or timezone.localdate()

        # 1. Lock Settlement
        settlement.status = ExitSettlement.STATUS_SETTLED
        settlement.is_locked = True
        settlement.signed_off_by = user
        settlement.signed_off_at = timezone.now()
        if sign_off_notes:
            settlement.sign_off_notes = sign_off_notes
        settlement.ensure_statement_number()
        settlement.save()

        # 2. Update Exit Request Status
        exit_req.status = ExitRequest.STATUS_SETTLED
        exit_req.save(update_fields=["status", "updated_at"])

        # 3. Transactional Employee Deactivation
        old_status = employee.employment_status
        employee.employment_status = "RESIGNED"
        employee.separation_date = lwd
        employee.separation_reason = f"F&F Settled: {exit_req.get_reason_category_display()}"
        employee.save()  # Employee.clean() automatically sets is_active = False

        # 4. User Login Deactivation
        if employee.user:
            employee.user.is_active = False
            employee.user.save(update_fields=["is_active"])
            record_event(
                SecurityEvent.ACCOUNT_DEACTIVATED,
                user=employee.user,
                username=employee.user.username,
                detail=f"Account deactivated upon offboarding F&F final sign-off by {user.username} (Statement: {settlement.statement_number})",
            )

        # 5. Immutable Audit Trail in EmploymentHistory
        EmploymentHistory.objects.create(
            employee=employee,
            field="employment_status",
            old_value=old_status,
            new_value="RESIGNED",
            changed_by=user,
        )
        EmploymentHistory.objects.create(
            employee=employee,
            field="separation",
            old_value="",
            new_value=f"F&F Settled (Ref: {settlement.statement_number})",
            changed_by=user,
        )

    return settlement


