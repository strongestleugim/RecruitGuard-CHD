import json

from django import template

from recruitment.models import (
    CompletionRequirement,
    ExamRecord,
    NotificationLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)

register = template.Library()


ROLE_LABELS = dict(RecruitmentUser.Role.choices)
STAGE_LABELS = dict(RecruitmentCase.Stage.choices)
STATUS_LABELS = {
    **dict(RecruitmentApplication.Status.choices),
    **dict(RecruitmentCase.CaseStatus.choices),
    **dict(PositionPosting.EntryStatus.choices),
    **dict(ScreeningRecord.CompletenessStatus.choices),
    **dict(ScreeningRecord.QualificationOutcome.choices),
    **dict(ExamRecord.ExamStatus.choices),
    **dict(CompletionRequirement.RequirementStatus.choices),
    **dict(NotificationLog.DeliveryStatus.choices),
}

STATUS_THEMES = {
    PositionPosting.EntryStatus.ACTIVE: "success",
    PositionPosting.EntryStatus.DRAFT: "neutral",
    PositionPosting.EntryStatus.SUSPENDED: "warning",
    PositionPosting.EntryStatus.CLOSED: "neutral",
    RecruitmentApplication.Status.DRAFT: "neutral",
    RecruitmentApplication.Status.SECRETARIAT_REVIEW: "info",
    RecruitmentApplication.Status.HRM_CHIEF_REVIEW: "info",
    RecruitmentApplication.Status.HRMPSB_REVIEW: "info",
    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: "info",
    RecruitmentApplication.Status.RETURNED_TO_APPLICANT: "warning",
    RecruitmentApplication.Status.APPROVED: "success",
    RecruitmentApplication.Status.REJECTED: "danger",
    RecruitmentApplication.Status.WITHDRAWN: "neutral",
    RecruitmentCase.CaseStatus.ACTIVE: "info",
    RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT: "warning",
    RecruitmentCase.CaseStatus.APPROVED: "success",
    RecruitmentCase.CaseStatus.REJECTED: "danger",
    ScreeningRecord.CompletenessStatus.COMPLETE: "success",
    ScreeningRecord.CompletenessStatus.INCOMPLETE: "warning",
    ScreeningRecord.QualificationOutcome.QUALIFIED: "success",
    ScreeningRecord.QualificationOutcome.NOT_QUALIFIED: "danger",
    ExamRecord.ExamStatus.COMPLETED: "success",
    ExamRecord.ExamStatus.WAIVED: "warning",
    ExamRecord.ExamStatus.ABSENT: "danger",
    CompletionRequirement.RequirementStatus.PENDING: "warning",
    CompletionRequirement.RequirementStatus.COMPLETED: "success",
    CompletionRequirement.RequirementStatus.NOT_APPLICABLE: "neutral",
    NotificationLog.DeliveryStatus.PENDING: "warning",
    NotificationLog.DeliveryStatus.SENT: "success",
    NotificationLog.DeliveryStatus.FAILED: "danger",
}

ROLE_THEMES = {
    RecruitmentUser.Role.APPLICANT: "applicant",
    RecruitmentUser.Role.SECRETARIAT: "secretariat",
    RecruitmentUser.Role.HRM_CHIEF: "hrm-chief",
    RecruitmentUser.Role.HRMPSB_MEMBER: "hrmpsb-member",
    RecruitmentUser.Role.APPOINTING_AUTHORITY: "appointing-authority",
    RecruitmentUser.Role.SYSTEM_ADMIN: "system-admin",
}


def _slug(value):
    return str(value).replace("_", "-").lower()


@register.filter
def role_label(value):
    if not value:
        return "Unassigned"
    return ROLE_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def stage_label(value):
    if not value:
        return "Not assigned"
    return STAGE_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def status_label(value):
    if not value:
        return "Not recorded"
    return STATUS_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def status_theme(value):
    return STATUS_THEMES.get(value, "neutral")


@register.filter
def branch_theme(value):
    if value == PositionPosting.Branch.PLANTILLA:
        return "plantilla"
    if value == PositionPosting.Branch.COS:
        return "cos"
    return "neutral"


@register.filter
def level_theme(value):
    if str(value) == str(PositionPosting.Level.LEVEL_1):
        return "level-1"
    if str(value) == str(PositionPosting.Level.LEVEL_2):
        return "level-2"
    return "neutral"


@register.filter
def role_theme(value):
    return ROLE_THEMES.get(value, _slug(value) if value else "neutral")


@register.filter
def pretty_json(value):
    if not value:
        return ""
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


@register.simple_tag
def workflow_stages(branch):
    stages = [
        {
            "value": RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            "label": RecruitmentCase.Stage.SECRETARIAT_REVIEW.label,
            "short_label": "Secretariat",
        },
        {
            "value": RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            "label": RecruitmentCase.Stage.HRM_CHIEF_REVIEW.label,
            "short_label": "HRM Chief",
        },
    ]
    if branch == PositionPosting.Branch.PLANTILLA:
        stages.append(
            {
                "value": RecruitmentCase.Stage.HRMPSB_REVIEW,
                "label": RecruitmentCase.Stage.HRMPSB_REVIEW.label,
                "short_label": "HRMPSB",
            }
        )
    stages.extend(
        [
            {
                "value": RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
                "label": RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW.label,
                "short_label": "Authority",
            },
            {
                "value": RecruitmentCase.Stage.COMPLETION,
                "label": RecruitmentCase.Stage.COMPLETION.label,
                "short_label": "Completion",
            },
            {
                "value": RecruitmentCase.Stage.CLOSED,
                "label": RecruitmentCase.Stage.CLOSED.label,
                "short_label": "Closed",
            },
        ]
    )
    return stages


@register.simple_tag
def workflow_stage_state(branch, current_stage, step_value, case_status=""):
    stages = [stage["value"] for stage in workflow_stages(branch)]
    if step_value not in stages:
        return "future"
    if step_value == RecruitmentCase.Stage.CLOSED:
        return "current" if current_stage == RecruitmentCase.Stage.CLOSED else "future"
    if current_stage == RecruitmentCase.Stage.CLOSED:
        return "complete"
    try:
        current_index = stages.index(current_stage)
    except ValueError:
        current_index = -1
    step_index = stages.index(step_value)
    if current_stage == step_value:
        return "current"
    if step_index < current_index:
        return "complete"
    if case_status in {
        RecruitmentCase.CaseStatus.APPROVED,
        RecruitmentCase.CaseStatus.REJECTED,
    } and current_stage == RecruitmentCase.Stage.CLOSED:
        return "complete"
    return "future"


PIPELINE_STAGES = [
    {"key": "publication", "label": "Publication"},
    {"key": "intake", "label": "Intake"},
    {"key": "screening", "label": "Screening"},
    {"key": "exam", "label": "Exam"},
    {"key": "interview", "label": "Interview"},
    {"key": "deliberation", "label": "Deliberation/CAR"},
    {"key": "submission", "label": "Submission"},
    {"key": "appointment", "label": "Appointment"},
    {"key": "archive", "label": "Archive"},
]

PIPELINE_STAGE_MAP = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW: "screening",
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: "screening",
    RecruitmentCase.Stage.HRMPSB_REVIEW: "deliberation",
    RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: "submission",
    RecruitmentCase.Stage.COMPLETION: "appointment",
    RecruitmentCase.Stage.CLOSED: "archive",
}


@register.simple_tag
def pipeline_stages():
    return PIPELINE_STAGES


@register.simple_tag
def pipeline_stage_state(current_stage, case_status, step_key):
    if not current_stage:
        if step_key in ("publication", "intake"):
            return "complete"
        return "future"

    mapped = PIPELINE_STAGE_MAP.get(current_stage, "screening")
    order = [s["key"] for s in PIPELINE_STAGES]

    try:
        current_idx = order.index(mapped)
    except ValueError:
        current_idx = 2

    try:
        step_idx = order.index(step_key)
    except ValueError:
        return "future"

    if case_status in {
        RecruitmentCase.CaseStatus.APPROVED,
        RecruitmentCase.CaseStatus.REJECTED,
    }:
        return "complete"

    if step_idx < current_idx:
        return "complete"
    if step_idx == current_idx:
        return "current"
    return "future"

