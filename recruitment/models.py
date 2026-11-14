import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from .managers import UserManager
from .validators import validate_resume_size


# ============================ Users ============================
class User(AbstractUser):
    """Custom user: email login + a single role field for RBAC."""

    class Role(models.TextChoices):
        RECRUITER = "recruiter", "Recruiter"
        CANDIDATE = "candidate", "Candidate"

    username = None  # email replaces username
    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CANDIDATE, db_index=True)
    phone = models.CharField(max_length=20, blank=True)
    company_name = models.CharField(max_length=150, blank=True, help_text="Recruiters only.")

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    @property
    def is_recruiter(self) -> bool:
        return self.role == self.Role.RECRUITER

    @property
    def is_candidate(self) -> bool:
        return self.role == self.Role.CANDIDATE


# ============================ Jobs ============================
class Job(models.Model):
    class JobType(models.TextChoices):
        FULL_TIME = "full_time", "Full-time"
        PART_TIME = "part_time", "Part-time"
        CONTRACT = "contract", "Contract"
        INTERNSHIP = "internship", "Internship"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    recruiter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="posted_jobs",
        limit_choices_to={"role": "recruiter"},
    )
    title = models.CharField(max_length=200)
    company_name = models.CharField(max_length=150)
    location = models.CharField(max_length=150, blank=True)
    is_remote = models.BooleanField(default=False)
    job_type = models.CharField(max_length=20, choices=JobType.choices, default=JobType.FULL_TIME)
    description = models.TextField(help_text="Full job description; fed to the AI matcher.")

    # List of lowercase strings, e.g. ["python", "django", "postgresql"]
    required_skills = models.JSONField(default=list, blank=True)
    min_experience_years = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    deadline = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"{self.title} @ {self.company_name}"

    def get_absolute_url(self):
        return reverse("recruitment:job_detail", kwargs={"pk": self.pk})

    @property
    def is_accepting_applications(self) -> bool:
        """Single source of truth: open AND deadline not passed."""
        return self.status == self.Status.OPEN and (
            self.deadline is None or self.deadline >= timezone.localdate()
        )

    def save(self, *args, **kwargs):
        # Normalise skills so matching/filtering is case-insensitive.
        self.required_skills = sorted({str(s).strip().lower() for s in self.required_skills if str(s).strip()})
        super().save(*args, **kwargs)


# ============================ Resumes & applications ============================
def resume_upload_path(instance, filename):
    # Random filename avoids collisions and doesn't leak candidate names in paths.
    return f"resumes/{instance.owner_id}/{uuid.uuid4().hex}.pdf"


class ProcessingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class Resume(models.Model):
    """An uploaded PDF plus everything the AI pipeline derives from it."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="resumes",
        limit_choices_to={"role": "candidate"},
    )
    file = models.FileField(
        upload_to=resume_upload_path,
        validators=[FileExtensionValidator(["pdf"]), validate_resume_size],
    )
    original_filename = models.CharField(max_length=255)
    extracted_text = models.TextField(blank=True)
    parsed_data = models.JSONField(default=dict, blank=True)  # normalised Groq output
    parse_status = models.CharField(
        max_length=12, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING, db_index=True
    )
    parse_error = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.owner.email})"


class Application(models.Model):
    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        SCREENING = "screening", "Screening"
        INTERVIEW = "interview", "Interview"
        OFFER = "offer", "Offer"
        HIRED = "hired", "Hired"
        REJECTED = "rejected", "Rejected"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    candidate = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="applications",
        limit_choices_to={"role": "candidate"},
    )
    # RESTRICT: a resume in use can't be deleted on its own, but deleting the candidate's account
    # (which cascades to both the applications and the resumes) still works. PROTECT would block that.
    resume = models.ForeignKey(Resume, on_delete=models.RESTRICT, related_name="applications")
    cover_letter = models.TextField(blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.APPLIED, db_index=True)
    recruiter_notes = models.TextField(blank=True)

    # --- AI matching output ---
    match_score = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(100)], db_index=True,
        help_text="0-100 job fit score, computed in Python.",
    )
    ai_summary = models.TextField(blank=True)
    matched_skills = models.JSONField(default=list, blank=True)
    missing_skills = models.JSONField(default=list, blank=True)
    ai_status = models.CharField(
        max_length=12, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING, db_index=True
    )
    ai_error = models.TextField(blank=True)

    applied_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # PostgreSQL sorts NULLs first on DESC; unscored applications must go last.
        ordering = [models.F("match_score").desc(nulls_last=True), "-applied_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "candidate"], name="unique_application_per_job"),
        ]
        indexes = [models.Index(fields=["job", "status"])]

    def __str__(self):
        return f"{self.candidate.email} -> {self.job.title}"

    @property
    def can_withdraw(self) -> bool:
        # Once a recruiter has moved past screening, the candidate can't delete the record.
        return self.status in (self.Status.APPLIED, self.Status.SCREENING)


class Interview(models.Model):
    class Mode(models.TextChoices):
        VIDEO = "video", "Video call"
        PHONE = "phone", "Phone"
        ONSITE = "onsite", "On-site"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="interviews")
    scheduled_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(
        default=45, validators=[MinValueValidator(15), MaxValueValidator(480)]
    )
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.VIDEO)
    location = models.CharField(max_length=300, blank=True, help_text="Meeting link or address.")
    notes = models.TextField(blank=True, help_text="Internal. Never shown to the candidate.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_at"]
        indexes = [models.Index(fields=["status", "scheduled_at"])]

    def __str__(self):
        return f"{self.application} @ {self.scheduled_at:%Y-%m-%d %H:%M}"

    @property
    def is_link(self) -> bool:
        # Only http(s) values become <a href>; blocks javascript: URLs typed into the field.
        return self.location.startswith(("http://", "https://"))
