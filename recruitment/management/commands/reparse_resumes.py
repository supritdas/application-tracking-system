from django.core.management.base import BaseCommand

from recruitment.models import ProcessingStatus as PS
from recruitment.models import Resume
from recruitment.tasks import parse_resume


class Command(BaseCommand):
    help = "Re-enqueue resumes stuck in PENDING (e.g. broker was down). Use --failed to include FAILED."

    def add_arguments(self, parser):
        parser.add_argument("--failed", action="store_true")

    def handle(self, *args, **opts):
        statuses = [PS.PENDING] + ([PS.FAILED] if opts["failed"] else [])
        ids = list(Resume.objects.filter(parse_status__in=statuses).values_list("pk", flat=True))
        for pk in ids:
            parse_resume.delay(pk)
        self.stdout.write(self.style.SUCCESS(f"Enqueued {len(ids)} resume(s)."))
