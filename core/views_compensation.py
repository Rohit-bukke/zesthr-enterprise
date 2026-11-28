from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import roles
from core.models import Employee
from core.models_compensation import CompensationBand, SalaryRevision
from core.models_expenses import ExpenseCategory, ExpenseClaim
from core.permissions import require_employee_profile
from core.services import compensation, expenses
from apps.organizations.tenancy import scope_queryset


@login_required
def compensation_dashboard(request):
    """
    Main compensation landing page:
    - If user has compensation permissions (HR/Admin/Payroll), shows organization salary bands,
      pending revisions, and recent revisions.
    - Otherwise, redirects to the employee's self-service Total Rewards view.
    """
    can_manage = request.user.has_perm(roles.VIEW_EMPLOYEE_PAY) or request.user.has_perm(roles.GENERATE_PAYROLL) or request.user.is_superuser
    if not can_manage:
        return redirect("my_compensation")

    bands = scope_queryset(CompensationBand.objects.select_related("department", "organization"), request.user)
    pending_revisions = scope_queryset(
        SalaryRevision.objects.filter(status=SalaryRevision.STATUS_PROPOSED).select_related("employee", "proposed_by"),
        request.user,
        organization_field="employee__organization"
    )
    recent_revisions = scope_queryset(
        SalaryRevision.objects.exclude(status=SalaryRevision.STATUS_PROPOSED).select_related("employee", "reviewed_by"),
        request.user,
        organization_field="employee__organization"
    )[:15]

    return render(request, "compensation/dashboard.html", {
        "bands": bands,
        "pending_revisions": pending_revisions,
        "recent_revisions": recent_revisions,
        "can_change": request.user.has_perm(roles.CHANGE_EMPLOYEE_PAY) or request.user.is_superuser,
    })


@login_required
def my_compensation(request):
    """
    Self-service Total Rewards & Compensation Statement for the logged-in employee.
    """
    employee = getattr(request.user, "employee_profile", None)
    if not employee:
        # If superuser or admin without employee profile, view the first active employee for preview
        employee = scope_queryset(Employee.objects.filter(is_active=True), request.user).first()
        if not employee:
            messages.error(request, "No employee record associated with your account.")
            return redirect("dashboard")

    rewards = compensation.get_employee_total_rewards(employee)
    claims = employee.expense_claims.select_related("category").order_by("-submitted_at")[:10]

    return render(request, "compensation/my_rewards.html", {
        "employee": employee,
        "rewards": rewards,
        "claims": claims,
    })


@login_required
def salary_revision_propose(request):
    """
    Propose an effective-dated salary revision for an employee.
    """
    if not (request.user.has_perm(roles.CHANGE_EMPLOYEE_PAY) or request.user.is_superuser):
        raise PermissionDenied("You do not have permission to modify employee compensation.")

    employees = scope_queryset(Employee.objects.filter(is_active=True).select_related("department"), request.user)

    if request.method == "POST":
        employee_id = request.POST.get("employee_id")
        revised_salary = request.POST.get("revised_salary")
        reason = request.POST.get("reason", SalaryRevision.REASON_ANNUAL_APPRAISAL)
        effective_date_str = request.POST.get("effective_date")
        review_notes = request.POST.get("review_notes", "").strip()

        try:
            employee = employees.get(pk=employee_id)
            revised_salary = Decimal(revised_salary)
            from datetime import datetime
            effective_date = datetime.strptime(effective_date_str, "%Y-%m-%d").date()

            revision = compensation.propose_salary_revision(
                employee=employee,
                revised_salary=revised_salary,
                reason=reason,
                effective_date=effective_date,
                proposed_by=request.user,
                review_notes=review_notes,
            )
            messages.success(request, f"Salary revision proposed for {employee.name} (Effective {effective_date}).")
            return redirect("compensation_dashboard")
        except (ValueError, InvalidOperation, ValidationError, Employee.DoesNotExist) as exc:
            messages.error(request, f"Error proposing revision: {exc}")

    return render(request, "compensation/propose_revision.html", {
        "employees": employees,
        "reasons": SalaryRevision.REASON_CHOICES,
        "today": timezone.localdate(),
    })


@login_required
def salary_revision_action(request, pk):
    """
    Approve or reject a proposed salary revision.
    """
    if not (request.user.has_perm(roles.CHANGE_EMPLOYEE_PAY) or request.user.is_superuser):
        raise PermissionDenied("You do not have permission to approve/reject salary revisions.")

    revision = get_object_or_404(
        scope_queryset(SalaryRevision.objects.select_related("employee"), request.user, organization_field="employee__organization"),
        pk=pk
    )

    if request.method == "POST":
        action = request.POST.get("action")
        notes = request.POST.get("notes", "").strip()
        try:
            if action == "approve":
                compensation.approve_salary_revision(revision.pk, reviewer=request.user, review_notes=notes)
                messages.success(request, f"Salary revision for {revision.employee.name} approved.")
            elif action == "reject":
                compensation.reject_salary_revision(revision.pk, reviewer=request.user, review_notes=notes)
                messages.warning(request, f"Salary revision for {revision.employee.name} rejected.")
            else:
                messages.error(request, "Invalid revision action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    return redirect("compensation_dashboard")


# =========================================================================
# EXPENSE CLAIMS WORKFLOW
# =========================================================================

@login_required
def expenses_list(request):
    """
    Expense claims list:
    - Employees see their own claims.
    - Managers/Admins also see claims pending review across the organization.
    """
    can_review = request.user.has_perm(roles.APPROVE_LEAVE) or request.user.has_perm(roles.GENERATE_PAYROLL) or request.user.is_superuser
    employee = getattr(request.user, "employee_profile", None)

    my_claims = []
    if employee:
        my_claims = ExpenseClaim.objects.filter(employee=employee).select_related("category").order_by("-submitted_at")

    pending_review = []
    if can_review:
        pending_review = scope_queryset(
            ExpenseClaim.objects.filter(status=ExpenseClaim.STATUS_SUBMITTED).select_related("employee", "category"),
            request.user,
            organization_field="employee__organization"
        )

    categories = scope_queryset(ExpenseCategory.objects.filter(is_active=True), request.user)

    return render(request, "compensation/expenses.html", {
        "my_claims": my_claims,
        "pending_review": pending_review,
        "can_review": can_review,
        "categories": categories,
    })


@login_required
def expense_submit(request):
    """
    Submit a new expense reimbursement claim.
    """
    employee = require_employee_profile(request.user)
    categories = scope_queryset(ExpenseCategory.objects.filter(is_active=True), request.user)

    if request.method == "POST":
        category_id = request.POST.get("category_id")
        title = request.POST.get("title", "").strip()
        amount_str = request.POST.get("amount", "0")
        incurred_date_str = request.POST.get("incurred_date")
        description = request.POST.get("description", "").strip()
        receipt = request.FILES.get("receipt")

        try:
            category = categories.get(pk=category_id)
            amount = Decimal(amount_str)
            from datetime import datetime
            incurred_date = datetime.strptime(incurred_date_str, "%Y-%m-%d").date()

            claim = expenses.submit_expense_claim(
                employee=employee,
                category=category,
                title=title,
                amount=amount,
                incurred_date=incurred_date,
                receipt=receipt,
                description=description,
            )
            messages.success(request, f"Expense claim {claim.claim_number} submitted successfully.")
            return redirect("expenses_list")
        except (ValueError, InvalidOperation, ValidationError, ExpenseCategory.DoesNotExist) as exc:
            messages.error(request, f"Submission error: {exc}")

    return render(request, "compensation/expense_submit.html", {
        "categories": categories,
        "today": timezone.localdate(),
    })


@login_required
def expense_action(request, pk):
    """
    Manager/HR approval or rejection of an expense claim.
    """
    can_review = request.user.has_perm(roles.APPROVE_LEAVE) or request.user.has_perm(roles.GENERATE_PAYROLL) or request.user.is_superuser
    if not can_review:
        raise PermissionDenied("You do not have permission to approve or reject expense claims.")

    claim = get_object_or_404(
        scope_queryset(ExpenseClaim.objects.select_related("employee"), request.user, organization_field="employee__organization"),
        pk=pk
    )

    if request.method == "POST":
        action = request.POST.get("action")
        notes = request.POST.get("notes", "").strip()
        try:
            if action == "approve":
                expenses.approve_expense_claim(claim.pk, reviewer=request.user, decision_notes=notes)
                messages.success(request, f"Claim {claim.claim_number} approved for reimbursement.")
            elif action == "reject":
                expenses.reject_expense_claim(claim.pk, reviewer=request.user, decision_notes=notes)
                messages.warning(request, f"Claim {claim.claim_number} rejected.")
            else:
                messages.error(request, "Invalid action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    return redirect("expenses_list")
