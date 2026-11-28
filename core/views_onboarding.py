from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import OrganizationMembership
from apps.organizations.tenancy import scope_queryset
from core import roles
from core.forms_onboarding import (
    CandidateInvitationForm,
    CandidateRegistrationForm,
    OnboardingStep1PersonalForm,
    OnboardingStep2StatutoryForm,
    OnboardingStep3BankingForm,
    OnboardingStep4PolicyForm,
)
from core.models import Employee
from core.models_banking import EmployeeBankAccount
from core.models_onboarding import CandidateInvitation, OnboardingChecklist, OnboardingProfile, OnboardingTask
from core.models_statutory import EmployeeStatutoryProfile
from core.services.onboarding import instantiate_onboarding_workflow


def _can_manage_onboarding(user):
    return (
        user.is_superuser
        or user.has_perm(roles.ADD_EMPLOYEE)
        or user.has_perm(roles.CHANGE_EMPLOYEE)
    )


def _can_access_onboarding_tracker(user):
    return (
        user.is_authenticated
        and (
            user.is_superuser
            or user.is_staff
            or _can_manage_onboarding(user)
            or user.has_perm(roles.VIEW_EMPLOYEE)
            or user.has_perm(roles.CHANGE_EMPLOYEE)
        )
    )


@login_required
def invitations_list(request):
    """
    Console for HR and administrators to track sent candidate pre-onboarding invitations.
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to manage candidate onboarding invitations.")

    queryset = scope_queryset(
        CandidateInvitation.objects.select_related("department", "position", "invited_by", "organization"),
        request.user
    )

    # Status filter
    status_filter = request.GET.get("status", "all").upper()
    if status_filter in dict(CandidateInvitation.STATUS_CHOICES):
        queryset = queryset.filter(status=status_filter)

    # Search filter
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        queryset = queryset.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
        )

    paginator = Paginator(queryset, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Counts summary
    all_invites = scope_queryset(CandidateInvitation.objects.all(), request.user)
    counts = {
        "all": all_invites.count(),
        "invited": all_invites.filter(status=CandidateInvitation.STATUS_INVITED, expires_at__gt=timezone.now()).count(),
        "accepted": all_invites.filter(status=CandidateInvitation.STATUS_ACCEPTED).count(),
        "expired": all_invites.filter(status=CandidateInvitation.STATUS_EXPIRED).count() + all_invites.filter(status=CandidateInvitation.STATUS_INVITED, expires_at__lte=timezone.now()).count(),
        "revoked": all_invites.filter(status=CandidateInvitation.STATUS_REVOKED).count(),
    }

    return render(request, "onboarding/invitations_list.html", {
        "page_obj": page_obj,
        "counts": counts,
        "current_status": status_filter.lower(),
        "search_query": q,
        "now": timezone.now(),
    })


@login_required
def invitation_create(request):
    """
    Dispatch a new pre-onboarding invitation to a candidate.
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to issue candidate invitations.")

    org = getattr(request.user, "active_organization", None)
    if not org:
        from apps.organizations.models import Organization
        org = Organization.objects.first()

    form = CandidateInvitationForm(
        request.POST or None,
        user=request.user,
        organization=org
    )

    if request.method == "POST" and form.is_valid():
        invitation = form.save()
        invite_url = request.build_absolute_uri(
            reverse("onboarding_candidate_landing", kwargs={"token": invitation.token})
        )
        messages.success(
            request,
            f"Candidate invitation created for {invitation.full_name} ({invitation.email}). Link: {invite_url}"
        )
        return redirect("onboarding_invitations")

    return render(request, "onboarding/invitation_create.html", {
        "form": form,
        "today": timezone.localdate(),
    })


@login_required
def invitation_action(request, pk):
    """
    Action endpoint to revoke or resend an invitation.
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to manage candidate invitations.")

    invitation = get_object_or_404(
        scope_queryset(CandidateInvitation.objects.all(), request.user),
        pk=pk
    )

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "revoke":
                invitation.revoke()
                messages.warning(request, f"Invitation for {invitation.full_name} has been revoked.")
            elif action == "resend":
                # Refresh token and extend expiry by 72 hours
                invitation.expires_at = timezone.now() + timedelta(hours=72)
                invitation.status = CandidateInvitation.STATUS_INVITED
                invitation.save(update_fields=["expires_at", "status"])
                invite_url = request.build_absolute_uri(
                    reverse("onboarding_candidate_landing", kwargs={"token": invitation.token})
                )
                messages.success(
                    request,
                    f"Invitation refreshed for {invitation.full_name}. Valid until {invitation.expires_at.strftime('%d %b %Y %H:%M')}. Link: {invite_url}"
                )
            else:
                messages.error(request, "Invalid invitation action.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    return redirect("onboarding_invitations")


def candidate_landing(request, token):
    """
    Public token-verification and candidate initial account setup view.
    Validates token validity, expiration, and organization status.
    Allows candidate to accept invite and create their user account credentials.
    """
    try:
        invitation = CandidateInvitation.objects.select_related(
            "organization", "department", "position"
        ).get(token=token)
    except (CandidateInvitation.DoesNotExist, ValueError):
        return render(
            request,
            "onboarding/candidate_landing.html",
            {
                "invitation_valid": False,
                "error_message": "This invitation link is invalid or does not exist.",
            },
            status=404,
        )

    # Check if organization is active
    if not invitation.organization.is_active:
        return render(
            request,
            "onboarding/candidate_landing.html",
            {
                "invitation": invitation,
                "invitation_valid": False,
                "error_message": "The organization associated with this invitation is currently inactive.",
            },
            status=403,
        )

    # Check if already accepted
    if invitation.status == CandidateInvitation.STATUS_ACCEPTED:
        return render(
            request,
            "onboarding/candidate_landing.html",
            {
                "invitation": invitation,
                "invitation_valid": False,
                "error_message": "This invitation has already been accepted. Please sign in with your credentials.",
            },
        )

    # Check validity (revoked or expired)
    if not invitation.is_valid():
        error_msg = "This invitation has been revoked." if invitation.status == CandidateInvitation.STATUS_REVOKED else "This invitation has expired. Please contact your HR team for a new link."
        return render(
            request,
            "onboarding/candidate_landing.html",
            {
                "invitation": invitation,
                "invitation_valid": False,
                "error_message": error_msg,
            },
        )

    if request.method == "POST":
        form = CandidateRegistrationForm(request.POST, invitation=invitation)
        if form.is_valid():
            with transaction.atomic():
                User = get_user_model()
                username = form.cleaned_data["username"]
                first_name = form.cleaned_data["first_name"]
                last_name = form.cleaned_data["last_name"]
                phone = form.cleaned_data.get("phone", "")
                password = form.cleaned_data["password"]

                # 1. Create User
                user = User.objects.create_user(
                    username=username,
                    email=invitation.email,
                    first_name=first_name,
                    last_name=last_name,
                    password=password,
                    is_active=True,
                )

                # 2. Grant Organization Membership
                OrganizationMembership.objects.get_or_create(
                    user=user,
                    organization=invitation.organization,
                    defaults={"is_active": True},
                )

                # 3. Create initial pre-hire Employee Profile
                employee_name = f"{first_name} {last_name}".strip()
                position_title = invitation.position.title if invitation.position else "New Hire"
                employee = Employee.objects.create(
                    user=user,
                    name=employee_name,
                    email=invitation.email,
                    phone=phone,
                    designation=position_title,
                    department=invitation.department,
                    position=invitation.position,
                    organization=invitation.organization,
                    joined_on=invitation.proposed_joining_date,
                    employment_status="PROBATION",
                    basic_salary=Decimal("0.00"),
                )

                # 4. Accept the invitation
                invitation.accept(employee=employee)

                # 5. Create or get Onboarding Profile for the candidate
                onboarding_profile, _ = OnboardingProfile.objects.get_or_create(
                    invitation=invitation,
                    defaults={
                        "user": user,
                        "organization": invitation.organization,
                        "current_step": OnboardingProfile.STEP_1_PERSONAL,
                        "status": OnboardingProfile.STATUS_DRAFT,
                    }
                )

                # 6. Instantiate Onboarding Checklist & Provisioning Tasks
                instantiate_onboarding_workflow(invitation, employee=employee)

                # 7. Log the user in
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")

                messages.success(
                    request,
                    f"Welcome to {invitation.organization.name}! Please complete your onboarding details."
                )

                # Redirect to Onboarding Wizard Step 1
                return redirect("onboarding_wizard_step1")
    else:
        form = CandidateRegistrationForm(invitation=invitation)

    return render(
        request,
        "onboarding/candidate_landing.html",
        {
            "invitation": invitation,
            "invitation_valid": True,
            "form": form,
        },
    )


def _get_candidate_onboarding_profile(user):
    """
    Retrieves or establishes the OnboardingProfile for the logged-in candidate.
    """
    profile = OnboardingProfile.objects.filter(user=user).select_related(
        "invitation", "organization", "invitation__department", "invitation__position"
    ).first()
    if not profile:
        # Check if user has an accepted invitation
        invitation = CandidateInvitation.objects.filter(email__iexact=user.email).order_by("-created_at").first()
        if invitation:
            profile, _ = OnboardingProfile.objects.get_or_create(
                invitation=invitation,
                defaults={
                    "user": user,
                    "organization": invitation.organization,
                    "current_step": OnboardingProfile.STEP_1_PERSONAL,
                }
            )
    return profile


@login_required
def onboarding_wizard_step1(request):
    """
    Multi-Step Onboarding Wizard - Step 1: Personal Profile & Emergency Contacts.
    """
    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        messages.info(request, "No active onboarding checklist found for your account.")
        return redirect("dashboard")

    if request.method == "POST":
        form = OnboardingStep1PersonalForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.step_1_completed = True
            # Advance wizard step if candidate was on step 1
            if profile.current_step <= OnboardingProfile.STEP_1_PERSONAL:
                profile.current_step = OnboardingProfile.STEP_2_STATUTORY
            profile.save()

            # Sync emergency contact and address to Employee record if profile linked
            if hasattr(request.user, "employee_profile") and request.user.employee_profile:
                emp = request.user.employee_profile
                emp.date_of_birth = profile.date_of_birth
                emp.address = profile.current_address
                emp.emergency_contact_name = profile.emergency_contact_name
                emp.emergency_contact_phone = profile.emergency_contact_phone
                emp.save(update_fields=["date_of_birth", "address", "emergency_contact_name", "emergency_contact_phone"])

            messages.success(request, "Step 1: Personal profile & emergency contacts saved successfully.")
            # Move to Step 2 (or redirect to wizard step 2 route)
            return redirect("onboarding_wizard_step2")
    else:
        # Pre-fill address/phone from employee profile if blank
        initial = {}
        if hasattr(request.user, "employee_profile") and request.user.employee_profile:
            emp = request.user.employee_profile
            if not profile.current_address and emp.address:
                initial["current_address"] = emp.address
            if not profile.date_of_birth and emp.date_of_birth:
                initial["date_of_birth"] = emp.date_of_birth
            if not profile.emergency_contact_name and emp.emergency_contact_name:
                initial["emergency_contact_name"] = emp.emergency_contact_name
            if not profile.emergency_contact_phone and emp.emergency_contact_phone:
                initial["emergency_contact_phone"] = emp.emergency_contact_phone

        form = OnboardingStep1PersonalForm(instance=profile, initial=initial)

    return render(
        request,
        "onboarding/wizard_step1.html",
        {
            "profile": profile,
            "invitation": profile.invitation,
            "form": form,
            "step": 1,
            "total_steps": 4,
            "title": "Onboarding Wizard - Step 1: Personal & Emergency Details",
        }
    )


@login_required
def onboarding_wizard_step2(request):
    """
    Multi-Step Onboarding Wizard - Step 2: Statutory Compliance & Document Collection.
    Collects PAN, Aadhaar, and UAN with document proofs. Integrates directly with EmployeeStatutoryProfile.
    """
    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        messages.info(request, "No active onboarding checklist found for your account.")
        return redirect("dashboard")

    if request.method == "POST":
        form = OnboardingStep2StatutoryForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.step_2_completed = True
            if profile.current_step <= OnboardingProfile.STEP_2_STATUTORY:
                profile.current_step = OnboardingProfile.STEP_3_BANKING
            profile.save()

            # Integrate with EmployeeStatutoryProfile
            if hasattr(request.user, "employee_profile") and request.user.employee_profile:
                emp = request.user.employee_profile
                stat_profile, _ = EmployeeStatutoryProfile.objects.get_or_create(
                    employee=emp,
                    defaults={
                        "pan_number": profile.pan_number,
                        "uan_number": profile.uan_number,
                        "epf_enrolled": True,
                        "esi_enrolled": True,
                        "pt_enrolled": True,
                    }
                )
                stat_profile.pan_number = profile.pan_number
                stat_profile.uan_number = profile.uan_number
                stat_profile.save()

            messages.success(request, "Step 2: Statutory compliance & document proofs saved successfully.")
            return redirect("onboarding_wizard_step3")
    else:
        # Pre-fill from existing statutory profile if available
        initial = {}
        if hasattr(request.user, "employee_profile") and request.user.employee_profile:
            emp = request.user.employee_profile
            stat_profile = getattr(emp, "statutory_profile", None)
            if stat_profile:
                if not profile.pan_number and stat_profile.pan_number:
                    initial["pan_number"] = stat_profile.pan_number
                if not profile.uan_number and stat_profile.uan_number:
                    initial["uan_number"] = stat_profile.uan_number
                    initial["has_existing_uan"] = bool(stat_profile.uan_number)

        form = OnboardingStep2StatutoryForm(instance=profile, initial=initial)

    return render(
        request,
        "onboarding/wizard_step2.html",
        {
            "profile": profile,
            "invitation": profile.invitation,
            "form": form,
            "step": 2,
            "total_steps": 4,
            "title": "Onboarding Wizard - Step 2: Statutory Compliance",
        }
    )


@login_required
def onboarding_wizard_step3(request):
    """
    Multi-Step Onboarding Wizard - Step 3: Banking Details & Cheque Proof.
    Captures bank, account number, IFSC, and cheque proof. Directly populates EmployeeBankAccount.
    """
    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        messages.info(request, "No active onboarding checklist found for your account.")
        return redirect("dashboard")

    if request.method == "POST":
        form = OnboardingStep3BankingForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.step_3_completed = True
            if profile.current_step <= OnboardingProfile.STEP_3_BANKING:
                profile.current_step = OnboardingProfile.STEP_4_POLICIES
            profile.save()

            # Populate / Sync with EmployeeBankAccount
            if hasattr(request.user, "employee_profile") and request.user.employee_profile:
                emp = request.user.employee_profile
                bank_acc, _ = EmployeeBankAccount.objects.get_or_create(
                    employee=emp,
                    defaults={
                        "bank_name": profile.bank_name,
                        "branch_name": profile.branch_name,
                        "account_holder_name": profile.account_holder_name or emp.name,
                        "account_number": profile.account_number,
                        "ifsc_code": profile.ifsc_code,
                        "account_type": profile.account_type,
                        "payment_method": "NEFT",
                    }
                )
                bank_acc.bank_name = profile.bank_name
                bank_acc.branch_name = profile.branch_name
                bank_acc.account_holder_name = profile.account_holder_name or emp.name
                bank_acc.account_number = profile.account_number
                bank_acc.ifsc_code = profile.ifsc_code
                bank_acc.account_type = profile.account_type
                bank_acc.save()

            messages.success(request, "Step 3: Banking details & cancelled cheque recorded successfully.")
            return redirect("onboarding_wizard_step4")
    else:
        initial = {}
        if not profile.account_holder_name:
            initial["account_holder_name"] = profile.invitation.full_name
        if hasattr(request.user, "employee_profile") and request.user.employee_profile:
            emp = request.user.employee_profile
            bank_acc = getattr(emp, "bank_account", None)
            if bank_acc:
                if not profile.bank_name and bank_acc.bank_name:
                    initial["bank_name"] = bank_acc.bank_name
                if not profile.branch_name and bank_acc.branch_name:
                    initial["branch_name"] = bank_acc.branch_name
                if not profile.account_number and bank_acc.account_number:
                    initial["account_number"] = bank_acc.account_number
                if not profile.ifsc_code and bank_acc.ifsc_code:
                    initial["ifsc_code"] = bank_acc.ifsc_code

        form = OnboardingStep3BankingForm(instance=profile, initial=initial)

    return render(
        request,
        "onboarding/wizard_step3.html",
        {
            "profile": profile,
            "invitation": profile.invitation,
            "form": form,
            "step": 3,
            "total_steps": 4,
            "title": "Onboarding Wizard - Step 3: Banking Details",
        }
    )


def _get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


@login_required
def onboarding_wizard_step4(request):
    """
    Multi-Step Onboarding Wizard - Step 4: Company Policies Acknowledgment & Digital Sign-Off.
    Presents Code of Conduct, Information Security, and NDA with digital signature and IP capture.
    """
    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        messages.info(request, "No active onboarding checklist found for your account.")
        return redirect("dashboard")

    if request.method == "POST":
        form = OnboardingStep4PolicyForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.step_4_completed = True
            profile.policy_accepted_at = timezone.now()
            profile.policy_signer_ip = _get_client_ip(request)
            if profile.current_step <= OnboardingProfile.STEP_4_POLICIES:
                profile.current_step = OnboardingProfile.STEP_5_REVIEW
            profile.save()

            messages.success(request, "Step 4: Company policies acknowledged and electronically signed successfully.")
            return redirect("onboarding_wizard_step5")
    else:
        initial = {}
        if not profile.policy_signed_full_name:
            initial["policy_signed_full_name"] = profile.invitation.full_name

        form = OnboardingStep4PolicyForm(instance=profile, initial=initial)

    return render(
        request,
        "onboarding/wizard_step4.html",
        {
            "profile": profile,
            "invitation": profile.invitation,
            "form": form,
            "step": 4,
            "total_steps": 4,
            "title": "Onboarding Wizard - Step 4: Company Policies",
        }
    )


@login_required
def onboarding_wizard_step5(request):
    """
    Onboarding Wizard - Step 5: Candidate Final Review & Summary before submission to HR.
    """
    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        messages.info(request, "No active onboarding checklist found for your account.")
        return redirect("dashboard")

    return render(
        request,
        "onboarding/wizard_summary.html",
        {
            "profile": profile,
            "invitation": profile.invitation,
            "title": "Onboarding Review & Submission",
        }
    )


@login_required
def onboarding_wizard_submit(request):
    """
    Candidate action to formally submit onboarding package into the HR review queue.
    """
    if request.method != "POST":
        return redirect("onboarding_wizard_step5")

    profile = _get_candidate_onboarding_profile(request.user)
    if not profile:
        return redirect("dashboard")

    if not (profile.step_1_completed and profile.step_2_completed and profile.step_3_completed and profile.step_4_completed):
        messages.error(request, "Please complete all onboarding steps before final submission.")
        return redirect("onboarding_wizard_step5")

    profile.status = OnboardingProfile.STATUS_SUBMITTED
    profile.submitted_at = timezone.now()
    profile.save(update_fields=["status", "submitted_at"])

    messages.success(request, "Your onboarding package has been submitted to HR for compliance verification.")
    return redirect("onboarding_wizard_step5")


@login_required
def onboarding_review_queue(request):
    """
    HR compliance review queue listing candidate onboarding packages awaiting verification.
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to access the onboarding review queue.")

    base_qs = scope_queryset(
        OnboardingProfile.objects.select_related(
            "invitation", "invitation__department", "invitation__position", "user", "organization"
        ),
        request.user
    )

    pending_count = base_qs.filter(status=OnboardingProfile.STATUS_SUBMITTED).count()
    verified_count = base_qs.filter(status=OnboardingProfile.STATUS_VERIFIED).count()
    draft_count = base_qs.filter(status=OnboardingProfile.STATUS_DRAFT).count()

    status_filter = request.GET.get("status", "SUBMITTED")
    queryset = base_qs
    if status_filter != "all" and status_filter in dict(OnboardingProfile.ONBOARDING_STATUS_CHOICES):
        queryset = queryset.filter(status=status_filter)

    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        queryset = queryset.filter(
            Q(invitation__first_name__icontains=q)
            | Q(invitation__last_name__icontains=q)
            | Q(invitation__email__icontains=q)
        )

    paginator = Paginator(queryset, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "onboarding/review_queue.html",
        {
            "page_obj": page_obj,
            "pending_count": pending_count,
            "verified_count": verified_count,
            "draft_count": draft_count,
            "current_status": status_filter,
            "title": "Onboarding Review Queue",
        }
    )


@login_required
def onboarding_review_detail(request, pk):
    """
    HR candidate detail verification view. Inspects PAN, Aadhaar, Cheque, and policy signatures.
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to review candidate onboarding packages.")

    profile = get_object_or_404(
        scope_queryset(
            OnboardingProfile.objects.select_related(
                "invitation", "invitation__department", "invitation__position", "user", "organization", "verified_by"
            ),
            request.user
        ),
        pk=pk
    )

    return render(
        request,
        "onboarding/review_detail.html",
        {
            "profile": profile,
            "title": f"Review Onboarding: {profile.invitation.full_name}",
        }
    )


@login_required
def onboarding_review_action(request, pk):
    """
    HR compliance decision: 'approve' (activates employee and unlocks full system) or 'reject' (requests changes).
    """
    if not _can_manage_onboarding(request.user):
        raise PermissionDenied("You do not have permission to decide onboarding reviews.")

    if request.method != "POST":
        return redirect("onboarding_review_queue")

    profile = get_object_or_404(
        scope_queryset(OnboardingProfile.objects.select_related("invitation", "user", "organization"), request.user),
        pk=pk
    )

    action = request.POST.get("action")
    notes = request.POST.get("notes", "").strip()

    if action == "approve":
        with transaction.atomic():
            profile.status = OnboardingProfile.STATUS_VERIFIED
            profile.verified_at = timezone.now()
            profile.verified_by = request.user
            profile.verification_notes = notes
            profile.save(update_fields=["status", "verified_at", "verified_by", "verification_notes"])

            # Activate employee record
            emp = getattr(profile.user, "employee_profile", None)
            if emp:
                emp.employment_status = "ACTIVE"
                emp.is_active = True
                emp.address = profile.current_address
                emp.emergency_contact_name = profile.emergency_contact_name
                emp.emergency_contact_phone = profile.emergency_contact_phone
                emp.save(update_fields=["employment_status", "is_active", "address", "emergency_contact_name", "emergency_contact_phone"])

            # Also ensure user login is active
            if not profile.user.is_active:
                profile.user.is_active = True
                profile.user.save(update_fields=["is_active"])

            # Ensure checklist & provisioning tasks are instantiated
            instantiate_onboarding_workflow(profile.invitation, employee=emp)

            messages.success(
                request,
                f"Candidate {profile.invitation.full_name} has been verified and fully activated as an active Employee."
            )
            return redirect("onboarding_review_detail", pk=profile.pk)

    elif action == "reject":
        profile.status = OnboardingProfile.STATUS_REJECTED
        profile.verification_notes = notes
        profile.save(update_fields=["status", "verification_notes"])

        messages.warning(
            request,
            f"Onboarding package for {profile.invitation.full_name} was returned with change requests."
        )
        return redirect("onboarding_review_detail", pk=profile.pk)

    else:
        messages.error(request, "Invalid review action.")
        return redirect("onboarding_review_detail", pk=profile.pk)


@login_required
def onboarding_tracker(request):
    """
    Departmental Task Tracker Dashboard & Progress UI.
    Provides cross-department visibility into candidate provisioning tasks with progress bars and one-click actions.
    """
    if not _can_access_onboarding_tracker(request.user):
        raise PermissionDenied("You do not have permission to access the onboarding task tracker.")

    current_role = request.GET.get("role", "all").strip().upper()
    current_status = request.GET.get("status", "all").strip().upper()
    search_query = request.GET.get("q", "").strip()

    checklists_qs = scope_queryset(
        OnboardingChecklist.objects.select_related(
            "invitation",
            "invitation__department",
            "invitation__position",
            "employee",
            "organization",
        ).prefetch_related("tasks", "tasks__completed_by"),
        request.user
    )

    if search_query:
        checklists_qs = checklists_qs.filter(
            models.Q(invitation__first_name__icontains=search_query)
            | models.Q(invitation__last_name__icontains=search_query)
            | models.Q(invitation__email__icontains=search_query)
            | models.Q(title__icontains=search_query)
            | models.Q(invitation__position__title__icontains=search_query)
            | models.Q(invitation__department__name__icontains=search_query)
        )

    if current_status in dict(OnboardingChecklist.STATUS_CHOICES):
        checklists_qs = checklists_qs.filter(status=current_status)

    # Scoped checklists for global counters
    all_scoped_checklists = list(scope_queryset(OnboardingChecklist.objects.all(), request.user))
    total_checklists = len(all_scoped_checklists)
    in_progress_checklists = sum(1 for c in all_scoped_checklists if c.status == OnboardingChecklist.STATUS_IN_PROGRESS)
    completed_checklists = sum(1 for c in all_scoped_checklists if c.status == OnboardingChecklist.STATUS_COMPLETED)
    pending_checklists = sum(1 for c in all_scoped_checklists if c.status == OnboardingChecklist.STATUS_PENDING)

    today = timezone.localdate()

    checklist_items = []
    total_tasks_count = 0
    completed_tasks_count = 0
    pending_tasks_count = 0

    valid_roles = [OnboardingTask.ROLE_IT, OnboardingTask.ROLE_HR, OnboardingTask.ROLE_FACILITIES, OnboardingTask.ROLE_MANAGER]

    for chk in checklists_qs:
        all_tasks = list(chk.tasks.all())
        if current_role in valid_roles:
            filtered_tasks = [t for t in all_tasks if t.department_role == current_role]
        else:
            filtered_tasks = all_tasks

        for t in filtered_tasks:
            t.is_overdue = bool(t.due_date and t.due_date < today and t.status != OnboardingTask.STATUS_COMPLETED)
            total_tasks_count += 1
            if t.status == OnboardingTask.STATUS_COMPLETED:
                completed_tasks_count += 1
            else:
                pending_tasks_count += 1

        tot = len(filtered_tasks)
        comp = sum(1 for t in filtered_tasks if t.status == OnboardingTask.STATUS_COMPLETED)
        prog = int((comp / tot) * 100) if tot > 0 else 0

        checklist_items.append({
            "checklist": chk,
            "tasks": filtered_tasks,
            "total_tasks": tot,
            "completed_tasks": comp,
            "progress_percentage": prog,
            "overall_progress": chk.progress_percentage,
        })

    paginator = Paginator(checklist_items, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    roles_list = [
        ("ALL", "All Departments"),
        (OnboardingTask.ROLE_IT, "IT / Tech Provisioning"),
        (OnboardingTask.ROLE_HR, "Human Resources"),
        (OnboardingTask.ROLE_FACILITIES, "Admin / Facilities"),
        (OnboardingTask.ROLE_MANAGER, "Hiring Manager"),
    ]

    return render(
        request,
        "onboarding/tracker.html",
        {
            "page_obj": page_obj,
            "total_checklists": total_checklists,
            "in_progress_checklists": in_progress_checklists,
            "completed_checklists": completed_checklists,
            "pending_checklists": pending_checklists,
            "total_tasks_count": total_tasks_count,
            "completed_tasks_count": completed_tasks_count,
            "pending_tasks_count": pending_tasks_count,
            "current_role": current_role,
            "current_status": current_status,
            "search_query": search_query,
            "roles_list": roles_list,
            "today": today,
            "n": "onboarding_tracker",
            "title": "Departmental Onboarding Tracker",
        }
    )


@login_required
def onboarding_task_action(request, pk):
    """
    Handle one-click task status toggling and blocker notes updates.
    """
    if not _can_access_onboarding_tracker(request.user):
        raise PermissionDenied("You do not have permission to manage onboarding tasks.")

    if request.method != "POST":
        return redirect("onboarding_tracker")

    task = get_object_or_404(
        OnboardingTask.objects.select_related("checklist", "checklist__organization"),
        pk=pk
    )

    # Tenancy check
    scoped_checklists = scope_queryset(OnboardingChecklist.objects.all(), request.user)
    if not scoped_checklists.filter(pk=task.checklist_id).exists():
        raise PermissionDenied("You do not have access to this onboarding task.")

    action = request.POST.get("action", "").strip()
    notes = request.POST.get("notes", "").strip()

    if action == "complete":
        task.mark_completed(user=request.user, notes=notes if notes else task.notes)
        messages.success(request, f"Task '{task.title}' marked as completed.")
    elif action == "in_progress":
        task.status = OnboardingTask.STATUS_IN_PROGRESS
        if notes:
            task.notes = notes
        task.save(update_fields=["status", "notes", "updated_at"])
        task.checklist.update_overall_status()
        messages.info(request, f"Task '{task.title}' moved to In Progress.")
    elif action == "reopen":
        task.status = OnboardingTask.STATUS_PENDING
        task.completed_at = None
        task.completed_by = None
        if notes:
            task.notes = notes
        task.save(update_fields=["status", "completed_at", "completed_by", "notes", "updated_at"])
        task.checklist.update_overall_status()
        messages.warning(request, f"Task '{task.title}' reopened as Pending.")
    elif action == "update_notes":
        task.notes = notes
        task.save(update_fields=["notes", "updated_at"])
        messages.success(request, f"Fulfillment notes updated for '{task.title}'.")
    else:
        messages.error(request, "Invalid task action.")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("onboarding_tracker")
    return redirect(next_url)








