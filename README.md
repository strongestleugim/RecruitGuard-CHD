# RecruitGuard-CHD

RecruitGuard-CHD is a secure-by-design Django prototype for DOH-CHD CALABARZON recruitment. The current baseline includes internal identity and access control, applicant intake with OTP verification, recruitment entry management, branch-aware workflow handling, level-aware routing, stage-locked records, notification history, encrypted evidence storage, audit logging, and controlled export.

The UI is split into two portals while keeping one shared backend, one shared database, and one shared workflow engine:

- Applicant Portal under `/apply/` for public intake, OTP verification, receipt, and status lookup
- Internal Portal under `/internal/` for staff authentication, routing, workflow, evaluation, records, audit, and export

## Implemented scope

- One Django platform and one database schema
- Internal authentication and role-aware access control for the locked internal actor set
- Position catalog plus branch-aware recruitment entry management for Plantilla and COS
- Public applicant intake, OTP verification, receipt, and status lookup
- Branch-aware applications for Plantilla and COS
- Level-aware submission routing with Level 1 -> Secretariat and Level 2 -> HRM Chief
- Secretariat is blocked from processing Level 2 unless a system-admin override is granted and audit-logged
- Branch-aware workflow handling for Plantilla and COS
- Plantilla supports screening, exam, interview, deliberation, Comparative Assessment Report generation, final decision, completion tracking, and controlled case closure
- COS follows a lighter path, skips HRMPSB endorsement, and supports the applicable screening, interview, deliberation, final decision, completion, and closure steps
- Notification logging for submission, selection, non-selection, checklist, and reminder emails
- Evidence Vault with SHA-256 digesting and AES-256-GCM encryption at rest
- Application and system audit trails, routing history, and protected-record access logging
- Controlled export package using ReportLab and zipfile with evidence inventory, routing history, audit log, submission packet, and verification outputs
- Automated tests covering the major workflow, audit, notification, completion, and export paths

## Local setup

### Quick start on Windows PowerShell

```powershell
cd C:\Users\Jerico\Documents\RecruitGuard-CHD
.\.venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py runserver
```

Open these URLs after the server starts:

- Applicant portal: `http://127.0.0.1:8000/apply/`
- Internal login: `http://127.0.0.1:8000/internal/login/`
- Django admin: `http://127.0.0.1:8000/admin/`

### First-time setup

1. Create and activate a virtual environment if `.venv` does not already exist.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set the required values for:
   - `DJANGO_SECRET_KEY`
   - `APPLICATION_OTP_HASH_SECRET`
   - `EVIDENCE_ENCRYPTION_SECRET`
   - Gmail SMTP settings such as `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and `DEFAULT_FROM_EMAIL`
4. Run `python manage.py migrate`.
5. Create an admin account with `python manage.py createsuperuser`.
6. Run `python manage.py check`.
7. Run the test suite with `python manage.py test recruitment`.
8. Start the server with `python manage.py runserver`.

SQLite is used automatically for local development if PostgreSQL environment variables are not set. PostgreSQL settings remain ready for later deployment, and media/static paths plus basic security cookie settings are already configured in the base project.

## Portal entry points

- Public Applicant Portal: `/apply/`
- Internal Portal login: `/internal/login/`
- Internal Portal dashboard: `/internal/`
- Django admin: `/admin/`

## Manual role setup

Create users through Django admin and assign these roles:

- `applicant`
- `secretariat`
- `hrm_chief`
- `hrmpsb_member`
- `appointing_authority`
- `system_admin`

## Thesis-aligned boundaries

- Full onboarding, offboarding, payroll, and full employee lifecycle remain out of scope.
- Applicant pages do not expose internal workflow navigation.
- Internal portal access remains server-side protected and limited to authenticated internal users.
