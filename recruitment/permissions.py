from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin


class RecruiterRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Recruiter-only class-based views (anonymous -> login, wrong role -> 403)."""

    def test_func(self):
        return self.request.user.is_recruiter


class CandidateRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Candidate-only class-based views."""

    def test_func(self):
        return self.request.user.is_candidate
