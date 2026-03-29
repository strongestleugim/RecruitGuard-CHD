from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import FormView, TemplateView

from .forms import ApplicantOTPForm, ApplicantPortalIntakeForm, ApplicantStatusLookupForm
from .models import PositionPosting, RecruitmentApplication
from .services import (
    create_public_application_draft,
    get_public_recruitment_entries,
    issue_application_otp,
    submit_application,
    verify_application_otp,
)


class ApplicantPortalView(TemplateView):
    template_name = "recruitment/applicant_portal.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["plantilla_entries"] = get_public_recruitment_entries(PositionPosting.Branch.PLANTILLA)
        context["cos_entries"] = get_public_recruitment_entries(PositionPosting.Branch.COS)
        return context


class ApplicantPortalIntakeView(FormView):
    template_name = "recruitment/applicant_intake_form.html"
    form_class = ApplicantPortalIntakeForm

    def dispatch(self, request, *args, **kwargs):
        self.entry = get_object_or_404(
            PositionPosting.objects.select_related("position_reference"),
            pk=kwargs["pk"],
        )
        if not self.entry.is_open_for_intake:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["entry"] = self.entry
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entry"] = self.entry
        return context

    def form_valid(self, form):
        try:
            application = create_public_application_draft(
                entry=self.entry,
                cleaned_data=form.cleaned_data,
                uploaded_documents=form.get_uploaded_documents(),
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        messages.success(
            self.request,
            "Your draft has been prepared. Check your email for the OTP needed to finalize submission.",
        )
        return redirect("applicant-otp", token=application.public_token)


class ApplicantOTPView(TemplateView):
    template_name = "recruitment/applicant_otp.html"

    def get_application(self):
        return get_object_or_404(
            RecruitmentApplication.objects.select_related("position", "applicant"),
            public_token=self.kwargs["token"],
        )

    def get(self, request, *args, **kwargs):
        application = self.get_application()
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = kwargs.get("application") or self.get_application()
        context["application"] = application
        context["otp_form"] = kwargs.get("otp_form") or ApplicantOTPForm()
        context["otp_validity_minutes"] = settings.APPLICATION_OTP_VALIDITY_MINUTES
        context["document_requirement_statuses"] = application.document_requirement_statuses
        context["document_requirements_complete"] = application.has_complete_required_documents
        return context

    def post(self, request, *args, **kwargs):
        application = self.get_application()
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)

        action = request.POST.get("action")
        if action == "resend":
            try:
                issue_application_otp(application)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "A new OTP has been sent to your registered email address.")
            return redirect("applicant-otp", token=application.public_token)

        if action == "verify":
            otp_form = ApplicantOTPForm(request.POST)
            if otp_form.is_valid():
                try:
                    verify_application_otp(application, otp_form.cleaned_data["otp"])
                except ValueError as exc:
                    otp_form.add_error("otp", str(exc))
                else:
                    messages.success(request, "OTP verified. You may now finalize your submission.")
                    return redirect("applicant-otp", token=application.public_token)
            return self.render_to_response(
                self.get_context_data(application=application, otp_form=otp_form)
            )

        if action == "finalize":
            try:
                submit_application(application, application.applicant)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("applicant-otp", token=application.public_token)
            messages.success(request, "Application submitted successfully.")
            return redirect("applicant-receipt", token=application.public_token)

        messages.error(request, "Unsupported applicant portal action.")
        return redirect("applicant-otp", token=application.public_token)


class ApplicantReceiptView(TemplateView):
    template_name = "recruitment/applicant_receipt.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["application"] = get_object_or_404(
            RecruitmentApplication.objects.select_related("position"),
            public_token=self.kwargs["token"],
            submitted_at__isnull=False,
        )
        return context


class ApplicantStatusLookupView(FormView):
    template_name = "recruitment/applicant_status_lookup.html"
    form_class = ApplicantStatusLookupForm

    def form_valid(self, form):
        application = (
            RecruitmentApplication.objects.select_related("position")
            .filter(
                reference_number=form.cleaned_data["application_id"],
                applicant_email__iexact=form.cleaned_data["email"],
                submitted_at__isnull=False,
            )
            .first()
        )
        if not application:
            form.add_error(
                None,
                "No submitted application matched the provided Application ID and email address.",
            )
            return self.form_invalid(form)
        return self.render_to_response(self.get_context_data(form=form, application=application))
