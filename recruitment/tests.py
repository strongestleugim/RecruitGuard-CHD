import io
import re
import zipfile
from datetime import timedelta

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
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    InterviewRating,
    InterviewSession,
    Position,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
    WorkflowOverride,
)
from .requirements import PERFORMANCE_RATING, get_applicant_document_requirements
from .services import (
    build_submission_packet,
    generate_comparative_assessment_report,
    get_queue_for_user,
    issue_application_otp,
    process_workflow_action,
    record_final_decision,
    reopen_recruitment_case,
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

    def build_requirement_upload(self, requirement_code):
        return SimpleUploadedFile(
            f"{requirement_code}.txt",
            f"sample evidence for {requirement_code}".encode("utf-8"),
            content_type="text/plain",
        )

    def upload_complete_required_documents(self, application, actor, include_performance_rating=False):
        application.performance_rating_not_applicable = not include_performance_rating
        application.save(update_fields=["performance_rating_not_applicable", "updated_at"])

        for requirement in get_applicant_document_requirements():
            if requirement.code == PERFORMANCE_RATING and application.performance_rating_not_applicable:
                continue
            upload_evidence_item(
                application=application,
                actor=actor,
                label=requirement.title,
                uploaded_file=self.build_requirement_upload(requirement.code),
                document_type=requirement.code,
            )

    def make_application(self, position):
        return self.make_application_for_user(
            position=position,
            user=self.applicant,
            first_name="Test",
            last_name="Applicant",
            email="applicant@example.com",
        )

    def make_application_for_user(self, position, user, first_name, last_name, email):
        application = RecruitmentApplication.objects.create(
            applicant=user,
            position=position,
            applicant_first_name=first_name,
            applicant_last_name=last_name,
            applicant_email=email,
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            performance_rating_not_applicable=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
        )
        self.upload_complete_required_documents(application, user)
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
        submit_application(application, self.applicant)
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
    def portal_payload(self, include_performance_rating=False, **overrides):
        payload = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "email": "portal.applicant@example.com",
            "phone": "09171234567",
            "qualification_summary": "Qualified applicant with complete supporting credentials.",
            "cover_letter": "Please consider this application.",
            "performance_rating_not_applicable": "on",
            "checklist_privacy_consent": "on",
            "checklist_documents_complete": "on",
            "checklist_information_certified": "on",
        }
        for requirement in get_applicant_document_requirements():
            if requirement.code == PERFORMANCE_RATING and not include_performance_rating:
                continue
            payload[requirement.file_field_name] = self.build_requirement_upload(requirement.code)
        if include_performance_rating:
            payload.pop("performance_rating_not_applicable", None)
        payload.update(overrides)
        return payload

    def test_shared_portal_lists_plantilla_and_cos_paths(self):
        response = self.client.get(reverse("applicant-portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Plantilla Recruitment")
        self.assertContains(response, "COS Recruitment")
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
        self.assertTrue(application.performance_rating_not_applicable)
        self.assertTrue(application.has_complete_required_documents)

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

    def test_public_intake_requires_each_named_document_before_creating_a_draft(self):
        response = self.client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(
                email="missing.document@example.com",
                transcript_of_records="",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.assertFalse(
            RecruitmentApplication.objects.filter(
                applicant_email="missing.document@example.com",
                position=self.level1_position,
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_public_intake_accepts_uploaded_performance_rating_when_provided(self):
        client = Client()
        response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            self.portal_payload(
                include_performance_rating=True,
                email="rated.applicant@example.com",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="rated.applicant@example.com",
        )
        self.assertFalse(application.performance_rating_not_applicable)
        self.assertTrue(
            application.evidence_items.filter(document_type=PERFORMANCE_RATING).exists()
        )

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

    def test_submit_application_rejects_missing_required_documents_server_side(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            applicant_first_name="Test",
            applicant_last_name="Applicant",
            applicant_email="incomplete@example.com",
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            performance_rating_not_applicable=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
            otp_verified_at=timezone.now(),
            otp_expires_at=timezone.now() + timedelta(minutes=5),
        )
        upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Signed Cover Letter addressed to VOLTAIRE S. GUADALUPE, MD, MPH, MAHPS, Director IV",
            uploaded_file=self.build_requirement_upload("signed_cover_letter"),
            document_type="signed_cover_letter",
        )

        with self.assertRaisesMessage(
            ValueError,
            "Upload all required documents before proceeding.",
        ):
            submit_application(application, self.applicant)


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

    def test_endorsement_requires_finalized_exam_when_exam_record_exists_for_stage(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "82.50",
                "exam_result": "Draft result",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=180),
                "exam_notes": "Draft exam output before endorsement.",
            },
            finalize=False,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the examination record before endorsing this application.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "endorse",
                "Attempted before exam finalization.",
            )

        self.finalize_exam_for_current_stage(
            application,
            self.secretariat,
            exam_score="90.00",
            exam_result="Passed",
            valid_from=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=365),
            exam_notes="Finalized examination output.",
        )
        process_workflow_action(
            application,
            self.secretariat,
            "endorse",
            "Forward after exam finalization.",
        )
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

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

    def test_closed_case_is_locked_and_can_be_reopened(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "COS screening done.")
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "COS endorsed.")
        self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Approved after appointing-authority review.",
        )
        application.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)

        with self.assertRaisesMessage(
            ValueError,
            "This recruitment case is stage-locked. Use controlled reopen before proceeding.",
        ):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Duplicate approval.",
            )

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("workflow-reopen", kwargs={"pk": application.pk}),
            {"reason": "Correcting the final review packet."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertFalse(application.case.is_stage_locked)
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

        self.assertContains(response, "Recruitment Case")
        self.assertContains(response, "Screening Record")
        self.assertContains(response, "Examination Record")
        self.assertContains(response, "Interview Sessions and Ratings")
        self.assertContains(response, "Deliberation and Decision Support")
        self.assertContains(response, "Routing History")
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

    def test_exam_record_must_link_to_the_same_application_case(self):
        application = self.make_application(self.level1_position)
        other_application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        self.verify_application_for_submission(other_application)
        submit_application(application, self.applicant)
        submit_application(other_application, self.applicant)

        exam_record = ExamRecord(
            application=application,
            recruitment_case=other_application.case,
            review_stage=application.case.current_stage,
            recorded_by=self.secretariat,
            branch=application.branch,
            level=application.level,
            exam_type="Technical Examination",
            exam_status=ExamRecord.ExamStatus.COMPLETED,
            exam_score="85.00",
            exam_result="Passed",
            exam_notes="Mismatched case linkage should fail.",
        )

        with self.assertRaises(ValidationError) as raised:
            exam_record.full_clean()

        self.assertIn("recruitment_case", raised.exception.message_dict)

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
            remarks="Scanned panel sheet captured during Secretariat-stage coordination.",
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(
                location="Secretariat Coordination Room",
                session_notes="Fallback rating sheet preserved for this stage.",
            ),
            finalize=True,
        )

        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_session.review_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(fallback_evidence.recruitment_case, application.case)
        self.assertEqual(fallback_evidence.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_FINALIZED,
            ).exists()
        )

    def test_hrm_chief_can_record_direct_interview_rating_for_cos_case(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)

        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(location="Virtual Panel Room"),
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
                location="Virtual Panel Room",
                session_notes="Direct HRM Chief interview rating finalized.",
            ),
            finalize=True,
        )

        self.assertEqual(interview_session.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_rating.rated_by, self.hrm_chief)
        self.assertEqual(str(interview_rating.rating_score), "91.25")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
            ).exists()
        )

    def test_rating_below_threshold_requires_justification(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        with self.assertRaises(ValidationError):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.rating_payload(
                    rating_score="70.00",
                    justification="",
                ),
            )

    def test_hrmpsb_member_can_record_interview_rating_for_plantilla_stage(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)

        interview_session = save_interview_session(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.session_payload(location="HRMPSB Deliberation Room"),
            finalize=False,
        )
        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(rating_score="87.00"),
        )

        self.assertEqual(interview_session.review_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertEqual(interview_rating.review_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertEqual(interview_rating.rated_by, self.hrmpsb)

    def test_finalized_interview_session_blocks_session_rating_and_fallback_changes(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        interview_session = save_interview_session(
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
            "Finalized interview sessions are locked and cannot be modified.",
        ):
            save_interview_session(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.session_payload(location="Changed after lock"),
                finalize=False,
            )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept rating changes.",
        ):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.rating_payload(rating_score="95.00"),
            )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept fallback rating uploads.",
        ):
            upload_interview_fallback_rating(
                application=application,
                actor=self.hrm_chief,
                uploaded_file=SimpleUploadedFile(
                    "late-fallback.pdf",
                    b"late fallback upload",
                    content_type="application/pdf",
                ),
                remarks="Should be blocked after finalization.",
            )

        interview_session.refresh_from_db()
        self.assertTrue(interview_session.is_finalized)

    def test_interview_finalization_requires_rating_or_fallback(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session.",
        ):
            save_interview_session(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.session_payload(session_notes="Attempted empty finalization."),
                finalize=True,
            )

    def test_unauthorized_user_cannot_record_interview_rating(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("interview-rating", kwargs={"pk": application.pk}),
            {
                "rating_score": "90.00",
                "rating_notes": "Unauthorized attempt.",
                "justification": "",
            },
        )
        self.assertEqual(response.status_code, 403)


class DeliberationDecisionSupportTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Board Room A",
            "session_notes": "Decision-support interview session prepared.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, **overrides):
        payload = {
            "rating_score": "90.00",
            "rating_notes": "Interview performance supports the recommendation.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def deliberation_payload(self, **overrides):
        payload = {
            "deliberated_at": timezone.now(),
            "deliberation_minutes": "Formal deliberation minutes were recorded.",
            "decision_support_summary": "Consolidated outputs support the next workflow action.",
            "ranking_position": None,
            "ranking_notes": "Ranking notes are optional for this branch.",
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
            cleaned_data=self.session_payload(location="Virtual Deliberation Room"),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(rating_score="91.50"),
        )
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(
                location="Virtual Deliberation Room",
                session_notes="Interview output locked before deliberation.",
            ),
            finalize=True,
        )

        deliberation_record = save_deliberation_record(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.deliberation_payload(
                decision_support_summary="COS decision-support packet finalized.",
            ),
            finalize=True,
        )

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
            "91.50",
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.DELIBERATION_FINALIZED,
            ).exists()
        )

    def test_cos_endorsement_requires_finalized_deliberation(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the deliberation record before endorsing this COS application.",
        ):
            process_workflow_action(application, self.hrm_chief, "endorse", "Attempted without deliberation.")

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

    def test_hrmpsb_can_generate_finalized_plantilla_car_with_ranked_rows(self):
        second_applicant = User.objects.create_user(
            username="second-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        primary_application = self.make_application(self.level1_position)
        secondary_application = self.make_application_for_user(
            position=self.level1_position,
            user=second_applicant,
            first_name="Second",
            last_name="Applicant",
            email="second@example.com",
        )

        for application, applicant in (
            (primary_application, self.applicant),
            (secondary_application, second_applicant),
        ):
            self.verify_application_for_submission(application)
            submit_application(application, applicant)
            self.finalize_screening_for_current_stage(application, self.secretariat)
            process_workflow_action(application, self.secretariat, "endorse", "Forward to HRM Chief.")
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
            process_workflow_action(application, self.hrm_chief, "endorse", "Forward to HRMPSB.")

        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)

        report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)
        report.refresh_from_db()

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.recruitment_entry, self.level1_position)
        self.assertIsNotNone(report.evidence_item)
        self.assertEqual(ComparativeAssessmentReport.objects.filter(application=primary_application).count(), 1)
        self.assertEqual(ComparativeAssessmentReportItem.objects.filter(report=report).count(), 2)
        self.assertTrue(
            ComparativeAssessmentReportItem.objects.filter(
                report=report,
                application=secondary_application,
                rank_order=2,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=primary_application,
                action=AuditLog.Action.CAR_FINALIZED,
            ).exists()
        )

    def test_finalized_deliberation_and_car_outputs_are_locked(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        report = self.finalize_car_for_current_stage(application, self.hrmpsb)

        with self.assertRaisesMessage(
            ValueError,
            "Finalized deliberation records are locked and cannot be modified.",
        ):
            save_deliberation_record(
                application=application,
                actor=self.hrmpsb,
                cleaned_data=self.deliberation_payload(
                    ranking_position=1,
                    decision_support_summary="Attempted update after lock.",
                ),
                finalize=False,
            )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized Comparative Assessment Reports are locked and cannot be modified.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.hrmpsb,
                cleaned_data={"summary_notes": "Attempted CAR update after lock."},
                finalize=False,
            )

        report.refresh_from_db()
        self.assertTrue(report.is_finalized)

    def test_unauthorized_user_cannot_record_deliberation(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("deliberation-record", kwargs={"pk": application.pk}),
            {
                "deliberated_at": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "deliberation_minutes": "Unauthorized attempt.",
                "decision_support_summary": "Should be blocked.",
                "ranking_position": "",
                "ranking_notes": "",
                "operation": "save",
            },
        )
        self.assertEqual(response.status_code, 403)


class DecisionApprovalHandlingTests(BaseRecruitmentTestCase):
    def test_appointing_authority_can_record_selected_final_decision_with_preserved_packet(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after reviewing the final submission packet.",
        )
        application.refresh_from_db()

        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertTrue(decision.submission_packet_snapshot["summary"]["ready_for_final_decision"])
        self.assertTrue(
            decision.submission_packet_snapshot["summary"]["has_comparative_assessment_report"]
        )
        self.assertGreaterEqual(
            decision.submission_packet_snapshot["summary"]["evidence_reference_count"],
            1,
        )
        audit_entry = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.DECISION_RECORDED,
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.metadata["final_decision_id"], decision.id)

    def test_appointing_authority_can_record_not_selected_decision_history_after_reopen(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        first_decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.NOT_SELECTED,
            decision_notes="Not selected after initial review.",
        )
        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Rechecking the appointing-authority packet.",
        )
        application.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertFalse(application.case.is_stage_locked)

        second_decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after controlled reopen review.",
        )
        application.refresh_from_db()

        history = list(application.final_decisions.order_by("created_at"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].id, first_decision.id)
        self.assertEqual(history[1].id, second_decision.id)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)

    def test_submission_packet_view_and_final_decision_history_are_visible_on_case_page(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)
        self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after panel review.",
        )

        client = Client()
        client.force_login(self.appointing)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Submission Packet")
        self.assertContains(response, "Decision History")
        self.assertContains(response, "Selected")
        self.assertContains(response, "The pre-decision packet snapshot remains preserved as read-only.")

    def test_only_appointing_authority_can_submit_final_decision(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("final-decision-record", kwargs={"pk": application.pk}),
            {
                "decision_outcome": FinalDecision.Outcome.NOT_SELECTED,
                "decision_notes": "Unauthorized attempt.",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_submission_packet_builder_marks_required_components_ready(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        packet = build_submission_packet(application)

        self.assertTrue(packet["summary"]["ready_for_final_decision"])
        self.assertTrue(packet["summary"]["has_deliberation_record"])
        self.assertFalse(packet["summary"]["missing_components"])
        self.assertGreaterEqual(packet["summary"]["evidence_reference_count"], 1)


class EvidenceVaultTests(BaseRecruitmentTestCase):
    def test_evidence_is_encrypted_and_digest_is_stored(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Personal Data Sheet (CS Form No. 212, Revised 2025) with recent passport-sized picture",
            uploaded_file=SimpleUploadedFile(
                "resume.txt",
                b"plain-text resume",
                content_type="text/plain",
            ),
            document_type="personal_data_sheet",
        )

        evidence = EvidenceVaultItem.objects.get(application=application)
        self.assertNotEqual(bytes(evidence.ciphertext), b"plain-text resume")
        self.assertEqual(len(evidence.sha256_digest), 64)
        self.assertEqual(evidence.document_type, "personal_data_sheet")


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

    def test_export_bundle_returns_zip_with_pdf_and_manifests(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        self.assertIn(f"{application.reference_number}.pdf", names)
        self.assertIn("manifest.json", names)
        self.assertIn("audit_log.csv", names)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXPORT_GENERATED,
            ).exists()
        )
