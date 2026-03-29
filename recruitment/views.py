from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from .forms import (
    ApplicationForm,
    ComparativeAssessmentReportForm,
    DeliberationRecordForm,
    ExamRecordForm,
    EvidenceUploadForm,
    FinalDecisionForm,
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    InterviewSessionForm,
    ScreeningReviewForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from .models import AuditLog, PositionPosting, RecruitmentApplication, RecruitmentUser
from .permissions import (
    InternalUserRequiredMixin,
    SystemAdministratorRequiredMixin,
    WorkflowProcessorRequiredMixin,
)
from .services import (
    build_submission_packet,
    build_export_bundle,
    decrypt_evidence_bytes,
    generate_comparative_assessment_report,
    get_comparative_assessment_report,
    get_comparative_assessment_report_items_for_report,
    get_exam_record,
    get_exam_records,
    get_case_timeline,
    get_deliberation_record,
    get_deliberation_records,
    get_final_decision_history,
    get_interview_fallback_evidence,
    get_interview_rating_for_user,
    get_interview_ratings,
    get_interview_session,
    get_interview_sessions,
    get_latest_final_decision,
    get_latest_finalized_comparative_assessment_report,
    get_screening_record,
    get_screening_records,
    get_queue_for_user,
    get_visible_positions_for_user,
    grant_secretariat_override,
    process_workflow_action,
    record_final_decision,
    record_audit_event,
    reopen_recruitment_case,
    save_deliberation_record,
    save_exam_record,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    upload_interview_fallback_rating,
    upload_evidence_item,
    user_can_manage_exam,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
    user_can_manage_interview_rating,
    user_can_manage_interview_session,
    user_can_manage_screening,
    user_can_process_application,
    user_can_record_final_decision,
    user_can_reopen_case,
    user_can_upload_interview_fallback,
    user_can_upload_evidence,
    user_can_view_application,
)


class ApplicantRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return (
            self.request.user.is_authenticated
            and self.request.user.role == RecruitmentUser.Role.APPLICANT
        )


class StaffRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_workflow_staff


class DashboardView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    template_name = "recruitment/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["positions"] = get_visible_positions_for_user(user)[:6]
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            internal_users = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())
            context["internal_user_count"] = internal_users.count()
            context["active_internal_user_count"] = internal_users.filter(is_active=True).count()
            context["recent_identity_logs"] = AuditLog.objects.filter(
                action__in=[
                    AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_UPDATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                    AuditLog.Action.INTERNAL_ROLE_CHANGED,
                ]
            )[:5]
        else:
            context["queue"] = get_queue_for_user(user)
        return context


class PositionListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/position_list.html"
    context_object_name = "positions"

    def get_queryset(self):
        return get_visible_positions_for_user(self.request.user)


class ApplicationListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"

    def get_queryset(self):
        user = self.request.user
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return get_queue_for_user(user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = False
        return context


class ApplicationCreateView(LoginRequiredMixin, ApplicantRequiredMixin, CreateView):
    model = RecruitmentApplication
    form_class = ApplicationForm
    template_name = "recruitment/application_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.applicant = self.request.user
        response = super().form_valid(form)
        record_audit_event(
            application=self.object,
            actor=self.request.user,
            action=AuditLog.Action.APPLICATION_CREATED,
            description="Applicant created a draft application.",
        )
        messages.success(self.request, "Application draft created.")
        return response

    def get_success_url(self):
        return reverse("application-detail", kwargs={"pk": self.object.pk})


class ApplicationUpdateView(LoginRequiredMixin, ApplicantRequiredMixin, UpdateView):
    model = RecruitmentApplication
    form_class = ApplicationForm
    template_name = "recruitment/application_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_queryset(self):
        return self.request.user.applications.filter(
            status__in=[
                RecruitmentApplication.Status.DRAFT,
                RecruitmentApplication.Status.RETURNED_TO_APPLICANT,
            ]
        )

    def form_valid(self, form):
        response = super().form_valid(form)
        record_audit_event(
            application=self.object,
            actor=self.request.user,
            action=AuditLog.Action.APPLICATION_UPDATED,
            description="Applicant updated the draft application.",
        )
        messages.success(self.request, "Application draft updated.")
        return response

    def get_success_url(self):
        return reverse("application-detail", kwargs={"pk": self.object.pk})


class ApplicationDetailView(LoginRequiredMixin, InternalUserRequiredMixin, DetailView):
    model = RecruitmentApplication
    template_name = "recruitment/application_detail.html"
    context_object_name = "application"

    def get_object(self, queryset=None):
        application = super().get_object(queryset)
        if not user_can_view_application(self.request.user, application):
            raise Http404
        return application

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context["application"]
        user = self.request.user
        context["recruitment_case"] = getattr(application, "case", None)
        context["case_timeline"] = get_case_timeline(application) if context["recruitment_case"] else []
        context["routing_history"] = application.routing_history.select_related("actor", "recruitment_case")
        context["screening_records"] = get_screening_records(application)
        context["current_screening_record"] = get_screening_record(application)
        context["exam_records"] = get_exam_records(application)
        context["current_exam_record"] = get_exam_record(application)
        context["interview_sessions"] = get_interview_sessions(application)
        context["current_interview_session"] = get_interview_session(application)
        context["current_interview_ratings"] = get_interview_ratings(application)
        context["current_interview_fallback_evidence"] = get_interview_fallback_evidence(application)
        context["current_user_interview_rating"] = get_interview_rating_for_user(application, user)
        context["deliberation_records"] = get_deliberation_records(application)
        context["current_deliberation_record"] = get_deliberation_record(application)
        context["current_comparative_assessment_report"] = get_comparative_assessment_report(application)
        if not context["current_comparative_assessment_report"]:
            context["current_comparative_assessment_report"] = (
                get_latest_finalized_comparative_assessment_report(application)
            )
        context["current_comparative_assessment_report_items"] = (
            get_comparative_assessment_report_items_for_report(
                context["current_comparative_assessment_report"]
            )
        )
        context["submission_packet"] = (
            build_submission_packet(application) if context["recruitment_case"] else {}
        )
        context["final_decision_history"] = get_final_decision_history(application)
        context["latest_final_decision"] = get_latest_final_decision(application)
        if user_can_upload_evidence(user, application):
            context["evidence_form"] = EvidenceUploadForm()
        if user_can_manage_screening(user, application):
            screening_record = context["current_screening_record"]
            if screening_record and screening_record.is_finalized:
                context["screening_locked"] = True
            else:
                context["screening_form"] = ScreeningReviewForm(instance=screening_record)
        if user_can_manage_exam(user, application):
            exam_record = context["current_exam_record"]
            if exam_record and exam_record.is_finalized:
                context["exam_locked"] = True
            else:
                context["exam_form"] = ExamRecordForm(instance=exam_record)
        if user_can_manage_interview_session(user, application):
            interview_session = context["current_interview_session"]
            if interview_session and interview_session.is_finalized:
                context["interview_session_locked"] = True
            else:
                context["interview_session_form"] = InterviewSessionForm(instance=interview_session)
        if user_can_manage_interview_rating(user, application):
            interview_session = context["current_interview_session"]
            if not interview_session:
                context["interview_rating_requires_session"] = True
            elif interview_session.is_finalized:
                context["interview_rating_locked"] = True
            else:
                context["interview_rating_form"] = InterviewRatingForm(
                    instance=context["current_user_interview_rating"]
                )
        if user_can_upload_interview_fallback(user, application):
            interview_session = context["current_interview_session"]
            if not interview_session:
                context["interview_fallback_requires_session"] = True
            elif interview_session.is_finalized:
                context["interview_fallback_locked"] = True
            else:
                context["interview_fallback_form"] = InterviewFallbackUploadForm()
        if user_can_manage_deliberation(user, application):
            deliberation_record = context["current_deliberation_record"]
            if deliberation_record and deliberation_record.is_finalized:
                context["deliberation_locked"] = True
            else:
                context["deliberation_form"] = DeliberationRecordForm(instance=deliberation_record)
        if user_can_manage_comparative_assessment_report(user, application):
            deliberation_record = context["current_deliberation_record"]
            report = context["current_comparative_assessment_report"]
            if not deliberation_record:
                context["car_requires_deliberation"] = True
            elif not deliberation_record.is_finalized:
                context["car_requires_finalized_deliberation"] = True
            elif report and report.is_finalized:
                context["car_locked"] = True
            else:
                context["car_form"] = ComparativeAssessmentReportForm(instance=report)
        if user_can_record_final_decision(user, application):
            context["final_decision_form"] = FinalDecisionForm()
        if user_can_process_application(user, application):
            context["action_form"] = WorkflowActionForm(application=application, user=user)
        if (
            user.role == RecruitmentUser.Role.SYSTEM_ADMIN
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            context["override_form"] = WorkflowOverrideForm()
        if context["recruitment_case"] and user_can_reopen_case(user, context["recruitment_case"]):
            context["reopen_form"] = WorkflowReopenForm()
        context["can_export"] = user.role in {
            RecruitmentUser.Role.HRM_CHIEF,
            RecruitmentUser.Role.APPOINTING_AUTHORITY,
        } and user_can_view_application(user, application)
        return context


class SubmitApplicationView(LoginRequiredMixin, ApplicantRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk, applicant=request.user)
        try:
            submit_application(application, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Application submitted and routed according to level-aware workflow.",
            )
        return redirect("application-detail", pk=pk)


class EvidenceUploadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_evidence(request.user, application):
            raise PermissionDenied
        form = EvidenceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload_evidence_item(
                application=application,
                actor=request.user,
                label=form.cleaned_data["label"],
                uploaded_file=form.cleaned_data["file"],
            )
            messages.success(request, "Evidence stored in the encrypted vault.")
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("application-detail", pk=pk)


class EvidenceDownloadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk, evidence_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        evidence = get_object_or_404(application.evidence_items, pk=evidence_pk)
        content = decrypt_evidence_bytes(evidence, request.user)
        response = HttpResponse(
            content,
            content_type=evidence.content_type or "application/octet-stream",
        )
        response["Content-Disposition"] = f'attachment; filename="{evidence.original_filename}"'
        return response


class WorkflowQueueView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"

    def get_queryset(self):
        return get_queue_for_user(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = True
        return context


class WorkflowActionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_process_application(request.user, application):
            raise PermissionDenied
        form = WorkflowActionForm(request.POST, application=application, user=request.user)
        if form.is_valid():
            try:
                process_workflow_action(
                    application=application,
                    actor=request.user,
                    action=form.cleaned_data["action"],
                    remarks=form.cleaned_data["remarks"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Workflow decision recorded.")
        else:
            messages.error(request, "Invalid workflow action.")
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class ScreeningReviewView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_screening(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        form = ScreeningReviewForm(request.POST)
        if form.is_valid():
            try:
                screening_record = save_screening_review(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                if screening_record.is_finalized:
                    messages.success(request, "Screening output finalized and locked.")
                else:
                    messages.success(request, "Screening review saved.")
        else:
            messages.error(request, "Complete all screening fields before saving.")
        return redirect("application-detail", pk=pk)


class ExaminationRecordView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_exam(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        form = ExamRecordForm(request.POST)
        if form.is_valid():
            try:
                exam_record = save_exam_record(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if exam_record.is_finalized:
                    messages.success(request, "Examination output finalized and locked.")
                else:
                    messages.success(request, "Examination record saved.")
        else:
            messages.error(request, "Complete the required examination fields before saving.")
        return redirect("application-detail", pk=pk)


class InterviewSessionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        form = InterviewSessionForm(request.POST)
        if form.is_valid():
            try:
                interview_session = save_interview_session(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if interview_session.is_finalized:
                    messages.success(request, "Interview session finalized and locked.")
                else:
                    messages.success(request, "Interview session schedule saved.")
        else:
            messages.error(request, "Complete the required interview scheduling fields before saving.")
        return redirect("application-detail", pk=pk)


class InterviewRatingView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_rating(request.user, application):
            raise PermissionDenied

        form = InterviewRatingForm(request.POST)
        if form.is_valid():
            try:
                save_interview_rating(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Interview rating saved.")
        else:
            messages.error(request, "Complete the required interview rating fields before saving.")
        return redirect("application-detail", pk=pk)


class InterviewFallbackUploadView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_interview_fallback(request.user, application):
            raise PermissionDenied

        form = InterviewFallbackUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                upload_interview_fallback_rating(
                    application=application,
                    actor=request.user,
                    uploaded_file=form.cleaned_data["file"],
                    remarks=form.cleaned_data["remarks"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Fallback interview rating sheet uploaded to the Evidence Vault.")
        else:
            messages.error(request, "Provide the scanned fallback rating sheet and upload remarks.")
        return redirect("application-detail", pk=pk)


class DeliberationRecordView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_deliberation(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        form = DeliberationRecordForm(request.POST)
        if form.is_valid():
            try:
                deliberation_record = save_deliberation_record(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if deliberation_record.is_finalized:
                    messages.success(request, "Deliberation record finalized and locked.")
                else:
                    messages.success(request, "Deliberation record saved.")
        else:
            messages.error(request, "Complete the required deliberation fields before saving.")
        return redirect("application-detail", pk=pk)


class ComparativeAssessmentReportView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_comparative_assessment_report(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        form = ComparativeAssessmentReportForm(request.POST)
        if form.is_valid():
            try:
                report = generate_comparative_assessment_report(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if report.is_finalized:
                    messages.success(request, "Comparative Assessment Report finalized and locked.")
                else:
                    messages.success(request, "Comparative Assessment Report generated.")
        else:
            messages.error(request, "Provide the CAR notes before generating the report.")
        return redirect("application-detail", pk=pk)


class FinalDecisionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_record_final_decision(request.user, application):
            raise PermissionDenied

        form = FinalDecisionForm(request.POST)
        if form.is_valid():
            try:
                decision = record_final_decision(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Final decision recorded as "
                    f"{decision.get_decision_outcome_display().lower()}.",
                )
        else:
            messages.error(request, "Choose the final outcome and provide the decision remarks.")
        return redirect("application-detail", pk=pk)


class WorkflowOverrideView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        form = WorkflowOverrideForm(request.POST)
        if form.is_valid():
            try:
                grant_secretariat_override(
                    application=application,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Secretariat override granted and audit-logged.")
        else:
            messages.error(request, "Override reason is required.")
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("dashboard")


class WorkflowReopenView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        form = WorkflowReopenForm(request.POST)
        if form.is_valid():
            try:
                reopen_recruitment_case(
                    application=application,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Recruitment case reopened under controlled action.")
        else:
            messages.error(request, "A reopen reason is required.")
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class ExportApplicationBundleView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            request.user.role
            in {
                RecruitmentUser.Role.HRM_CHIEF,
                RecruitmentUser.Role.APPOINTING_AUTHORITY,
            }
            and user_can_view_application(request.user, application)
        ):
            raise PermissionDenied
        bundle = build_export_bundle(application, request.user)
        response = HttpResponse(bundle, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{application.reference_number}.zip"'
        return response
