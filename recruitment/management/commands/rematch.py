from django.core.management.base import BaseCommand

from recruitment.models import Application
from recruitment.models import ProcessingStatus as PS
from recruitment.tasks import match_application


class Command(BaseCommand):
    help = (
        "Default: enqueue PENDING/FAILED applications whose resume is parsed (broker-outage recovery). "
        "--job ID: force re-score ALL applications for that job (after editing it). One Groq call each."
    )

    def add_arguments(self, parser):
        parser.add_argument("--job", type=int)

    def handle(self, *args, **opts):
        qs = Application.objects.filter(resume__parse_status=PS.DONE)
        if opts["job"]:
            qs, force = qs.filter(job_id=opts["job"]), True
        else:
            qs, force = qs.filter(ai_status__in=[PS.PENDING, PS.FAILED]), False
        ids = list(qs.values_list("pk", flat=True))
        for pk in ids:
            match_application.delay(pk, force)
        self.stdout.write(self.style.SUCCESS(f"Enqueued {len(ids)} application(s)."))
