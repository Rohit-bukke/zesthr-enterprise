from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.organizations.tenancy import scope_queryset
from core import roles
from core.forms_onboarding import CandidateInvitationForm
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


def candidate_landing_stub(request, token):
    """
    Candidate landing stub route for token link preview (full wizard arrives in Phase 3).
    """
    invitation = get_object_or_404(CandidateInvitation, token=token)
    return render(
        request,
        "onboarding/invitation_create.html",
        {
            "invitation": invitation,
            "title": f"Welcome {invitation.first_name}",
        }
    )

