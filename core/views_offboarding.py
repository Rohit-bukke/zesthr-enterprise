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
from core.forms_offboarding import ExitRequestReviewForm, ResignationSubmissionForm
from core.models import Employee
from core.models_offboarding import ExitRequest
from core.services.offboarding import review_exit_request, submit_resignation


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
