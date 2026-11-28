from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.organizations.tenancy import scope_queryset
from core import roles
from core.forms_offboarding import (
    ExitRequestReviewForm,
    ExitSettlementAdjustmentForm,
    ResignationSubmissionForm,
)
from core.models import Employee
from core.models_offboarding import ExitClearanceItem, ExitRequest, ExitSettlement
from core.services.offboarding import (
    action_clearance_item,
    approve_fnf_settlement,
    calculate_fnf_settlement,
    finalize_fnf_and_deactivate_employee,
    generate_exit_clearance_items,
    review_exit_request,
    submit_resignation,
)


def _can_manage_offboarding(user):
    """Checks if user has HR, administrative, or managerial privileges to review exit requests."""
    return (
        user.is_superuser
        or user.is_staff
        or user.has_perm(roles.VIEW_EMPLOYEE)
        or user.has_perm(roles.CHANGE_EMPLOYEE)
        or (hasattr(user, "employee_profile") and user.employee_profile.direct_reports.exists())
    )


@login_required
def resignation_submit(request):
    """
    Employee self-service resignation submission and notice period preview.
    """
    employee = getattr(request.user, "employee_profile", None)
    if not employee:
        messages.error(request, "No employee record is linked to your account.")
        return redirect("dashboard")

    # If employee already has an active or approved exit request, show current status
    active_exit = ExitRequest.objects.filter(
        employee=employee,
        status__in=[
            ExitRequest.STATUS_SUBMITTED,
            ExitRequest.STATUS_APPROVED,
            ExitRequest.STATUS_CLEARANCE_IN_PROGRESS,
        ]
    ).first()

    if active_exit:
        return redirect("offboarding_resignation_status")

    if request.method == "POST":
        form = ResignationSubmissionForm(request.POST, employee=employee)
        if form.is_valid():
            try:
                exit_req = submit_resignation(
                    employee=employee,
                    requested_lwd=form.cleaned_data["requested_lwd"],
                    reason_category=form.cleaned_data["reason_category"],
                    reason_detail=form.cleaned_data.get("reason_detail", ""),
                    notice_period_days=30,
                )
                messages.success(
                    request,
                    "Your resignation has been submitted and is pending managerial review."
                )
                return redirect("offboarding_resignation_status")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = ResignationSubmissionForm(employee=employee)

    today = timezone.localdate()
    standard_lwd = today + timedelta(days=30)

    return render(
        request,
        "offboarding/resignation_submit.html",
        {
            "form": form,
            "employee": employee,
            "today": today,
            "standard_lwd": standard_lwd,
            "notice_period_days": 30,
            "title": "Submit Resignation",
        }
    )


@login_required
def resignation_status(request):
    """
    View status, notice period breakdown, and approval progress of the logged-in employee's exit request.
    """
    employee = getattr(request.user, "employee_profile", None)
    if not employee:
        messages.error(request, "No employee record is linked to your account.")
        return redirect("dashboard")

    exit_req = ExitRequest.objects.filter(employee=employee).order_by("-created_at").first()
    if not exit_req:
        return redirect("offboarding_resignation_submit")

    today = timezone.localdate()
    days_remaining = None
    effective_lwd = exit_req.official_lwd or exit_req.requested_lwd
    if effective_lwd and effective_lwd >= today:
        days_remaining = (effective_lwd - today).days

    return render(
        request,
        "offboarding/resignation_status.html",
        {
            "exit_req": exit_req,
            "employee": employee,
            "days_remaining": days_remaining,
            "title": "Resignation Status",
        }
    )


@login_required
def exit_requests_list(request):
    """
    Console for Managers, HR, and Admins to track and review all employee resignations.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to view offboarding exit requests.")

    current_status = request.GET.get("status", "all").strip().upper()
    search_query = request.GET.get("q", "").strip()

    qs = scope_queryset(
        ExitRequest.objects.select_related(
            "employee",
            "employee__department",
            "employee__position",
            "reviewed_by",
            "organization",
        ),
        request.user
    )

    # Restrict non-HR/Admins to their own direct reports
    is_hr_or_admin = (
        request.user.is_superuser
        or request.user.is_staff
        or request.user.has_perm(roles.VIEW_EMPLOYEE)
        or request.user.has_perm(roles.CHANGE_EMPLOYEE)
    )
    if not is_hr_or_admin:
        emp = getattr(request.user, "employee_profile", None)
        if emp:
            qs = qs.filter(employee__manager=emp)
        else:
            qs = qs.none()

    if current_status in dict(ExitRequest.STATUS_CHOICES):
        qs = qs.filter(status=current_status)

    if search_query:
        qs = qs.filter(
            models.Q(employee__name__icontains=search_query)
            | models.Q(employee__email__icontains=search_query)
            | models.Q(reason_detail__icontains=search_query)
        )

    # Metric counts
    scoped_all = scope_queryset(ExitRequest.objects.all(), request.user)
    if not is_hr_or_admin and getattr(request.user, "employee_profile", None):
        scoped_all = scoped_all.filter(employee__manager=request.user.employee_profile)

    counts = {
        "all": scoped_all.count(),
        "submitted": scoped_all.filter(status=ExitRequest.STATUS_SUBMITTED).count(),
        "approved": scoped_all.filter(status=ExitRequest.STATUS_APPROVED).count(),
        "clearance": scoped_all.filter(status=ExitRequest.STATUS_CLEARANCE_IN_PROGRESS).count(),
        "settled": scoped_all.filter(status=ExitRequest.STATUS_SETTLED).count(),
    }

    paginator = Paginator(qs, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "offboarding/requests_list.html",
        {
            "page_obj": page_obj,
            "counts": counts,
            "current_status": current_status,
            "search_query": search_query,
            "n": "offboarding_requests_list",
            "title": "Exit & Resignation Requests",
        }
    )


@login_required
def exit_request_detail(request, pk):
    """
    Detailed inspection of an employee resignation with managerial review controls.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to view exit requests.")

    exit_req = get_object_or_404(
        scope_queryset(
            ExitRequest.objects.select_related(
                "employee",
                "employee__department",
                "employee__position",
                "employee__manager",
                "reviewed_by",
                "organization",
            ),
            request.user
        ),
        pk=pk
    )

    form = ExitRequestReviewForm(exit_request=exit_req)

    return render(
        request,
        "offboarding/request_detail.html",
        {
            "exit_req": exit_req,
            "form": form,
            "title": f"Exit Request: {exit_req.employee.name}",
        }
    )


@login_required
def exit_request_review(request, pk):
    """
    POST endpoint to process managerial / HR decision on an exit request.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to review exit requests.")

    if request.method != "POST":
        return redirect("offboarding_request_detail", pk=pk)

    exit_req = get_object_or_404(
        scope_queryset(ExitRequest.objects.all(), request.user),
        pk=pk
    )

    form = ExitRequestReviewForm(request.POST, exit_request=exit_req)
    if form.is_valid():
        action = form.cleaned_data["action"]
        official_lwd = form.cleaned_data.get("official_lwd")
        waive_shortfall = form.cleaned_data.get("waive_shortfall", False)
        review_notes = form.cleaned_data.get("review_notes", "")

        try:
            review_exit_request(
                exit_request_id=exit_req.pk,
                reviewer=request.user,
                action=action,
                official_lwd=official_lwd,
                waive_shortfall=waive_shortfall,
                review_notes=review_notes,
            )
            messages.success(
                request,
                f"Resignation for {exit_req.employee.name} was successfully {action}d."
            )
        except ValidationError as e:
            messages.error(request, str(e))
    else:
        for err_list in form.errors.values():
            for err in err_list:
                messages.error(request, err)

    return redirect("offboarding_request_detail", pk=pk)


@login_required
def exit_clearance_console(request):
    """
    Multi-departmental exit clearance tracking console (IT, Finance, HR, Facilities).
    Shows departing employees with gating status, progress bars, and one-click actions.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to access the exit clearance console.")

    current_role = request.GET.get("role", "all").strip().upper()
    current_status = request.GET.get("status", "all").strip().upper()
    search_query = request.GET.get("q", "").strip()

    # Query active exit requests undergoing or ready for clearance
    exit_requests_qs = scope_queryset(
        ExitRequest.objects.filter(
            status__in=[
                ExitRequest.STATUS_CLEARANCE_IN_PROGRESS,
                ExitRequest.STATUS_APPROVED,
                ExitRequest.STATUS_SETTLED,
            ]
        ).select_related(
            "employee",
            "employee__department",
            "employee__position",
            "organization",
        ).prefetch_related(
            "clearance_items",
            "clearance_items__cleared_by",
        ),
        request.user
    )

    if search_query:
        exit_requests_qs = exit_requests_qs.filter(
            models.Q(employee__name__icontains=search_query)
            | models.Q(employee__email__icontains=search_query)
            | models.Q(employee__department__name__icontains=search_query)
            | models.Q(employee__position__title__icontains=search_query)
        )

    today = timezone.localdate()
    valid_roles = [
        ExitClearanceItem.ROLE_IT,
        ExitClearanceItem.ROLE_FINANCE,
        ExitClearanceItem.ROLE_HR,
        ExitClearanceItem.ROLE_FACILITIES,
    ]

    total_employees = exit_requests_qs.count()
    completed_clearance_count = 0
    blocked_items_count = 0
    total_items_count = 0
    cleared_items_count = 0

    clearance_data = []
    for exit_req in exit_requests_qs:
        all_items = list(exit_req.clearance_items.all())

        # Auto-instantiate if not yet generated
        if not all_items:
            all_items = generate_exit_clearance_items(exit_req)

        # Department gates summary for this employee
        gates = {
            "IT": all(item.status == ExitClearanceItem.STATUS_CLEARED for item in all_items if item.department_role == ExitClearanceItem.ROLE_IT),
            "FINANCE": all(item.status == ExitClearanceItem.STATUS_CLEARED for item in all_items if item.department_role == ExitClearanceItem.ROLE_FINANCE),
            "HR": all(item.status == ExitClearanceItem.STATUS_CLEARED for item in all_items if item.department_role == ExitClearanceItem.ROLE_HR),
            "FACILITIES": all(item.status == ExitClearanceItem.STATUS_CLEARED for item in all_items if item.department_role == ExitClearanceItem.ROLE_FACILITIES),
        }

        # Filter items if specific role is selected
        if current_role in valid_roles:
            filtered_items = [i for i in all_items if i.department_role == current_role]
        else:
            filtered_items = all_items

        if current_status in dict(ExitClearanceItem.STATUS_CHOICES):
            filtered_items = [i for i in filtered_items if i.status == current_status]

        for item in all_items:
            total_items_count += 1
            if item.status == ExitClearanceItem.STATUS_CLEARED:
                cleared_items_count += 1
            elif item.status == ExitClearanceItem.STATUS_BLOCKED:
                blocked_items_count += 1

        tot = len(all_items)
        clr = sum(1 for i in all_items if i.status == ExitClearanceItem.STATUS_CLEARED)
        prog = int((clr / tot) * 100) if tot > 0 else 0
        if prog == 100:
            completed_clearance_count += 1

        effective_lwd = exit_req.official_lwd or exit_req.requested_lwd
        days_to_lwd = (effective_lwd - today).days if effective_lwd else None

        clearance_data.append({
            "exit_req": exit_req,
            "items": filtered_items,
            "total_items": tot,
            "cleared_items": clr,
            "progress_percentage": prog,
            "gates": gates,
            "effective_lwd": effective_lwd,
            "days_to_lwd": days_to_lwd,
        })

    paginator = Paginator(clearance_data, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    roles_list = [
        ("all", "All Departments"),
        (ExitClearanceItem.ROLE_IT, "IT Department"),
        (ExitClearanceItem.ROLE_FINANCE, "Finance & Accounts"),
        (ExitClearanceItem.ROLE_HR, "Human Resources"),
        (ExitClearanceItem.ROLE_FACILITIES, "Admin & Facilities"),
    ]

    return render(
        request,
        "offboarding/clearance_console.html",
        {
            "page_obj": page_obj,
            "total_employees": total_employees,
            "completed_clearance_count": completed_clearance_count,
            "blocked_items_count": blocked_items_count,
            "total_items_count": total_items_count,
            "cleared_items_count": cleared_items_count,
            "current_role": current_role,
            "current_status": current_status,
            "search_query": search_query,
            "roles_list": roles_list,
            "today": today,
            "n": "offboarding_clearance_console",
            "title": "Exit Clearance Console",
        }
    )


@login_required
def exit_clearance_action(request, pk):
    """
    POST action to clear, block, reopen, or annotate an exit clearance item.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to action exit clearance items.")

    if request.method != "POST":
        return redirect("offboarding_clearance_console")

    item = get_object_or_404(
        ExitClearanceItem.objects.select_related("exit_request", "organization"),
        pk=pk
    )

    # Scoped tenancy check
    scoped_requests = scope_queryset(ExitRequest.objects.all(), request.user)
    if not scoped_requests.filter(pk=item.exit_request_id).exists():
        raise PermissionDenied("You do not have access to this exit clearance item.")

    action = request.POST.get("action", "").strip()
    notes = request.POST.get("notes", "").strip()

    try:
        action_clearance_item(item.pk, user=request.user, action=action, notes=notes if notes else item.notes)
        messages.success(request, f"Clearance item '{item.item_name}' updated.")
    except ValidationError as e:
        messages.error(request, str(e))

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("offboarding_clearance_console")
    return redirect(next_url)


@login_required
def exit_settlement_detail(request, pk):
    """
    Full & Final (F&F) Settlement review and calculation inspection console.
    Displays itemized payable earnings, deductible recoveries, statutory breakdown,
    and adjustments form.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to view exit settlements.")

    exit_req = get_object_or_404(
        scope_queryset(
            ExitRequest.objects.select_related(
                "employee",
                "employee__department",
                "employee__position",
                "organization",
            ),
            request.user
        ),
        pk=pk
    )

    # Auto-generate draft settlement if it doesn't exist yet
    if hasattr(exit_req, "settlement") and exit_req.settlement is not None:
        settlement = exit_req.settlement
    else:
        settlement = calculate_fnf_settlement(exit_req, user=request.user)

    form = ExitSettlementAdjustmentForm(settlement=settlement)

    return render(
        request,
        "offboarding/settlement_detail.html",
        {
            "exit_req": exit_req,
            "settlement": settlement,
            "form": form,
            "title": f"F&F Settlement: {exit_req.employee.name}",
            "n": "offboarding_settlement_detail",
        }
    )


@login_required
def exit_settlement_action(request, pk):
    """
    POST action endpoint to recalculate settlement with custom overrides or finalize approval.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to manage exit settlements.")

    if request.method != "POST":
        return redirect("offboarding_settlement_detail", pk=pk)

    exit_req = get_object_or_404(
        scope_queryset(ExitRequest.objects.all(), request.user),
        pk=pk
    )

    action = request.POST.get("action", "recalculate").strip().lower()

    if action == "approve":
        if hasattr(exit_req, "settlement") and exit_req.settlement is not None:
            remarks = request.POST.get("remarks", "")
            approve_fnf_settlement(exit_req.settlement.pk, user=request.user, remarks=remarks)
            messages.success(request, f"Full & Final Settlement for {exit_req.employee.name} approved successfully.")
        else:
            messages.error(request, "Cannot approve settlement before calculation.")
    elif action == "sign_off":
        if hasattr(exit_req, "settlement") and exit_req.settlement is not None:
            sign_off_notes = request.POST.get("sign_off_notes", "").strip()
            try:
                finalize_fnf_and_deactivate_employee(exit_req.settlement.pk, user=request.user, sign_off_notes=sign_off_notes)
                messages.success(
                    request,
                    f"Official F&F sign-off completed for {exit_req.employee.name}. "
                    "Settlement locked, employee status set to Resigned, and user login deactivated."
                )
            except ValidationError as e:
                messages.error(request, str(e))
        else:
            messages.error(request, "Cannot sign off settlement before it is calculated.")
    else:
        # Recalculate with form overrides
        form = ExitSettlementAdjustmentForm(request.POST)
        if form.is_valid():
            overrides = form.cleaned_data
            calculate_fnf_settlement(exit_req, user=request.user, overrides=overrides)
            messages.success(request, "Settlement recalculated and updated successfully.")
        else:
            messages.error(request, "Failed to recalculate settlement. Check the provided values.")

    return redirect("offboarding_settlement_detail", pk=pk)


@login_required
def exit_settlement_statement(request, pk):
    """
    Printable / PDF-ready official Full & Final (F&F) Settlement Statement sheet.
    Contains complete employee credentials, tenure, itemized earnings & deductions,
    net payable/recoverable balance in bold words & figures, and dual signatory blocks.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to view settlement statements.")

    exit_req = get_object_or_404(
        scope_queryset(
            ExitRequest.objects.select_related(
                "employee",
                "employee__department",
                "employee__position",
                "organization",
            ).prefetch_related(
                "clearance_items",
                "clearance_items__cleared_by",
            ),
            request.user
        ),
        pk=pk
    )

    if not hasattr(exit_req, "settlement") or exit_req.settlement is None:
        messages.warning(request, "Generating draft settlement statement.")
        settlement = calculate_fnf_settlement(exit_req, user=request.user)
    else:
        settlement = exit_req.settlement

    statement_number = settlement.ensure_statement_number()

    employee = exit_req.employee
    try:
        statutory_profile = employee.statutory_profile
    except Exception:
        statutory_profile = None

    try:
        bank_account = employee.bank_account
    except Exception:
        bank_account = None

    today = timezone.localdate()

    return render(
        request,
        "offboarding/statement.html",
        {
            "exit_req": exit_req,
            "settlement": settlement,
            "employee": employee,
            "statutory_profile": statutory_profile,
            "bank_account": bank_account,
            "statement_number": statement_number,
            "today": today,
            "title": f"F&F Statement - {statement_number}",
        }
    )



@login_required
def exit_settlements_list(request):
    """
    Dashboard of all employee Full & Final settlements.
    Shows net balance breakdown, payment status, and filtering by settlement state.
    """
    if not _can_manage_offboarding(request.user):
        raise PermissionDenied("You do not have permission to view the settlements dashboard.")

    status_filter = request.GET.get("status", "all").strip().upper()
    search_query = request.GET.get("q", "").strip()

    qs = scope_queryset(
        ExitSettlement.objects.select_related(
            "employee",
            "employee__department",
            "employee__position",
            "exit_request",
            "organization",
        ),
        request.user
    )

    if status_filter in dict(ExitSettlement.STATUS_CHOICES):
        qs = qs.filter(status=status_filter)
    elif status_filter == "RECOVERABLE":
        qs = qs.filter(is_recoverable=True)
    elif status_filter == "PAYABLE":
        qs = qs.filter(is_recoverable=False)

    if search_query:
        qs = qs.filter(
            models.Q(employee__name__icontains=search_query)
            | models.Q(employee__email__icontains=search_query)
            | models.Q(remarks__icontains=search_query)
        )

    # Scoped metric counters
    all_settlements = scope_queryset(ExitSettlement.objects.all(), request.user)
    counts = {
        "all": all_settlements.count(),
        "calculated": all_settlements.filter(status=ExitSettlement.STATUS_CALCULATED).count(),
        "approved": all_settlements.filter(status=ExitSettlement.STATUS_APPROVED).count(),
        "settled": all_settlements.filter(status=ExitSettlement.STATUS_SETTLED).count(),
        "recoverable": all_settlements.filter(is_recoverable=True).count(),
    }

    paginator = Paginator(qs, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "offboarding/settlements_list.html",
        {
            "page_obj": page_obj,
            "counts": counts,
            "current_status": status_filter,
            "search_query": search_query,
            "title": "Full & Final (F&F) Settlements",
            "n": "offboarding_settlements_list",
        }
    )


