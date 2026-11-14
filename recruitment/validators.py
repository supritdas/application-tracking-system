from django.conf import settings
from django.core.exceptions import ValidationError


def validate_resume_size(file):
    limit = settings.MAX_RESUME_SIZE_MB * 1024 * 1024
    if file.size > limit:
        raise ValidationError(f"Resume must be smaller than {settings.MAX_RESUME_SIZE_MB} MB.")


def validate_pdf_signature(file):
    """Cheap content check: a file named .pdf must actually start with %PDF-."""
    header = file.read(5)
    file.seek(0)
    if header != b"%PDF-":
        raise ValidationError("This file is not a valid PDF.")
