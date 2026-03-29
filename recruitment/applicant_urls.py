from django.urls import path

from .portal_views import (
    ApplicantOTPView,
    ApplicantPortalIntakeView,
    ApplicantPortalView,
    ApplicantReceiptView,
    ApplicantStatusLookupView,
)


urlpatterns = [
    path("", ApplicantPortalView.as_view(), name="applicant-portal"),
    path("status/", ApplicantStatusLookupView.as_view(), name="applicant-status-lookup"),
    path("entries/<int:pk>/", ApplicantPortalIntakeView.as_view(), name="applicant-intake"),
    path("<uuid:token>/otp/", ApplicantOTPView.as_view(), name="applicant-otp"),
    path("<uuid:token>/receipt/", ApplicantReceiptView.as_view(), name="applicant-receipt"),
]
