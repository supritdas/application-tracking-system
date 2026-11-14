import logging
from collections import Counter
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.db import IntegrityError
from django.db.models import Avg, Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import (
    CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView,
)
from django_htmx.http import HttpResponseClientRedirect

from .forms import ApplyForm, InterviewForm, JobForm, RegisterForm
from .models import Application, Interview, Job, Resume
from .models import ProcessingStatus as PS
from .permissions import CandidateRequiredMixin, RecruiterRequiredMixin
from .tasks import enqueue_application_match, enqueue_resume_parse, match_application

logger = logging.getLogger(__name__)


def wants_fragment(request) -> bool:
    """True for HTMX partial requests. history_restore_request (Back button cache miss) needs a FULL page."""
    htmx = request.htmx
    return bool(htmx) and not htmx.history_restore_request


def role_home(user) -> str:
    return reverse("recruitment:dashboard") if user.is_recruiter else reverse("recruitment:job_list")


# ============================ Accounts ============================
class RoleAwareLoginView(LoginView):
    template_name = "recruitment/login.html"
    redirect_authenticated_user = True

    def get_default_redirect_url(self):  # used only when there is no safe ?next=
        user = self.request.user
        return role_home(user) if user.is_authenticated else super().get_default_redirect_url()


class RegisterView(CreateView):
    form_class = RegisterForm
    template_name = "recruitment/register.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(role_home(request.user))
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)  # auto-login after signup
        messages.success(self.request, f"Welcome, {user.first_name}!")
        return redirect(role_home(user))


# ============================ Jobs (public) ============================
class JobListView(ListView):
    paginate_by = 10
    context_object_name = "jobs"

    def get_template_names(self):
        return ["recruitment/_job_cards.html"] if wants_fragment(self.request) else ["recruitment/job_list.html"]

    def get_queryset(self):
        today = timezone.localdate()
        qs = Job.objects.filter(status=Job.Status.OPEN).filter(Q(deadline__isnull=True) | Q(deadline__gte=today))
        g = self.request.GET
        if q := g.get("q", "").strip():
            qs = qs.filter(
                Q(title__icontains=q) | Q(company_name__icontains=q) | Q(description__icontains=q)
                | Q(required_skills__contains=[q.lower()])  # JSON containment (PostgreSQL)
            )
        if job_type := g.get("job_type"):
            qs = qs.filter(job_type=job_type)
        if g.get("remote") == "1":
            qs = qs.filter(is_remote=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        qs = params.urlencode()
        ctx["query_string"] = f"{qs}&" if qs else ""  # keeps filters across pages
        ctx["job_types"] = Job.JobType.choices
        return ctx


class JobDetailView(DetailView):
    template_name = "recruitment/job_detail.html"
    context_object_name = "job"

    def get_queryset(self):
        # Open jobs are public; drafts/closed are visible only to their owner.
        user = self.request.user
        visible = Q(status=Job.Status.OPEN)
        if user.is_authenticated and user.is_recruiter:
            visible |= Q(recruiter=user)
        return Job.objects.select_related("recruiter").filter(visible)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["has_applied"] = (
            user.is_authenticated and user.is_candidate
            and self.object.applications.filter(candidate=user).exists()
        )
        return ctx


# ============================ Jobs (recruiter CRUD) ============================
class OwnJobsMixin(RecruiterRequiredMixin):
    """Scopes every query to the logged-in recruiter's jobs (others get a 404)."""

    def get_queryset(self):
        return Job.objects.filter(recruiter=self.request.user)


class ManageJobsView(RecruiterRequiredMixin, ListView):
    template_name = "recruitment/job_manage.html"
    context_object_name = "jobs"

    def get_queryset(self):
        return Job.objects.filter(recruiter=self.request.user).annotate(app_count=Count("applications"))


class JobCreateView(RecruiterRequiredMixin, CreateView):
    form_class = JobForm
    template_name = "recruitment/job_form.html"
    success_url = reverse_lazy("recruitment:job_manage")

    def get_initial(self):
        return {"company_name": self.request.user.company_name}

    def form_valid(self, form):
        form.instance.recruiter = self.request.user  # never trust the client for ownership
        messages.success(self.request, "Job posted.")
        return super().form_valid(form)


class JobUpdateView(OwnJobsMixin, UpdateView):
    form_class = JobForm
    template_name = "recruitment/job_form.html"
    success_url = reverse_lazy("recruitment:job_manage")

    def form_valid(self, form):
        messages.success(self.request, "Job updated.")
        return super().form_valid(form)


class JobDeleteView(OwnJobsMixin, DeleteView):
    template_name = "recruitment/job_confirm_delete.html"
    context_object_name = "job"
    success_url = reverse_lazy("recruitment:job_manage")

    def form_valid(self, form):
        messages.success(self.request, "Job deleted.")
        return super().form_valid(form)


class JobToggleStatusView(RecruiterRequiredMixin, View):
    """HTMX: open <-> closed (draft -> open). Returns the updated table row."""

    def post(self, request, pk):
        job = get_object_or_404(
            Job.objects.annotate(app_count=Count("applications")), pk=pk, recruiter=request.user
        )
        job.status = Job.Status.CLOSED if job.status == Job.Status.OPEN else Job.Status.OPEN
        job.save(update_fields=["status", "updated_at"])
        if request.htmx:
            return render(request, "recruitment/_manage_row.html", {"job": job})
        return redirect("recruitment:job_manage")


# ============================ Candidate: apply & track ============================
class ApplyView(CandidateRequiredMixin, FormView):
    template_name = "recruitment/apply.html"
    form_class = ApplyForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.job = get_object_or_404(Job, pk=kwargs["pk"], status=Job.Status.OPEN)

    def _blocked(self):
        """Redirect away if the candidate shouldn't see or submit the form."""
        if not self.job.is_accepting_applications:
            messages.error(self.request, "This job is no longer accepting applications.")
            return redirect(self.job)
        if Application.objects.filter(job=self.job, candidate=self.request.user).exists():
            messages.info(self.request, "You have already applied to this job.")
            return redirect("recruitment:my_applications")

    def get(self, request, *args, **kwargs):
        if blocked := self._blocked():
            return blocked
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if blocked := self._blocked():
            return blocked
        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(job=self.job, candidate=self.request.user)
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(job=self.job, **kwargs)

    def form_valid(self, form):
        try:
            application = form.save()
        except IntegrityError:
            messages.info(self.request, "You have already applied to this job.")
            return redirect("recruitment:my_applications")
        enqueue_resume_parse(application.resume)       # no-op if this resume was already parsed
        enqueue_application_match(application.pk)      # no-op until the resume is parsed
        messages.success(self.request, f"Application submitted for \u201c{self.job.title}\u201d.")
        return redirect("recruitment:my_applications")


class MyApplicationsView(CandidateRequiredMixin, ListView):
    template_name = "recruitment/my_applications.html"
    context_object_name = "applications"

    def get_queryset(self):
        return (
            Application.objects.filter(candidate=self.request.user)
            .select_related("job", "resume").prefetch_related("interviews")
            .order_by("-applied_at")  # candidates never see the AI score
        )


class WithdrawView(CandidateRequiredMixin, View):
    """HTMX: deletes the row on success (empty 200 + outerHTML swap removes it)."""

    def post(self, request, pk):
        app = get_object_or_404(Application, pk=pk, candidate=request.user)  # own applications only
        if not app.can_withdraw:
            messages.error(request, "This application can no longer be withdrawn.")
            if request.htmx:
                return HttpResponseClientRedirect(reverse("recruitment:my_applications"))
            return redirect("recruitment:my_applications")
        app.delete()
        if request.htmx:
            return HttpResponse("")
        messages.success(request, "Application withdrawn.")
        return redirect("recruitment:my_applications")


@method_decorator(xframe_options_sameorigin, name="dispatch")
class ResumeDownloadView(LoginRequiredMixin, View):
    """The ONLY way to reach a resume file. Unauthorized users get 404, not 403."""

    def get(self, request, pk):
        user = request.user
        if user.is_candidate:
            qs = Resume.objects.filter(owner=user)
        elif user.is_recruiter:
            # Recruiters may open resumes only from applications to THEIR jobs.
            qs = Resume.objects.filter(applications__job__recruiter=user).distinct()
        else:
            qs = Resume.objects.none()
        resume = get_object_or_404(qs, pk=pk)

        try:
            handle = resume.file.open("rb")
        except FileNotFoundError:
            raise Http404
        response = FileResponse(handle, content_type="application/pdf", filename=resume.original_filename)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


# ============================ Recruiter dashboard ============================
def recruiter_apps(user):
    """Every recruiter-facing query starts here: only applications to the user's OWN jobs.
    Anything else is a 404, so IDs can't be probed."""
    return Application.objects.filter(job__recruiter=user)


class DashboardView(RecruiterRequiredMixin, TemplateView):
    template_name = "recruitment/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user, now = self.request.user, timezone.now()
        apps = recruiter_apps(user)

        ctx["totals"] = apps.aggregate(
            total=Count("pk"),
            last_30=Count("pk", filter=Q(applied_at__gte=now - timedelta(days=30))),
            avg_score=Avg("match_score"),
            strong=Count("pk", filter=Q(match_score__gte=75)),
        )
        by_status = dict(apps.order_by().values_list("status").annotate(n=Count("pk")))
        ctx["stages"] = [
            {"label": label, "count": by_status.get(value, 0)} for value, label in Application.Status.choices
        ]
        ctx["jobs"] = (
            Job.objects.filter(recruiter=user)
            .annotate(
                n=Count("applications"),
                avg=Avg("applications__match_score"),
                new=Count("applications", filter=Q(applications__status=Application.Status.APPLIED)),
            )
            .order_by("-created_at")
        )
        ctx["interviews"] = Interview.objects.filter(
            application__job__recruiter=user, status=Interview.Status.SCHEDULED, scheduled_at__gte=now
        ).select_related("application__candidate", "application__job")[:8]
        # Skills gap: which required skills do applicants lack most often? (last 500 applications)
        missing = apps.order_by("-applied_at").values_list("missing_skills", flat=True)[:500]
        ctx["gaps"] = Counter(s for lst in missing for s in lst).most_common(8)
        return ctx


class JobApplicantsView(RecruiterRequiredMixin, ListView):
    paginate_by = 20
    context_object_name = "applications"

    @cached_property
    def job(self):  # lazy: runs after the permission mixin, so request.user is a real recruiter
        return get_object_or_404(Job, pk=self.kwargs["pk"], recruiter=self.request.user)

    def get_template_names(self):
        if wants_fragment(self.request):
            return ["recruitment/_applicant_rows.html"]
        return ["recruitment/job_applicants.html"]

    def get_queryset(self):
        qs = Application.objects.filter(job=self.job).select_related("candidate", "resume")
        g = self.request.GET
        if g.get("status") in Application.Status.values:
            qs = qs.filter(status=g["status"])
        if g.get("min_score", "").isdigit():
            qs = qs.filter(match_score__gte=min(int(g["min_score"]), 100))
        if q := g.get("q", "").strip():
            qs = qs.filter(
                Q(candidate__first_name__icontains=q) | Q(candidate__last_name__icontains=q)
                | Q(candidate__email__icontains=q)
            )
        # Skill filter: the parsed resume must contain ALL listed skills (jsonb @>).
        skills = [s.strip().lower() for s in g.get("skills", "").split(",") if s.strip()][:10]
        if skills:
            qs = qs.filter(resume__parsed_data__skills__contains=skills)
        if g.get("sort") == "newest":
            qs = qs.order_by("-applied_at")
        return qs  # default: Meta ordering (score DESC, NULLs last)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        qs = params.urlencode()
        ctx.update(job=self.job, statuses=Application.Status.choices, query_string=f"{qs}&" if qs else "")
        return ctx


class ApplicationDetailView(RecruiterRequiredMixin, DetailView):
    template_name = "recruitment/application_detail.html"
    context_object_name = "app"

    def get_queryset(self):
        return (
            recruiter_apps(self.request.user)
            .select_related("job", "candidate", "resume").prefetch_related("interviews")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("interview_form", InterviewForm())
        return ctx

    def post(self, request, *args, **kwargs):
        """Schedule an interview (re-renders with errors if invalid)."""
        self.object = app = self.get_object()
        form = InterviewForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(interview_form=form))
        interview = form.save(commit=False)
        interview.application, interview.created_by = app, request.user
        interview.save()
        if app.status in (Application.Status.APPLIED, Application.Status.SCREENING):
            app.status = Application.Status.INTERVIEW
            app.save(update_fields=["status", "updated_at"])
        messages.success(request, "Interview scheduled.")
        return redirect("recruitment:application_detail", pk=app.pk)


class ApplicationStatusView(RecruiterRequiredMixin, View):
    """HTMX from the list (returns the row) or a plain form from the detail page (redirects)."""

    def post(self, request, pk):
        app = get_object_or_404(recruiter_apps(request.user).select_related("candidate", "resume"), pk=pk)
        new = request.POST.get("status")
        if new not in Application.Status.values:
            return HttpResponseBadRequest("Invalid status.")
        if new != app.status:
            app.status = new
            app.save(update_fields=["status", "updated_at"])
            if new == Application.Status.REJECTED:  # don't leave ghost interviews on the calendar
                app.interviews.filter(status=Interview.Status.SCHEDULED).update(status=Interview.Status.CANCELLED)
        if request.htmx:
            return render(request, "recruitment/_applicant_row.html", {"app": app})
        return redirect("recruitment:application_detail", pk=app.pk)


class NotesView(RecruiterRequiredMixin, View):
    def post(self, request, pk):
        app = get_object_or_404(recruiter_apps(request.user), pk=pk)
        app.recruiter_notes = request.POST.get("recruiter_notes", "")[:5000]
        app.save(update_fields=["recruiter_notes", "updated_at"])
        messages.success(request, "Notes saved.")
        return redirect("recruitment:application_detail", pk=pk)


class InterviewStatusView(RecruiterRequiredMixin, View):
    def post(self, request, pk):
        iv = get_object_or_404(Interview.objects.filter(application__job__recruiter=request.user), pk=pk)
        new = request.POST.get("status")
        if new not in (Interview.Status.COMPLETED, Interview.Status.CANCELLED):
            return HttpResponseBadRequest("Invalid status.")
        iv.status = new
        iv.save(update_fields=["status"])
        return redirect("recruitment:application_detail", pk=iv.application_id)


class AIPanelView(RecruiterRequiredMixin, View):
    """HTMX polling target while scoring is in flight."""

    def get(self, request, pk):
        app = get_object_or_404(recruiter_apps(request.user), pk=pk)
        return render(request, "recruitment/_ai_panel.html", {"app": app})


class RescoreView(RecruiterRequiredMixin, View):
    """Manual re-score (e.g. after the job was edited). Costs one Groq call."""

    def post(self, request, pk):
        app = get_object_or_404(recruiter_apps(request.user).select_related("resume"), pk=pk)
        if app.resume.parse_status == PS.DONE and app.ai_status != PS.PROCESSING:
            Application.objects.filter(pk=pk).update(ai_status=PS.PENDING, ai_error="")
            app.ai_status = PS.PENDING
            try:
                match_application.delay(pk, True)  # force=True re-claims a DONE application
            except Exception:
                logger.exception("Could not enqueue match_application(%s)", pk)
                Application.objects.filter(pk=pk).update(ai_status=PS.FAILED, ai_error="Task queue unavailable.")
                app.ai_status, app.ai_error = PS.FAILED, "Task queue unavailable."
        return render(request, "recruitment/_ai_panel.html", {"app": app})
