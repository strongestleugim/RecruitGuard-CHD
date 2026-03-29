import csv
import hashlib
import hmac
import json
import os
import secrets
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

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
from .requirements import APPLICANT_DOCUMENT_REQUIREMENTS_BY_CODE, get_applicant_document_requirements
from .permissions import WORKFLOW_PROCESSOR_ROLES


EXPORT_ROLES = {
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.APPOINTING_AUTHORITY,
}
ENTRY_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.SYSTEM_ADMIN,
}
CASE_REOPEN_ROLES = {
    RecruitmentUser.Role.HRM_CHIEF,
}
SCREENING_REVIEW_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}
SCREENING_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
}
EXAM_REVIEW_ROLES = SCREENING_REVIEW_ROLES
EXAM_STAGES = SCREENING_STAGES
INTERVIEW_SESSION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.HRMPSB_MEMBER,
}
INTERVIEW_SESSION_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    RecruitmentCase.Stage.HRMPSB_REVIEW,
}
INTERVIEW_RATING_ROLES_BY_STAGE = {
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: {RecruitmentUser.Role.HRM_CHIEF},
    RecruitmentCase.Stage.HRMPSB_REVIEW: {RecruitmentUser.Role.HRMPSB_MEMBER},
}
INTERVIEW_FALLBACK_LABEL = "Interview Rating Sheet (Fallback)"
DELIBERATION_STAGES_BY_BRANCH = {
    PositionPosting.Branch.COS: RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    PositionPosting.Branch.PLANTILLA: RecruitmentCase.Stage.HRMPSB_REVIEW,
}
DELIBERATION_ROLES_BY_BRANCH = {
    PositionPosting.Branch.COS: {RecruitmentUser.Role.HRM_CHIEF},
    PositionPosting.Branch.PLANTILLA: {RecruitmentUser.Role.HRMPSB_MEMBER},
}
CAR_REVIEW_STAGE = RecruitmentCase.Stage.HRMPSB_REVIEW
CAR_MANAGER_ROLES = {RecruitmentUser.Role.HRMPSB_MEMBER}
CAR_LABEL = "Comparative Assessment Report"
FINAL_DECISION_OUTCOME_TO_STATUS = {
    FinalDecision.Outcome.SELECTED: RecruitmentApplication.Status.APPROVED,
    FinalDecision.Outcome.NOT_SELECTED: RecruitmentApplication.Status.REJECTED,
}


def record_audit_event(application, actor, action, description, metadata=None):
    return AuditLog.objects.create(
        application=application,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        action=action,
        description=description,
        metadata=metadata or {},
    )


def record_system_audit_event(actor, action, description, metadata=None):
    return record_audit_event(
        application=None,
        actor=actor,
        action=action,
        description=description,
        metadata=metadata,
    )


def record_routing_history_event(
    application,
    actor,
    route_type,
    description,
    *,
    recruitment_case=None,
    from_handler_role="",
    to_handler_role="",
    from_status="",
    to_status="",
    from_stage="",
    to_stage="",
    notes="",
    is_override=False,
):
    return RoutingHistory.objects.create(
        application=application,
        recruitment_case=recruitment_case,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        branch=application.branch,
        level=application.level,
        route_type=route_type,
        from_handler_role=from_handler_role,
        to_handler_role=to_handler_role,
        from_status=from_status,
        to_status=to_status,
        from_stage=from_stage,
        to_stage=to_stage,
        description=description,
        notes=notes,
        is_override=is_override,
    )


def get_visible_positions_for_user(user):
    if user.role == RecruitmentUser.Role.APPLICANT:
        entries = PositionPosting.objects.filter(
            status=PositionPosting.EntryStatus.ACTIVE,
        ).select_related("position_reference")
        return [entry for entry in entries if entry.is_open_for_intake]
    return PositionPosting.objects.select_related("position_reference").all()


def get_queue_for_user(user):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return RecruitmentApplication.objects.none()
    queryset = RecruitmentApplication.objects.select_related("applicant", "position", "case")
    queryset = queryset.filter(current_handler_role=user.role)
    if user.role == RecruitmentUser.Role.SECRETARIAT:
        queryset = queryset.filter(
            Q(level=PositionPosting.Level.LEVEL_1)
            | Q(
                overrides__is_active=True,
                overrides__target_role=RecruitmentUser.Role.SECRETARIAT,
            )
        ).distinct()
    return queryset


def get_manageable_positions(user):
    if user.role not in ENTRY_MANAGER_ROLES:
        return Position.objects.none()
    return Position.objects.all().order_by("title", "position_code")


def get_manageable_recruitment_entries(user):
    if user.role not in ENTRY_MANAGER_ROLES:
        return PositionPosting.objects.none()
    return PositionPosting.objects.select_related(
        "position_reference",
        "created_by",
        "updated_by",
    ).order_by("-updated_at")


def user_can_view_application(user, application):
    if user.role == RecruitmentUser.Role.APPLICANT:
        return application.applicant_id == user.id
    if application.current_handler_role == user.role:
        if (
            user.role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            return application.active_secretariat_override is not None
        return True
    if application.status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    } and user.role in EXPORT_ROLES:
        return True
    return False


def get_effective_role_for_action(user, application):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return ""
    return user.role


def user_can_process_application(user, application):
    case = getattr(application, "case", None)
    effective_role = get_effective_role_for_action(user, application)
    if not effective_role:
        return False
    expected_role = case.current_handler_role if case else application.current_handler_role
    if effective_role != expected_role:
        return False
    if case and case.is_stage_locked:
        return False
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return application.active_secretariat_override is not None
    return effective_role in WORKFLOW_PROCESSOR_ROLES


def user_can_upload_evidence(user, application):
    if user.role == RecruitmentUser.Role.APPLICANT and application.applicant_id == user.id:
        return application.is_editable_by_applicant
    return user_can_process_application(user, application)


def generate_submission_hash(application):
    payload = "|".join(
        [
            application.reference_number or "pending-reference",
            str(application.applicant_id),
            str(application.position_id),
            application.status,
            timezone.now().isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate_application_reference():
    return f"RG-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def _review_stage_from_application_status(application_status):
    stage_map = {
        RecruitmentApplication.Status.SECRETARIAT_REVIEW: RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        RecruitmentApplication.Status.HRM_CHIEF_REVIEW: RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        RecruitmentApplication.Status.HRMPSB_REVIEW: RecruitmentCase.Stage.HRMPSB_REVIEW,
        RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
    }
    return stage_map.get(application_status, "")


def _application_status_from_stage(stage):
    status_map = {
        RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentApplication.Status.SECRETARIAT_REVIEW,
        RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
        RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentApplication.Status.HRMPSB_REVIEW,
        RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
    }
    return status_map.get(stage, "")


def _handler_role_from_stage(stage):
    role_map = {
        RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentUser.Role.SECRETARIAT,
        RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
        RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: RecruitmentUser.Role.APPOINTING_AUTHORITY,
    }
    return role_map.get(stage, "")


def _case_timeline_metadata(case):
    return {
        "case_stage": case.current_stage,
        "case_status": case.case_status,
        "case_handler_role": case.current_handler_role,
        "case_locked": case.is_stage_locked,
    }


def _ensure_case_stage_alignment(application, case):
    expected_stage = _review_stage_from_application_status(application.status)
    if expected_stage and case.current_stage != expected_stage:
        raise ValueError("The recruitment case is out of sync with the application workflow state.")


def get_case_timeline(application):
    return application.audit_logs.filter(
        action__in=[
            AuditLog.Action.APPLICATION_SUBMITTED,
            AuditLog.Action.CASE_CREATED,
            AuditLog.Action.CASE_REOPENED,
            AuditLog.Action.ROUTED,
            AuditLog.Action.SCREENING_RECORDED,
            AuditLog.Action.SCREENING_FINALIZED,
            AuditLog.Action.EXAM_RECORDED,
            AuditLog.Action.EXAM_FINALIZED,
            AuditLog.Action.INTERVIEW_SCHEDULED,
            AuditLog.Action.INTERVIEW_FINALIZED,
            AuditLog.Action.INTERVIEW_RATING_RECORDED,
            AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
            AuditLog.Action.DELIBERATION_RECORDED,
            AuditLog.Action.DELIBERATION_FINALIZED,
            AuditLog.Action.CAR_GENERATED,
            AuditLog.Action.CAR_FINALIZED,
            AuditLog.Action.DECISION_RECORDED,
            AuditLog.Action.OVERRIDE_GRANTED,
            AuditLog.Action.OVERRIDE_USED,
        ]
    ).order_by("created_at")


def user_can_reopen_case(user, case):
    return bool(
        case
        and user.role in CASE_REOPEN_ROLES
        and case.is_stage_locked
        and case.locked_stage
    )


def get_current_review_stage(application):
    case = getattr(application, "case", None)
    if case:
        return case.current_stage
    return _review_stage_from_application_status(application.status)


def get_screening_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).filter(review_stage=review_stage).first()


def get_screening_records(application):
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).order_by("created_at")


def user_can_manage_screening(user, application):
    current_stage = get_current_review_stage(application)
    if user.role not in SCREENING_REVIEW_ROLES or current_stage not in SCREENING_STAGES:
        return False
    return user_can_process_application(user, application)


def screening_is_finalized_for_current_stage(application):
    screening_record = get_screening_record(application)
    return bool(screening_record and screening_record.is_finalized)


def get_exam_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
    ).filter(review_stage=review_stage).first()


def get_exam_records(application):
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
    ).order_by("created_at")


def user_can_manage_exam(user, application):
    current_stage = get_current_review_stage(application)
    if user.role not in EXAM_REVIEW_ROLES or current_stage not in EXAM_STAGES:
        return False
    return user_can_process_application(user, application)


def get_interview_session(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.interview_sessions.select_related(
        "scheduled_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def get_interview_fallback_evidence(application, stage=None):
    queryset = application.evidence_items.select_related(
        "uploaded_by",
        "recruitment_case",
    ).filter(
        label__startswith=INTERVIEW_FALLBACK_LABEL,
    )
    case = getattr(application, "case", None)
    if case:
        queryset = queryset.filter(recruitment_case=case)
    if stage:
        queryset = queryset.filter(workflow_stage=stage)
    return queryset.order_by("created_at")


def get_interview_sessions(application):
    sessions = list(
        application.interview_sessions.select_related(
            "scheduled_by",
            "finalized_by",
            "recruitment_case",
            "recruitment_entry",
        ).prefetch_related(
            "ratings__rated_by",
        ).order_by("created_at")
    )
    fallback_items = list(get_interview_fallback_evidence(application))
    fallback_by_stage = {}
    for item in fallback_items:
        fallback_by_stage.setdefault(item.workflow_stage, []).append(item)
    for session in sessions:
        session.fallback_evidence_items = fallback_by_stage.get(session.review_stage, [])
    return sessions


def get_interview_ratings(application, stage=None):
    interview_session = get_interview_session(application, stage=stage)
    if not interview_session:
        return InterviewRating.objects.none()
    return interview_session.ratings.select_related("rated_by").order_by("created_at")


def get_interview_rating_for_user(application, user, stage=None):
    interview_session = get_interview_session(application, stage=stage)
    if not interview_session:
        return None
    return interview_session.ratings.select_related("rated_by").filter(rated_by=user).first()


def user_can_manage_interview_session(user, application):
    current_stage = get_current_review_stage(application)
    if user.role not in INTERVIEW_SESSION_MANAGER_ROLES or current_stage not in INTERVIEW_SESSION_STAGES:
        return False
    return user_can_process_application(user, application)


def user_can_manage_interview_rating(user, application):
    current_stage = get_current_review_stage(application)
    if user.role not in INTERVIEW_RATING_ROLES_BY_STAGE.get(current_stage, set()):
        return False
    return user_can_process_application(user, application)


def user_can_upload_interview_fallback(user, application):
    current_stage = get_current_review_stage(application)
    if user.role not in INTERVIEW_SESSION_MANAGER_ROLES or current_stage not in INTERVIEW_SESSION_STAGES:
        return False
    return user_can_process_application(user, application)


def _decimal_string(value):
    if value is None:
        return ""
    return str(value)


def _average_interview_rating(interview_session):
    ratings = list(interview_session.ratings.all())
    if not ratings:
        return None
    total = sum((rating.rating_score for rating in ratings), Decimal("0"))
    return (total / Decimal(len(ratings))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _deliberation_snapshot_for_screening(screening_record):
    return {
        "id": screening_record.id,
        "review_stage": screening_record.review_stage,
        "completeness_status": screening_record.completeness_status,
        "qualification_outcome": screening_record.qualification_outcome,
        "finalized_at": screening_record.finalized_at.isoformat() if screening_record.finalized_at else "",
    }


def _deliberation_snapshot_for_exam(exam_record):
    return {
        "id": exam_record.id,
        "review_stage": exam_record.review_stage,
        "exam_type": exam_record.exam_type,
        "exam_status": exam_record.exam_status,
        "exam_score": _decimal_string(exam_record.exam_score),
        "exam_result": exam_record.exam_result,
        "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
        "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
        "finalized_at": exam_record.finalized_at.isoformat() if exam_record.finalized_at else "",
    }


def _deliberation_snapshot_for_interview(application, interview_session):
    average_score = _average_interview_rating(interview_session)
    fallback_count = get_interview_fallback_evidence(application, stage=interview_session.review_stage).count()
    return {
        "id": interview_session.id,
        "review_stage": interview_session.review_stage,
        "scheduled_for": interview_session.scheduled_for.isoformat(),
        "rating_count": interview_session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": _decimal_string(average_score),
        "finalized_at": interview_session.finalized_at.isoformat() if interview_session.finalized_at else "",
    }


def build_deliberation_consolidation(application):
    screening_records = list(
        application.screening_records.filter(is_finalized=True).select_related(
            "reviewed_by",
            "finalized_by",
        ).order_by("created_at")
    )
    exam_records = list(
        application.exam_records.filter(is_finalized=True).select_related(
            "recorded_by",
            "finalized_by",
        ).order_by("created_at")
    )
    interview_sessions = list(
        application.interview_sessions.filter(is_finalized=True).select_related(
            "scheduled_by",
            "finalized_by",
        ).prefetch_related("ratings").order_by("created_at")
    )
    latest_screening = screening_records[-1] if screening_records else None
    latest_exam = exam_records[-1] if exam_records else None
    latest_interview = interview_sessions[-1] if interview_sessions else None
    latest_interview_average = _average_interview_rating(latest_interview) if latest_interview else None
    return {
        "application_reference": application.reference_number or "",
        "entry_code": application.position.job_code,
        "branch": application.branch,
        "level": application.level,
        "generated_at": timezone.now().isoformat(),
        "screening_records": [_deliberation_snapshot_for_screening(item) for item in screening_records],
        "exam_records": [_deliberation_snapshot_for_exam(item) for item in exam_records],
        "interview_sessions": [
            _deliberation_snapshot_for_interview(application, item) for item in interview_sessions
        ],
        "summary": {
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "latest_qualification_outcome": (
                latest_screening.qualification_outcome if latest_screening else ""
            ),
            "latest_exam_status": latest_exam.exam_status if latest_exam else "",
            "latest_exam_score": _decimal_string(latest_exam.exam_score if latest_exam else None),
            "latest_interview_average": _decimal_string(latest_interview_average),
        },
    }


def get_deliberation_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def get_deliberation_records(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("created_at")


def get_comparative_assessment_report(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.comparative_assessment_reports.select_related(
        "generated_by",
        "finalized_by",
        "evidence_item",
    ).filter(review_stage=review_stage).first()


def get_latest_finalized_deliberation_record(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(is_finalized=True).order_by("-finalized_at", "-created_at").first()


def get_latest_finalized_comparative_assessment_report(application):
    return application.comparative_assessment_reports.select_related(
        "generated_by",
        "finalized_by",
        "evidence_item",
    ).filter(is_finalized=True).order_by("-finalized_at", "-updated_at", "-created_at").first()


def get_comparative_assessment_report_items_for_report(report):
    if not report:
        return ComparativeAssessmentReportItem.objects.none()
    return report.items.select_related(
        "application",
        "recruitment_case",
        "deliberation_record",
    ).order_by("rank_order", "created_at")


def get_comparative_assessment_report_items(application, stage=None):
    report = get_comparative_assessment_report(application, stage=stage)
    return get_comparative_assessment_report_items_for_report(report)


def get_final_decision_history(application):
    return application.final_decisions.select_related(
        "decided_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("-decided_at", "-created_at")


def get_latest_final_decision(application):
    return get_final_decision_history(application).first()


def user_can_manage_deliberation(user, application):
    current_stage = get_current_review_stage(application)
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch, "")
    allowed_roles = DELIBERATION_ROLES_BY_BRANCH.get(application.branch, set())
    if current_stage != expected_stage or user.role not in allowed_roles:
        return False
    return user_can_process_application(user, application)


def user_can_manage_comparative_assessment_report(user, application):
    current_stage = get_current_review_stage(application)
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or current_stage != CAR_REVIEW_STAGE
        or user.role not in CAR_MANAGER_ROLES
    ):
        return False
    return user_can_process_application(user, application)


def user_can_record_final_decision(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role != RecruitmentUser.Role.APPOINTING_AUTHORITY
        or current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
    ):
        return False
    return user_can_process_application(user, application)


def _decision_packet_screening_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "reviewed_by": str(record.reviewed_by) if record.reviewed_by else "",
        "reviewed_by_role": record.reviewed_by_role,
        "completeness_status": record.completeness_status,
        "completeness_status_label": record.get_completeness_status_display(),
        "qualification_outcome": record.qualification_outcome,
        "qualification_outcome_label": record.get_qualification_outcome_display(),
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_exam_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "exam_type": record.exam_type,
        "exam_status": record.exam_status,
        "exam_status_label": record.get_exam_status_display(),
        "exam_score": _decimal_string(record.exam_score),
        "exam_result": record.exam_result,
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_interview_session(application, session):
    average_score = _average_interview_rating(session)
    fallback_count = get_interview_fallback_evidence(application, stage=session.review_stage).count()
    return {
        "id": session.id,
        "review_stage": session.review_stage,
        "review_stage_label": session.get_review_stage_display(),
        "scheduled_by": str(session.scheduled_by) if session.scheduled_by else "",
        "scheduled_by_role": session.scheduled_by_role,
        "scheduled_for": session.scheduled_for.isoformat(),
        "location": session.location,
        "rating_count": session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": _decimal_string(average_score),
        "finalized_at": session.finalized_at.isoformat() if session.finalized_at else "",
        "is_read_only": session.is_finalized,
    }


def _decision_packet_deliberation_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "deliberated_at": record.deliberated_at.isoformat(),
        "decision_support_summary": record.decision_support_summary,
        "ranking_position": record.ranking_position,
        "ranking_notes": record.ranking_notes,
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "finalized_screening_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_screening_count",
            0,
        ),
        "finalized_exam_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_exam_count",
            0,
        ),
        "finalized_interview_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_interview_count",
            0,
        ),
        "latest_interview_average": record.consolidated_snapshot.get("summary", {}).get(
            "latest_interview_average",
            "",
        ),
        "is_read_only": record.is_finalized,
    }


def _decision_packet_car_item(item):
    return {
        "id": item.id,
        "rank_order": item.rank_order,
        "application_id": item.application_id,
        "application_reference": item.application.reference_number or "",
        "applicant_name": item.application.applicant_display_name,
        "qualification_outcome": item.qualification_outcome,
        "exam_status": item.exam_status,
        "exam_score": _decimal_string(item.exam_score),
        "interview_average_score": _decimal_string(item.interview_average_score),
        "decision_support_summary": item.decision_support_summary,
    }


def _decision_packet_car_report(report):
    items = list(get_comparative_assessment_report_items_for_report(report))
    evidence = report.evidence_item
    return {
        "id": report.id,
        "review_stage": report.review_stage,
        "review_stage_label": report.get_review_stage_display(),
        "generated_by": str(report.generated_by) if report.generated_by else "",
        "generated_by_role": report.generated_by_role,
        "summary_notes": report.summary_notes,
        "generation_count": report.generation_count,
        "candidate_count": len(items),
        "finalized_at": report.finalized_at.isoformat() if report.finalized_at else "",
        "evidence_item": (
            {
                "id": evidence.id,
                "label": evidence.label,
                "original_filename": evidence.original_filename,
                "sha256_digest": evidence.sha256_digest,
            }
            if evidence
            else {}
        ),
        "items": [_decision_packet_car_item(item) for item in items],
        "is_read_only": report.is_finalized,
    }


def _decision_packet_evidence_reference(evidence):
    return {
        "id": evidence.id,
        "label": evidence.label,
        "document_type": evidence.document_type,
        "document_type_label": evidence.get_document_type_display() if evidence.document_type else "",
        "workflow_stage": evidence.workflow_stage,
        "workflow_stage_label": evidence.get_workflow_stage_display() if evidence.workflow_stage else "",
        "original_filename": evidence.original_filename,
        "sha256_digest": evidence.sha256_digest,
        "uploaded_at": evidence.created_at.isoformat(),
    }


def build_submission_packet(application):
    case = getattr(application, "case", None)
    screening_records = list(
        application.screening_records.filter(is_finalized=True).select_related(
            "reviewed_by",
            "finalized_by",
        ).order_by("created_at")
    )
    exam_records = list(
        application.exam_records.filter(is_finalized=True).select_related(
            "recorded_by",
            "finalized_by",
        ).order_by("created_at")
    )
    interview_sessions = list(
        application.interview_sessions.filter(is_finalized=True).select_related(
            "scheduled_by",
            "finalized_by",
        ).prefetch_related("ratings").order_by("created_at")
    )
    deliberation_record = get_latest_finalized_deliberation_record(application)
    comparative_assessment_report = get_latest_finalized_comparative_assessment_report(application)
    evidence_items = list(
        application.evidence_items.select_related(
            "uploaded_by",
            "recruitment_case",
        ).order_by("created_at")
    )

    missing_components = []
    if not deliberation_record:
        missing_components.append("Finalized deliberation record")
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and not comparative_assessment_report
    ):
        missing_components.append("Finalized Comparative Assessment Report")

    preserved_artifact_ids = {
        "screening_record_ids": [record.id for record in screening_records],
        "exam_record_ids": [record.id for record in exam_records],
        "interview_session_ids": [session.id for session in interview_sessions],
        "deliberation_record_ids": [deliberation_record.id] if deliberation_record else [],
        "comparative_assessment_report_ids": (
            [comparative_assessment_report.id] if comparative_assessment_report else []
        ),
        "evidence_item_ids": [item.id for item in evidence_items],
    }

    return {
        "context": {
            "built_at": timezone.now().isoformat(),
            "application_reference": application.reference_number or "",
            "application_id": application.id,
            "branch": application.branch,
            "branch_label": application.get_branch_display(),
            "level": application.level,
            "level_label": application.get_level_display(),
            "applicant_name": application.applicant_display_name,
            "applicant_email": application.applicant_email,
            "recruitment_entry_code": application.position.job_code,
            "recruitment_entry_title": application.position.title,
            "current_stage": getattr(case, "current_stage", ""),
            "current_stage_label": case.get_current_stage_display() if case else "",
            "case_status": getattr(case, "case_status", ""),
            "case_status_label": case.get_case_status_display() if case else "",
            "current_handler_role": getattr(case, "current_handler_role", ""),
            "locked_stage": getattr(case, "locked_stage", ""),
            "is_stage_locked": getattr(case, "is_stage_locked", False),
        },
        "summary": {
            "ready_for_final_decision": not missing_components,
            "missing_components": missing_components,
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "evidence_reference_count": len(evidence_items),
            "has_deliberation_record": bool(deliberation_record),
            "has_comparative_assessment_report": bool(comparative_assessment_report),
        },
        "screening_records": [_decision_packet_screening_record(record) for record in screening_records],
        "exam_records": [_decision_packet_exam_record(record) for record in exam_records],
        "interview_sessions": [
            _decision_packet_interview_session(application, session) for session in interview_sessions
        ],
        "deliberation_record": (
            _decision_packet_deliberation_record(deliberation_record) if deliberation_record else {}
        ),
        "comparative_assessment_report": (
            _decision_packet_car_report(comparative_assessment_report)
            if comparative_assessment_report
            else {}
        ),
        "evidence_references": [
            _decision_packet_evidence_reference(item) for item in evidence_items
        ],
        "preserved_artifact_ids": preserved_artifact_ids,
    }


def save_screening_review(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before screening can be recorded.")
    if not user_can_manage_screening(actor, application):
        raise ValueError("You cannot record screening for this application at its current workflow stage.")

    review_stage = application.case.current_stage
    if review_stage not in SCREENING_STAGES:
        raise ValueError("Screening is only available during Secretariat or HRM Chief review stages.")

    screening_record = get_screening_record(application, stage=review_stage)
    if screening_record and screening_record.is_finalized:
        raise ValueError("Finalized screening outputs are locked and cannot be modified.")

    created = screening_record is None
    if screening_record is None:
        screening_record = ScreeningRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            reviewed_by=actor,
            branch=application.branch,
            level=application.level,
        )

    screening_record.recruitment_case = application.case
    screening_record.reviewed_by = actor
    screening_record.completeness_status = cleaned_data["completeness_status"]
    screening_record.completeness_notes = cleaned_data["completeness_notes"]
    screening_record.qualification_outcome = cleaned_data["qualification_outcome"]
    screening_record.screening_notes = cleaned_data["screening_notes"]
    screening_record.is_finalized = finalize
    if finalize:
        screening_record.finalized_by = actor
        screening_record.finalized_at = timezone.now()
    else:
        screening_record.finalized_by = None
        screening_record.finalized_at = None
    screening_record.full_clean()
    screening_record.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.SCREENING_FINALIZED
            if finalize
            else AuditLog.Action.SCREENING_RECORDED
        ),
        description=(
            "Finalized screening output."
            if finalize
            else "Saved screening review."
        ),
        metadata={
            "screening_record_id": screening_record.id,
            "created": created,
            "review_stage": review_stage,
            "completeness_status": screening_record.completeness_status,
            "qualification_outcome": screening_record.qualification_outcome,
            "is_finalized": screening_record.is_finalized,
        },
    )
    return screening_record


def save_exam_record(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before examination data can be recorded.")
    if not user_can_manage_exam(actor, application):
        raise ValueError(
            "You cannot record examination data for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    if review_stage not in EXAM_STAGES:
        raise ValueError("Examination records are only available during Secretariat or HRM Chief review stages.")

    exam_record = get_exam_record(application, stage=review_stage)
    if exam_record and exam_record.is_finalized:
        raise ValueError("Finalized examination outputs are locked and cannot be modified.")

    created = exam_record is None
    if exam_record is None:
        exam_record = ExamRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            recorded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    exam_record.recruitment_case = application.case
    exam_record.recorded_by = actor
    exam_record.exam_type = cleaned_data["exam_type"]
    exam_record.exam_status = cleaned_data["exam_status"]
    exam_record.exam_score = cleaned_data["exam_score"]
    exam_record.exam_result = cleaned_data["exam_result"]
    exam_record.valid_from = cleaned_data["valid_from"]
    exam_record.valid_until = cleaned_data["valid_until"]
    exam_record.exam_notes = cleaned_data["exam_notes"]
    exam_record.is_finalized = finalize
    if finalize:
        exam_record.finalized_by = actor
        exam_record.finalized_at = timezone.now()
    else:
        exam_record.finalized_by = None
        exam_record.finalized_at = None
    exam_record.full_clean()
    exam_record.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.EXAM_FINALIZED
            if finalize
            else AuditLog.Action.EXAM_RECORDED
        ),
        description=(
            "Finalized examination output."
            if finalize
            else "Saved examination record."
        ),
        metadata={
            "exam_record_id": exam_record.id,
            "created": created,
            "review_stage": review_stage,
            "exam_type": exam_record.exam_type,
            "exam_status": exam_record.exam_status,
            "exam_score": str(exam_record.exam_score) if exam_record.exam_score is not None else "",
            "exam_result": exam_record.exam_result,
            "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
            "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
            "is_finalized": exam_record.is_finalized,
        },
    )
    return exam_record


@transaction.atomic
def save_interview_session(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before interview scheduling can be recorded.")
    if not user_can_manage_interview_session(actor, application):
        raise ValueError(
            "You cannot manage interview scheduling for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    if review_stage not in INTERVIEW_SESSION_STAGES:
        raise ValueError(
            "Interview sessions are only available during Secretariat, HRM Chief, or HRMPSB review stages."
        )

    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot be modified.")

    created = interview_session is None
    if interview_session is None:
        interview_session = InterviewSession(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            scheduled_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_session.recruitment_case = application.case
    interview_session.recruitment_entry = application.position
    interview_session.scheduled_by = actor
    interview_session.scheduled_for = cleaned_data["scheduled_for"]
    interview_session.location = cleaned_data["location"]
    interview_session.session_notes = cleaned_data["session_notes"]

    existing_rating_count = interview_session.ratings.count() if interview_session.pk else 0
    fallback_count = get_interview_fallback_evidence(application, stage=review_stage).count()
    if finalize and existing_rating_count == 0 and fallback_count == 0:
        raise ValueError(
            "Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session."
        )

    interview_session.is_finalized = finalize
    if finalize:
        interview_session.finalized_by = actor
        interview_session.finalized_at = timezone.now()
    else:
        interview_session.finalized_by = None
        interview_session.finalized_at = None
    interview_session.full_clean()
    interview_session.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.INTERVIEW_FINALIZED
            if finalize
            else AuditLog.Action.INTERVIEW_SCHEDULED
        ),
        description=(
            "Finalized interview session output."
            if finalize
            else "Saved interview session schedule."
        ),
        metadata={
            "interview_session_id": interview_session.id,
            "created": created,
            "review_stage": review_stage,
            "scheduled_for": interview_session.scheduled_for.isoformat(),
            "location": interview_session.location,
            "rating_count": existing_rating_count,
            "fallback_count": fallback_count,
            "is_finalized": interview_session.is_finalized,
        },
    )
    return interview_session


@transaction.atomic
def save_interview_rating(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before interview ratings can be recorded.")
    if not user_can_manage_interview_rating(actor, application):
        raise ValueError(
            "You cannot record interview ratings for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if not interview_session:
        raise ValueError("Schedule the interview session before recording interview ratings.")
    if interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot accept rating changes.")

    interview_rating = interview_session.ratings.filter(rated_by=actor).first()
    created = interview_rating is None
    if interview_rating is None:
        interview_rating = InterviewRating(
            interview_session=interview_session,
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            rated_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_rating.rating_score = cleaned_data["rating_score"]
    interview_rating.rating_notes = cleaned_data["rating_notes"]
    interview_rating.justification = cleaned_data["justification"]
    interview_rating.full_clean()
    interview_rating.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
        description="Recorded interview rating.",
        metadata={
            "interview_session_id": interview_session.id,
            "interview_rating_id": interview_rating.id,
            "created": created,
            "review_stage": review_stage,
            "rating_score": str(interview_rating.rating_score),
            "has_justification": bool(interview_rating.justification),
        },
    )
    return interview_rating


@transaction.atomic
def upload_interview_fallback_rating(application, actor, uploaded_file, remarks):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before fallback interview ratings can be uploaded.")
    if not user_can_upload_interview_fallback(actor, application):
        raise ValueError(
            "You cannot upload a fallback interview rating sheet for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if not interview_session:
        raise ValueError("Schedule the interview session before uploading a fallback rating sheet.")
    if interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot accept fallback rating uploads.")

    evidence = upload_evidence_item(
        application=application,
        actor=actor,
        label=f"{INTERVIEW_FALLBACK_LABEL} - {application.case.get_current_stage_display()}",
        uploaded_file=uploaded_file,
        recruitment_case=application.case,
        workflow_stage=review_stage,
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
        description="Uploaded fallback interview rating sheet.",
        metadata={
            "interview_session_id": interview_session.id,
            "evidence_id": evidence.id,
            "review_stage": review_stage,
            "remarks": remarks,
            "filename": evidence.original_filename,
        },
    )
    return evidence


def _finalized_deliberation_queryset_for_entry(recruitment_entry, review_stage):
    return DeliberationRecord.objects.filter(
        recruitment_entry=recruitment_entry,
        review_stage=review_stage,
        is_finalized=True,
    ).select_related(
        "application",
        "recruitment_case",
        "recruitment_entry",
        "recorded_by",
        "finalized_by",
    ).order_by("ranking_position", "application__reference_number")


@transaction.atomic
def save_deliberation_record(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before deliberation data can be recorded.")
    if not user_can_manage_deliberation(actor, application):
        raise ValueError(
            "You cannot manage deliberation records for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch, "")
    if review_stage != expected_stage:
        raise ValueError(
            "Deliberation is only available during the branch-appropriate decision-support stage."
        )

    deliberation_record = get_deliberation_record(application, stage=review_stage)
    if deliberation_record and deliberation_record.is_finalized:
        raise ValueError("Finalized deliberation records are locked and cannot be modified.")

    created = deliberation_record is None
    if deliberation_record is None:
        deliberation_record = DeliberationRecord(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            recorded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    current_screening_record = get_screening_record(application, stage=review_stage)
    current_exam_record = get_exam_record(application, stage=review_stage)
    current_interview_session = get_interview_session(application, stage=review_stage)
    if (
        finalize
        and review_stage in SCREENING_STAGES
        and (not current_screening_record or not current_screening_record.is_finalized)
    ):
        raise ValueError("Finalize the screening record before finalizing the deliberation record.")
    if finalize and current_exam_record and not current_exam_record.is_finalized:
        raise ValueError("Finalize the examination record before finalizing the deliberation record.")
    if finalize and current_interview_session and not current_interview_session.is_finalized:
        raise ValueError("Finalize the interview session before finalizing the deliberation record.")

    consolidated_snapshot = build_deliberation_consolidation(application)
    consolidated_source_count = (
        len(consolidated_snapshot["screening_records"])
        + len(consolidated_snapshot["exam_records"])
        + len(consolidated_snapshot["interview_sessions"])
    )
    if finalize and consolidated_source_count == 0:
        raise ValueError(
            "Finalize at least one screening, examination, or interview output before finalizing deliberation."
        )
    if (
        finalize
        and application.branch == PositionPosting.Branch.PLANTILLA
        and not cleaned_data.get("ranking_position")
    ):
        raise ValueError(
            "Record the ranking position before finalizing the Plantilla deliberation record."
        )

    deliberation_record.recruitment_case = application.case
    deliberation_record.recruitment_entry = application.position
    deliberation_record.recorded_by = actor
    deliberation_record.deliberated_at = cleaned_data["deliberated_at"]
    deliberation_record.deliberation_minutes = cleaned_data["deliberation_minutes"]
    deliberation_record.decision_support_summary = cleaned_data["decision_support_summary"]
    deliberation_record.ranking_position = cleaned_data["ranking_position"]
    deliberation_record.ranking_notes = cleaned_data["ranking_notes"]
    deliberation_record.consolidated_snapshot = consolidated_snapshot
    deliberation_record.is_finalized = finalize
    if finalize:
        deliberation_record.finalized_by = actor
        deliberation_record.finalized_at = timezone.now()
    else:
        deliberation_record.finalized_by = None
        deliberation_record.finalized_at = None
    deliberation_record.full_clean()
    deliberation_record.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.DELIBERATION_FINALIZED
            if finalize
            else AuditLog.Action.DELIBERATION_RECORDED
        ),
        description=(
            "Finalized deliberation and decision-support record."
            if finalize
            else "Saved deliberation and decision-support record."
        ),
        metadata={
            "deliberation_record_id": deliberation_record.id,
            "created": created,
            "review_stage": review_stage,
            "ranking_position": deliberation_record.ranking_position,
            "finalized_screening_count": len(consolidated_snapshot["screening_records"]),
            "finalized_exam_count": len(consolidated_snapshot["exam_records"]),
            "finalized_interview_count": len(consolidated_snapshot["interview_sessions"]),
            "is_finalized": deliberation_record.is_finalized,
        },
    )
    return deliberation_record


def _car_candidate_rows(recruitment_entry, review_stage):
    rows = []
    deliberation_records = list(_finalized_deliberation_queryset_for_entry(recruitment_entry, review_stage))
    if not deliberation_records:
        raise ValueError(
            "Finalize at least one Plantilla deliberation record before generating the Comparative Assessment Report."
        )
    if any(record.ranking_position is None for record in deliberation_records):
        raise ValueError(
            "All finalized Plantilla deliberation records for this recruitment entry must include a ranking position before CAR generation."
        )
    rank_positions = [record.ranking_position for record in deliberation_records]
    if len(rank_positions) != len(set(rank_positions)):
        raise ValueError(
            "Comparative Assessment Report generation requires unique ranking positions within the same recruitment entry."
        )

    for record in deliberation_records:
        summary = record.consolidated_snapshot.get("summary", {})
        rows.append(
            {
                "application": record.application,
                "recruitment_case": record.recruitment_case,
                "deliberation_record": record,
                "rank_order": record.ranking_position,
                "qualification_outcome": summary.get("latest_qualification_outcome", ""),
                "exam_status": summary.get("latest_exam_status", ""),
                "exam_score": summary.get("latest_exam_score", ""),
                "interview_average_score": summary.get("latest_interview_average", ""),
                "decision_support_summary": record.decision_support_summary,
            }
        )
    return sorted(rows, key=lambda row: (row["rank_order"], row["application"].reference_number or ""))


@transaction.atomic
def generate_comparative_assessment_report(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before a Comparative Assessment Report can be generated.")
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "You cannot generate the Comparative Assessment Report for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    if review_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError("Comparative Assessment Report generation is only available for Plantilla cases at the HRMPSB stage.")

    deliberation_record = get_deliberation_record(application, stage=review_stage)
    if not deliberation_record or not deliberation_record.is_finalized:
        raise ValueError(
            "Finalize the deliberation record before generating the Comparative Assessment Report."
        )

    report = get_comparative_assessment_report(application, stage=review_stage)
    if report and report.is_finalized:
        raise ValueError("Finalized Comparative Assessment Reports are locked and cannot be modified.")

    candidate_rows = _car_candidate_rows(application.position, review_stage)
    generation_number = (report.generation_count + 1) if report else 1
    consolidated_snapshot = {
        "generated_at": timezone.now().isoformat(),
        "entry_code": application.position.job_code,
        "review_stage": review_stage,
        "candidate_count": len(candidate_rows),
        "ranked_candidates": [
            {
                "rank_order": row["rank_order"],
                "application_reference": row["application"].reference_number or "",
                "applicant_name": row["application"].applicant_display_name,
                "qualification_outcome": row["qualification_outcome"],
                "exam_status": row["exam_status"],
                "exam_score": row["exam_score"],
                "interview_average_score": row["interview_average_score"],
            }
            for row in candidate_rows
        ],
    }

    pdf_bytes = _build_comparative_assessment_report_pdf(
        application=application,
        actor=actor,
        candidate_rows=candidate_rows,
        generation_number=generation_number,
        summary_notes=cleaned_data["summary_notes"],
    )
    evidence = store_generated_evidence_item(
        application=application,
        actor=actor,
        label=f"{CAR_LABEL} - {application.case.get_current_stage_display()}",
        filename=f"{application.position.job_code.lower()}-{review_stage.replace('_', '-')}-car-v{generation_number}.pdf",
        raw_bytes=pdf_bytes,
        content_type="application/pdf",
        recruitment_case=application.case,
        workflow_stage=review_stage,
    )

    created = report is None
    if report is None:
        report = ComparativeAssessmentReport(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            generated_by=actor,
            branch=application.branch,
        )

    report.application = application
    report.recruitment_case = application.case
    report.recruitment_entry = application.position
    report.generated_by = actor
    report.summary_notes = cleaned_data["summary_notes"]
    report.consolidated_snapshot = consolidated_snapshot
    report.generation_count = generation_number
    report.evidence_item = evidence
    report.is_finalized = finalize
    if finalize:
        report.finalized_by = actor
        report.finalized_at = timezone.now()
    else:
        report.finalized_by = None
        report.finalized_at = None
    report.full_clean()
    report.save()

    report.items.all().delete()
    for row in candidate_rows:
        item = ComparativeAssessmentReportItem(
            report=report,
            application=row["application"],
            recruitment_case=row["recruitment_case"],
            deliberation_record=row["deliberation_record"],
            rank_order=row["rank_order"],
            qualification_outcome=row["qualification_outcome"],
            exam_status=row["exam_status"],
            exam_score=Decimal(row["exam_score"]) if row["exam_score"] else None,
            interview_average_score=(
                Decimal(row["interview_average_score"]) if row["interview_average_score"] else None
            ),
            decision_support_summary=row["decision_support_summary"],
        )
        item.full_clean()
        item.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CAR_FINALIZED if finalize else AuditLog.Action.CAR_GENERATED,
        description=(
            "Finalized the Comparative Assessment Report."
            if finalize
            else "Generated or updated the Comparative Assessment Report."
        ),
        metadata={
            "car_report_id": report.id,
            "created": created,
            "review_stage": review_stage,
            "generation_count": report.generation_count,
            "candidate_count": len(candidate_rows),
            "evidence_id": evidence.id,
            "is_finalized": report.is_finalized,
        },
    )
    return report


@transaction.atomic
def record_final_decision(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before a final decision can be recorded.")
    if application.case.is_stage_locked:
        raise ValueError("This recruitment case is stage-locked. Use controlled reopen before proceeding.")
    if not user_can_record_final_decision(actor, application):
        raise ValueError(
            "Only the Appointing Authority may record the final decision at the current workflow stage."
        )
    if application.case.current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW:
        raise ValueError(
            "Final decisions may only be recorded during the Appointing Authority review stage."
        )

    submission_packet = build_submission_packet(application)
    missing_components = submission_packet["summary"]["missing_components"]
    if missing_components:
        raise ValueError(
            "Submission packet is incomplete for final decision recording. Missing: "
            + "; ".join(missing_components)
            + "."
        )

    decision = FinalDecision(
        application=application,
        recruitment_case=application.case,
        recruitment_entry=application.position,
        review_stage=RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
        decided_by=actor,
        branch=application.branch,
        level=application.level,
        decision_outcome=cleaned_data["decision_outcome"],
        decision_notes=cleaned_data["decision_notes"],
        submission_packet_snapshot=submission_packet,
    )
    decision.full_clean()
    decision.save()

    next_status = FINAL_DECISION_OUTCOME_TO_STATUS[decision.decision_outcome]
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = _sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role="",
        next_status=next_status,
        remarks=decision.decision_notes,
    )
    application.current_handler_role = ""
    application.status = next_status
    application.closed_at = timezone.now()
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=(
            "Final decision recorded as selected by the Appointing Authority."
            if decision.is_selected
            else "Final decision recorded as not selected by the Appointing Authority."
        ),
        metadata={
            "final_decision_id": decision.id,
            "decision_outcome": decision.decision_outcome,
            "decision_notes": decision.decision_notes,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": "",
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
            "preserved_artifact_ids": submission_packet["preserved_artifact_ids"],
        },
    )
    return decision


def _upsert_recruitment_case_for_submission(application, actor, next_role, next_status):
    next_stage = _review_stage_from_application_status(next_status)
    if not next_stage:
        raise ValueError("Submitted applications must route to a valid internal review stage.")

    case, created = RecruitmentCase.objects.get_or_create(
        application=application,
        defaults={
            "branch": application.branch,
            "current_stage": next_stage,
            "current_handler_role": next_role,
            "case_status": RecruitmentCase.CaseStatus.ACTIVE,
            "is_stage_locked": False,
            "locked_stage": "",
        },
    )
    if not created:
        if case.case_status != RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT:
            raise ValueError("A recruitment case already exists for this application.")
        case.current_stage = next_stage
        case.current_handler_role = next_role
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
        case.reopened_at = timezone.now()
        case.save(
            update_fields=[
                "branch",
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "closed_at",
                "reopened_at",
                "updated_at",
            ]
        )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_CREATED,
        description="Recruitment case created from the finalized application.",
        metadata={"created": created, **_case_timeline_metadata(case)},
    )
    return case


def _sync_case_after_workflow_action(application, actor, next_role, next_status, remarks):
    case = application.case
    _ensure_case_stage_alignment(application, case)
    previous_stage = case.current_stage
    previous_case_status = case.case_status

    next_stage = _review_stage_from_application_status(next_status)
    if next_stage:
        case.current_stage = next_stage
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.current_handler_role = next_role
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
    elif next_status == RecruitmentApplication.Status.RETURNED_TO_APPLICANT:
        case.case_status = RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT
        case.current_handler_role = RecruitmentUser.Role.APPLICANT
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = None
    elif next_status == RecruitmentApplication.Status.APPROVED:
        case.current_stage = RecruitmentCase.Stage.CLOSED
        case.case_status = RecruitmentCase.CaseStatus.APPROVED
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    elif next_status == RecruitmentApplication.Status.REJECTED:
        case.current_stage = RecruitmentCase.Stage.CLOSED
        case.case_status = RecruitmentCase.CaseStatus.REJECTED
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    else:
        raise ValueError("Unsupported recruitment case transition target.")

    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "updated_at",
        ]
    )
    return {
        "previous_stage": previous_stage,
        "previous_case_status": previous_case_status,
        "case": case,
    }


def get_public_recruitment_entries(branch=None):
    entries = PositionPosting.objects.filter(
        status=PositionPosting.EntryStatus.ACTIVE,
    ).select_related("position_reference")
    if branch:
        entries = entries.filter(branch=branch)
    return [entry for entry in entries.order_by("branch", "title") if entry.is_open_for_intake]


def _build_portal_applicant_username():
    return f"portal-{uuid.uuid4().hex[:12]}"


def create_portal_applicant_identity(first_name, last_name, email, phone):
    applicant = RecruitmentUser(
        username=_build_portal_applicant_username(),
        first_name=first_name,
        last_name=last_name,
        email=email,
        office_name="Public Applicant",
        employee_id="",
        role=RecruitmentUser.Role.APPLICANT,
        is_active=False,
    )
    applicant.set_unusable_password()
    applicant.save()
    return applicant


def _missing_required_documents_message(application):
    missing_documents = application.missing_required_document_labels
    if not missing_documents:
        return ""
    return (
        "Upload all required documents before proceeding. Missing: "
        + "; ".join(missing_documents)
        + "."
    )


def ensure_application_document_completeness(application):
    if application.has_complete_required_documents:
        return application
    raise ValueError(_missing_required_documents_message(application))


def _hash_application_otp(application, otp_code):
    payload = "|".join(
        [
            str(application.public_token),
            application.applicant_email.lower(),
            otp_code,
            settings.APPLICATION_OTP_HASH_SECRET,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generate_otp_code():
    return f"{secrets.randbelow(900000) + 100000:06d}"


def issue_application_otp(application, actor=None):
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("OTP can only be issued while the application is still in draft.")
    ensure_application_document_completeness(application)

    otp_code = _generate_otp_code()
    now = timezone.now()
    application.otp_hash = _hash_application_otp(application, otp_code)
    application.otp_requested_at = now
    application.otp_expires_at = now + timedelta(minutes=settings.APPLICATION_OTP_VALIDITY_MINUTES)
    application.otp_verified_at = None
    application.save(
        update_fields=[
            "otp_hash",
            "otp_requested_at",
            "otp_expires_at",
            "otp_verified_at",
            "updated_at",
        ]
    )
    send_mail(
        subject="RecruitGuard-CHD applicant verification code",
        message=(
            "Your RecruitGuard-CHD one-time password is "
            f"{otp_code}. It expires in {settings.APPLICATION_OTP_VALIDITY_MINUTES} minutes."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[application.applicant_email],
        fail_silently=False,
    )
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_SENT,
        description="Sent applicant OTP for final submission verification.",
        metadata={"otp_expires_at": application.otp_expires_at.isoformat()},
    )
    return otp_code


def verify_application_otp(application, otp_code, actor=None):
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("OTP verification is only available before final submission.")
    if not application.otp_hash or not application.otp_expires_at:
        raise ValueError("Request an OTP first before attempting verification.")
    if application.otp_expires_at < timezone.now():
        raise ValueError("The OTP has expired. Request a new code before final submission.")

    expected_hash = _hash_application_otp(application, otp_code)
    if not hmac.compare_digest(application.otp_hash, expected_hash):
        raise ValueError("The OTP is invalid.")

    application.otp_verified_at = timezone.now()
    application.save(update_fields=["otp_verified_at", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
        description="Applicant OTP verified successfully.",
        metadata={"verified_at": application.otp_verified_at.isoformat()},
    )
    return application


@transaction.atomic
def create_public_application_draft(entry, cleaned_data, uploaded_documents):
    if not entry.is_open_for_intake:
        raise ValueError("The selected recruitment entry is not currently open for intake.")

    applicant_email = cleaned_data["email"].lower()
    if RecruitmentApplication.objects.filter(
        position=entry,
        applicant_email__iexact=applicant_email,
        submitted_at__isnull=False,
    ).exists():
        raise ValueError(
            "An application for this recruitment entry has already been submitted using this email address."
        )

    applicant_user = create_portal_applicant_identity(
        first_name=cleaned_data["first_name"],
        last_name=cleaned_data["last_name"],
        email=applicant_email,
        phone=cleaned_data["phone"],
    )
    application = RecruitmentApplication.objects.create(
        applicant=applicant_user,
        position=entry,
        qualification_summary=cleaned_data["qualification_summary"],
        cover_letter=cleaned_data["cover_letter"],
        applicant_first_name=cleaned_data["first_name"],
        applicant_last_name=cleaned_data["last_name"],
        applicant_email=applicant_email,
        applicant_phone=cleaned_data["phone"],
        checklist_privacy_consent=cleaned_data["checklist_privacy_consent"],
        checklist_documents_complete=cleaned_data["checklist_documents_complete"],
        checklist_information_certified=cleaned_data["checklist_information_certified"],
        performance_rating_not_applicable=cleaned_data["performance_rating_not_applicable"],
    )
    for requirement in get_applicant_document_requirements():
        uploaded_file = uploaded_documents.get(requirement.code)
        if not uploaded_file:
            continue
        upload_evidence_item(
            application=application,
            actor=applicant_user,
            label=requirement.title,
            uploaded_file=uploaded_file,
            document_type=requirement.code,
        )
    ensure_application_document_completeness(application)
    record_audit_event(
        application=application,
        actor=applicant_user,
        action=AuditLog.Action.APPLICATION_CREATED,
        description="Applicant created an accountless application draft.",
        metadata={
            "public_token": str(application.public_token),
            "document_count": len(uploaded_documents),
            "performance_rating_not_applicable": application.performance_rating_not_applicable,
        },
    )
    issue_application_otp(application, actor=applicant_user)
    return application


def _get_aes_key():
    return hashlib.sha256(settings.EVIDENCE_ENCRYPTION_SECRET.encode("utf-8")).digest()


def encrypt_evidence_bytes(raw_bytes):
    nonce = os.urandom(12)
    cipher = AESGCM(_get_aes_key())
    return nonce, cipher.encrypt(nonce, raw_bytes, None)


def decrypt_evidence_bytes(evidence, actor):
    cipher = AESGCM(_get_aes_key())
    plaintext = cipher.decrypt(bytes(evidence.nonce), bytes(evidence.ciphertext), None)
    record_audit_event(
        application=evidence.application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_DOWNLOADED,
        description=f"Downloaded evidence '{evidence.label}'.",
        metadata={"evidence_id": evidence.id, "filename": evidence.original_filename},
    )
    return plaintext


def store_generated_evidence_item(
    application,
    actor,
    label,
    filename,
    raw_bytes,
    content_type="",
    document_type="",
    recruitment_case=None,
    workflow_stage="",
):
    if recruitment_case and recruitment_case.application_id != application.id:
        raise ValueError("Evidence must stay linked to the recruitment case of the same application.")
    sha256_digest = hashlib.sha256(raw_bytes).hexdigest()
    nonce, ciphertext = encrypt_evidence_bytes(raw_bytes)
    evidence = EvidenceVaultItem.objects.create(
        application=application,
        recruitment_case=recruitment_case,
        workflow_stage=workflow_stage,
        uploaded_by=actor,
        label=label,
        document_type=document_type,
        original_filename=filename,
        content_type=content_type or "",
        size_bytes=len(raw_bytes),
        sha256_digest=sha256_digest,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_UPLOADED,
        description=f"Uploaded evidence '{label}'.",
        metadata={
            "evidence_id": evidence.id,
            "sha256": sha256_digest,
            "document_type": document_type,
            "workflow_stage": workflow_stage,
            "document_title": APPLICANT_DOCUMENT_REQUIREMENTS_BY_CODE.get(document_type).title
            if document_type in APPLICANT_DOCUMENT_REQUIREMENTS_BY_CODE
            else "",
        },
    )
    return evidence


def upload_evidence_item(
    application,
    actor,
    label,
    uploaded_file,
    document_type="",
    recruitment_case=None,
    workflow_stage="",
):
    raw_bytes = uploaded_file.read()
    return store_generated_evidence_item(
        application=application,
        actor=actor,
        label=label,
        filename=uploaded_file.name,
        raw_bytes=raw_bytes,
        content_type=getattr(uploaded_file, "content_type", "") or "",
        document_type=document_type,
        recruitment_case=recruitment_case,
        workflow_stage=workflow_stage,
    )


def _route_for_submission(application):
    if application.level == PositionPosting.Level.LEVEL_1:
        return RecruitmentUser.Role.SECRETARIAT, RecruitmentApplication.Status.SECRETARIAT_REVIEW
    return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW


@transaction.atomic
def submit_application(application, actor):
    if application.applicant_id != actor.id:
        raise ValueError("Only the owning applicant can submit this application.")
    if not application.is_editable_by_applicant:
        raise ValueError("This application can no longer be submitted.")
    if not application.checklist_complete:
        raise ValueError("Complete the submission checklist before final submission.")
    ensure_application_document_completeness(application)
    if not application.position.is_open_for_intake:
        raise ValueError("This recruitment entry is not currently open for intake.")
    if not application.otp_is_currently_valid:
        if application.otp_expires_at and application.otp_expires_at < timezone.now():
            raise ValueError("Your OTP verification has expired. Request a new code before final submission.")
        raise ValueError("Valid OTP verification is required before final submission.")

    previous_status = application.status
    previous_role = application.current_handler_role
    next_role, next_status = _route_for_submission(application)
    if not application.reference_number:
        application.reference_number = generate_application_reference()
    application.status = next_status
    application.current_handler_role = next_role
    application.submitted_at = timezone.now()
    application.submission_hash = generate_submission_hash(application)
    application.save(
        update_fields=[
            "reference_number",
            "status",
            "current_handler_role",
            "submitted_at",
            "submission_hash",
            "branch",
            "level",
            "updated_at",
        ]
    )
    case = _upsert_recruitment_case_for_submission(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_SUBMITTED,
        description="Applicant submitted the application.",
        metadata={"submission_hash": application.submission_hash, **_case_timeline_metadata(case)},
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Application routed to {next_role}.",
        metadata={
            "status": next_status,
            "current_handler_role": next_role,
            **_case_timeline_metadata(case),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.INITIAL,
        description=f"Application routed to {next_role}.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=next_role,
        from_status=previous_status,
        to_status=next_status,
        from_stage="",
        to_stage=case.current_stage,
    )
    return application


def get_available_actions(application, user):
    effective_role = get_effective_role_for_action(user, application)
    case = getattr(application, "case", None)
    if case and case.is_stage_locked:
        return []
    current_stage = case.current_stage if case else _review_stage_from_application_status(application.status)

    if effective_role == RecruitmentUser.Role.SECRETARIAT and current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
        return [
            ("endorse", "Endorse to HRM Chief"),
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]
    if effective_role == RecruitmentUser.Role.HRM_CHIEF and current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
        endorse_label = (
            "Endorse to Appointing Authority"
            if application.branch == PositionPosting.Branch.COS
            else "Endorse to HRMPSB"
        )
        return [
            ("endorse", endorse_label),
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]
    if effective_role == RecruitmentUser.Role.HRMPSB_MEMBER and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
        return [
            ("recommend", "Recommend to Appointing Authority"),
            ("return_to_hrm_chief", "Return to HRM Chief"),
            ("reject", "Reject Application"),
        ]
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
    ):
        return [
            ("return_to_hrm_chief", "Return to HRM Chief"),
        ]
    return []


def _transition_target(application, effective_role, action):
    if effective_role == RecruitmentUser.Role.SECRETARIAT:
        if action == "endorse":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Endorsed by Secretariat."
        if action == "return_to_applicant":
            return RecruitmentUser.Role.APPLICANT, RecruitmentApplication.Status.RETURNED_TO_APPLICANT, "Returned by Secretariat."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by Secretariat."
    if effective_role == RecruitmentUser.Role.HRM_CHIEF:
        if action == "endorse":
            if application.branch == PositionPosting.Branch.COS:
                return (
                    RecruitmentUser.Role.APPOINTING_AUTHORITY,
                    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
                    "COS application endorsed by HRM Chief.",
                )
            return RecruitmentUser.Role.HRMPSB_MEMBER, RecruitmentApplication.Status.HRMPSB_REVIEW, "Plantilla application endorsed to HRMPSB."
        if action == "return_to_applicant":
            return RecruitmentUser.Role.APPLICANT, RecruitmentApplication.Status.RETURNED_TO_APPLICANT, "Returned by HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by HRM Chief."
    if effective_role == RecruitmentUser.Role.HRMPSB_MEMBER:
        if action == "recommend":
            return RecruitmentUser.Role.APPOINTING_AUTHORITY, RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW, "Recommended by HRMPSB."
        if action == "return_to_hrm_chief":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Returned by HRMPSB to HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by HRMPSB."
    if effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY:
        if action == "approve":
            return "", RecruitmentApplication.Status.APPROVED, "Approved by Appointing Authority."
        if action == "return_to_hrm_chief":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Returned by Appointing Authority to HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by Appointing Authority."
    raise ValueError("Unsupported workflow action for the current stage.")


@transaction.atomic
def process_workflow_action(application, actor, action, remarks):
    effective_role = get_effective_role_for_action(actor, application)
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before workflow actions can be processed.")
    if application.case.is_stage_locked:
        raise ValueError("This recruitment case is stage-locked. Use controlled reopen before proceeding.")
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and action in {"approve", "reject"}
    ):
        raise ValueError(
            "Use final decision recording for selected or not selected outcomes at the Appointing Authority stage."
        )
    if not user_can_process_application(actor, application):
        if (
            effective_role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat cannot process Level 2 applications without an active override."
            )
        raise ValueError("You cannot process this application at its current workflow stage.")
    if (
        effective_role in SCREENING_REVIEW_ROLES
        and application.case.current_stage in SCREENING_STAGES
        and action == "endorse"
        and not screening_is_finalized_for_current_stage(application)
    ):
        raise ValueError("Finalize the screening record before endorsing this application.")
    current_exam_record = get_exam_record(application)
    if (
        effective_role in EXAM_REVIEW_ROLES
        and application.case.current_stage in EXAM_STAGES
        and action == "endorse"
        and current_exam_record
        and not current_exam_record.is_finalized
    ):
        raise ValueError("Finalize the examination record before endorsing this application.")
    current_deliberation_record = get_deliberation_record(application)
    if (
        application.branch == PositionPosting.Branch.COS
        and effective_role == RecruitmentUser.Role.HRM_CHIEF
        and application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        and action == "endorse"
        and (not current_deliberation_record or not current_deliberation_record.is_finalized)
    ):
        raise ValueError("Finalize the deliberation record before endorsing this COS application.")
    current_car_report = get_comparative_assessment_report(application)
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and effective_role == RecruitmentUser.Role.HRMPSB_MEMBER
        and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and action == "recommend"
        and (not current_deliberation_record or not current_deliberation_record.is_finalized)
    ):
        raise ValueError("Finalize the deliberation record before recommending this Plantilla application.")
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and effective_role == RecruitmentUser.Role.HRMPSB_MEMBER
        and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and action == "recommend"
        and (not current_car_report or not current_car_report.is_finalized)
    ):
        raise ValueError(
            "Finalize the Comparative Assessment Report before recommending this Plantilla application."
        )

    next_role, next_status, description = _transition_target(application, effective_role, action)
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = _sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
        remarks=remarks,
    )
    application.current_handler_role = next_role
    application.status = next_status
    if next_status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    }:
        application.closed_at = timezone.now()
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=description,
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "action": action,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
        },
    )
    if next_role:
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=f"Application routed to {next_role}.",
            metadata={
                "status": next_status,
                "current_handler_role": next_role,
                **_case_timeline_metadata(application.case),
            },
        )
        record_routing_history_event(
            application=application,
            actor=actor,
            route_type=RoutingHistory.RouteType.FORWARD,
            description=f"Application routed to {next_role}.",
            recruitment_case=application.case,
            from_handler_role=previous_role,
            to_handler_role=next_role,
            from_status=previous_status,
            to_status=next_status,
            from_stage=case_transition["previous_stage"],
            to_stage=application.case.current_stage,
            notes=remarks,
        )
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        override = application.active_secretariat_override
        if override:
            override.mark_used()
            record_audit_event(
                application=application,
                actor=actor,
                action=AuditLog.Action.OVERRIDE_USED,
                description="Secretariat override consumed during Level 2 processing.",
                metadata={"override_id": override.id},
            )
    return application


@transaction.atomic
def grant_secretariat_override(application, actor, reason):
    if actor.role != RecruitmentUser.Role.SYSTEM_ADMIN:
        raise ValueError("Only the System Administrator can grant a Secretariat override.")
    if application.level != PositionPosting.Level.LEVEL_2:
        raise ValueError("Overrides are only available for Level 2 applications.")
    if application.status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    }:
        raise ValueError("Closed applications cannot be overridden.")
    previous_role = application.current_handler_role
    previous_status = application.status
    application.overrides.filter(is_active=True).update(
        is_active=False,
        revoked_at=timezone.now(),
    )
    override = WorkflowOverride.objects.create(
        application=application,
        granted_by=actor,
        target_role=RecruitmentUser.Role.SECRETARIAT,
        reason=reason,
    )
    case = getattr(application, "case", None)
    previous_stage = case.current_stage if case else _review_stage_from_application_status(application.status)
    if case:
        case.current_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        case.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
        case.save(
            update_fields=[
                "branch",
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "closed_at",
                "updated_at",
            ]
        )
    application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
    application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
    application.save(update_fields=["current_handler_role", "status", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.OVERRIDE_GRANTED,
        description="Secretariat override granted for a Level 2 application.",
        metadata={
            "override_id": override.id,
            "reason": reason,
            **(_case_timeline_metadata(case) if case else {}),
        },
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description="Application rerouted to Secretariat under controlled override.",
        metadata={
            "status": application.status,
            "current_handler_role": application.current_handler_role,
            **(_case_timeline_metadata(case) if case else {}),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.OVERRIDE,
        description="Application rerouted to Secretariat under controlled override.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=application.current_handler_role,
        from_status=previous_status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage if case else _review_stage_from_application_status(application.status),
        notes=reason,
        is_override=True,
    )
    return override


@transaction.atomic
def reopen_recruitment_case(application, actor, reason):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case does not exist for this application.")
    case = application.case
    if actor.role not in CASE_REOPEN_ROLES:
        raise ValueError("Only the HRM Chief can perform a controlled reopen in this prototype.")
    if not case.is_stage_locked or not case.locked_stage:
        raise ValueError("Only stage-locked recruitment cases can be reopened.")

    previous_stage = case.current_stage
    previous_role = application.current_handler_role
    previous_status = application.status
    reopened_stage = case.locked_stage
    reopened_status = _application_status_from_stage(reopened_stage)
    if not reopened_status:
        raise ValueError("The locked recruitment case does not point to a reopenable workflow stage.")

    case.current_stage = reopened_stage
    case.current_handler_role = _handler_role_from_stage(reopened_stage)
    case.case_status = RecruitmentCase.CaseStatus.ACTIVE
    case.is_stage_locked = False
    case.locked_stage = ""
    case.closed_at = None
    case.reopened_at = timezone.now()
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "reopened_at",
            "updated_at",
        ]
    )

    application.status = reopened_status
    application.current_handler_role = case.current_handler_role
    application.closed_at = None
    application.save(update_fields=["status", "current_handler_role", "closed_at", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_REOPENED,
        description="Controlled reopen applied to a stage-locked recruitment case.",
        metadata={
            "reason": reason,
            "reopened_stage": reopened_stage,
            **_case_timeline_metadata(case),
        },
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Recruitment case reopened to {case.current_handler_role}.",
        metadata={
            "status": application.status,
            "current_handler_role": application.current_handler_role,
            **_case_timeline_metadata(case),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.REOPEN,
        description=f"Recruitment case reopened to {case.current_handler_role}.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=application.current_handler_role,
        from_status=previous_status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=reason,
    )
    return case


def _build_application_pdf(application):
    case = getattr(application, "case", None)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    lines = [
        "RecruitGuard-CHD Controlled Export",
        f"Reference: {application.reference_number}",
        f"Applicant: {application.applicant}",
        f"Position: {application.position.title}",
        f"Branch: {application.position.get_branch_display()}",
        f"Level: {application.position.get_level_display()}",
        f"Status: {application.get_status_display()}",
        f"Current Handler: {application.current_handler_role or 'Closed'}",
        f"Case Stage: {case.get_current_stage_display() if case else 'Not created'}",
        f"Case Status: {case.get_case_status_display() if case else 'N/A'}",
        "",
        "Qualification Summary:",
        application.qualification_summary,
        "",
        "Cover Letter:",
        application.cover_letter or "N/A",
    ]
    pdf.setTitle(application.reference_number)
    pdf.setFont("Helvetica", 11)
    for line in lines:
        pdf.drawString(50, y, line[:100])
        y -= 18
        if y < 70:
            pdf.showPage()
            pdf.setFont("Helvetica", 11)
            y = height - 50
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _build_comparative_assessment_report_pdf(
    application,
    actor,
    candidate_rows,
    generation_number,
    summary_notes,
):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    lines = [
        "RecruitGuard-CHD Comparative Assessment Report",
        f"Recruitment Entry: {application.position.title} [{application.position.job_code}]",
        f"Branch: {application.position.get_branch_display()}",
        f"Workflow Stage: {application.case.get_current_stage_display()}",
        f"Generated By: {actor}",
        f"Generated At: {timezone.now():%Y-%m-%d %H:%M}",
        f"Generation Version: {generation_number}",
        "",
        "Ranked Candidates",
    ]
    if summary_notes:
        lines.extend(["Summary Notes:", summary_notes, ""])
    for row in candidate_rows:
        lines.extend(
            [
                (
                    f"Rank {row['rank_order']} | {row['application'].reference_number} | "
                    f"{row['application'].applicant_display_name}"
                ),
                (
                    f"Qualification: {row['qualification_outcome'] or 'N/A'} | "
                    f"Exam: {row['exam_status'] or 'N/A'} ({row['exam_score'] or 'N/A'}) | "
                    f"Interview Avg: {row['interview_average_score'] or 'N/A'}"
                ),
                row["decision_support_summary"] or "No decision-support summary recorded.",
                "",
            ]
        )
    pdf.setTitle(f"{application.position.job_code} Comparative Assessment Report")
    pdf.setFont("Helvetica", 11)
    for line in lines:
        pdf.drawString(50, y, line[:100])
        y -= 18
        if y < 70:
            pdf.showPage()
            pdf.setFont("Helvetica", 11)
            y = height - 50
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _audit_log_csv(application):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "actor", "actor_role", "action", "description"])
    for row in application.audit_logs.select_related("actor").order_by("created_at"):
        writer.writerow(
            [
                row.created_at.isoformat(),
                row.actor.username if row.actor else "",
                row.actor_role,
                row.action,
                row.description,
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _manifest_json(application):
    case = getattr(application, "case", None)
    payload = {
        "reference_number": application.reference_number,
        "branch": application.branch,
        "level": application.level,
        "status": application.status,
        "current_handler_role": application.current_handler_role,
        "submission_hash": application.submission_hash,
        "case": {
            "current_stage": getattr(case, "current_stage", ""),
            "case_status": getattr(case, "case_status", ""),
            "current_handler_role": getattr(case, "current_handler_role", ""),
            "is_stage_locked": getattr(case, "is_stage_locked", False),
        },
        "evidence": [
            {
                "label": item.label,
                "document_type": item.document_type,
                "workflow_stage": item.workflow_stage,
                "original_filename": item.original_filename,
                "sha256_digest": item.sha256_digest,
                "size_bytes": item.size_bytes,
            }
            for item in application.evidence_items.all()
        ],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def build_export_bundle(application, actor):
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EXPORT_GENERATED,
        description="Generated controlled export package.",
        metadata={"generated_at": timezone.now().isoformat()},
    )
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{application.reference_number}.pdf", _build_application_pdf(application))
        archive.writestr("manifest.json", _manifest_json(application))
        archive.writestr("audit_log.csv", _audit_log_csv(application))
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def persist_position(position, actor, changed_fields):
    is_create = position.pk is None
    position.save()
    action = AuditLog.Action.POSITION_CREATED if is_create else AuditLog.Action.POSITION_UPDATED
    description = (
        f"Created position catalog record '{position.position_code}'."
        if is_create
        else f"Updated position catalog record '{position.position_code}'."
    )
    record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "position_id": position.id,
            "position_code": position.position_code,
            "changed_fields": changed_fields,
        },
    )
    return position


def persist_recruitment_entry(entry, actor, changed_fields):
    is_create = entry.pk is None
    entry.updated_by = actor
    if is_create and not entry.created_by_id:
        entry.created_by = actor
    entry.save()
    action = (
        AuditLog.Action.RECRUITMENT_ENTRY_CREATED
        if is_create
        else AuditLog.Action.RECRUITMENT_ENTRY_UPDATED
    )
    description = (
        f"Created recruitment entry '{entry.job_code}'."
        if is_create
        else f"Updated recruitment entry '{entry.job_code}'."
    )
    record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "engagement_type": entry.branch,
            "routing_basis": entry.level,
            "status": entry.status,
            "changed_fields": changed_fields,
        },
    )
    return entry


def update_recruitment_entry_status(entry, actor, new_status):
    previous_status = entry.status
    entry.status = new_status
    entry.updated_by = actor
    if new_status == PositionPosting.EntryStatus.CLOSED and not entry.closing_date:
        entry.closing_date = timezone.localdate()
    entry.save(update_fields=["status", "closing_date", "updated_by", "is_active", "updated_at"])
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
        description=f"Changed recruitment entry '{entry.job_code}' status from {previous_status} to {new_status}.",
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "old_status": previous_status,
            "new_status": new_status,
        },
    )
    return entry
