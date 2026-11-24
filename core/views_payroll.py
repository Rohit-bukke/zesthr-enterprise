"""HTTP boundary for payroll review and finalization."""
import csv
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from apps.organizations.models import Organization
from apps.organizations.tenancy import scope_queryset
from . import roles
from .models import PayrollRun, Salary
from .permissions import permission_required
from .services import payroll
from .services.csv_export import safe_cell


class PayrollRunForm(forms.Form):
    organization = forms.ModelChoiceField(queryset=Organization.objects.none())
    month = forms.IntegerField(min_value=1, max_value=12)
    year = forms.IntegerField(min_value=2000, max_value=2100)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = scope_queryset(Organization.objects.all(), user, "pk")


@permission_required(roles.VIEW_PAYROLL_REPORT)
def runs(request):
    today = timezone.localdate()
    form = PayrollRunForm(request.POST or None, user=request.user,
                          initial={"month": today.month, "year": today.year})
    if request.method == "POST" and form.is_valid():
        try:
            run = payroll.create_payroll_run(user=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("payroll_run_detail", pk=run.pk)
    items = scope_queryset(PayrollRun.objects.select_related("organization"), request.user)
    from django.core.paginator import Paginator
    return render(request, "payroll_runs.html", {
        "form": form, "runs": Paginator(items, 30).get_page(request.GET.get("page"))})


@permission_required(roles.VIEW_PAYROLL_REPORT)
def run_detail(request, pk):
    run = get_object_or_404(scope_queryset(PayrollRun.objects.select_related("organization"), request.user), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "submit":
                payroll.submit_payroll_for_approval(run, request.user)
            elif action == "approve":
                payroll.approve_payroll(run, request.user)
            elif action == "finalize":
                payroll.finalize_payroll(run, request.user)
            elif action == "reverse":
                payroll.reverse_payroll(run, user=request.user, reason=request.POST.get("reason", ""))
            else:
                raise ValidationError("Choose a valid payroll action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "Payroll action completed.")
        return redirect("payroll_run_detail", pk=run.pk)
    return render(request, "payroll_run_detail.html", {"run": run,
        "events": run.events.select_related("actor"), "rows": run.calculation_snapshot})


@permission_required(roles.VIEW_PAYROLL_REPORT)
def payroll_csv(request):
    try:
        month, year = int(request.GET.get("month", "")), int(request.GET.get("year", ""))
        payroll.validate_period(month, year)
    except (ValueError, TypeError, ValidationError):
        return HttpResponse("Choose a valid payroll month and year.", status=400)
    records = scope_queryset(Salary.objects.filter(month=month, year=year).select_related("emp"), request.user,
                             "emp__organization")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="payroll-{year}-{month:02d}.csv"'
    response["Cache-Control"] = "private, no-store"
    writer = csv.writer(response)
    writer.writerow(["Employee ID", "Employee", "Gross", "Deduction", "Net"])
    for record in records.iterator(chunk_size=500):
        writer.writerow([safe_cell(record.emp.employee_number), safe_cell(record.emp.name),
                         record.gross, record.deduction, record.net])
    return response


@permission_required(roles.VIEW_PAYROLL_REPORT)
def payout_file(request, pk, format_type="standard"):
    run = get_object_or_404(scope_queryset(PayrollRun.objects.select_related("organization"), request.user), pk=pk)
    if run.status != PayrollRun.FINALIZED:
        messages.error(request, "Bank payout files can only be exported for finalized payroll runs.")
        return redirect("payroll_run_detail", pk=run.pk)

    from .services import bank_payout
    debit_account = request.GET.get("debit_account", "")
    try:
        payout = bank_payout.generate_payout_file(run, format_type=format_type, debit_account=debit_account)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("payroll_run_detail", pk=run.pk)

    # Log audit event
    from .models import PayrollEvent
    PayrollEvent.objects.create(
        run=run,
        action="PAYOUT_EXPORTED",
        actor=request.user,
        reason=f"Exported {format_type.upper()} payout file ({payout.record_count} records, total: {payout.total_amount})"
    )

    response = HttpResponse(payout.content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{payout.filename}"'
    response["Cache-Control"] = "private, no-store"
    return response
