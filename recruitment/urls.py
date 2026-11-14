from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "recruitment"

urlpatterns = [
    # --- Accounts ---
    path("accounts/login/", views.RoleAwareLoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),  # POST-only in Django 5
    path("accounts/register/", views.RegisterView.as_view(), name="register"),
    # --- Public jobs ---
    path("", views.JobListView.as_view(), name="job_list"),
    path("jobs/<int:pk>/", views.JobDetailView.as_view(), name="job_detail"),
    # --- Candidate ---
    path("jobs/<int:pk>/apply/", views.ApplyView.as_view(), name="apply"),
    path("my/applications/", views.MyApplicationsView.as_view(), name="my_applications"),
    path("my/applications/<int:pk>/withdraw/", views.WithdrawView.as_view(), name="withdraw"),
    path("resumes/<int:pk>/download/", views.ResumeDownloadView.as_view(), name="resume_download"),
    # --- Recruiter: jobs ---
    path("recruiter/jobs/", views.ManageJobsView.as_view(), name="job_manage"),
    path("recruiter/jobs/new/", views.JobCreateView.as_view(), name="job_create"),
    path("recruiter/jobs/<int:pk>/edit/", views.JobUpdateView.as_view(), name="job_update"),
    path("recruiter/jobs/<int:pk>/delete/", views.JobDeleteView.as_view(), name="job_delete"),
    path("recruiter/jobs/<int:pk>/toggle/", views.JobToggleStatusView.as_view(), name="job_toggle"),
    # --- Recruiter: dashboard ---
    path("recruiter/dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("recruiter/jobs/<int:pk>/applicants/", views.JobApplicantsView.as_view(), name="job_applicants"),
    path("recruiter/applications/<int:pk>/", views.ApplicationDetailView.as_view(), name="application_detail"),
    path("recruiter/applications/<int:pk>/status/", views.ApplicationStatusView.as_view(), name="application_status"),
    path("recruiter/applications/<int:pk>/notes/", views.NotesView.as_view(), name="application_notes"),
    path("recruiter/applications/<int:pk>/rescore/", views.RescoreView.as_view(), name="application_rescore"),
    path("recruiter/applications/<int:pk>/ai-panel/", views.AIPanelView.as_view(), name="ai_panel"),
    path("recruiter/interviews/<int:pk>/status/", views.InterviewStatusView.as_view(), name="interview_status"),
]
