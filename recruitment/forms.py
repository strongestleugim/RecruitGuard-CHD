from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm

from .models import (
    ComparativeAssessmentReport,
    DeliberationRecord,
    ExamRecord,
    FinalDecision,
    InterviewRating,
    InterviewSession,
    Position,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentUser,
    ScreeningRecord,
)
from .requirements import PERFORMANCE_RATING, get_applicant_document_requirements
from .services import get_available_actions


class BootstrapFormMixin:
    def _apply_bootstrap(self):
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = f"{css_class} form-check-input".strip()
            elif isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                field.widget.attrs["class"] = f"{css_class} form-select".strip()
            else:
                field.widget.attrs["class"] = f"{css_class} form-control".strip()


def internal_role_choices():
    return [
        choice
        for choice in RecruitmentUser.Role.choices
        if choice[0] != RecruitmentUser.Role.APPLICANT
    ]


class InternalAuthenticationForm(BootstrapFormMixin, AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autofocus": True}))
    password = forms.CharField(strip=False, widget=forms.PasswordInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_internal_user:
            raise forms.ValidationError(
                "This sign-in page is restricted to internal users.",
                code="non_internal_user",
            )


class InternalPasswordChangeForm(BootstrapFormMixin, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class InternalUserCreateForm(BootstrapFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = RecruitmentUser
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "employee_id",
            "office_name",
            "role",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = internal_role_choices()
        self.fields["email"].required = True
        self.fields["is_active"].initial = True
        self._apply_bootstrap()


class InternalUserUpdateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = RecruitmentUser
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "employee_id",
            "office_name",
            "role",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = internal_role_choices()
        self.fields["email"].required = True
        self._apply_bootstrap()


class ApplicationForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = RecruitmentApplication
        fields = ["position", "qualification_summary", "cover_letter"]
        widgets = {
            "qualification_summary": forms.Textarea(attrs={"rows": 5}),
            "cover_letter": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = PositionPosting.objects.filter(
            status=PositionPosting.EntryStatus.ACTIVE,
        ).select_related("position_reference")
        if self.instance.pk:
            queryset = queryset | PositionPosting.objects.filter(pk=self.instance.position_id)
        self.fields["position"].queryset = queryset.distinct().order_by("title")
        self.fields["position"].label = "Recruitment Entry"
        self.fields["position"].help_text = "Select an active Plantilla or COS recruitment entry."
        self._apply_bootstrap()
        self.user = user

    def clean_position(self):
        position = self.cleaned_data["position"]
        existing_position = getattr(self.instance, "position", None)
        if not self.user:
            return position
        duplicate_qs = RecruitmentApplication.objects.filter(
            applicant=self.user,
            position=position,
        )
        if self.instance.pk:
            duplicate_qs = duplicate_qs.exclude(pk=self.instance.pk)
        if duplicate_qs.exists():
            raise forms.ValidationError(
                "You already have an application record for this recruitment entry."
            )
        if not position.is_open_for_intake and position != existing_position:
            raise forms.ValidationError(
                "The selected recruitment entry is not currently open for intake."
            )
        return position


class ApplicantPortalIntakeForm(BootstrapFormMixin, forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50)
    qualification_summary = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    cover_letter = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    performance_rating_not_applicable = forms.BooleanField(
        required=False,
        label="I do not have a prior performance rating applicable to this application.",
    )
    checklist_privacy_consent = forms.BooleanField(
        label="I consent to the use of my submitted information for recruitment processing.",
    )
    checklist_documents_complete = forms.BooleanField()
    checklist_information_certified = forms.BooleanField(
        label="I certify that the submitted information and uploaded documents are true and complete.",
    )

    def __init__(self, *args, entry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry = entry
        self.document_requirements = get_applicant_document_requirements()
        for requirement in self.document_requirements:
            self.fields[requirement.file_field_name] = forms.FileField(
                required=requirement.is_required,
                label=requirement.title,
                help_text=requirement.help_text,
            )
        branch_label = (
            entry.get_branch_display() if entry else "selected"
        )
        self.fields["checklist_documents_complete"].label = (
            f"I uploaded every required document in its designated field for the {branch_label} application path."
        )
        self._apply_bootstrap()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def get_uploaded_documents(self):
        return {
            requirement.code: self.cleaned_data[requirement.file_field_name]
            for requirement in self.document_requirements
            if self.cleaned_data.get(requirement.file_field_name)
        }

    def clean(self):
        cleaned_data = super().clean()
        performance_rating_not_applicable = cleaned_data.get("performance_rating_not_applicable")
        performance_rating_file = cleaned_data.get(PERFORMANCE_RATING)

        if performance_rating_not_applicable and performance_rating_file:
            self.add_error(
                PERFORMANCE_RATING,
                "Remove the uploaded performance rating or clear the not-applicable checkbox.",
            )
        if not performance_rating_not_applicable and not performance_rating_file:
            self.add_error(
                PERFORMANCE_RATING,
                "Upload the performance rating or mark this requirement as not applicable.",
            )

        for requirement in self.document_requirements:
            uploaded_file = cleaned_data.get(requirement.file_field_name)
            if uploaded_file and uploaded_file.size > settings.MAX_EVIDENCE_UPLOAD_BYTES:
                self.add_error(
                    requirement.file_field_name,
                    "Uploaded file exceeds the configured Evidence Vault size limit.",
                )

        if self.entry and not self.entry.is_open_for_intake:
            raise forms.ValidationError(
                "The selected recruitment entry is not currently open for intake."
            )
        if self.entry and cleaned_data.get("email"):
            duplicate_exists = RecruitmentApplication.objects.filter(
                position=self.entry,
                applicant_email__iexact=cleaned_data["email"],
                submitted_at__isnull=False,
            ).exists()
            if duplicate_exists:
                self.add_error(
                    "email",
                    "An application for this recruitment entry has already been submitted using this email address.",
                )
        return cleaned_data


class ApplicantOTPForm(BootstrapFormMixin, forms.Form):
    otp = forms.CharField(max_length=6, min_length=6, label="One-time password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_otp(self):
        otp = self.cleaned_data["otp"].strip()
        if not otp.isdigit():
            raise forms.ValidationError("Enter the 6-digit OTP sent to your email address.")
        return otp


class ApplicantStatusLookupForm(BootstrapFormMixin, forms.Form):
    application_id = forms.CharField(max_length=30, label="Application ID")
    email = forms.EmailField(label="Applicant email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_application_id(self):
        return self.cleaned_data["application_id"].strip().upper()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class EvidenceUploadForm(BootstrapFormMixin, forms.Form):
    label = forms.CharField(max_length=150)
    file = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        if uploaded_file.size > settings.MAX_EVIDENCE_UPLOAD_BYTES:
            raise forms.ValidationError(
                "Uploaded file exceeds the configured Evidence Vault size limit."
            )
        return uploaded_file


class WorkflowActionForm(BootstrapFormMixin, forms.Form):
    action = forms.ChoiceField()
    remarks = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, application, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action"].choices = get_available_actions(application, user)
        self._apply_bootstrap()

    def clean_action(self):
        action = self.cleaned_data["action"]
        valid_actions = {value for value, _label in self.fields["action"].choices}
        if action not in valid_actions:
            raise forms.ValidationError(
                "Selected action is not valid for this workflow stage."
            )
        return action


class WorkflowOverrideForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class WorkflowReopenForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class ScreeningReviewForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ScreeningRecord
        fields = [
            "completeness_status",
            "completeness_notes",
            "qualification_outcome",
            "screening_notes",
        ]
        widgets = {
            "completeness_notes": forms.Textarea(attrs={"rows": 3}),
            "screening_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["completeness_status"].label = "Completeness Finding"
        self.fields["qualification_outcome"].label = "Qualification Outcome"
        self.fields["screening_notes"].label = "Screening Notes"
        self._apply_bootstrap()


class ExamRecordForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ExamRecord
        fields = [
            "exam_type",
            "exam_status",
            "exam_score",
            "exam_result",
            "valid_from",
            "valid_until",
            "exam_notes",
        ]
        widgets = {
            "exam_score": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "exam_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exam_type"].label = "Exam Type"
        self.fields["exam_status"].label = "Exam Status"
        self.fields["exam_score"].label = "Exam Score"
        self.fields["exam_result"].label = "Exam Result"
        self.fields["valid_from"].label = "Validity Start"
        self.fields["valid_until"].label = "Validity End"
        self.fields["exam_notes"].label = "Exam Notes / Remarks"
        self.fields["exam_notes"].required = False
        self.fields["exam_result"].required = False
        self.fields["valid_from"].required = False
        self.fields["valid_until"].required = False
        self.fields["exam_score"].required = False
        self._apply_bootstrap()


class InterviewSessionForm(BootstrapFormMixin, forms.ModelForm):
    scheduled_for = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = InterviewSession
        fields = [
            "scheduled_for",
            "location",
            "session_notes",
        ]
        widgets = {
            "session_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_for"].label = "Interview Schedule"
        self.fields["location"].label = "Interview Location / Medium"
        self.fields["session_notes"].label = "Session Notes"
        self.fields["session_notes"].required = False
        self._apply_bootstrap()


class InterviewRatingForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = InterviewRating
        fields = [
            "rating_score",
            "rating_notes",
            "justification",
        ]
        widgets = {
            "rating_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "rating_notes": forms.Textarea(attrs={"rows": 3}),
            "justification": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rating_score"].label = "Interview Rating Score"
        self.fields["rating_notes"].label = "Rating Notes"
        self.fields["justification"].label = "Justification"
        self.fields["rating_notes"].required = False
        self.fields["justification"].required = False
        self._apply_bootstrap()


class InterviewFallbackUploadForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(label="Scanned Fallback Rating Sheet")
    remarks = forms.CharField(
        label="Upload Remarks",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class DeliberationRecordForm(BootstrapFormMixin, forms.ModelForm):
    deliberated_at = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = DeliberationRecord
        fields = [
            "deliberated_at",
            "deliberation_minutes",
            "decision_support_summary",
            "ranking_position",
            "ranking_notes",
        ]
        widgets = {
            "deliberation_minutes": forms.Textarea(attrs={"rows": 4}),
            "decision_support_summary": forms.Textarea(attrs={"rows": 4}),
            "ranking_position": forms.NumberInput(attrs={"min": "1", "step": "1"}),
            "ranking_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["deliberated_at"].label = "Deliberation Date and Time"
        self.fields["deliberation_minutes"].label = "Deliberation Minutes / Record"
        self.fields["decision_support_summary"].label = "Decision-Support Summary"
        self.fields["ranking_position"].label = "Ranking Position"
        self.fields["ranking_notes"].label = "Ranking Notes"
        self.fields["ranking_position"].required = False
        self.fields["ranking_notes"].required = False
        self._apply_bootstrap()


class ComparativeAssessmentReportForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ComparativeAssessmentReport
        fields = [
            "summary_notes",
        ]
        widgets = {
            "summary_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["summary_notes"].label = "CAR Summary Notes"
        self.fields["summary_notes"].required = False
        self._apply_bootstrap()


class FinalDecisionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FinalDecision
        fields = [
            "decision_outcome",
            "decision_notes",
        ]
        widgets = {
            "decision_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["decision_outcome"].label = "Final Outcome"
        self.fields["decision_notes"].label = "Decision Notes / Remarks"
        self._apply_bootstrap()


class PositionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Position
        fields = [
            "position_code",
            "title",
            "unit",
            "description",
            "requirements",
            "qualification_reference",
            "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "requirements": forms.Textarea(attrs={"rows": 4}),
            "qualification_reference": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class RecruitmentEntryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PositionPosting
        fields = [
            "position_reference",
            "job_code",
            "branch",
            "level",
            "intake_mode",
            "status",
            "publication_date",
            "opening_date",
            "closing_date",
            "qualification_reference",
        ]
        widgets = {
            "publication_date": forms.DateInput(attrs={"type": "date"}),
            "opening_date": forms.DateInput(attrs={"type": "date"}),
            "closing_date": forms.DateInput(attrs={"type": "date"}),
            "qualification_reference": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position_reference"].queryset = Position.objects.filter(is_active=True).order_by(
            "title",
            "position_code",
        )
        if self.instance.pk and self.instance.position_reference_id:
            self.fields["position_reference"].queryset = (
                self.fields["position_reference"].queryset
                | Position.objects.filter(pk=self.instance.position_reference_id)
            )
        self.fields["job_code"].label = "Entry Code"
        self.fields["branch"].label = "Engagement Type"
        self.fields["level"].label = "Routing Basis"
        self._apply_bootstrap()

    def clean(self):
        cleaned_data = super().clean()
        branch = cleaned_data.get("branch")
        intake_mode = cleaned_data.get("intake_mode")
        closing_date = cleaned_data.get("closing_date")

        if branch == PositionPosting.Branch.PLANTILLA and intake_mode != PositionPosting.IntakeMode.FIXED_PERIOD:
            self.add_error("intake_mode", "Plantilla entries must use the fixed period intake mode.")

        if branch == PositionPosting.Branch.COS and intake_mode == PositionPosting.IntakeMode.FIXED_PERIOD:
            self.add_error(
                "intake_mode",
                "COS entries may only use opening-based, continuous, or pooling intake.",
            )

        if (
            branch == PositionPosting.Branch.COS
            and intake_mode in {PositionPosting.IntakeMode.CONTINUOUS, PositionPosting.IntakeMode.POOLING}
            and closing_date
        ):
            self.add_error(
                "closing_date",
                "Continuous or pooling COS entries must not define a fixed closing date.",
            )

        return cleaned_data
