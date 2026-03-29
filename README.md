# RecruitGuard-CHD

RecruitGuard-CHD is a secure-by-design Django prototype for DOH-CHD CALABARZON recruitment. The current baseline includes internal identity and access control, recruitment entry and vacancy management, branch-aware applications, level-aware routing, audit logging, encrypted evidence storage, and controlled export.

The UI is split into two portals while keeping one shared backend, one shared database, and one shared workflow engine:

- Applicant Portal under `/apply/` for public intake, OTP verification, receipt, and status lookup
- Internal Portal under `/internal/` for staff authentication, routing, workflow, evaluation, records, audit, and export

## Implemented scope

- One Django platform and one database schema
- Internal authentication and role-aware access control for the locked internal actor set
- Position catalog plus branch-aware recruitment entry management for Plantilla and COS
- Branch-aware applications for Plantilla and COS
- Level-aware submission routing
  - Level 1 routes to Secretariat
  - Level 2 routes to HRM Chief
  - Secretariat is blocked from processing Level 2 unless a system-admin override is granted and audit-logged
- COS follows a lighter path and skips HRMPSB endorsement
- Evidence Vault with SHA-256 digesting and AES-256-GCM encryption at rest
- Controlled export package using ReportLab and zipfile
- Admin registrations and test coverage for the core workflow

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set the required secrets and environment values.
4. Run `python manage.py migrate`.
5. Create an admin account with `python manage.py createsuperuser`.
6. Start the server with `python manage.py runserver`.

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
