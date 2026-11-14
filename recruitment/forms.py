from django import forms
from django.conf import settings
from django.contrib.auth.forms import BaseUserCreationForm
from django.core.validators import FileExtensionValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Application, Interview, Job, Resume, User
from .validators import validate_pdf_signature, validate_resume_size


# ============================ Accounts ============================
class RegisterForm(BaseUserCreationForm):
    """Email + password signup with a role choice (password1/2 come from the base class)."""

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "role", "company_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        self.fields["company_name"].help_text = "Required for recruiters."

    def clean_email(self):
        return self.cleaned_data["email"].lower()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") == User.Role.RECRUITER and not cleaned.get("company_name"):
            self.add_error("company_name", "Recruiters must provide a company name.")
        return cleaned


class AdminUserCreationForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "role")


# ============================ Jobs ============================
class JobForm(forms.ModelForm):
    # Declared field overrides the JSONField widget; clean_ returns a list.
    required_skills = forms.CharField(
        required=False, help_text="Comma-separated, e.g. Python, Django, PostgreSQL",
    )

    class Meta:
        model = Job
        fields = [
            "title", "company_name", "location", "is_remote", "job_type",
            "description", "required_skills", "min_experience_years", "deadline", "status",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 10}),
            "deadline": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:  # list -> "a, b, c" for editing
            self.initial["required_skills"] = ", ".join(self.instance.required_skills)

    def clean_required_skills(self):
        raw = self.cleaned_data["required_skills"]
        return [s.strip() for s in raw.split(",") if s.strip()]  # normalised again in Job.save()

    def clean_deadline(self):
        deadline = self.cleaned_data.get("deadline")
        if deadline and "deadline" in self.changed_data and deadline < timezone.localdate():
            raise forms.ValidationError("Deadline cannot be in the past.")
        return deadline


# ============================ Applications ============================
class ApplyForm(forms.Form):
    resume = forms.ModelChoiceField(
        queryset=Resume.objects.none(), required=False,
        empty_label="- Upload a new resume instead -", label="Use a previous resume",
    )
    new_resume = forms.FileField(
        required=False, label="Upload a PDF resume",
        validators=[FileExtensionValidator(["pdf"]), validate_resume_size, validate_pdf_signature],
    )
    cover_letter = forms.CharField(required=False, max_length=5000, widget=forms.Textarea(attrs={"rows": 6}))

    def __init__(self, *args, job, candidate, **kwargs):
        super().__init__(*args, **kwargs)
        self.job, self.candidate = job, candidate

        # Only the candidate's own resumes can ever be selected (prevents IDOR).
        resumes = candidate.resumes.all()
        if resumes.exists():
            self.fields["resume"].queryset = resumes
            self.fields["resume"].label_from_instance = (
                lambda r: f"{r.original_filename} (uploaded {r.uploaded_at:%b %d, %Y})"
            )
        else:
            del self.fields["resume"]

        self.fields["new_resume"].widget.attrs["accept"] = "application/pdf"
        self.fields["new_resume"].help_text = (
            f"PDF only, up to {settings.MAX_RESUME_SIZE_MB} MB. "
            "If you upload a file, it is used instead of the selection above."
        )

    def clean(self):
        cleaned = super().clean()
        # State can change between GET and POST, so re-check here.
        if not self.job.is_accepting_applications:
            raise forms.ValidationError("This job is no longer accepting applications.")
        if Application.objects.filter(job=self.job, candidate=self.candidate).exists():
            raise forms.ValidationError("You have already applied to this job.")
        if self.errors:  # field errors (bad PDF etc.) take priority over the "choose one" message
            return cleaned
        if not cleaned.get("resume") and not cleaned.get("new_resume"):
            raise forms.ValidationError("Select a previous resume or upload a PDF.")
        return cleaned

    def save(self) -> Application:
        cd = self.cleaned_data
        upload, new_resume = cd.get("new_resume"), None
        try:
            with transaction.atomic():
                resume = cd.get("resume")
                if upload:
                    resume = new_resume = Resume.objects.create(
                        owner=self.candidate, file=upload, original_filename=upload.name[:255],
                    )
                return Application.objects.create(
                    job=self.job, candidate=self.candidate, resume=resume, cover_letter=cd["cover_letter"],
                )
        except IntegrityError:  # double-submit race hit the unique constraint
            if new_resume:
                new_resume.file.delete(save=False)  # don't leave an orphaned file on disk
            raise


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        fields = ["scheduled_at", "duration_minutes", "mode", "location", "notes"]
        widgets = {
            "scheduled_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean_scheduled_at(self):
        when = self.cleaned_data["scheduled_at"]
        if when <= timezone.now():
            raise forms.ValidationError("Interview must be scheduled in the future.")
        return when
