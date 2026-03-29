from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0009_alter_auditlog_action_examrecord"),
    ]

    operations = [
        migrations.AddField(
            model_name="recruitmentapplication",
            name="performance_rating_not_applicable",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="evidencevaultitem",
            name="document_type",
            field=models.CharField(
                blank=True,
                choices=[
                    (
                        "signed_cover_letter",
                        "Signed Cover Letter addressed to VOLTAIRE S. GUADALUPE, MD, MPH, MAHPS, Director IV",
                    ),
                    (
                        "personal_data_sheet",
                        "Personal Data Sheet (CS Form No. 212, Revised 2025) with recent passport-sized picture",
                    ),
                    ("work_experience_sheet", "Work Experience Sheet"),
                    ("performance_rating", "Performance Rating in the last rating period"),
                    ("eligibility_or_license", "Certificate of Eligibility, Rating, or License"),
                    ("transcript_of_records", "Authenticated Transcript of Records"),
                    ("diploma", "Diploma"),
                    ("certificate_of_employment", "Certificate of Employment"),
                    ("training_certificates", "Training Certificates"),
                ],
                db_index=True,
                max_length=50,
            ),
        ),
    ]
