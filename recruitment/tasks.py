import logging

from celery import Task, shared_task
from django.db import transaction
from groq import APIConnectionError, InternalServerError, RateLimitError

from .models import Application, Resume
from .models import ProcessingStatus as PS
from .services.matcher import MatchError, ai_assess, compute
from .services.pdf_extractor import ExtractionError, extract_text
from .services.resume_parser import ParseError, parse_resume_text

logger = logging.getLogger(__name__)

# Transient: back off and retry. (APITimeoutError subclasses APIConnectionError.)
TRANSIENT = (RateLimitError, APIConnectionError, InternalServerError)
RETRY_OPTS = dict(
    autoretry_for=TRANSIENT, retry_backoff=10, retry_backoff_max=300, retry_jitter=True,
    max_retries=5, rate_limit="20/m",  # per-worker throttle; align with your Groq console limits
)


def _redelivered_or_retry(task) -> bool:
    return bool(task.request.retries or (task.request.delivery_info or {}).get("redelivered"))


# ======================= Resume parsing =======================
def _fail(resume_id: int, message: str) -> None:
    Resume.objects.filter(pk=resume_id).update(parse_status=PS.FAILED, parse_error=message[:500])
    # Applications waiting on this resume can't be scored; flag them so the UI doesn't wait forever.
    Application.objects.filter(resume_id=resume_id).exclude(ai_status=PS.DONE).update(
        ai_status=PS.FAILED, ai_error="Resume could not be parsed."
    )


class ResumeTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error("parse_resume(%s) failed: %r", args[0], exc)
        _fail(args[0], f"{type(exc).__name__}: {exc}")


@shared_task(bind=True, base=ResumeTask, **RETRY_OPTS)
def parse_resume(self, resume_id: int) -> None:
    # Atomic claim: duplicate deliveries can't process the same resume twice.
    allowed = [PS.PENDING, PS.FAILED] + ([PS.PROCESSING] if _redelivered_or_retry(self) else [])
    claimed = Resume.objects.filter(pk=resume_id, parse_status__in=allowed).update(
        parse_status=PS.PROCESSING, parse_error=""
    )
    if not claimed:
        logger.info("parse_resume(%s) skipped: already processed or missing.", resume_id)
        return

    resume = Resume.objects.get(pk=resume_id)
    try:
        if not resume.extracted_text:  # cached, so Groq retries don't re-read the PDF
            with resume.file.open("rb") as fh:
                resume.extracted_text = extract_text(fh.read())
            resume.save(update_fields=["extracted_text"])
        data = parse_resume_text(resume.extracted_text)
    except (ExtractionError, ParseError, FileNotFoundError) as exc:
        _fail(resume_id, str(exc))
        return

    Resume.objects.filter(pk=resume_id).update(parsed_data=data, parse_status=PS.DONE, parse_error="")
    # Score every application that uses this resume (duplicates are harmless; see the claim below).
    for app_id in Application.objects.filter(resume_id=resume_id).values_list("pk", flat=True):
        _dispatch_match(app_id)


def enqueue_resume_parse(resume: Resume) -> None:
    """Queue parsing after the DB commit. A broker outage must never fail the application itself."""
    if resume.parse_status not in (PS.PENDING, PS.FAILED):
        return  # already parsed (resume reuse) or in flight

    def _dispatch():
        try:
            parse_resume.delay(resume.pk)
        except Exception:
            logger.exception("Could not enqueue parse_resume(%s)", resume.pk)  # `reparse_resumes` recovers

    transaction.on_commit(_dispatch)


# ======================= Matching =======================
class MatchTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error("match_application(%s) failed: %r", args[0], exc)
        Application.objects.filter(pk=args[0]).update(
            ai_status=PS.FAILED, ai_error=f"{type(exc).__name__}: {exc}"[:500]
        )


@shared_task(bind=True, base=MatchTask, **RETRY_OPTS)
def match_application(self, application_id: int, force: bool = False) -> None:
    app = Application.objects.select_related("job", "resume").filter(pk=application_id).first()
    if app is None:
        return
    if app.resume.parse_status != PS.DONE:
        # Not ready: stay PENDING. parse_resume enqueues us when it finishes (or fails the app).
        return

    # Atomic claim: duplicate deliveries become no-ops. DONE is re-claimable only with force=True.
    allowed = [PS.PENDING, PS.FAILED]
    if force:
        allowed.append(PS.DONE)
    if _redelivered_or_retry(self):
        allowed.append(PS.PROCESSING)
    if not Application.objects.filter(pk=application_id, ai_status__in=allowed).update(
        ai_status=PS.PROCESSING, ai_error=""
    ):
        logger.info("match_application(%s) skipped.", application_id)
        return

    job, parsed = app.job, app.resume.parsed_data

    # 1) Deterministic baseline, saved first so a Groq outage still leaves a usable ranking.
    base = compute(job, parsed)
    Application.objects.filter(pk=application_id).update(
        match_score=base["score"], matched_skills=base["matched"], missing_skills=base["missing"]
    )

    # 2) Groq: synonym rescue + recruiter summary.
    try:
        extra, summary = ai_assess(job, parsed, base["missing"])
    except MatchError as exc:
        Application.objects.filter(pk=application_id).update(ai_status=PS.FAILED, ai_error=str(exc)[:500])
        return

    # 3) Final score recomputed in Python from validated matches only.
    final = compute(job, parsed, extra)
    Application.objects.filter(pk=application_id).update(
        match_score=final["score"], matched_skills=final["matched"], missing_skills=final["missing"],
        ai_summary=summary, ai_status=PS.DONE, ai_error="",
    )


def _dispatch_match(application_id: int, force: bool = False) -> None:
    try:
        match_application.delay(application_id, force)
    except Exception:
        logger.exception("Could not enqueue match_application(%s)", application_id)  # `rematch` recovers


def enqueue_application_match(application_id: int) -> None:
    """Called after apply. If the resume isn't parsed yet the task exits, and parse_resume triggers it later."""
    transaction.on_commit(lambda: _dispatch_match(application_id))
