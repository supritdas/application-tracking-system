import shutil
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Application, Job, Resume, User
from .models import ProcessingStatus as PS
from .services import resume_parser
from .services.matcher import MatchError, ai_assess, compute
from .services.pdf_extractor import ExtractionError, extract_text
from .services.resume_parser import ParseError, normalize, parse_resume_text
from .tasks import match_application, parse_resume

TMP_MEDIA = tempfile.mkdtemp()
PW = "pw12345!x"


def mk_user(email, role, **kw):
    return User.objects.create_user(email, PW, role=role, first_name="N", last_name=email[0], **kw)


def pdf(name="cv.pdf", content=b"%PDF-1.4\n%fake"):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def fake_job(skills, years=0):
    return SimpleNamespace(required_skills=skills, min_experience_years=years, title="Dev", description="d")


def mock_client(content, finish="stop"):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(finish_reason=finish, message=MagicMock(content=content))]
    )
    return client


# ============================ Pure services ============================
class ServiceTests(SimpleTestCase):
    def test_normalize_cleans_garbage_output(self):
        out = normalize({
            "skills": ["Python", " python ", "", None, "Django"],
            "total_experience_years": "4.26",
            "experience": [{"title": "Dev", "years": "abc"}, "junk"],
            "education": "not a list",
        })
        self.assertEqual(out["skills"], ["python", "django"])
        self.assertEqual(out["total_experience_years"], 4.3)
        self.assertEqual(len(out["experience"]), 1)
        self.assertIsNone(out["experience"][0]["years"])
        self.assertEqual(out["education"], [])

    def test_normalize_total_falls_back_to_sum_of_roles(self):
        self.assertEqual(normalize({"experience": [{"years": 2}, {"years": 1.5}]})["total_experience_years"], 3.5)

    def test_garbage_pdf_raises_extraction_error(self):
        with self.assertRaises(ExtractionError):
            extract_text(b"definitely not a pdf")

    def test_parse_uses_json_mode(self):
        client = mock_client('{"skills": ["Python"], "total_experience_years": 3}')
        with patch.object(resume_parser, "_client", return_value=client):
            out = parse_resume_text("some resume text " * 10)
        self.assertEqual(out["skills"], ["python"])
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["temperature"], 0)

    def test_parse_bad_output_raises(self):
        for content, finish in [("not json", "stop"), ('{"skills": [', "length")]:
            with patch.object(resume_parser, "_client", return_value=mock_client(content, finish)):
                with self.assertRaises(ParseError):
                    parse_resume_text("text")

    def test_weighted_score(self):
        # skills 2/3 * .7 = .467, experience 2/4 * .3 = .15  ->  61.7 -> 62
        out = compute(fake_job(["python", "django", "aws"], 4), {"skills": ["Python", "Django"], "total_experience_years": 2})
        self.assertEqual(out["score"], 62)
        self.assertEqual(out["missing"], ["aws"])

    def test_aliases_unknown_experience_and_no_criteria(self):
        self.assertEqual(compute(fake_job(["postgresql", "javascript"]), {"skills": ["Postgres", "JS"]})["score"], 100)
        self.assertEqual(compute(fake_job([], 2), {"skills": []})["score"], 50)
        self.assertIsNone(compute(fake_job([]), {"skills": ["python"]})["score"])

    def test_model_can_only_rescue_unmatched_required_skills(self):
        client = mock_client('{"additional_matches": ["AWS", "kubernetes"], "summary": " Solid fit. "}')
        with patch.object(resume_parser, "_client", return_value=client):
            extra, summary = ai_assess(fake_job(["python", "aws", "terraform"]), {"skills": ["python"]}, ["aws", "terraform"])
        self.assertEqual(extra, ["aws"])  # "kubernetes" is not a required skill, so it is dropped
        self.assertEqual(summary, "Solid fit.")

    def test_assess_bad_output_raises(self):
        for content, finish in [("nope", "stop"), ('{"summary": "x', "length")]:
            with patch.object(resume_parser, "_client", return_value=mock_client(content, finish)):
                with self.assertRaises(MatchError):
                    ai_assess(fake_job(["a"]), {}, ["a"])


# ============================ Jobs & access control ============================
class JobAccessTests(TestCase):
    def setUp(self):
        self.rec = mk_user("r@x.com", "recruiter", company_name="Acme")
        self.cand = mk_user("c@x.com", "candidate")
        self.job = Job.objects.create(
            recruiter=self.rec, title="Dev", company_name="Acme", description="d", required_skills=["Python", " python "],
        )

    def test_skills_are_normalised(self):
        self.assertEqual(self.job.required_skills, ["python"])

    def test_public_list_and_skill_search(self):
        self.assertContains(self.client.get(reverse("recruitment:job_list")), "Dev")
        self.assertContains(self.client.get(reverse("recruitment:job_list"), {"q": "python"}), "Dev")

    def test_candidate_gets_403_on_create(self):
        self.client.force_login(self.cand)
        self.assertEqual(self.client.get(reverse("recruitment:job_create")).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("recruitment:job_manage"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("recruitment:login"), resp.url)

    def test_recruiter_cannot_edit_another_recruiters_job(self):
        self.client.force_login(mk_user("o@x.com", "recruiter", company_name="B"))
        self.assertEqual(self.client.get(reverse("recruitment:job_update", args=[self.job.pk])).status_code, 404)

    def test_register_logs_in_and_redirects_by_role(self):
        resp = self.client.post(reverse("recruitment:register"), {
            "email": "NEW@x.com", "first_name": "A", "last_name": "B", "role": "recruiter",
            "company_name": "Co", "password1": "S3cure-pass-99", "password2": "S3cure-pass-99",
        })
        self.assertRedirects(resp, reverse("recruitment:dashboard"))
        self.assertTrue(User.objects.filter(email="new@x.com", role="recruiter").exists())


# ============================ Apply flow ============================
@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ApplicationFlowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.rec = mk_user("r@x.com", "recruiter", company_name="Acme")
        self.cand = mk_user("c@x.com", "candidate")
        self.job = Job.objects.create(recruiter=self.rec, title="Dev", company_name="Acme", description="d")
        self.url = reverse("recruitment:apply", args=[self.job.pk])

    def apply(self, **data):
        self.client.force_login(self.cand)
        return self.client.post(self.url, {"cover_letter": "hi", **data})

    def test_apply_with_new_pdf(self):
        self.assertRedirects(self.apply(new_resume=pdf()), reverse("recruitment:my_applications"))
        app = Application.objects.get()
        self.assertEqual((app.resume.owner, app.resume.original_filename), (self.cand, "cv.pdf"))

    def test_reuse_existing_resume(self):
        r = Resume.objects.create(owner=self.cand, file=pdf(), original_filename="old.pdf")
        self.apply(resume=r.pk)
        self.assertEqual(Application.objects.get().resume, r)

    def test_non_pdf_rejected(self):
        resp = self.apply(new_resume=pdf("cv.pdf", b"definitely not a pdf"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Application.objects.exists())

    def test_cannot_apply_twice(self):
        self.apply(new_resume=pdf())
        self.assertRedirects(self.client.get(self.url), reverse("recruitment:my_applications"))
        self.apply(new_resume=pdf())
        self.assertEqual((Application.objects.count(), Resume.objects.count()), (1, 1))

    def test_recruiter_cannot_apply_and_closed_job_is_404(self):
        self.client.force_login(self.rec)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.job.status = Job.Status.CLOSED
        self.job.save()
        self.client.force_login(self.cand)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_resume_download_permissions(self):
        self.apply(new_resume=pdf())
        url = reverse("recruitment:resume_download", args=[Application.objects.get().resume_id])
        other_cand = mk_user("c2@x.com", "candidate")
        other_rec = mk_user("r2@x.com", "recruiter", company_name="B")
        for user, expected in [(self.cand, 200), (self.rec, 200), (other_cand, 404), (other_rec, 404)]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.email)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_withdraw_only_in_early_stages(self):
        self.apply(new_resume=pdf())
        app = Application.objects.get()
        url = reverse("recruitment:withdraw", args=[app.pk])
        app.status = Application.Status.INTERVIEW
        app.save()
        self.client.post(url)
        self.assertTrue(Application.objects.exists())
        app.status = Application.Status.APPLIED
        app.save()
        self.client.post(url)
        self.assertFalse(Application.objects.exists())


# ============================ Celery pipeline ============================
@override_settings(MEDIA_ROOT=TMP_MEDIA)
class PipelineTests(TestCase):
    def setUp(self):
        rec = mk_user("r@x.com", "recruiter", company_name="Acme")
        self.cand = mk_user("c@x.com", "candidate")
        self.job = Job.objects.create(
            recruiter=rec, title="Dev", company_name="Acme", description="d",
            required_skills=["python", "aws"], min_experience_years=2,
        )
        self.resume = Resume.objects.create(
            owner=self.cand, file=pdf(), original_filename="cv.pdf",
            parsed_data={"skills": ["python"], "total_experience_years": 3},
        )
        self.app = Application.objects.create(job=self.job, candidate=self.cand, resume=self.resume)

    def set_resume(self, **fields):
        Resume.objects.filter(pk=self.resume.pk).update(**fields)

    @patch("recruitment.tasks.parse_resume_text", return_value={"skills": ["python"]})
    @patch("recruitment.tasks.extract_text", return_value="resume text")
    @patch("recruitment.tasks.match_application.delay")
    def test_parse_success_then_enqueues_matching(self, delay, _extract, _parse):
        parse_resume(self.resume.pk)
        self.resume.refresh_from_db()
        self.assertEqual(self.resume.parse_status, PS.DONE)
        self.assertEqual(self.resume.extracted_text, "resume text")
        delay.assert_called_once_with(self.app.pk, False)

    @patch("recruitment.tasks.parse_resume_text")
    @patch("recruitment.tasks.extract_text", side_effect=ExtractionError("scanned image"))
    def test_permanent_failure_skips_groq_and_flags_application(self, _extract, parse):
        parse_resume(self.resume.pk)
        self.resume.refresh_from_db()
        self.app.refresh_from_db()
        self.assertEqual(self.resume.parse_status, PS.FAILED)
        self.assertIn("scanned", self.resume.parse_error)
        self.assertEqual(self.app.ai_status, PS.FAILED)
        parse.assert_not_called()

    @patch("recruitment.tasks.extract_text")
    def test_already_parsed_resume_is_skipped(self, extract):
        self.set_resume(parse_status=PS.DONE)
        parse_resume(self.resume.pk)
        extract.assert_not_called()

    @patch("recruitment.tasks.ai_assess", return_value=(["aws"], "Solid fit."))
    def test_match_scores_and_stores(self, _):
        self.set_resume(parse_status=PS.DONE)
        match_application(self.app.pk)
        self.app.refresh_from_db()
        self.assertEqual((self.app.ai_status, self.app.match_score), (PS.DONE, 100))
        self.assertEqual((self.app.missing_skills, self.app.ai_summary), ([], "Solid fit."))

    @patch("recruitment.tasks.ai_assess", side_effect=MatchError("bad json"))
    def test_groq_failure_keeps_baseline_score(self, _):
        self.set_resume(parse_status=PS.DONE)
        match_application(self.app.pk)
        self.app.refresh_from_db()
        self.assertEqual(self.app.ai_status, PS.FAILED)
        self.assertEqual(self.app.match_score, 65)  # 1/2 skills * .7 + full experience * .3
        self.assertEqual(self.app.missing_skills, ["aws"])

    @patch("recruitment.tasks.ai_assess")
    def test_match_waits_for_unparsed_resume(self, assess):
        self.set_resume(parse_status=PS.PROCESSING)
        match_application(self.app.pk)
        assess.assert_not_called()
        self.app.refresh_from_db()
        self.assertEqual(self.app.ai_status, PS.PENDING)

    @patch("recruitment.tasks.ai_assess", return_value=([], "x"))
    def test_done_is_skipped_unless_forced(self, assess):
        self.set_resume(parse_status=PS.DONE)
        Application.objects.filter(pk=self.app.pk).update(ai_status=PS.DONE)
        match_application(self.app.pk)
        assess.assert_not_called()
        match_application(self.app.pk, force=True)
        assess.assert_called_once()


# ============================ Recruiter dashboard ============================
class DashboardTests(TestCase):
    def setUp(self):
        self.rec = mk_user("r@x.com", "recruiter", company_name="Acme")
        self.other_rec = mk_user("o@x.com", "recruiter", company_name="B")
        self.c1, self.c2 = mk_user("a@x.com", "candidate"), mk_user("b@x.com", "candidate")
        self.job = Job.objects.create(recruiter=self.rec, title="Dev", company_name="Acme", description="d")

        def make(cand, skills, score):
            r = Resume.objects.create(owner=cand, file="r/x.pdf", original_filename="x.pdf",
                                      parse_status=PS.DONE, parsed_data={"skills": skills})
            return Application.objects.create(job=self.job, candidate=cand, resume=r, match_score=score)

        self.strong = make(self.c1, ["python", "django"], 90)
        self.weak = make(self.c2, ["python"], 40)
        self.list_url = reverse("recruitment:job_applicants", args=[self.job.pk])
        self.client.force_login(self.rec)

    def apps(self, **params):
        return list(self.client.get(self.list_url, params).context["applications"])

    def test_sorted_by_score_desc(self):
        self.assertEqual(self.apps(), [self.strong, self.weak])

    def test_skill_filter_requires_all_skills(self):
        self.assertEqual(self.apps(skills="Python, Django"), [self.strong])
        self.assertEqual(len(self.apps(skills="python")), 2)

    def test_min_score_filter(self):
        self.assertEqual(self.apps(min_score="50"), [self.strong])

    def test_other_recruiter_gets_404_everywhere(self):
        self.client.force_login(self.other_rec)
        self.assertEqual(self.client.get(self.list_url).status_code, 404)
        self.assertEqual(self.client.get(reverse("recruitment:application_detail", args=[self.strong.pk])).status_code, 404)
        resp = self.client.post(reverse("recruitment:application_status", args=[self.strong.pk]), {"status": "hired"})
        self.assertEqual(resp.status_code, 404)
        self.strong.refresh_from_db()
        self.assertEqual(self.strong.status, "applied")

    def test_candidate_forbidden(self):
        self.client.force_login(self.c1)
        self.assertEqual(self.client.get(reverse("recruitment:dashboard")).status_code, 403)

    def test_htmx_status_change_returns_row(self):
        resp = self.client.post(reverse("recruitment:application_status", args=[self.weak.pk]),
                                {"status": "screening"}, HTTP_HX_REQUEST="true")
        self.assertContains(resp, f'id="app-{self.weak.pk}"')
        self.weak.refresh_from_db()
        self.assertEqual(self.weak.status, "screening")

    def test_invalid_status_rejected(self):
        resp = self.client.post(reverse("recruitment:application_status", args=[self.weak.pk]), {"status": "nonsense"})
        self.assertEqual(resp.status_code, 400)

    def test_scheduling_interview_advances_status(self):
        when = (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(reverse("recruitment:application_detail", args=[self.strong.pk]),
                         {"scheduled_at": when, "duration_minutes": 45, "mode": "video"})
        self.strong.refresh_from_db()
        self.assertEqual(self.strong.status, "interview")
        self.assertEqual(self.strong.interviews.count(), 1)

    def test_past_interview_rejected(self):
        past = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        resp = self.client.post(reverse("recruitment:application_detail", args=[self.strong.pk]),
                                {"scheduled_at": past, "duration_minutes": 45, "mode": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.strong.interviews.count(), 0)

    def test_rejecting_cancels_scheduled_interviews(self):
        self.strong.interviews.create(scheduled_at=timezone.now() + timedelta(days=1))
        self.client.post(reverse("recruitment:application_status", args=[self.strong.pk]), {"status": "rejected"})
        self.assertEqual(self.strong.interviews.get().status, "cancelled")

    def test_pages_render(self):
        self.assertContains(self.client.get(reverse("recruitment:dashboard")), "Recruiter dashboard")
        self.assertContains(self.client.get(reverse("recruitment:application_detail", args=[self.strong.pk])), "AI assessment")
