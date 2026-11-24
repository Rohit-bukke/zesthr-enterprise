"""Corporate bank disbursement payout file generator.

Generates bank-ready batch upload files for corporate banking systems
(Standard NEFT/RTGS CSV, HDFC CMS/E-Net, and ICICI CIB).
"""

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..models import PayrollRun
from .csv_export import safe_cell


@dataclass(frozen=True)
class PayoutResult:
    content: str
    filename: str
    record_count: int
    total_amount: Decimal
    skipped_count: int
    format_type: str


def generate_payout_file(run: PayrollRun, format_type: str = "standard", debit_account: str = "") -> PayoutResult:
    """
    Generate corporate bank batch payout file for a finalized PayrollRun.
    Ensures payroll is finalized, validates beneficiary bank accounts,
    and returns streamable CSV formatted data.
    """
    if run.status != PayrollRun.FINALIZED:
        raise ValidationError("Bank payout files can only be generated for finalized payroll runs.")

    snapshot = run.calculation_snapshot or []
    if not snapshot:
        raise ValidationError("Payroll run does not contain any calculation records.")

    format_type = (format_type or "standard").lower().strip()
    if format_type not in ("standard", "hdfc", "icici"):
        raise ValidationError(f"Unsupported payout format '{format_type}'. Supported: standard, hdfc, icici.")

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    records = 0
    skipped = 0
    total_amount = Decimal("0.00")
    period_label = f"{run.month:02d}{run.year}"
    today_str = timezone.localdate().strftime("%d/%m/%Y")

    if format_type == "standard":
        writer.writerow([
            "Payment Mode",
            "Beneficiary Name",
            "Account Number",
            "IFSC Code",
            "Amount",
            "Currency",
            "Transaction Ref",
            "Narrative",
            "Employee ID",
        ])
    elif format_type == "hdfc":
        writer.writerow([
            "Transaction Type",
            "Beneficiary Code",
            "Account Number",
            "Amount",
            "Beneficiary Name",
            "Payment Details",
            "IFSC Code",
        ])
    elif format_type == "icici":
        writer.writerow([
            "Debit Account No",
            "Beneficiary Account No",
            "Beneficiary Name",
            "Amount",
            "Payment Mode",
            "Date",
            "IFSC",
            "Payment Ref",
        ])

    for row in snapshot:
        net = Decimal(str(row.get("net", "0.00")))
        if net <= Decimal("0.00"):
            skipped += 1
            continue

        bank = row.get("bank_details") or {}
        # If snapshot bank_details is missing raw_account_number, fallback to live DB
        raw_account = bank.get("raw_account_number")
        ifsc = bank.get("ifsc_code")
        holder_name = bank.get("account_holder_name") or row.get("name", "")
        method = bank.get("payment_method", "NEFT")

        if not raw_account or not ifsc:
            # Check if employee has bank_account in DB
            from ..models import Employee
            try:
                emp = Employee.objects.select_related("bank_account").get(pk=row.get("employee_id"))
                if hasattr(emp, "bank_account") and emp.bank_account:
                    raw_account = emp.bank_account.account_number
                    ifsc = emp.bank_account.ifsc_code
                    holder_name = emp.bank_account.account_holder_name or emp.name
                    method = emp.bank_account.payment_method
            except (Employee.DoesNotExist, AttributeError):
                pass

        if not raw_account or not ifsc or method in ("CASH", "CHEQUE"):
            skipped += 1
            continue

        emp_code = str(row.get("employee_id", ""))
        emp_name = safe_cell(holder_name)
        tx_ref = f"SAL-{period_label}-{emp_code}"
        narrative = f"Salary {run.month:02d}/{run.year}"

        # Resolve mode: RTGS for amounts >= 200,000, else NEFT
        pay_mode = "RTGS" if (net >= Decimal("200000.00") and method != "IMPS") else ("IMPS" if method == "IMPS" else "NEFT")

        if format_type == "standard":
            writer.writerow([
                pay_mode,
                emp_name,
                safe_cell(raw_account),
                ifsc.upper(),
                f"{net:.2f}",
                row.get("currency", "INR"),
                tx_ref,
                narrative,
                emp_code,
            ])
        elif format_type == "hdfc":
            # HDFC CMS format: 'N' for NEFT, 'R' for RTGS, 'I' for internal
            hdfc_type = "R" if pay_mode == "RTGS" else ("N" if pay_mode == "NEFT" else "N")
            writer.writerow([
                hdfc_type,
                emp_code,
                safe_cell(raw_account),
                f"{net:.2f}",
                emp_name,
                narrative,
                ifsc.upper(),
            ])
        elif format_type == "icici":
            writer.writerow([
                safe_cell(debit_account or "CORP_DEBIT_ACCOUNT"),
                safe_cell(raw_account),
                emp_name,
                f"{net:.2f}",
                pay_mode,
                today_str,
                ifsc.upper(),
                tx_ref,
            ])

        records += 1
        total_amount += net

    filename = f"payout_run_{run.pk}_{format_type}_{run.year}_{run.month:02d}.csv"
    return PayoutResult(
        content=output.getvalue(),
        filename=filename,
        record_count=records,
        total_amount=total_amount,
        skipped_count=skipped,
        format_type=format_type,
    )
