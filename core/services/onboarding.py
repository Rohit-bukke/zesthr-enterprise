"""
Onboarding & Provisioning Service Module.
Handles automated provisioning task template generation across IT, HR, Facilities, and Manager roles.
"""
from datetime import timedelta
from typing import Dict, List, Optional
from django.db import transaction
from django.utils import timezone

from core.models_onboarding import CandidateInvitation, OnboardingChecklist, OnboardingTask


# Configurable department-wise default provisioning task templates
DEFAULT_DEPARTMENT_TEMPLATES: Dict[str, List[Dict]] = {
    OnboardingTask.ROLE_IT: [
        {
            "title": "Laptop & Workstation Configuration",
            "description": "Procure and configure employee development laptop, OS, security software, and device encryption.",
            "days_due": 1,
        },
        {
            "title": "Google Workspace & Corporate Email Setup",
            "description": "Create primary corporate email address, alias, and Google Workspace account.",
            "days_due": 1,
        },
        {
            "title": "Slack & GitHub Team Invitations",
            "description": "Send invitations to company Slack workspace and GitHub organization repositories.",
            "days_due": 2,
        },
    ],
    OnboardingTask.ROLE_HR: [
        {
            "title": "Employee ID Card Generation",
            "description": "Generate employee identity badge with photo and employee ID number.",
            "days_due": 2,
        },
        {
            "title": "Biometric & Attendance Enrollment",
            "description": "Register employee biometric credentials in attendance tracking and door access system.",
            "days_due": 2,
        },
        {
            "title": "PF & Benefits Enrollment",
            "description": "Enroll employee in corporate health insurance policy, provident fund, and statutory benefits.",
            "days_due": 5,
        },
    ],
    OnboardingTask.ROLE_FACILITIES: [
        {
            "title": "Access Card Issuance",
            "description": "Issue physical RFID building and floor access card / badge.",
            "days_due": 1,
        },
        {
            "title": "Workstation & Desk Assignment",
            "description": "Assign dedicated desk space, chair, pedestal, and office supplies.",
            "days_due": 1,
        },
    ],
    OnboardingTask.ROLE_MANAGER: [
        {
            "title": "Assign Onboarding Buddy",
            "description": "Pair the new hire with a senior peer buddy for day-to-day guidance and cultural integration.",
            "days_due": 1,
        },
        {
            "title": "Day 1 Welcome & Kickoff Meeting",
            "description": "Conduct 1-on-1 welcome session, introduce team members, and review 30-day expectations.",
            "days_due": 1,
        },
    ],
}


def get_default_templates(department_role: Optional[str] = None) -> List[Dict]:
    """
    Returns default task template definitions, optionally filtered by department role.
    """
    if department_role:
        return [
            {**tpl, "department_role": department_role}
            for tpl in DEFAULT_DEPARTMENT_TEMPLATES.get(department_role, [])
        ]

    templates = []
    for dept_role, tpl_list in DEFAULT_DEPARTMENT_TEMPLATES.items():
        for tpl in tpl_list:
            templates.append({**tpl, "department_role": dept_role})
    return templates


def create_or_get_onboarding_checklist(
    invitation: CandidateInvitation,
    employee=None,
    target_date=None,
) -> OnboardingChecklist:
    """
    Retrieves or initializes the OnboardingChecklist for a candidate invitation.
    """
    emp = employee or invitation.employee_profile
    base_date = target_date or invitation.proposed_joining_date

    checklist, created = OnboardingChecklist.objects.get_or_create(
        invitation=invitation,
        defaults={
            "organization": invitation.organization,
            "employee": emp,
            "title": f"Onboarding Checklist: {invitation.full_name}",
            "target_date": base_date,
            "status": OnboardingChecklist.STATUS_PENDING,
        }
    )

    if not created and emp and not checklist.employee:
        checklist.employee = emp
        checklist.save(update_fields=["employee"])

    return checklist


def generate_provisioning_tasks(
    checklist: OnboardingChecklist,
    custom_templates: Optional[List[Dict]] = None,
    base_date=None,
) -> List[OnboardingTask]:
    """
    Instantiates departmental provisioning tasks for an onboarding checklist.
    Prevents duplicates by matching checklist, department_role, and title.
    """
    templates = custom_templates if custom_templates is not None else get_default_templates()
    effective_base_date = (
        base_date
        or checklist.target_date
        or (checklist.invitation.proposed_joining_date if checklist.invitation else None)
        or timezone.now().date()
    )

    existing_task_keys = set(
        checklist.tasks.values_list("department_role", "title")
    )

    new_tasks = []
    with transaction.atomic():
        for tpl in templates:
            dept_role = tpl.get("department_role", OnboardingTask.ROLE_HR)
            title = tpl["title"]

            if (dept_role, title) in existing_task_keys:
                continue

            days_due = tpl.get("days_due", 1)
            due_date = effective_base_date + timedelta(days=days_due)

            task = OnboardingTask(
                checklist=checklist,
                department_role=dept_role,
                title=title,
                description=tpl.get("description", ""),
                due_date=due_date,
                status=OnboardingTask.STATUS_PENDING,
            )
            new_tasks.append(task)

        if new_tasks:
            OnboardingTask.objects.bulk_create(new_tasks)
            checklist.update_overall_status()

    return new_tasks


def instantiate_onboarding_workflow(
    invitation: CandidateInvitation,
    employee=None,
    custom_templates: Optional[List[Dict]] = None,
) -> tuple[OnboardingChecklist, List[OnboardingTask]]:
    """
    High-level entry point: Initializes checklist and generates all default departmental tasks.
    """
    with transaction.atomic():
        checklist = create_or_get_onboarding_checklist(invitation, employee=employee)
        created_tasks = generate_provisioning_tasks(checklist, custom_templates=custom_templates)
        return checklist, created_tasks
