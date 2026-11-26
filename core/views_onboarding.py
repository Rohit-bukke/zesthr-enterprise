from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import OrganizationMembership
from apps.organizations.tenancy import scope_queryset
from core import roles
from core.forms_onboarding import CandidateInvitationForm, CandidateRegistrationForm
from core.models import Employee
from core.models_onboarding import CandidateInvitation


def _can_manage_onboarding(user):
    return (
        user.is_superuser
        or user.has_perm(roles.ADD_EMPLOYEE)
        or user.has_perm(roles.CHANGE_EMPLOYEE)
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

                # 5. Log the user in
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")

                messages.success(
                    request,
                    f"Welcome to {invitation.organization.name}! Your account has been created."
                )

                # Redirect to dashboard or onboarding wizard entry
                return redirect("dashboard")
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


