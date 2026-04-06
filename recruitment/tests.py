import importlib
import io
import json
import re
import zipfile
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CompletionRequirement,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    NotificationLog,
    Position,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
    WorkflowOverride,
)
from .requirements import get_required_applicant_document_requirements
from .services import (
    build_export_bundle,
    build_submission_packet,
    generate_comparative_assessment_report,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    process_workflow_action,
    record_final_decision,
    record_system_audit_event,
    save_deliberation_record,
    save_exam_record,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    upload_interview_fallback_rating,
    upload_evidence_item,
    user_can_view_application,
    verify_application_otp,
)


User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseRecruitmentTestCase(TestCase):
    def setUp(self):
        self.applicant = User.objects.create_user(
            username="applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        self.secretariat = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.hrm_chief = User.objects.create_user(
            username="hrmchief",
            password="testpass123",
            role=RecruitmentUser.Role.HRM_CHIEF,
        )
        self.hrmpsb = User.objects.create_user(
            username="hrmpsb",
            password="testpass123",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        self.appointing = User.objects.create_user(
            username="appointing",
            password="testpass123",
            role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
        )
        self.sysadmin = User.objects.create_user(
            username="sysadmin",
            password="testpass123",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        self.admin_aide_position = Position.objects.create(
            position_code="POS-001",
            title="Administrative Aide VI",
            unit="HR Unit",
            description="Administrative support role.",
            requirements="Standard requirements.",
        )
        self.medical_officer_position = Position.objects.create(
            position_code="POS-002",
            title="Medical Officer V",
            unit="Regional Office",
            description="Clinical leadership role.",
            requirements="Leadership requirements.",
        )
        self.project_assistant_position = Position.objects.create(
            position_code="POS-003",
            title="Project Technical Assistant",
            unit="Special Projects",
            description="Project support role.",
            requirements="Flexible requirements.",
        )
        self.level1_position = PositionPosting.objects.create(
            position_reference=self.admin_aide_position,
            job_code="PL-001",
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        self.level2_position = PositionPosting.objects.create(
            position_reference=self.medical_officer_position,
            job_code="PL-002",
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_2,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        self.cos_position = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            job_code="COS-001",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
        )

    def level1_closing_date(self):
        return timezone.localdate() + timedelta(days=15)

    def upload_required_applicant_documents(self, application, actor, *, content_prefix="sample"):
        uploaded_evidence = []
        for requirement in get_required_applicant_document_requirements():
            uploaded_evidence.append(
                upload_evidence_item(
                    application=application,
                    actor=actor,
                    label=requirement.title,
                    uploaded_file=SimpleUploadedFile(
                        f"{requirement.code}.txt",
                        f"{content_prefix}:{requirement.code}".encode("utf-8"),
                        content_type="text/plain",
                    ),
                    document_key=requirement.code,
                    artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                    artifact_type="applicant_document",
                )
            )
        return uploaded_evidence

    def make_application(self, position):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=position,
            applicant_first_name="Test",
            applicant_last_name="Applicant",
            applicant_email="applicant@example.com",
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
        )
        self.upload_required_applicant_documents(application, self.applicant)
        return application

    def verify_application_for_submission(self, application):
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        return otp_code

    def finalize_screening_for_current_stage(
        self,
        application,
        actor,
        completeness_status=ScreeningRecord.CompletenessStatus.COMPLETE,
        qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
        completeness_notes="All required screening documents were reviewed.",
        screening_notes="Qualification screening completed.",
    ):
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
                "completeness_status": completeness_status,
                "completeness_notes": completeness_notes,
                "qualification_outcome": qualification_outcome,
                "screening_notes": screening_notes,
            },
            finalize=True,
        )

    def finalize_exam_for_current_stage(
        self,
        application,
        actor,
        exam_type="Technical Examination",
        exam_status=ExamRecord.ExamStatus.COMPLETED,
        exam_score="88.50",
        exam_result="Passed",
        valid_from=None,
        valid_until=None,
        exam_notes="Formal examination output recorded.",
    ):
        return save_exam_record(
            application=application,
            actor=actor,
            cleaned_data={
                "exam_type": exam_type,
                "exam_status": exam_status,
                "exam_score": exam_score,
                "exam_result": exam_result,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "exam_notes": exam_notes,
            },
            finalize=True,
        )

    def move_application_to_hrm_chief_review(self, application):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        if application.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(application, self.secretariat)
            process_workflow_action(application, self.secretariat, "endorse", "Forward to HRM Chief.")
            application.refresh_from_db()
        return application

    def move_application_to_hrmpsb_review(self, application):
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "Forward to HRMPSB.")
        application.refresh_from_db()
        return application

    def move_application_to_appointing_review(self, application):
        if application.branch == PositionPosting.Branch.PLANTILLA:
            self.move_application_to_hrmpsb_review(application)
            self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
            self.finalize_car_for_current_stage(application, self.hrmpsb)
            process_workflow_action(application, self.hrmpsb, "recommend", "Forward to Appointing Authority.")
        else:
            self.move_application_to_hrm_chief_review(application)
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
            self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
            process_workflow_action(application, self.hrm_chief, "endorse", "Forward to Appointing Authority.")
        application.refresh_from_db()
        return application

    def finalize_deliberation_for_current_stage(
        self,
        application,
        actor,
        ranking_position=None,
        deliberated_at=None,
        deliberation_minutes="Recorded structured deliberation minutes.",
        decision_support_summary="Decision-support summary preserved for routing.",
        ranking_notes="Ranking basis recorded for decision support.",
    ):
        return save_deliberation_record(
            application=application,
            actor=actor,
            cleaned_data={
                "deliberated_at": deliberated_at or timezone.now(),
                "deliberation_minutes": deliberation_minutes,
                "decision_support_summary": decision_support_summary,
                "ranking_position": ranking_position,
                "ranking_notes": ranking_notes,
            },
            finalize=True,
        )

    def finalize_car_for_current_stage(
        self,
        application,
        actor,
        summary_notes="Comparative ranking sheet generated for Plantilla decision support.",
    ):
        return generate_comparative_assessment_report(
            application=application,
            actor=actor,
            cleaned_data={"summary_notes": summary_notes},
            finalize=True,
        )

    def record_final_decision_for_current_stage(
        self,
        application,
        actor,
        decision_outcome=FinalDecision.Outcome.SELECTED,
        decision_notes="Final decision recorded after packet review.",
    ):
        return record_final_decision(
            application=application,
            actor=actor,
            cleaned_data={
                "decision_outcome": decision_outcome,
                "decision_notes": decision_notes,
            },
        )

    def extract_otp_from_last_email(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def make_selected_application(self, position):
        application = self.make_application(position)
        self.move_application_to_appointing_review(application)
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved for completion tracking.",
            )
        application.refresh_from_db()
        return application


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FoundationSmokeTests(TestCase):
    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal Login")
        self.assertTrue(reverse("login").startswith("/internal/"))

    def test_dashboard_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_root_redirects_to_public_applicant_portal(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("applicant-portal"))

    def test_public_status_lookup_page_loads_without_login(self):
        response = self.client.get(reverse("applicant-status-lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(reverse("applicant-status-lookup").startswith("/apply/"))

    def test_workflow_queue_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("workflow-queue"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_internal_user_can_log_in(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("dashboard"))
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )

    def test_applicant_cannot_use_internal_login(self):
        User.objects.create_user(
            username="applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "applicant", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "restricted to internal users")

    def test_inactive_internal_user_cannot_log_in(self):
        User.objects.create_user(
            username="inactive-chief",
            password="testpass123",
            role=RecruitmentUser.Role.HRM_CHIEF,
            is_active=False,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "inactive-chief", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")


class IdentityAdministrationTests(BaseRecruitmentTestCase):
    def test_system_admin_can_create_internal_account(self):
        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("internal-user-create"),
            {
                "username": "chief-two",
                "first_name": "Chief",
                "last_name": "Two",
                "email": "chief.two@example.com",
                "employee_id": "EMP-002",
                "office_name": "HR Office",
                "role": RecruitmentUser.Role.HRM_CHIEF,
                "is_active": "on",
                "password1": "VeryStrongPass123",
                "password2": "VeryStrongPass123",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(username="chief-two")
        self.assertEqual(created_user.role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                metadata__target_username="chief-two",
            ).exists()
        )

    def test_non_admin_cannot_access_internal_user_directory(self):
        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("internal-user-list"))
        self.assertEqual(response.status_code, 403)

    def test_system_admin_can_update_role_and_activation_with_audit(self):
        managed_user = User.objects.create_user(
            username="member-one",
            password="testpass123",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
            email="member.one@example.com",
        )
        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("internal-user-update", kwargs={"pk": managed_user.pk}),
            {
                "username": "member-one",
                "first_name": "Member",
                "last_name": "One",
                "email": "member.one@example.com",
                "employee_id": "EMP-019",
                "office_name": "Board Office",
                "role": RecruitmentUser.Role.APPOINTING_AUTHORITY,
                "is_active": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        managed_user.refresh_from_db()
        self.assertEqual(managed_user.role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertFalse(managed_user.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ROLE_CHANGED,
                metadata__target_username="member-one",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                metadata__target_username="member-one",
            ).exists()
        )

    def test_system_admin_cannot_view_case_content_by_default(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_system_admin_role_does_not_inherit_django_admin_access(self):
        self.sysadmin.refresh_from_db()
        self.assertFalse(self.sysadmin.is_staff)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)


class RecruitmentEntryManagementTests(BaseRecruitmentTestCase):
    def test_entry_manager_can_create_position_catalog_record(self):
        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("position-catalog-create"),
            {
                "position_code": "POS-010",
                "title": "Nurse II",
                "unit": "Clinical Services",
                "description": "Clinical nursing support role.",
                "requirements": "PRC license required.",
                "qualification_reference": "Plantilla nursing pool.",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Position.objects.filter(position_code="POS-010").exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.POSITION_CREATED,
                metadata__position_code="POS-010",
            ).exists()
        )

    def test_plantilla_entry_requires_fixed_period_and_closing_date(self):
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            job_code="PL-003",
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_cos_pooling_entry_cannot_set_closing_date(self):
        entry = PositionPosting(
            position_reference=self.project_assistant_position,
            job_code="COS-002",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_entry_manager_can_create_recruitment_entry(self):
        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "job_code": "COS-NEW",
                "branch": PositionPosting.Branch.COS,
                "level": PositionPosting.Level.LEVEL_2,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": "2026-03-27",
                "closing_date": "",
                "qualification_reference": "Continuous talent pool for technical support.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.get(job_code="COS-NEW")
        self.assertEqual(created_entry.created_by, self.secretariat)
        self.assertEqual(created_entry.updated_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_CREATED,
                metadata__entry_code="COS-NEW",
            ).exists()
        )

    def test_non_entry_manager_cannot_access_entry_management(self):
        client = Client()
        client.force_login(self.hrmpsb)
        response = client.get(reverse("recruitment-entry-list"))
        self.assertEqual(response.status_code, 403)

    def test_entry_status_change_is_audited(self):
        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.SUSPENDED},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)
        self.assertFalse(self.level1_position.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code="PL-001",
            ).exists()
        )


class ApplicantPortalFlowTests(BaseRecruitmentTestCase):
    def portal_payload(self, **overrides):
        payload = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "email": "portal.applicant@example.com",
            "phone": "09171234567",
            "qualification_summary": "Qualified applicant with complete supporting credentials.",
            "cover_letter": "Please consider this application.",
            "checklist_privacy_consent": "on",
            "checklist_documents_complete": "on",
            "checklist_information_certified": "on",
        }
        for requirement in get_required_applicant_document_requirements():
            payload[requirement.code] = SimpleUploadedFile(
                f"{requirement.code}.txt",
                f"portal:{requirement.code}".encode("utf-8"),
                content_type="text/plain",
            )
        payload.update(overrides)
        return payload

    def test_shared_portal_lists_plantilla_and_cos_paths(self):
        response = self.client.get(reverse("applicant-portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vacancies List")
        self.assertContains(response, "Vacancy Detail")
        self.assertNotContains(response, "Internal Login")
        self.assertNotContains(response, "My Queue")
        self.assertNotContains(response, "Manage Entries")
        self.assertNotContains(response, "Internal Users")

    def test_plantilla_public_submission_requires_valid_otp_before_finalization(self):
        client = Client()
        response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(email="plantilla.portal@example.com"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="plantilla.portal@example.com",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(application.otp_hash)
        self.assertIsNone(application.submitted_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "Valid OTP verification is required before final submission.")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertIsNotNone(application.otp_verified_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        application.refresh_from_db()
        self.assertContains(response, application.reference_number)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
            ).exists()
        )

    def test_cos_public_submission_completes_and_status_lookup_works(self):
        client = Client()
        client.post(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
            self.portal_payload(email="cos.portal@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email="cos.portal@example.com",
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        receipt_response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(receipt_response, application.reference_number)
        self.assertEqual(application.branch, PositionPosting.Branch.COS)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

        status_response = client.post(
            reverse("applicant-status-lookup"),
            {
                "application_id": application.reference_number,
                "email": "cos.portal@example.com",
            },
        )
        self.assertContains(status_response, application.get_status_display())

    def test_invalid_otp_is_rejected(self):
        client = Client()
        client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(email="invalid.otp@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="invalid.otp@example.com",
        )

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": "000000"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(response, "The OTP is invalid.")
        self.assertIsNone(application.otp_verified_at)

    def test_portal_intake_requires_requirement_coded_documents(self):
        client = Client()
        missing_requirement = get_required_applicant_document_requirements()[0]
        payload = self.portal_payload(email="missing.requirement@example.com")
        payload.pop(missing_requirement.code)

        response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"Upload the required document for {missing_requirement.title}.",
        )
        self.assertFalse(
            RecruitmentApplication.objects.filter(
                position=self.level1_position,
                applicant_email="missing.requirement@example.com",
            ).exists()
        )

    def test_portal_reuses_existing_draft_and_requirement_documents_for_same_entry_email(self):
        client = Client()
        initial_response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(email="reused.draft@example.com"),
        )
        self.assertEqual(initial_response.status_code, 302)
        original_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="reused.draft@example.com",
        )
        applicant_id = original_application.applicant_id

        payload = self.portal_payload(
            email="reused.draft@example.com",
            qualification_summary="Updated qualifications for the same draft.",
        )
        for requirement in get_required_applicant_document_requirements():
            payload.pop(requirement.code)

        followup_response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            payload,
        )

        self.assertEqual(followup_response.status_code, 302)
        refreshed_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="reused.draft@example.com",
        )
        self.assertEqual(RecruitmentApplication.objects.filter(position=self.level1_position).count(), 1)
        self.assertEqual(refreshed_application.pk, original_application.pk)
        self.assertEqual(refreshed_application.applicant_id, applicant_id)
        self.assertEqual(
            refreshed_application.qualification_summary,
            "Updated qualifications for the same draft.",
        )
        self.assertEqual(
            RecruitmentUser.objects.filter(
                role=RecruitmentUser.Role.APPLICANT,
                email__iexact="reused.draft@example.com",
            ).count(),
            1,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=refreshed_application,
                action=AuditLog.Action.APPLICATION_UPDATED,
            ).exists()
        )

    def test_portal_reuses_existing_applicant_identity_by_email_across_entries(self):
        client = Client()
        shared_email = "shared.identity@example.com"
        first_response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(email=shared_email),
        )
        self.assertEqual(first_response.status_code, 302)
        first_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email=shared_email,
        )

        second_response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
            self.portal_payload(email=shared_email),
        )
        self.assertEqual(second_response.status_code, 302)
        second_application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email=shared_email,
        )

        self.assertEqual(first_application.applicant_id, second_application.applicant_id)
        self.assertEqual(
            RecruitmentUser.objects.filter(
                role=RecruitmentUser.Role.APPLICANT,
                email__iexact=shared_email,
            ).count(),
            1,
        )

    def test_expired_otp_cannot_be_used_to_verify_or_finalize(self):
        client = Client()
        client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(email="expired.otp@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="expired.otp@example.com",
        )
        otp_code = self.extract_otp_from_last_email()
        application.otp_expires_at = timezone.now() - timedelta(minutes=1)
        application.save(update_fields=["otp_expires_at", "updated_at"])

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": otp_code},
            follow=True,
        )
        self.assertContains(response, "The OTP has expired.")

        application.refresh_from_db()
        application.otp_verified_at = timezone.now() - timedelta(minutes=2)
        application.save(update_fields=["otp_verified_at", "updated_at"])
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "Your OTP verification has expired.")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)


class WorkflowRoutingTests(BaseRecruitmentTestCase):
    def test_level1_submission_routes_to_secretariat(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(routing_event.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_1)

    def test_level2_submission_routes_to_hrm_chief(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_2)

    def test_secretariat_cannot_process_level2_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "Secretariat cannot process Level 2 applications without an active override.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "endorse",
                "Attempted without override.",
            )

    def test_endorsement_requires_finalized_screening_for_screening_stages(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the screening record before endorsing this application.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "endorse",
                "Attempted before screening finalization.",
            )

    def test_secretariat_cannot_view_or_queue_level2_without_override_even_if_misassigned(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        self.assertFalse(user_can_view_application(self.secretariat, application))
        self.assertFalse(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_allows_secretariat_processing_of_level2(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("workflow-override", kwargs={"pk": application.pk}),
            {"reason": "Controlled screening support."},
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            screening_notes="Override-backed screening completed.",
        )
        process_workflow_action(application, self.secretariat, "endorse", "Pre-screen completed.")
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(WorkflowOverride.objects.filter(application=application, is_active=True).exists())
        routing_events = list(application.routing_history.values_list("route_type", "to_handler_role"))
        self.assertEqual(
            routing_events,
            [
                (RoutingHistory.RouteType.INITIAL, RecruitmentUser.Role.HRM_CHIEF),
                (RoutingHistory.RouteType.OVERRIDE, RecruitmentUser.Role.SECRETARIAT),
                (RoutingHistory.RouteType.FORWARD, RecruitmentUser.Role.HRM_CHIEF),
            ],
        )

    def test_secretariat_cannot_view_finalized_level2_case_without_authorized_basis(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.NOT_SELECTED,
                decision_notes="Finalize Level 2 case without Secretariat visibility.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertFalse(user_can_view_application(self.secretariat, application))

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_is_limited_to_active_hrm_chief_review_stage(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Secretariat overrides are only available while a Level 2 application is actively assigned to the HRM Chief review stage.",
        ):
            grant_secretariat_override(
                application=application,
                actor=self.sysadmin,
                reason="Improper reroute attempt.",
            )

        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertFalse(WorkflowOverride.objects.filter(application=application).exists())

    def test_cos_skips_hrmpsb_stage(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "COS screening done.")
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "COS endorsed.")
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW)


class RecruitmentCaseWorkflowTests(BaseRecruitmentTestCase):
    def test_submission_creates_one_recruitment_case(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertTrue(hasattr(application, "case"))
        self.assertEqual(RecruitmentCase.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CREATED,
            ).exists()
        )

    def test_stage_progression_updates_case_stage_in_defined_order(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)

        self.finalize_screening_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "Forward to HRM Chief.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)

        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "Forward to HRMPSB.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.finalize_car_for_current_stage(application, self.hrmpsb)
        process_workflow_action(application, self.hrmpsb, "recommend", "Forward to appointing authority.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved for completion.",
            )
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)

    def test_closed_case_is_locked_after_completion_and_can_be_reopened(self):
        application = self.make_selected_application(self.cos_position)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            {
                "completion_reference": "COS-CONTRACT-001",
                "completion_date": timezone.localdate().isoformat(),
                "deadline": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "remarks": "Contract requirements fully tracked.",
                "completion_requirements-TOTAL_FORMS": "3",
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
                "completion_requirements-0-item_label": "Signed contract",
                "completion_requirements-0-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-0-notes": "Submitted.",
                "completion_requirements-1-item_label": "Government-issued ID",
                "completion_requirements-1-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-1-notes": "Verified.",
                "completion_requirements-2-item_label": "",
                "completion_requirements-2-status": CompletionRequirement.RequirementStatus.PENDING,
                "completion_requirements-2-notes": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished."},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)

        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("workflow-reopen", kwargs={"pk": application.pk}),
            {"reason": "Correcting the completion tracking record."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertFalse(application.case.is_stage_locked)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_REOPENED,
            ).exists()
        )

    def test_case_timeline_is_visible_on_application_detail(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertContains(response, "Case Workspace")
        self.assertContains(response, "Qualification Screening")
        self.assertContains(response, "Examination Management")
        self.assertContains(response, "Workflow Snapshot")
        self.assertContains(response, "Case Timeline")
        self.assertContains(response, "Initial Routing")
        self.assertContains(response, "Case Created")


class ScreeningRecordTests(BaseRecruitmentTestCase):
    def screening_payload(self, **overrides):
        payload = {
            "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
            "completeness_notes": "All required application documents were reviewed.",
            "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
            "screening_notes": "Applicant satisfies the documented qualification basis.",
        }
        payload.update(overrides)
        return payload

    def test_current_handler_can_save_and_finalize_screening_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        screening_record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(),
            finalize=False,
        )
        self.assertFalse(screening_record.is_finalized)
        self.assertEqual(
            screening_record.qualification_outcome,
            ScreeningRecord.QualificationOutcome.QUALIFIED,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.SCREENING_RECORDED,
            ).exists()
        )

        screening_record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(screening_notes="Screening finalized."),
            finalize=True,
        )
        screening_record.refresh_from_db()
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.finalized_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.SCREENING_FINALIZED,
            ).exists()
        )

    def test_finalized_screening_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        screening_record = self.finalize_screening_for_current_stage(application, self.secretariat)

        with self.assertRaisesMessage(
            ValueError,
            "Finalized screening outputs are locked and cannot be modified.",
        ):
            save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    "completeness_notes": "Changed after finalization.",
                    "qualification_outcome": ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                    "screening_notes": "Should not be saved.",
                },
                finalize=False,
            )

        screening_record.refresh_from_db()
        self.assertEqual(screening_record.completeness_status, ScreeningRecord.CompletenessStatus.COMPLETE)
        self.assertEqual(screening_record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED)

    def test_unauthorized_user_cannot_record_screening(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrmpsb)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_record_level2_screening_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class ExamRecordTests(BaseRecruitmentTestCase):
    def exam_payload(self, **overrides):
        payload = {
            "exam_type": "Technical Examination",
            "exam_status": ExamRecord.ExamStatus.COMPLETED,
            "exam_score": "86.75",
            "exam_result": "Passed",
            "valid_from": timezone.localdate().isoformat(),
            "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
            "exam_notes": "Validated through the current review stage.",
        }
        payload.update(overrides)
        return payload

    def test_current_handler_can_create_update_and_finalize_exam_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "82.50",
                "exam_result": "Initial pass",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=180),
                "exam_notes": "Initial exam draft.",
            },
            finalize=False,
        )
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.recruitment_case, application.case)
        self.assertEqual(ExamRecord.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_RECORDED,
            ).exists()
        )

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "90.00",
                "exam_result": "Passed with updated score",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Updated before finalization.",
            },
            finalize=True,
        )
        exam_record.refresh_from_db()
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "90.00")
        self.assertEqual(exam_record.finalized_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_FINALIZED,
            ).exists()
        )

    def test_finalized_exam_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        exam_record = self.finalize_exam_for_current_stage(
            application,
            self.secretariat,
            valid_from=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=90),
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized examination outputs are locked and cannot be modified.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": "Technical Examination",
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "70.00",
                    "exam_result": "Changed after finalization",
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=30),
                    "exam_notes": "Should not save.",
                },
                finalize=False,
            )

        exam_record.refresh_from_db()
        self.assertEqual(str(exam_record.exam_score), "88.50")
        self.assertEqual(exam_record.exam_result, "Passed")

    def test_cos_exam_waiver_can_be_finalized_without_score_or_validity(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Internal COS Assessment",
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": None,
                "exam_result": "",
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Waived under COS office control.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(exam_record.branch, PositionPosting.Branch.COS)
        self.assertIsNone(exam_record.exam_score)
        self.assertIsNone(exam_record.valid_from)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.WAIVED)

    def test_completed_exam_requires_score_or_result(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaises(ValidationError):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": "Technical Examination",
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": None,
                    "exam_result": "",
                    "valid_from": None,
                    "valid_until": None,
                    "exam_notes": "Missing score and result.",
                },
                finalize=False,
            )

    def test_unauthorized_user_cannot_record_exam(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrmpsb)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_record_level2_exam_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class EvidenceVaultTests(BaseRecruitmentTestCase):
    def test_evidence_is_encrypted_and_digest_is_stored_with_stage_metadata(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume.txt",
                b"plain-text resume",
                content_type="text/plain",
            ),
        )

        evidence = EvidenceVaultItem.objects.get(application=application)
        self.assertNotEqual(bytes(evidence.ciphertext), b"plain-text resume")
        self.assertEqual(len(evidence.sha256_digest), 64)
        self.assertEqual(evidence.digest_algorithm, "sha256")
        self.assertEqual(evidence.stage, EvidenceVaultItem.Stage.APPLICANT_INTAKE)
        self.assertEqual(evidence.artifact_scope, EvidenceVaultItem.OwnerScope.APPLICATION)
        self.assertEqual(evidence.application_id, application.id)
        self.assertIsNone(evidence.recruitment_case_id)
        self.assertIsNone(evidence.recruitment_entry_id)
        self.assertEqual(evidence.version_number, 1)
        self.assertTrue(evidence.is_current_version)
        self.assertEqual(evidence.uploaded_by_role, RecruitmentUser.Role.APPLICANT)

    def test_reuploading_same_label_preserves_version_history(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        first_version = upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume-v1.txt",
                b"resume version one",
                content_type="text/plain",
            ),
        )
        second_version = upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume-v2.txt",
                b"resume version two",
                content_type="text/plain",
            ),
        )

        first_version.refresh_from_db()
        second_version.refresh_from_db()
        self.assertEqual(first_version.version_number, 1)
        self.assertEqual(second_version.version_number, 2)
        self.assertEqual(second_version.artifact_scope, EvidenceVaultItem.OwnerScope.APPLICATION)
        self.assertEqual(first_version.version_family, second_version.version_family)
        self.assertEqual(second_version.previous_version, first_version)
        self.assertFalse(first_version.is_current_version)
        self.assertTrue(second_version.is_current_version)
        self.assertEqual(EvidenceVaultItem.objects.filter(application=application).count(), 2)

    def test_internal_handler_can_download_uploaded_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content,
            f"sample:{requirement_code}".encode("utf-8"),
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_evidence_service_rejects_unauthorized_upload_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot upload evidence for this application.",
        ):
            upload_evidence_item(
                application=application,
                actor=self.hrmpsb,
                label="Late Submission",
                uploaded_file=SimpleUploadedFile(
                    "late.txt",
                    b"late evidence",
                    content_type="text/plain",
                ),
            )

    def test_case_owned_workflow_evidence_uses_recruitment_case_owner(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        evidence = upload_evidence_item(
            application=application,
            actor=self.secretariat,
            label="Secretariat Routing Notes",
            uploaded_file=SimpleUploadedFile(
                "routing-notes.txt",
                b"internal routing notes",
                content_type="text/plain",
            ),
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type="workflow_evidence",
        )

        self.assertEqual(evidence.artifact_scope, EvidenceVaultItem.OwnerScope.CASE)
        self.assertIsNone(evidence.application_id)
        self.assertEqual(evidence.recruitment_case_id, application.case.id)
        self.assertIsNone(evidence.recruitment_entry_id)

    def test_evidence_vault_search_hides_inaccessible_cases_and_filters_archived_items(self):
        accessible_application = self.make_application(self.level1_position)
        self.verify_application_for_submission(accessible_application)
        submit_application(accessible_application, self.applicant)
        accessible_application.refresh_from_db()
        accessible_evidence = EvidenceVaultItem.objects.get(
            application=accessible_application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=get_required_applicant_document_requirements()[0].code,
        )

        inaccessible_application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level2_position,
            applicant_first_name="Hidden",
            applicant_last_name="Applicant",
            applicant_email="hidden@example.com",
            applicant_phone="09170000000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Hidden application.",
        )
        self.upload_required_applicant_documents(
            inaccessible_application,
            self.applicant,
            content_prefix="hidden",
        )
        self.verify_application_for_submission(inaccessible_application)
        submit_application(inaccessible_application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)

        initial_response = client.get(
            reverse("evidence-vault-list"),
            {
                "q": "Personal Data Sheet",
                "archival_status": "all",
            },
        )
        self.assertEqual(initial_response.status_code, 200)
        self.assertContains(initial_response, accessible_application.reference_label)
        self.assertNotContains(initial_response, inaccessible_application.reference_label)

        archive_response = client.post(
            reverse(
                "evidence-archive-toggle",
                kwargs={"pk": accessible_application.pk, "evidence_pk": accessible_evidence.pk},
            ),
            {
                "action": "archive",
                "archive_tag": "Closed case retention batch",
                "next": reverse("evidence-vault-list"),
            },
            follow=True,
        )
        self.assertEqual(archive_response.status_code, 200)
        accessible_evidence.refresh_from_db()
        self.assertTrue(accessible_evidence.is_archived)
        self.assertEqual(accessible_evidence.archive_tag, "Closed case retention batch")
        self.assertTrue(
            AuditLog.objects.filter(
                application=accessible_application,
                action=AuditLog.Action.EVIDENCE_ARCHIVED,
                metadata__evidence_id=accessible_evidence.id,
            ).exists()
        )

        archived_response = client.get(
            reverse("evidence-vault-list"),
            {
                "q": accessible_application.reference_label,
                "archival_status": "archived",
                "current_version_only": "",
            },
        )
        self.assertEqual(archived_response.status_code, 200)
        self.assertContains(archived_response, "Closed case retention batch")
        self.assertContains(archived_response, accessible_application.reference_label)


class ViewAndExportTests(BaseRecruitmentTestCase):
    def test_applicant_user_cannot_access_internal_dashboard(self):
        client = Client()
        client.force_login(self.applicant)
        response = client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_applicant_user_cannot_access_internal_application_detail(self):
        other_applicant = User.objects.create_user(
            username="otherapplicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=other_applicant,
            position=self.level1_position,
            qualification_summary="Another applicant.",
        )

        client = Client()
        client.force_login(self.applicant)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 403)

    def test_export_bundle_returns_structured_zip_with_inventory_and_verification_outputs(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        root = f"{application.reference_number}/"
        self.assertIn(f"{root}records/application_summary.pdf", names)
        self.assertIn(f"{root}records/submission_packet.json", names)
        self.assertIn(f"{root}records/case_manifest.json", names)
        self.assertIn(f"{root}inventory/evidence_inventory.csv", names)
        self.assertIn(f"{root}inventory/evidence_inventory.pdf", names)
        self.assertIn(f"{root}logs/audit_log.csv", names)
        self.assertIn(f"{root}logs/routing_history.csv", names)
        self.assertIn(f"{root}verification/verification_report.json", names)
        self.assertIn(f"{root}verification/checksums.sha256", names)
        self.assertIn(f"{root}verification/verification_summary.pdf", names)
        evidence_paths = [name for name in names if name.startswith(f"{root}evidence/")]
        self.assertTrue(evidence_paths)

        manifest = json.loads(archive.read(f"{root}records/case_manifest.json").decode("utf-8"))
        verification_report = json.loads(
            archive.read(f"{root}verification/verification_report.json").decode("utf-8")
        )
        audit_log_csv = archive.read(f"{root}logs/audit_log.csv").decode("utf-8")
        inventory_csv = archive.read(f"{root}inventory/evidence_inventory.csv").decode("utf-8")
        checksums = archive.read(f"{root}verification/checksums.sha256").decode("utf-8")
        required_document_count = len(get_required_applicant_document_requirements())

        self.assertEqual(manifest["source_application"]["id"], application.id)
        self.assertEqual(manifest["source_case"]["id"], application.case.id)
        self.assertEqual(manifest["export"]["bundle_root"], root)
        self.assertEqual(manifest["export"]["evidence_file_count"], required_document_count)
        self.assertEqual(
            manifest["bundle_contents"]["verification_paths"],
            [
                f"{root}verification/verification_report.json",
                f"{root}verification/checksums.sha256",
                f"{root}verification/verification_summary.pdf",
            ],
        )
        self.assertEqual(verification_report["case_reference"], application.reference_number)
        self.assertEqual(verification_report["source_case_id"], application.case.id)
        self.assertEqual(verification_report["evidence_file_count"], required_document_count)
        self.assertTrue(all(item["digest_match"] for item in verification_report["evidence_files"]))
        self.assertTrue(
            any(
                covered["path"] == evidence_paths[0]
                for covered in verification_report["covered_files"]
            )
        )
        self.assertIn("case_reference,workflow_stage,actor,actor_role,action,is_sensitive_access", audit_log_csv)
        self.assertIn("stored_sha256_digest,exported_sha256_digest,digest_match", inventory_csv)
        self.assertIn(evidence_paths[0], inventory_csv)
        self.assertIn(evidence_paths[0], checksums)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXPORT_GENERATED,
            ).exists()
        )
        export_log = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.EXPORT_GENERATED,
        )
        self.assertEqual(export_log.metadata["bundle_root"], root)
        self.assertEqual(export_log.metadata["source_case_id"], application.case.id)
        self.assertEqual(export_log.metadata["evidence_item_count"], required_document_count)

    def test_export_bundle_preserves_application_case_and_entry_scoped_artifacts(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        upload_evidence_item(
            application=application,
            actor=self.appointing,
            label="Appointing Review Notes",
            uploaded_file=SimpleUploadedFile(
                "appointing-notes.txt",
                b"appointing review notes",
                content_type="text/plain",
            ),
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type="workflow_evidence",
        )

        bundle_bytes = build_export_bundle(application, self.appointing)
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes))
        root = f"{application.reference_number}/"
        manifest = json.loads(archive.read(f"{root}records/case_manifest.json").decode("utf-8"))

        scopes = {item["artifact_scope"] for item in manifest["evidence"]}
        artifact_types = {item["artifact_type"] for item in manifest["evidence"]}

        self.assertEqual(scopes, {"application", "case", "entry"})
        self.assertIn("applicant_document", artifact_types)
        self.assertIn("workflow_evidence", artifact_types)
        self.assertIn("comparative_assessment_report", artifact_types)
        self.assertTrue(any(item["artifact_scope"] == "entry" for item in manifest["evidence"]))
        self.assertTrue(any(item["artifact_scope"] == "case" for item in manifest["evidence"]))
        self.assertTrue(any(item["artifact_scope"] == "application" for item in manifest["evidence"]))

    def test_secretariat_can_export_level1_case_when_they_can_view_it(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")

    def test_non_export_role_cannot_access_controlled_export(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrmpsb)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 403)

    def test_export_service_rejects_unauthorized_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot export this application.",
        ):
            build_export_bundle(application, self.hrmpsb)


class AuditLoggingTraceabilityTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def test_submission_audit_log_stores_traceability_fields(self):
        application = self.make_submitted_application()

        log = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.APPLICATION_SUBMITTED,
        )

        self.assertEqual(log.actor, self.applicant)
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, application.reference_number)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)

    def test_application_detail_view_logs_sensitive_record_access(self):
        application = self.make_submitted_application()

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application=application,
            actor=self.secretariat,
            action=AuditLog.Action.PROTECTED_RECORD_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(log.metadata["access_source"], "application_detail")

    def test_authorized_case_audit_review_is_available_and_logged(self):
        application = self.make_submitted_application()

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Application Audit Trail")
        log = AuditLog.objects.filter(
            application=application,
            actor=self.secretariat,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "application_audit")

    def test_evidence_vault_review_logs_sensitive_access(self):
        self.make_submitted_application()

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("evidence-vault-list"))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.secretariat,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)

    def test_system_admin_cannot_review_case_audit_without_case_visibility(self):
        application = self.make_submitted_application()

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 404)

    def test_system_admin_can_review_system_audit_logs_only(self):
        record_system_audit_event(
            actor=self.sysadmin,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="System administrator changed a password.",
            metadata={"target_user_id": self.secretariat.id},
        )

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Audit Logs")
        self.assertContains(response, "Password Changed")
        self.assertFalse(response.context["audit_logs"][0].application_id if response.context["audit_logs"] else False)
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "system_audit")

    def test_traceability_backfill_handles_draft_logs_without_reference_numbers(self):
        migration_module = importlib.import_module(
            "recruitment.migrations.0014_auditlog_traceability_fields"
        )
        application = self.make_application(self.level1_position)
        log = AuditLog.objects.create(
            application=application,
            actor=self.applicant,
            actor_role="",
            case_reference="",
            workflow_stage="",
            action=AuditLog.Action.APPLICATION_CREATED,
            description="Applicant created a draft application.",
            metadata={"review_stage": RecruitmentCase.Stage.SECRETARIAT_REVIEW},
            is_sensitive_access=False,
        )

        migration_module.backfill_audit_log_traceability(django_apps, None)

        log.refresh_from_db()
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, "")
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)


class NotificationManagementTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def make_approved_cos_application(self):
        return self.make_selected_application(self.cos_position)

    def make_approved_level2_plantilla_application(self):
        return self.make_selected_application(self.level2_position)

    def test_submission_acknowledgment_notification_is_sent_and_stored(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        )
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.recipient_email, "applicant@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(application.reference_number, mail.outbox[0].subject)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.NOTIFICATION_SENT,
                metadata__notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
            ).exists()
        )

    def test_approval_sends_selected_applicant_notification(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        mail.outbox.clear()

        self.finalize_screening_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "COS screening done.")
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "COS endorsed.")

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved.",
            )

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("selection result", mail.outbox[0].subject.lower())
        self.assertIn("COS", mail.outbox[0].body)

    def test_rejection_sends_non_selected_applicant_notification(self):
        application = self.make_submitted_application()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(application, self.secretariat, "reject", "Rejected at Secretariat.")

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("non-selection notice", mail.outbox[0].subject.lower())

    def test_secretariat_can_send_requirement_checklist_notification_for_level1_completion(self):
        application = self.make_approved_cos_application()
        mail.outbox.clear()
        client = Client()
        client.force_login(self.secretariat)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-checklist", kwargs={"pk": application.pk}),
                {
                    "checklist_items": "- Signed contract\n- Government-issued ID",
                    "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
                    "additional_message": "Bring original copies during submission.",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        notification = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.REQUIREMENT_CHECKLIST,
        ).latest("created_at")
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.secretariat)
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(response, "Requirement Checklist Notification")
        self.assertContains(response, notification.subject)

    def test_secretariat_cannot_send_requirement_checklist_before_selection(self):
        application = self.make_submitted_application()
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {
                "checklist_items": "- Any requirement",
                "deadline": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "additional_message": "",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_hrm_chief_can_send_reminder_notification_for_level2_completion(self):
        application = self.make_approved_level2_plantilla_application()
        mail.outbox.clear()
        client = Client()
        client.force_login(self.hrm_chief)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-reminder", kwargs={"pk": application.pk}),
                {
                    "reminder_subject": "Follow-up reminder for completion documents",
                    "reminder_message": "Please submit the remaining completion documents this week.",
                    "deadline": (timezone.localdate() + timedelta(days=3)).isoformat(),
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        notification = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.REMINDER,
        ).latest("created_at")
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.hrm_chief)
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(response, "Reminder Notification")


class CompletionTrackingTests(BaseRecruitmentTestCase):
    def completion_payload(self, items, **overrides):
        payload = {
            "completion_reference": "COMP-001",
            "completion_date": timezone.localdate().isoformat(),
            "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "remarks": "Completion tracking updated.",
        }
        payload.update(overrides)
        total_forms = len(items) + 1
        payload.update(
            {
                "completion_requirements-TOTAL_FORMS": str(total_forms),
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
            }
        )
        for index, item in enumerate(items):
            payload[f"completion_requirements-{index}-item_label"] = item["item_label"]
            payload[f"completion_requirements-{index}-status"] = item["status"]
            payload[f"completion_requirements-{index}-notes"] = item.get("notes", "")
        payload[f"completion_requirements-{len(items)}-item_label"] = ""
        payload[f"completion_requirements-{len(items)}-status"] = CompletionRequirement.RequirementStatus.PENDING
        payload[f"completion_requirements-{len(items)}-notes"] = ""
        return payload

    def test_plantilla_completion_tracking_stores_announcement_and_requirement_statuses(self):
        application = self.make_selected_application(self.level1_position)
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Validated by Secretariat.",
                    },
                    {
                        "item_label": "Medical certificate",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting submission.",
                    },
                ],
                completion_reference="PLANTILLA-APPT-001",
                announcement_reference="ANN-PL-2026-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Appointment completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(record.completion_reference, "PLANTILLA-APPT-001")
        self.assertEqual(record.announcement_reference, "ANN-PL-2026-001")
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(
            record.requirements.filter(
                item_label="Signed appointment paper",
                status=CompletionRequirement.RequirementStatus.COMPLETED,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.COMPLETION_RECORDED,
                metadata__completion_record_id=record.id,
            ).exists()
        )

    def test_cos_completion_tracking_ignores_announcement_fields_and_preserves_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Contract received.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing verified copy reused.",
                    },
                ],
                completion_reference="COS-CONTRACT-2026-001",
                announcement_reference="SHOULD-NOT-SAVE",
                announcement_date=timezone.localdate().isoformat(),
                remarks="COS contract completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.COS)
        self.assertEqual(record.completion_reference, "COS-CONTRACT-2026-001")
        self.assertEqual(record.announcement_reference, "")
        self.assertIsNone(record.announcement_date)
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(record.requirements_ready_for_closure)

    def test_case_close_requires_resolved_completion_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Tax form",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Still pending.",
                    },
                ],
                completion_reference="COS-CONTRACT-LOCK",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Trying to close too early."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "All completion requirements must be marked completed or not applicable before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_locks_case_and_closed_case_remains_retrievable(self):
        application = self.make_selected_application(self.level2_position)
        client = Client()
        client.force_login(self.hrm_chief)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Filed.",
                    },
                    {
                        "item_label": "Medical clearance",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Waived under recorded office rule.",
                    },
                ],
                completion_reference="PL2-APPT-001",
                announcement_reference="ANN-PL2-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Ready for case closure.",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished and archived."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, "")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )
        self.assertTrue(
            RoutingHistory.objects.filter(
                application=application,
                route_type=RoutingHistory.RouteType.CLOSE,
            ).exists()
        )

        detail_response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Completion Tracking")


class InterviewManagementTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Conference Room A",
            "session_notes": "Structured interview schedule prepared.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, **overrides):
        payload = {
            "rating_score": "89.50",
            "rating_notes": "Interview responses addressed the major competency areas.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def test_secretariat_can_schedule_upload_fallback_and_finalize_interview_at_secretariat_stage(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        interview_session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(location="Secretariat Coordination Room"),
            finalize=False,
        )
        fallback_evidence = upload_interview_fallback_rating(
            application=application,
            actor=self.secretariat,
            uploaded_file=SimpleUploadedFile(
                "fallback-rating.pdf",
                b"scanned fallback rating sheet",
                content_type="application/pdf",
            ),
            remarks="Fallback sheet retained for manual panel scoring.",
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(
                location="Secretariat Coordination Room",
                session_notes="Session locked after fallback upload.",
            ),
            finalize=True,
        )

        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_session.review_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(fallback_evidence.stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
            ).exists()
        )

    def test_hrm_chief_can_record_direct_interview_rating_for_cos_case(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)

        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(location="Virtual Interview Room"),
            finalize=False,
        )
        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(rating_score="91.25"),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(
                location="Virtual Interview Room",
                session_notes="Direct HRM Chief interview rating finalized.",
            ),
            finalize=True,
        )

        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_rating.rated_by, self.hrm_chief)
        self.assertEqual(str(interview_rating.rating_score), "91.25")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
            ).exists()
        )

    def test_finalized_interview_session_blocks_session_rating_and_fallback_changes(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)

        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Finalized interview session."),
            finalize=True,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept rating changes.",
        ):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.rating_payload(rating_score="90.00"),
            )
        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept fallback rating uploads.",
        ):
            upload_interview_fallback_rating(
                application=application,
                actor=self.hrm_chief,
                uploaded_file=SimpleUploadedFile("fallback.pdf", b"fallback", content_type="application/pdf"),
                remarks="Late upload.",
            )

        interview_session.refresh_from_db()
        self.assertTrue(interview_session.is_finalized)


class DeliberationDecisionSupportTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Virtual Deliberation Room",
            "session_notes": "Decision-support interview session prepared.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, **overrides):
        payload = {
            "rating_score": "92.00",
            "rating_notes": "Interview performance supports the recommendation.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def test_cos_deliberation_consolidates_finalized_outputs(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(),
        )
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Interview output locked before deliberation."),
            finalize=True,
        )

        deliberation_record = self.finalize_deliberation_for_current_stage(application, self.hrm_chief)

        self.assertTrue(deliberation_record.is_finalized)
        self.assertEqual(deliberation_record.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["finalized_screening_count"],
            2,
        )
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["finalized_interview_count"],
            1,
        )
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["latest_interview_average"],
            "92.00",
        )

    def test_plantilla_recommendation_requires_deliberation_and_car(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the deliberation record before recommending this Plantilla application.",
        ):
            process_workflow_action(application, self.hrmpsb, "recommend", "Attempted without deliberation.")

        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the Comparative Assessment Report before recommending this Plantilla application.",
        ):
            process_workflow_action(application, self.hrmpsb, "recommend", "Attempted without CAR.")

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)
        self.assertEqual(ComparativeAssessmentReportItem.objects.filter(report=report).count(), 1)

    def test_car_generation_creates_versioned_evidence(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.version_number, 1)
        self.assertTrue(report.evidence_item.is_current_version)
        self.assertEqual(report.evidence_item.stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertEqual(report.evidence_item.artifact_scope, EvidenceVaultItem.OwnerScope.ENTRY)
        self.assertEqual(report.evidence_item.artifact_type, "comparative_assessment_report")
        self.assertIsNone(report.evidence_item.application_id)
        self.assertIsNone(report.evidence_item.recruitment_case_id)
        self.assertEqual(report.evidence_item.recruitment_entry_id, application.position_id)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CAR_FINALIZED,
            ).exists()
        )

    def test_car_generation_creates_versioned_entry_reports_across_candidates(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="car-secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="car.second.candidate@example.com",
            applicant_phone="09179990001",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for CAR reuse testing.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="car-secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        process_workflow_action(
            secondary_application,
            self.secretariat,
            "endorse",
            "Forward second candidate to HRM Chief.",
        )
        secondary_application.refresh_from_db()
        self.finalize_screening_for_current_stage(secondary_application, self.hrm_chief)
        process_workflow_action(
            secondary_application,
            self.hrm_chief,
            "endorse",
            "Forward second candidate to HRMPSB.",
        )
        secondary_application.refresh_from_db()

        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)

        draft_report = generate_comparative_assessment_report(
            application=primary_application,
            actor=self.hrmpsb,
            cleaned_data={"summary_notes": "Draft entry-level CAR."},
            finalize=False,
        )
        finalized_report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.hrmpsb,
            cleaned_data={"summary_notes": "Final entry-level CAR."},
            finalize=True,
        )

        self.assertNotEqual(draft_report.pk, finalized_report.pk)
        self.assertEqual(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=self.level1_position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).count(),
            2,
        )
        self.assertEqual(draft_report.version_number, 1)
        self.assertEqual(finalized_report.version_number, 2)
        self.assertFalse(draft_report.is_finalized)
        self.assertTrue(finalized_report.is_finalized)
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=finalized_report).count(),
            2,
        )

    def test_finalized_car_is_reused_for_other_candidates_in_same_entry(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="second.candidate@example.com",
            applicant_phone="09179990000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for the same Plantilla entry.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        process_workflow_action(
            secondary_application,
            self.secretariat,
            "endorse",
            "Forward second candidate to HRM Chief.",
        )
        secondary_application.refresh_from_db()
        self.finalize_screening_for_current_stage(secondary_application, self.hrm_chief)
        process_workflow_action(
            secondary_application,
            self.hrm_chief,
            "endorse",
            "Forward second candidate to HRMPSB.",
        )
        secondary_application.refresh_from_db()

        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)
        shared_report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)

        secondary_packet = build_submission_packet(secondary_application)
        self.assertTrue(secondary_packet["summary"]["has_comparative_assessment_report"])
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=shared_report).count(),
            2,
        )

        process_workflow_action(
            secondary_application,
            self.hrmpsb,
            "recommend",
            "Shared CAR covers all ranked candidates in the entry.",
        )
        secondary_application.refresh_from_db()
        self.assertEqual(
            secondary_application.status,
            RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
        )
        self.assertEqual(
            secondary_application.case.current_stage,
            RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
        )


class FinalDecisionHandlingTests(BaseRecruitmentTestCase):
    def test_selected_final_decision_routes_case_to_completion_and_preserves_packet(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after reviewing the final submission packet.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertTrue(decision.submission_packet_snapshot["summary"]["has_deliberation_record"])
        self.assertTrue(decision.submission_packet_snapshot["summary"]["has_comparative_assessment_report"])
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )

    def test_not_selected_final_decision_closes_case_and_locks_it(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.NOT_SELECTED,
            decision_notes="Not selected after reviewing final records.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.NOT_SELECTED)
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_application_detail_exposes_decision_packet_sections(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        client.force_login(self.appointing)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Interview Scheduling")
        self.assertContains(response, "Deliberation Record")
        self.assertContains(response, "Workflow Snapshot")
        self.assertContains(response, "Final Decision Recording")

        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_deliberation_record"])
