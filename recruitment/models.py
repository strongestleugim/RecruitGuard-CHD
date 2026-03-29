import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .requirements import (
    APPLICANT_DOCUMENT_TYPE_CHOICES,
    PERFORMANCE_RATING,
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class RecruitmentUser(AbstractUser):
    class Role(models.TextChoices):
        APPLICANT = "applicant", "Applicant"
        SECRETARIAT = "secretariat", "Secretariat"
        HRM_CHIEF = "hrm_chief", "HRM Chief"
        HRMPSB_MEMBER = "hrmpsb_member", "HRMPSB Member"
        APPOINTING_AUTHORITY = "appointing_authority", "Appointing Authority"
        SYSTEM_ADMIN = "system_admin", "System Administrator"

    role = models.CharField(max_length=40, choices=Role.choices, default=Role.APPLICANT)
    office_name = models.CharField(max_length=255, blank=True)
    employee_id = models.CharField(max_length=50, blank=True)

    @classmethod
    def internal_roles(cls):
        return {
            cls.Role.SECRETARIAT,
            cls.Role.HRM_CHIEF,
            cls.Role.HRMPSB_MEMBER,
            cls.Role.APPOINTING_AUTHORITY,
            cls.Role.SYSTEM_ADMIN,
        }

    def save(self, *args, **kwargs):
        self.is_staff = self.is_superuser or self.role == self.Role.SYSTEM_ADMIN
        super().save(*args, **kwargs)

    @property
    def is_internal_user(self):
        return self.role in self.internal_roles()

    @property
    def is_workflow_staff(self):
        return self.role in self.internal_roles()

    def __str__(self):
        return self.get_full_name() or self.username


class Position(TimestampedModel):
    position_code = models.CharField(max_length=30, unique=True)
    title = models.CharField(max_length=255)
    unit = models.CharField(max_length=255)
    description = models.TextField()
    requirements = models.TextField()
    qualification_reference = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["title", "position_code"]

    def __str__(self):
        return f"{self.title} ({self.position_code})"


class PositionPosting(TimestampedModel):
    class Branch(models.TextChoices):
        PLANTILLA = "plantilla", "Plantilla"
        COS = "cos", "COS"

    class Level(models.IntegerChoices):
        LEVEL_1 = 1, "Level 1"
        LEVEL_2 = 2, "Level 2"

    class IntakeMode(models.TextChoices):
        FIXED_PERIOD = "fixed_period", "Fixed Period"
        OPENING_BASED = "opening_based", "Opening Based"
        CONTINUOUS = "continuous", "Continuous"
        POOLING = "pooling", "Pooling"

    class EntryStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    position_reference = models.ForeignKey(
        Position,
        on_delete=models.PROTECT,
        related_name="recruitment_entries",
        blank=True,
        null=True,
    )
    job_code = models.CharField(max_length=30, unique=True)
    title = models.CharField(max_length=255, blank=True)
    branch = models.CharField(max_length=20, choices=Branch.choices)
    level = models.PositiveSmallIntegerField(choices=Level.choices)
    unit = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    qualification_reference = models.TextField(blank=True)
    intake_mode = models.CharField(
        max_length=30,
        choices=IntakeMode.choices,
        default=IntakeMode.FIXED_PERIOD,
    )
    status = models.CharField(
        max_length=20,
        choices=EntryStatus.choices,
        default=EntryStatus.DRAFT,
    )
    publication_date = models.DateField(blank=True, null=True)
    opening_date = models.DateField(default=timezone.localdate)
    closing_date = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "RecruitmentUser",
        on_delete=models.PROTECT,
        related_name="created_recruitment_entries",
        blank=True,
        null=True,
    )
    updated_by = models.ForeignKey(
        "RecruitmentUser",
        on_delete=models.PROTECT,
        related_name="updated_recruitment_entries",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["title"]
        verbose_name = "Recruitment Entry"
        verbose_name_plural = "Recruitment Entries"

    def clean(self):
        errors = {}

        if not self.position_reference_id:
            errors["position_reference"] = "Position reference is required."
        if self.closing_date and self.closing_date < self.opening_date:
            errors["closing_date"] = "Closing date cannot be earlier than opening date."

        if self.branch == self.Branch.PLANTILLA:
            if self.intake_mode != self.IntakeMode.FIXED_PERIOD:
                errors["intake_mode"] = "Plantilla entries must use a fixed validity period."
            if not self.closing_date:
                errors["closing_date"] = "Plantilla entries require a closing date."
        elif self.branch == self.Branch.COS:
            if self.intake_mode == self.IntakeMode.FIXED_PERIOD:
                errors["intake_mode"] = "COS entries must use opening-based, continuous, or pooling intake."
            if (
                self.intake_mode in {self.IntakeMode.CONTINUOUS, self.IntakeMode.POOLING}
                and self.closing_date
            ):
                errors["closing_date"] = "Continuous or pooling COS entries must not set a fixed closing date."

        if self.status == self.EntryStatus.CLOSED and not self.closing_date:
            self.closing_date = timezone.localdate()

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.position_reference_id:
            self.title = self.position_reference.title
            self.unit = self.position_reference.unit
            self.description = self.position_reference.description
            self.requirements = self.position_reference.requirements
            if not self.qualification_reference:
                self.qualification_reference = self.position_reference.qualification_reference
        self.is_active = self.status == self.EntryStatus.ACTIVE
        super().save(*args, **kwargs)

    @property
    def is_open_for_intake(self):
        if self.status != self.EntryStatus.ACTIVE:
            return False
        if self.opening_date and self.opening_date > timezone.localdate():
            return False
        if self.branch == self.Branch.PLANTILLA:
            return bool(self.closing_date and self.closing_date >= timezone.localdate())
        if self.intake_mode == self.IntakeMode.OPENING_BASED and self.closing_date:
            return self.closing_date >= timezone.localdate()
        return True

    @property
    def engagement_type(self):
        return self.branch

    def __str__(self):
        return f"{self.title} [{self.job_code}]"


class RecruitmentApplication(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SECRETARIAT_REVIEW = "secretariat_review", "Secretariat Review"
        HRM_CHIEF_REVIEW = "hrm_chief_review", "HRM Chief Review"
        HRMPSB_REVIEW = "hrmpsb_review", "HRMPSB Review"
        APPOINTING_AUTHORITY_REVIEW = "appointing_authority_review", "Appointing Authority Review"
        RETURNED_TO_APPLICANT = "returned_to_applicant", "Returned to Applicant"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reference_number = models.CharField(
        max_length=30,
        unique=True,
        editable=False,
        blank=True,
        null=True,
    )
    applicant = models.ForeignKey("RecruitmentUser", on_delete=models.CASCADE, related_name="applications")
    position = models.ForeignKey(PositionPosting, on_delete=models.PROTECT, related_name="applications")
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.DRAFT)
    current_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    applicant_first_name = models.CharField(max_length=150, blank=True)
    applicant_last_name = models.CharField(max_length=150, blank=True)
    applicant_email = models.EmailField(blank=True)
    applicant_phone = models.CharField(max_length=50, blank=True)
    checklist_privacy_consent = models.BooleanField(default=False)
    checklist_documents_complete = models.BooleanField(default=False)
    checklist_information_certified = models.BooleanField(default=False)
    performance_rating_not_applicable = models.BooleanField(default=False)
    cover_letter = models.TextField(blank=True)
    qualification_summary = models.TextField()
    otp_hash = models.CharField(max_length=64, blank=True)
    otp_requested_at = models.DateTimeField(blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_verified_at = models.DateTimeField(blank=True, null=True)
    submission_hash = models.CharField(max_length=64, blank=True)
    submitted_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["applicant", "position"],
                name="unique_application_per_applicant_position",
            )
        ]

    def save(self, *args, **kwargs):
        self.branch = self.position.branch
        self.level = self.position.level
        super().save(*args, **kwargs)

    @property
    def is_editable_by_applicant(self):
        return self.status in {self.Status.DRAFT, self.Status.RETURNED_TO_APPLICANT}

    @property
    def applicant_display_name(self):
        full_name = " ".join(
            value for value in [self.applicant_first_name, self.applicant_last_name] if value
        ).strip()
        return full_name or str(self.applicant)

    @property
    def checklist_complete(self):
        return all(
            [
                self.checklist_privacy_consent,
                self.checklist_documents_complete,
                self.checklist_information_certified,
            ]
        )

    @property
    def uploaded_document_types(self):
        return set(
            self.evidence_items.exclude(document_type="").values_list("document_type", flat=True)
        )

    @property
    def missing_required_document_types(self):
        uploaded_document_types = self.uploaded_document_types
        return [
            requirement.code
            for requirement in get_required_applicant_document_requirements(
                performance_rating_not_applicable=self.performance_rating_not_applicable,
            )
            if requirement.code not in uploaded_document_types
        ]

    @property
    def missing_required_document_labels(self):
        missing_document_types = set(self.missing_required_document_types)
        return [
            requirement.title
            for requirement in get_applicant_document_requirements()
            if requirement.code in missing_document_types
        ]

    @property
    def has_complete_required_documents(self):
        return not self.missing_required_document_types

    @property
    def document_requirement_statuses(self):
        uploaded_document_types = self.uploaded_document_types
        statuses = []
        for requirement in get_applicant_document_requirements():
            is_not_applicable = (
                requirement.code == PERFORMANCE_RATING and self.performance_rating_not_applicable
            )
            is_uploaded = requirement.code in uploaded_document_types
            statuses.append(
                {
                    "code": requirement.code,
                    "title": requirement.title,
                    "help_text": requirement.help_text,
                    "is_uploaded": is_uploaded,
                    "is_not_applicable": is_not_applicable,
                    "is_complete": is_uploaded or is_not_applicable,
                }
            )
        return statuses

    @property
    def otp_is_currently_valid(self):
        return bool(
            self.otp_verified_at
            and self.otp_expires_at
            and self.otp_expires_at >= timezone.now()
        )

    @property
    def reference_label(self):
        return self.reference_number or "Generated after final submission"

    @property
    def active_secretariat_override(self):
        return self.overrides.filter(
            is_active=True,
            target_role=RecruitmentUser.Role.SECRETARIAT,
        ).first()

    def __str__(self):
        return self.reference_number or f"Draft Application #{self.pk or 'new'}"


class RecruitmentCase(TimestampedModel):
    class Stage(models.TextChoices):
        SECRETARIAT_REVIEW = "secretariat_review", "Secretariat Review"
        HRM_CHIEF_REVIEW = "hrm_chief_review", "HRM Chief Review"
        HRMPSB_REVIEW = "hrmpsb_review", "HRMPSB Review"
        APPOINTING_AUTHORITY_REVIEW = "appointing_authority_review", "Appointing Authority Review"
        CLOSED = "closed", "Closed"

    class CaseStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        RETURNED_TO_APPLICANT = "returned_to_applicant", "Returned to Applicant"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    application = models.OneToOneField(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="case",
    )
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    current_stage = models.CharField(max_length=40, choices=Stage.choices)
    current_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    case_status = models.CharField(
        max_length=40,
        choices=CaseStatus.choices,
        default=CaseStatus.ACTIVE,
    )
    is_stage_locked = models.BooleanField(default=False)
    locked_stage = models.CharField(max_length=40, choices=Stage.choices, blank=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    reopened_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-updated_at"]

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        super().save(*args, **kwargs)

    @property
    def timeline_entries(self):
        return self.application.audit_logs.order_by("created_at")

    def __str__(self):
        return f"Case for {self.application.reference_label}"


class WorkflowOverride(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="overrides",
    )
    granted_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="workflow_overrides",
    )
    target_role = models.CharField(max_length=40, choices=RecruitmentUser.Role.choices)
    reason = models.TextField()
    is_active = models.BooleanField(default=True)
    used_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.target_role != RecruitmentUser.Role.SECRETARIAT:
            raise ValidationError("Only Secretariat overrides are supported in this prototype.")
        if self.application.level != PositionPosting.Level.LEVEL_2:
            raise ValidationError("Overrides are only required for Level 2 applications.")

    def mark_used(self):
        self.is_active = False
        self.used_at = timezone.now()
        self.save(update_fields=["is_active", "used_at", "updated_at"])

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at", "updated_at"])

    def __str__(self):
        return f"Override for {self.application.reference_number}"


class RoutingHistory(TimestampedModel):
    class RouteType(models.TextChoices):
        INITIAL = "initial", "Initial Routing"
        FORWARD = "forward", "Forward Routing"
        OVERRIDE = "override", "Override Routing"
        REOPEN = "reopen", "Reopen Routing"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="routing_history",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="routing_history",
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="routing_history_actions",
        blank=True,
        null=True,
    )
    actor_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    route_type = models.CharField(max_length=20, choices=RouteType.choices)
    from_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    to_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    from_status = models.CharField(
        max_length=40,
        choices=RecruitmentApplication.Status.choices,
        blank=True,
    )
    to_status = models.CharField(
        max_length=40,
        choices=RecruitmentApplication.Status.choices,
        blank=True,
    )
    from_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        blank=True,
    )
    to_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        blank=True,
    )
    description = models.TextField()
    notes = models.TextField(blank=True)
    is_override = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        target = self.to_handler_role or "closed"
        return f"{self.application.reference_label} -> {target}"


class ScreeningRecord(TimestampedModel):
    class CompletenessStatus(models.TextChoices):
        COMPLETE = "complete", "Complete"
        INCOMPLETE = "incomplete", "Incomplete"

    class QualificationOutcome(models.TextChoices):
        QUALIFIED = "qualified", "Qualified"
        NOT_QUALIFIED = "not_qualified", "Not Qualified"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="screening_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="screening_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    reviewed_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="screening_records",
    )
    reviewed_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    completeness_status = models.CharField(
        max_length=20,
        choices=CompletenessStatus.choices,
    )
    completeness_notes = models.TextField()
    qualification_outcome = models.CharField(
        max_length=30,
        choices=QualificationOutcome.choices,
    )
    screening_notes = models.TextField()
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_screening_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_screening_record_per_application_stage",
            )
        ]

    def clean(self):
        if self.review_stage not in {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        }:
            raise ValidationError("Screening records are only supported for Secretariat and HRM Chief review stages.")
        if self.reviewed_by.role not in {
            RecruitmentUser.Role.SECRETARIAT,
            RecruitmentUser.Role.HRM_CHIEF,
        }:
            raise ValidationError("Only Secretariat or HRM Chief may record screening outputs.")
        if self.is_finalized and not self.finalized_by_id:
            raise ValidationError("Finalized screening outputs must record the finalizing user.")
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            raise ValidationError("Draft screening outputs cannot include finalization metadata.")

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.reviewed_by_role = self.reviewed_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage}"


class ExamRecord(TimestampedModel):
    class ExamStatus(models.TextChoices):
        COMPLETED = "completed", "Completed"
        WAIVED = "waived", "Waived"
        ABSENT = "absent", "Absent"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="exam_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="exam_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    recorded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_exam_records",
    )
    recorded_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    exam_type = models.CharField(max_length=100)
    exam_status = models.CharField(
        max_length=20,
        choices=ExamStatus.choices,
    )
    exam_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        blank=True,
        null=True,
    )
    exam_result = models.CharField(max_length=255, blank=True)
    valid_from = models.DateField(blank=True, null=True)
    valid_until = models.DateField(blank=True, null=True)
    exam_notes = models.TextField(blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_exam_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_exam_record_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Examination records must be linked to a recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Examination records must stay linked to the recruitment case of the same application."
            )
        if self.review_stage not in {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        }:
            errors["review_stage"] = (
                "Examination records are only supported for Secretariat and HRM Chief review stages."
            )
        if self.recorded_by.role not in {
            RecruitmentUser.Role.SECRETARIAT,
            RecruitmentUser.Role.HRM_CHIEF,
        }:
            errors["recorded_by"] = "Only Secretariat or HRM Chief may record examination outputs."
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Validity end date cannot be earlier than the validity start date."
        if self.exam_status == self.ExamStatus.COMPLETED:
            if self.exam_score is None and not self.exam_result:
                errors["exam_result"] = "Provide an exam result or score for completed examinations."
        else:
            if self.exam_score is not None:
                errors["exam_score"] = "Waived or absent exams must not store a numeric score."
            if self.valid_from or self.valid_until:
                errors["valid_from"] = "Only completed exams may record a validity period."
            if not self.exam_notes:
                errors["exam_notes"] = "Provide remarks explaining the waiver or absence."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized examination outputs must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft examination outputs cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recorded_by_role = self.recorded_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} examination"


class InterviewSession(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="interview_sessions",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="interview_sessions",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="interview_sessions",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    scheduled_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="scheduled_interview_sessions",
    )
    scheduled_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    scheduled_for = models.DateTimeField()
    location = models.CharField(max_length=255)
    session_notes = models.TextField(blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_interview_sessions",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "scheduled_for", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_interview_session_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        expected_roles = {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentUser.Role.SECRETARIAT,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
            RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        }
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Interview sessions must be linked to a recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Interview sessions must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = "Interview sessions must reference the recruitment entry of the application."
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Interview sessions must stay linked to the recruitment entry of the same application."
            )
        if self.review_stage not in expected_roles:
            errors["review_stage"] = (
                "Interview sessions are only supported during Secretariat, HRM Chief, or HRMPSB review stages."
            )
        elif self.scheduled_by.role != expected_roles[self.review_stage]:
            errors["scheduled_by"] = (
                "Only the authorized current-stage handler may schedule or update the interview session."
            )
        if self.branch == PositionPosting.Branch.COS and self.review_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
            errors["review_stage"] = "COS interview sessions cannot be scheduled during an HRMPSB review stage."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized interview sessions must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft interview sessions cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recruitment_entry = self.application.position
        self.scheduled_by_role = self.scheduled_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} interview"


class InterviewRating(TimestampedModel):
    interview_session = models.ForeignKey(
        InterviewSession,
        on_delete=models.CASCADE,
        related_name="ratings",
    )
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="interview_ratings",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="interview_ratings",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    rated_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="interview_ratings",
    )
    rated_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    rating_score = models.DecimalField(max_digits=5, decimal_places=2)
    rating_notes = models.TextField(blank=True)
    justification = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["interview_session", "rated_by"],
                name="unique_interview_rating_per_session_evaluator",
            )
        ]

    def clean(self):
        errors = {}
        allowed_roles = {
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
            RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        }
        if self.interview_session_id and self.interview_session.is_finalized:
            errors["interview_session"] = "Finalized interview sessions cannot accept additional rating changes."
        if self.application_id and self.interview_session_id and self.interview_session.application_id != self.application_id:
            errors["application"] = "Interview ratings must stay linked to the same application as the interview session."
        if self.recruitment_case_id and self.interview_session_id and self.interview_session.recruitment_case_id != self.recruitment_case_id:
            errors["recruitment_case"] = "Interview ratings must stay linked to the same recruitment case as the interview session."
        if self.interview_session_id and self.review_stage != self.interview_session.review_stage:
            errors["review_stage"] = "Interview ratings must use the same workflow stage as the interview session."
        if self.review_stage not in allowed_roles:
            errors["review_stage"] = "Direct interview ratings are only supported during HRM Chief or HRMPSB review stages."
        elif self.rated_by.role != allowed_roles[self.review_stage]:
            errors["rated_by"] = "Only the authorized evaluator for the current stage may record an interview rating."
        try:
            score = Decimal(str(self.rating_score))
        except (InvalidOperation, TypeError):
            score = None
        if score is None or score < 0 or score > 100:
            errors["rating_score"] = "Interview ratings must be between 0 and 100."
        if score is not None and score < Decimal("75") and not self.justification:
            errors["justification"] = "Provide a justification when the interview rating is below the passing threshold."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.application = self.interview_session.application
        self.recruitment_case = self.interview_session.recruitment_case
        self.review_stage = self.interview_session.review_stage
        self.branch = self.interview_session.branch
        self.level = self.interview_session.level
        self.rated_by_role = self.rated_by.role
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.application.reference_label} rating by {self.rated_by}"


class DeliberationRecord(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="deliberation_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="deliberation_records",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="deliberation_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    recorded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_deliberation_records",
    )
    recorded_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    deliberated_at = models.DateTimeField(default=timezone.now)
    deliberation_minutes = models.TextField()
    decision_support_summary = models.TextField()
    ranking_position = models.PositiveIntegerField(blank=True, null=True)
    ranking_notes = models.TextField(blank=True)
    consolidated_snapshot = models.JSONField(default=dict, blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_deliberation_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "deliberated_at", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_deliberation_record_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        expected_roles = {
            PositionPosting.Branch.COS: (
                RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
                RecruitmentUser.Role.HRM_CHIEF,
            ),
            PositionPosting.Branch.PLANTILLA: (
                RecruitmentCase.Stage.HRMPSB_REVIEW,
                RecruitmentUser.Role.HRMPSB_MEMBER,
            ),
        }
        expected_stage, expected_role = expected_roles.get(self.branch, ("", ""))
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Deliberation records must be linked to a recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Deliberation records must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = "Deliberation records must reference the recruitment entry of the application."
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Deliberation records must stay linked to the recruitment entry of the same application."
            )
        if expected_stage and self.review_stage != expected_stage:
            errors["review_stage"] = (
                "Deliberation is only supported during the HRM Chief review stage for COS or "
                "the HRMPSB review stage for Plantilla."
            )
        if expected_role and self.recorded_by.role != expected_role:
            errors["recorded_by"] = "Only the authorized decision-support handler may record deliberation minutes."
        if self.ranking_position is not None and self.ranking_position < 1:
            errors["ranking_position"] = "Ranking position must be a positive whole number."
        if self.is_finalized and not self.consolidated_snapshot:
            errors["consolidated_snapshot"] = "Finalized deliberation records must preserve the consolidated source snapshot."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized deliberation records must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft deliberation records cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recruitment_entry = self.application.position
        self.recorded_by_role = self.recorded_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} deliberation"


class ComparativeAssessmentReport(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_reports",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_reports",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_reports",
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    generated_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="generated_comparative_assessment_reports",
    )
    generated_by_role = models.CharField(max_length=40, blank=True)
    summary_notes = models.TextField(blank=True)
    consolidated_snapshot = models.JSONField(default=dict, blank=True)
    generation_count = models.PositiveIntegerField(default=1)
    evidence_item = models.ForeignKey(
        "EvidenceVaultItem",
        on_delete=models.SET_NULL,
        related_name="comparative_assessment_reports",
        blank=True,
        null=True,
    )
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_comparative_assessment_reports",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_car_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        if self.application_id and self.recruitment_entry_id and self.application.position_id != self.recruitment_entry_id:
            errors["recruitment_entry"] = (
                "Comparative Assessment Reports must stay linked to the recruitment entry of the same application."
            )
        if self.recruitment_case_id and self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Comparative Assessment Reports must stay linked to the recruitment case of the same application."
            )
        if self.recruitment_entry.branch != PositionPosting.Branch.PLANTILLA:
            errors["recruitment_entry"] = "Comparative Assessment Reports are only supported for Plantilla recruitment entries."
        if self.review_stage != RecruitmentCase.Stage.HRMPSB_REVIEW:
            errors["review_stage"] = "Comparative Assessment Reports are only supported during the HRMPSB review stage."
        if self.generated_by.role != RecruitmentUser.Role.HRMPSB_MEMBER:
            errors["generated_by"] = "Only an HRMPSB Member may generate or update a Comparative Assessment Report."
        if self.is_finalized and not self.evidence_item_id:
            errors["evidence_item"] = "Finalized Comparative Assessment Reports must link to the generated PDF artifact."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized Comparative Assessment Reports must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft Comparative Assessment Reports cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recruitment_entry = self.application.position
        self.recruitment_case = getattr(self.application, "case", None)
        self.branch = self.recruitment_entry.branch
        self.generated_by_role = self.generated_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.recruitment_entry.job_code} {self.review_stage} CAR"


class ComparativeAssessmentReportItem(TimestampedModel):
    report = models.ForeignKey(
        ComparativeAssessmentReport,
        on_delete=models.CASCADE,
        related_name="items",
    )
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_report_items",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_report_items",
        blank=True,
        null=True,
    )
    deliberation_record = models.ForeignKey(
        DeliberationRecord,
        on_delete=models.PROTECT,
        related_name="comparative_assessment_report_items",
    )
    rank_order = models.PositiveIntegerField()
    qualification_outcome = models.CharField(max_length=40, blank=True)
    exam_status = models.CharField(max_length=20, blank=True)
    exam_score = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    interview_average_score = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    decision_support_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["rank_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["report", "application"],
                name="unique_car_item_per_report_application",
            ),
            models.UniqueConstraint(
                fields=["report", "rank_order"],
                name="unique_car_rank_per_report",
            ),
        ]

    def clean(self):
        errors = {}
        if self.report_id and self.application.position_id != self.report.recruitment_entry_id:
            errors["application"] = "CAR items must stay linked to the same recruitment entry as the report."
        if self.recruitment_case_id and self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = "CAR items must stay linked to the recruitment case of the same application."
        if self.deliberation_record_id and self.application_id and self.deliberation_record.application_id != self.application_id:
            errors["deliberation_record"] = "CAR items must stay linked to the same application as the deliberation record."
        if self.rank_order < 1:
            errors["rank_order"] = "CAR rank order must be a positive whole number."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recruitment_case = getattr(self.application, "case", None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.report} #{self.rank_order}"


class FinalDecision(TimestampedModel):
    class Outcome(models.TextChoices):
        SELECTED = "selected", "Selected"
        NOT_SELECTED = "not_selected", "Not Selected"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="final_decisions",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="final_decisions",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="final_decisions",
    )
    review_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        default=RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
    )
    decided_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_final_decisions",
    )
    decided_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    decision_outcome = models.CharField(max_length=20, choices=Outcome.choices)
    decision_notes = models.TextField()
    submission_packet_snapshot = models.JSONField(default=dict, blank=True)
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-created_at"]

    def clean(self):
        errors = {}
        if self.review_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW:
            errors["review_stage"] = (
                "Final decisions are only supported during the Appointing Authority review stage."
            )
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Final decisions must be linked to the recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Final decisions must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = (
                "Final decisions must reference the recruitment entry of the same application."
            )
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Final decisions must stay linked to the recruitment entry of the same application."
            )
        if self.decided_by.role != RecruitmentUser.Role.APPOINTING_AUTHORITY:
            errors["decided_by"] = "Only the Appointing Authority may record a final decision."
        if not self.submission_packet_snapshot:
            errors["submission_packet_snapshot"] = (
                "Final decisions must preserve the submission packet snapshot."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recruitment_case = getattr(self.application, "case", None)
        self.recruitment_entry = self.application.position
        self.review_stage = RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        self.branch = self.application.branch
        self.level = self.application.level
        self.decided_by_role = self.decided_by.role
        super().save(*args, **kwargs)

    @property
    def is_selected(self):
        return self.decision_outcome == self.Outcome.SELECTED

    def __str__(self):
        return f"{self.application.reference_label} {self.get_decision_outcome_display()}"


class EvidenceVaultItem(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="evidence_items",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="evidence_items",
        blank=True,
        null=True,
    )
    workflow_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        blank=True,
    )
    uploaded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="uploaded_evidence",
    )
    label = models.CharField(max_length=150)
    document_type = models.CharField(
        max_length=50,
        choices=APPLICANT_DOCUMENT_TYPE_CHOICES,
        blank=True,
        db_index=True,
    )
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveIntegerField()
    sha256_digest = models.CharField(max_length=64)
    nonce = models.BinaryField()
    ciphertext = models.BinaryField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.application.reference_number} - {self.label}"


class AuditLog(TimestampedModel):
    class Action(models.TextChoices):
        INTERNAL_LOGIN = "internal_login", "Internal Login"
        INTERNAL_LOGOUT = "internal_logout", "Internal Logout"
        PASSWORD_CHANGED = "password_changed", "Password Changed"
        INTERNAL_ACCOUNT_CREATED = "internal_account_created", "Internal Account Created"
        INTERNAL_ACCOUNT_UPDATED = "internal_account_updated", "Internal Account Updated"
        INTERNAL_ACCOUNT_ACTIVATED = "internal_account_activated", "Internal Account Activated"
        INTERNAL_ACCOUNT_DEACTIVATED = "internal_account_deactivated", "Internal Account Deactivated"
        INTERNAL_ROLE_CHANGED = "internal_role_changed", "Internal Role Changed"
        POSITION_CREATED = "position_created", "Position Created"
        POSITION_UPDATED = "position_updated", "Position Updated"
        RECRUITMENT_ENTRY_CREATED = "recruitment_entry_created", "Recruitment Entry Created"
        RECRUITMENT_ENTRY_UPDATED = "recruitment_entry_updated", "Recruitment Entry Updated"
        RECRUITMENT_ENTRY_STATUS_CHANGED = "recruitment_entry_status_changed", "Recruitment Entry Status Changed"
        APPLICATION_CREATED = "application_created", "Application Created"
        APPLICATION_UPDATED = "application_updated", "Application Updated"
        APPLICATION_OTP_SENT = "application_otp_sent", "Application OTP Sent"
        APPLICATION_OTP_VERIFIED = "application_otp_verified", "Application OTP Verified"
        APPLICATION_SUBMITTED = "application_submitted", "Application Submitted"
        CASE_CREATED = "case_created", "Case Created"
        CASE_REOPENED = "case_reopened", "Case Reopened"
        ROUTED = "routed", "Application Routed"
        SCREENING_RECORDED = "screening_recorded", "Screening Recorded"
        SCREENING_FINALIZED = "screening_finalized", "Screening Finalized"
        EXAM_RECORDED = "exam_recorded", "Exam Recorded"
        EXAM_FINALIZED = "exam_finalized", "Exam Finalized"
        INTERVIEW_SCHEDULED = "interview_scheduled", "Interview Scheduled"
        INTERVIEW_FINALIZED = "interview_finalized", "Interview Finalized"
        INTERVIEW_RATING_RECORDED = "interview_rating_recorded", "Interview Rating Recorded"
        INTERVIEW_FALLBACK_UPLOADED = "interview_fallback_uploaded", "Interview Fallback Uploaded"
        DELIBERATION_RECORDED = "deliberation_recorded", "Deliberation Recorded"
        DELIBERATION_FINALIZED = "deliberation_finalized", "Deliberation Finalized"
        CAR_GENERATED = "car_generated", "Comparative Assessment Report Generated"
        CAR_FINALIZED = "car_finalized", "Comparative Assessment Report Finalized"
        DECISION_RECORDED = "decision_recorded", "Decision Recorded"
        OVERRIDE_GRANTED = "override_granted", "Override Granted"
        OVERRIDE_USED = "override_used", "Override Used"
        EVIDENCE_UPLOADED = "evidence_uploaded", "Evidence Uploaded"
        EVIDENCE_DOWNLOADED = "evidence_downloaded", "Evidence Downloaded"
        EXPORT_GENERATED = "export_generated", "Export Generated"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="audit_logs",
        blank=True,
        null=True,
    )
    actor_role = models.CharField(max_length=40, blank=True)
    action = models.CharField(max_length=50, choices=Action.choices)
    description = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()} @ {self.created_at:%Y-%m-%d %H:%M}"
