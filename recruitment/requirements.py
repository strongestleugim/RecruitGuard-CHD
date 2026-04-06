from dataclasses import dataclass


@dataclass(frozen=True)
class ApplicantDocumentRequirement:
    code: str
    title: str
    help_text: str
    is_required: bool = True
    allow_not_applicable: bool = False

    @property
    def file_field_name(self):
        return self.code

    @property
    def not_applicable_field_name(self):
        return f"{self.code}_not_applicable"


SIGNED_COVER_LETTER = "signed_cover_letter"
PERSONAL_DATA_SHEET = "personal_data_sheet"
WORK_EXPERIENCE_SHEET = "work_experience_sheet"
PERFORMANCE_RATING = "performance_rating"
ELIGIBILITY_OR_LICENSE = "eligibility_or_license"
TRANSCRIPT_OF_RECORDS = "transcript_of_records"
DIPLOMA = "diploma"
CERTIFICATE_OF_EMPLOYMENT = "certificate_of_employment"
TRAINING_CERTIFICATES = "training_certificates"


APPLICANT_DOCUMENT_REQUIREMENTS = (
    ApplicantDocumentRequirement(
        code=SIGNED_COVER_LETTER,
        title="Signed Cover Letter addressed to VOLTAIRE S. GUADALUPE, MD, MPH, MAHPS, Director IV",
        help_text="Upload the signed cover letter for this application.",
    ),
    ApplicantDocumentRequirement(
        code=PERSONAL_DATA_SHEET,
        title="Personal Data Sheet (CS Form No. 212, Revised 2025) with recent passport-sized picture",
        help_text="Upload the completed Personal Data Sheet with the required photo attached.",
    ),
    ApplicantDocumentRequirement(
        code=WORK_EXPERIENCE_SHEET,
        title="Work Experience Sheet",
        help_text="Upload the Work Experience Sheet that accompanies the Personal Data Sheet.",
    ),
    ApplicantDocumentRequirement(
        code=PERFORMANCE_RATING,
        title="Performance Rating in the last rating period",
        help_text="Upload your latest performance rating, or mark this requirement as not applicable.",
        is_required=False,
        allow_not_applicable=True,
    ),
    ApplicantDocumentRequirement(
        code=ELIGIBILITY_OR_LICENSE,
        title="Certificate of Eligibility, Rating, or License",
        help_text="Upload the eligibility, rating, or license document relevant to this application.",
    ),
    ApplicantDocumentRequirement(
        code=TRANSCRIPT_OF_RECORDS,
        title="Authenticated Transcript of Records",
        help_text="Upload the authenticated Transcript of Records.",
    ),
    ApplicantDocumentRequirement(
        code=DIPLOMA,
        title="Diploma",
        help_text="Upload a copy of your diploma.",
    ),
    ApplicantDocumentRequirement(
        code=CERTIFICATE_OF_EMPLOYMENT,
        title="Certificate of Employment",
        help_text="Upload your certificate or certificates of employment.",
    ),
    ApplicantDocumentRequirement(
        code=TRAINING_CERTIFICATES,
        title="Training Certificates",
        help_text="Upload your certificate or certificates for completed training courses.",
    ),
)


APPLICANT_DOCUMENT_REQUIREMENTS_BY_CODE = {
    requirement.code: requirement for requirement in APPLICANT_DOCUMENT_REQUIREMENTS
}

APPLICANT_DOCUMENT_TYPE_CHOICES = [
    (requirement.code, requirement.title) for requirement in APPLICANT_DOCUMENT_REQUIREMENTS
]


def get_applicant_document_requirements():
    return APPLICANT_DOCUMENT_REQUIREMENTS


def get_required_applicant_document_requirements(*, performance_rating_not_applicable=False):
    return tuple(
        requirement
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS
        if requirement.is_required
        or (requirement.allow_not_applicable and not performance_rating_not_applicable)
    )
