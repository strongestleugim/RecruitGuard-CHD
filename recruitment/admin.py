from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

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


@admin.register(RecruitmentUser)
class RecruitmentUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            "RecruitGuard",
            {"fields": ("role", "office_name", "employee_id")},
        ),
    )
    list_display = ("username", "email", "role", "office_name", "is_active")
    list_filter = ("role", "is_active", "is_staff")


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("position_code", "title", "unit", "is_active")
    list_filter = ("is_active",)
    search_fields = ("position_code", "title", "unit")


@admin.register(PositionPosting)
class PositionPostingAdmin(admin.ModelAdmin):
    list_display = (
        "job_code",
        "title",
        "branch",
        "intake_mode",
        "level",
        "status",
        "opening_date",
        "closing_date",
    )
    list_filter = ("branch", "intake_mode", "level", "status")
    search_fields = ("job_code", "title", "unit", "position_reference__position_code")


class EvidenceVaultItemInline(admin.TabularInline):
    model = EvidenceVaultItem
    extra = 0
    readonly_fields = (
        "recruitment_case",
        "workflow_stage",
        "document_type",
        "label",
        "original_filename",
        "sha256_digest",
        "size_bytes",
        "uploaded_by",
        "created_at",
    )
    can_delete = False


class AuditLogInline(admin.TabularInline):
    model = AuditLog
    extra = 0
    readonly_fields = (
        "created_at",
        "actor",
        "actor_role",
        "action",
        "description",
        "metadata",
    )
    can_delete = False


class RoutingHistoryInline(admin.TabularInline):
    model = RoutingHistory
    extra = 0
    readonly_fields = (
        "created_at",
        "route_type",
        "from_handler_role",
        "to_handler_role",
        "from_stage",
        "to_stage",
        "description",
        "notes",
        "is_override",
    )
    can_delete = False


class ScreeningRecordInline(admin.TabularInline):
    model = ScreeningRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "reviewed_by",
        "reviewed_by_role",
        "completeness_status",
        "completeness_notes",
        "qualification_outcome",
        "screening_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class ExamRecordInline(admin.TabularInline):
    model = ExamRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "recorded_by",
        "recorded_by_role",
        "exam_type",
        "exam_status",
        "exam_score",
        "exam_result",
        "valid_from",
        "valid_until",
        "exam_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class InterviewSessionInline(admin.TabularInline):
    model = InterviewSession
    extra = 0
    readonly_fields = (
        "review_stage",
        "scheduled_by",
        "scheduled_by_role",
        "scheduled_for",
        "location",
        "session_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class InterviewRatingInline(admin.TabularInline):
    model = InterviewRating
    extra = 0
    readonly_fields = (
        "review_stage",
        "rated_by",
        "rated_by_role",
        "rating_score",
        "rating_notes",
        "justification",
        "created_at",
    )
    can_delete = False


class DeliberationRecordInline(admin.TabularInline):
    model = DeliberationRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "recorded_by",
        "recorded_by_role",
        "deliberated_at",
        "deliberation_minutes",
        "decision_support_summary",
        "ranking_position",
        "ranking_notes",
        "consolidated_snapshot",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class ComparativeAssessmentReportInline(admin.TabularInline):
    model = ComparativeAssessmentReport
    extra = 0
    readonly_fields = (
        "review_stage",
        "recruitment_entry",
        "generated_by",
        "generated_by_role",
        "summary_notes",
        "generation_count",
        "evidence_item",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class FinalDecisionInline(admin.TabularInline):
    model = FinalDecision
    extra = 0
    readonly_fields = (
        "review_stage",
        "decided_by",
        "decided_by_role",
        "decision_outcome",
        "decision_notes",
        "submission_packet_snapshot",
        "decided_at",
        "created_at",
    )
    can_delete = False


@admin.register(RecruitmentApplication)
class RecruitmentApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "reference_number",
        "applicant_display_name",
        "applicant_email",
        "position",
        "branch",
        "level",
        "status",
        "current_handler_role",
        "performance_rating_not_applicable",
    )
    list_filter = ("branch", "level", "status", "current_handler_role", "performance_rating_not_applicable")
    search_fields = (
        "reference_number",
        "applicant__username",
        "applicant_first_name",
        "applicant_last_name",
        "applicant_email",
        "position__title",
    )
    inlines = [
        EvidenceVaultItemInline,
        ScreeningRecordInline,
        ExamRecordInline,
        InterviewSessionInline,
        InterviewRatingInline,
        DeliberationRecordInline,
        ComparativeAssessmentReportInline,
        FinalDecisionInline,
        RoutingHistoryInline,
        AuditLogInline,
    ]


@admin.register(RecruitmentCase)
class RecruitmentCaseAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "branch",
        "current_stage",
        "case_status",
        "current_handler_role",
        "is_stage_locked",
    )
    list_filter = ("branch", "current_stage", "case_status", "is_stage_locked")
    search_fields = ("application__reference_number", "application__position__title")


@admin.register(WorkflowOverride)
class WorkflowOverrideAdmin(admin.ModelAdmin):
    list_display = ("application", "target_role", "granted_by", "is_active", "created_at", "used_at")
    list_filter = ("target_role", "is_active")
    search_fields = ("application__reference_number", "granted_by__username")


@admin.register(EvidenceVaultItem)
class EvidenceVaultItemAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "document_type",
        "label",
        "original_filename",
        "size_bytes",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("workflow_stage", "document_type")
    search_fields = ("application__reference_number", "document_type", "label", "original_filename")
    readonly_fields = ("recruitment_case", "workflow_stage", "document_type", "sha256_digest", "nonce", "ciphertext")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("application", "action", "actor", "actor_role", "created_at")
    list_filter = ("action", "actor_role")
    search_fields = ("application__reference_number", "description", "actor__username")
    readonly_fields = ("created_at", "metadata")


@admin.register(RoutingHistory)
class RoutingHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "route_type",
        "from_handler_role",
        "to_handler_role",
        "branch",
        "level",
        "created_at",
    )
    list_filter = ("route_type", "branch", "level", "is_override", "to_handler_role")
    search_fields = ("application__reference_number", "description", "notes", "actor__username")
    readonly_fields = ("created_at",)


@admin.register(ScreeningRecord)
class ScreeningRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "reviewed_by",
        "completeness_status",
        "qualification_outcome",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "completeness_status", "qualification_outcome", "is_finalized")
    search_fields = ("application__reference_number", "completeness_notes", "screening_notes", "reviewed_by__username")
    readonly_fields = ("created_at", "updated_at", "reviewed_by_role", "finalized_by_role")


@admin.register(ExamRecord)
class ExamRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "recorded_by",
        "exam_type",
        "exam_status",
        "exam_score",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "exam_status", "is_finalized")
    search_fields = ("application__reference_number", "exam_type", "exam_result", "exam_notes", "recorded_by__username")
    readonly_fields = ("created_at", "updated_at", "recorded_by_role", "finalized_by_role")


@admin.register(InterviewSession)
class InterviewSessionAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "scheduled_by",
        "scheduled_for",
        "location",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "is_finalized")
    search_fields = ("application__reference_number", "location", "session_notes", "scheduled_by__username")
    readonly_fields = ("created_at", "updated_at", "scheduled_by_role", "finalized_by_role")


@admin.register(InterviewRating)
class InterviewRatingAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "rated_by",
        "rating_score",
        "created_at",
    )
    list_filter = ("review_stage", "branch", "level", "rated_by_role")
    search_fields = ("application__reference_number", "rating_notes", "justification", "rated_by__username")
    readonly_fields = ("created_at", "updated_at", "rated_by_role")


class ComparativeAssessmentReportItemInline(admin.TabularInline):
    model = ComparativeAssessmentReportItem
    extra = 0
    readonly_fields = (
        "application",
        "recruitment_case",
        "deliberation_record",
        "rank_order",
        "qualification_outcome",
        "exam_status",
        "exam_score",
        "interview_average_score",
        "decision_support_summary",
        "created_at",
    )
    can_delete = False


@admin.register(DeliberationRecord)
class DeliberationRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "recorded_by",
        "ranking_position",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "is_finalized")
    search_fields = (
        "application__reference_number",
        "deliberation_minutes",
        "decision_support_summary",
        "ranking_notes",
        "recorded_by__username",
    )
    readonly_fields = ("created_at", "updated_at", "recorded_by_role", "finalized_by_role", "consolidated_snapshot")


@admin.register(ComparativeAssessmentReport)
class ComparativeAssessmentReportAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "recruitment_entry",
        "review_stage",
        "generated_by",
        "generation_count",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "is_finalized")
    search_fields = (
        "application__reference_number",
        "recruitment_entry__job_code",
        "recruitment_entry__title",
        "summary_notes",
        "generated_by__username",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "generated_by_role",
        "finalized_by_role",
        "consolidated_snapshot",
        "generation_count",
    )
    inlines = [ComparativeAssessmentReportItemInline]


@admin.register(FinalDecision)
class FinalDecisionAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "decision_outcome",
        "decided_by",
        "review_stage",
        "decided_at",
    )
    list_filter = ("decision_outcome", "branch", "level", "review_stage")
    search_fields = (
        "application__reference_number",
        "recruitment_entry__job_code",
        "recruitment_entry__title",
        "decision_notes",
        "decided_by__username",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "decided_by_role",
        "submission_packet_snapshot",
    )
