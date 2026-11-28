from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from core import roles
from core.models import Department, Employee, JobPosition
from core.models_offboarding import ExitRequest
from core.services.offboarding import review_exit_request, submit_resignation

User = get_user_model()


class OffboardingPhase12ResignationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Wayne Enterprises", code="WAYNE", is_active=True)
        self.dept = Department.objects.create(name="Applied Sciences", code="WAYNE_SCI", organization=self.org)
        self.pos = JobPosition.objects.create(title="Defense Analyst", department=self.dept, organization=self.org)

        # HR Admin
        self.hr_user = User.objects.create_user(username="lucius_fox", password="password123", is_superuser=True)
        OrganizationMembership.objects.create(organization=self.org, user=self.hr_user, is_active=True)

        # Manager
        self.manager_user = User.objects.create_user(username="alfred_p", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.manager_user, is_active=True)
        self.manager_emp = Employee.objects.create(
            user=self.manager_user,
            name="Alfred Pennyworth",
            email="alfred@example.com",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            basic_salary=Decimal("80000.00"),
            joined_on=timezone.localdate() - timedelta(days=365),
            employment_status="ACTIVE",
        )

        # Employee (Resigning)
        self.emp_user = User.objects.create_user(username="dick_grayson", password="password123")
        OrganizationMembership.objects.create(organization=self.org, user=self.emp_user, is_active=True)
        self.employee = Employee.objects.create(
            user=self.emp_user,
            name="Dick Grayson",
            email="dick@example.com",
            department=self.dept,
            position=self.pos,
            organization=self.org,
            manager=self.manager_emp,
            basic_salary=Decimal("50000.00"),
            joined_on=timezone.localdate() - timedelta(days=200),
            employment_status="ACTIVE",
        )

    def test_model_notice_period_and_shortfall_calculation(self):
        """Test ExitRequest standard LWD and shortfall calculations."""
        today = timezone.localdate()

        # Case 1: Standard 30 days notice served in full
        req1 = ExitRequest(
            organization=self.org,
            employee=self.employee,
            resignation_date=today,
            requested_lwd=today + timedelta(days=30),
            notice_period_days=30,
        )
        req1.save()
        self.assertEqual(req1.standard_lwd, today + timedelta(days=30))
        self.assertEqual(req1.notice_shortfall_days, 0)

        # Case 2: Early release requested (10 days shortfall)
        req1.requested_lwd = today + timedelta(days=20)
        req1.save()
        self.assertEqual(req1.notice_shortfall_days, 10)

        # Case 3: Official LWD agreed to 25 days (5 days shortfall)
        req1.official_lwd = today + timedelta(days=25)
        req1.save()
        self.assertEqual(req1.notice_shortfall_days, 5)

    def test_model_validation_requested_lwd_before_resignation(self):
        """Requested LWD cannot be before resignation submission date."""
        today = timezone.localdate()
        req = ExitRequest(
            organization=self.org,
            employee=self.employee,
            resignation_date=today,
            requested_lwd=today - timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            req.clean()

    def test_service_submit_resignation(self):
        """Test submitting resignation through service layer."""
        today = timezone.localdate()
        exit_req = submit_resignation(
            employee=self.employee,
            requested_lwd=today + timedelta(days=30),
            reason_category=ExitRequest.REASON_CAREER_GROWTH,
            reason_detail="Joining Blüdhaven Police Dept",
            notice_period_days=30,
        )
        self.assertIsNotNone(exit_req.pk)
        self.assertEqual(exit_req.status, ExitRequest.STATUS_SUBMITTED)
        self.assertEqual(exit_req.notice_shortfall_days, 0)

        # Submitting duplicate while active raises error
        with self.assertRaises(ValidationError):
            submit_resignation(
                employee=self.employee,
                requested_lwd=today + timedelta(days=30),
                reason_category=ExitRequest.REASON_OTHER,
            )

    def test_service_review_approval_and_shortfall_waiver(self):
        """Test managerial approval, date adjustment, and notice shortfall waiver."""
        today = timezone.localdate()
        exit_req = submit_resignation(
            employee=self.employee,
            requested_lwd=today + timedelta(days=15),  # 15 days short of 30
            reason_category=ExitRequest.REASON_CAREER_GROWTH,
            notice_period_days=30,
        )
        self.assertEqual(exit_req.notice_shortfall_days, 15)

        # Manager approves with waiver
        approved_req = review_exit_request(
            exit_request_id=exit_req.pk,
            reviewer=self.manager_user,
            action="approve",
            official_lwd=today + timedelta(days=15),
            waive_shortfall=True,
            review_notes="Approved early release without penalty.",
        )
        self.assertEqual(approved_req.status, ExitRequest.STATUS_APPROVED)
        self.assertEqual(approved_req.official_lwd, today + timedelta(days=15))
        self.assertEqual(approved_req.notice_shortfall_days, 0)
        self.assertEqual(approved_req.reviewed_by, self.manager_user)

        # Verify employee separation date recorded
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.separation_date, today + timedelta(days=15))

    def test_service_review_rejection(self):
        """Test managerial rejection of an exit request."""
        today = timezone.localdate()
        exit_req = submit_resignation(
            employee=self.employee,
            requested_lwd=today + timedelta(days=30),
            reason_category=ExitRequest.REASON_BETTER_COMPENSATION,
        )

        rejected_req = review_exit_request(
            exit_request_id=exit_req.pk,
            reviewer=self.hr_user,
            action="reject",
            review_notes="Retention counter-offer accepted.",
        )
        self.assertEqual(rejected_req.status, ExitRequest.STATUS_REJECTED)
        self.assertEqual(rejected_req.review_notes, "Retention counter-offer accepted.")

    def test_view_employee_resignation_submission_flow(self):
        """Employee submits resignation form via web interface."""
        self.client.force_login(self.emp_user)
        today = timezone.localdate()
        lwd_str = (today + timedelta(days=30)).strftime("%Y-%m-%d")

        response = self.client.post(reverse("offboarding_resignation_submit"), {
            "requested_lwd": lwd_str,
            "reason_category": ExitRequest.REASON_HIGHER_EDUCATION,
            "reason_detail": "Enrolled in Gotham University Masters Program.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("offboarding_resignation_status"))

        # Verify exit request was created
        exit_req = ExitRequest.objects.get(employee=self.employee)
        self.assertEqual(exit_req.status, ExitRequest.STATUS_SUBMITTED)
        self.assertEqual(exit_req.reason_category, ExitRequest.REASON_HIGHER_EDUCATION)

        # Accessing submit again redirects to status
        resp2 = self.client.get(reverse("offboarding_resignation_submit"))
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.url, reverse("offboarding_resignation_status"))

    def test_view_exit_requests_list_and_permissions(self):
        """HR/Admin can view all requests, unauthorized employee gets 403."""
        today = timezone.localdate()
        submit_resignation(
            employee=self.employee,
            requested_lwd=today + timedelta(days=30),
            reason_category=ExitRequest.REASON_CAREER_GROWTH,
        )

        # Admin access
        self.client.force_login(self.hr_user)
        resp_admin = self.client.get(reverse("offboarding_requests_list"))
        self.assertEqual(resp_admin.status_code, 200)
        self.assertContains(resp_admin, "Dick Grayson")

        # Unauthorized employee access
        self.client.force_login(self.emp_user)
        resp_emp = self.client.get(reverse("offboarding_requests_list"))
        self.assertEqual(resp_emp.status_code, 403)

    def test_view_exit_request_review_action(self):
        """Manager reviews and approves exit request via POST action."""
        today = timezone.localdate()
        exit_req = submit_resignation(
            employee=self.employee,
            requested_lwd=today + timedelta(days=30),
            reason_category=ExitRequest.REASON_CAREER_GROWTH,
        )

        self.client.force_login(self.hr_user)
        review_url = reverse("offboarding_request_review", kwargs={"pk": exit_req.pk})
        post_data = {
            "action": "approve",
            "official_lwd": (today + timedelta(days=30)).strftime("%Y-%m-%d"),
            "review_notes": "All transition items planned.",
        }
        resp = self.client.post(review_url, post_data)
        self.assertEqual(resp.status_code, 302)

        exit_req.refresh_from_db()
        self.assertEqual(exit_req.status, ExitRequest.STATUS_APPROVED)
        self.assertEqual(exit_req.reviewed_by, self.hr_user)
