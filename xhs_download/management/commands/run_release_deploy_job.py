from django.core.management.base import BaseCommand, CommandError

from xhs_download.models import ReleaseDeployJob, ReleaseDeployJobStatus
from xhs_download.services.release_management import (
    claim_pending_release_deploy_job,
    execute_release_deploy_job,
)


class Command(BaseCommand):
    help = "Run a queued release deployment job outside the Django request thread."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, required=True)

    def handle(self, *args, **options):
        job_id = options["job_id"]
        job = claim_pending_release_deploy_job(job_id)
        if job is None:
            try:
                current_status = ReleaseDeployJob.objects.values_list("status", flat=True).get(pk=job_id)
            except ReleaseDeployJob.DoesNotExist as error:
                raise CommandError(f"ReleaseDeployJob #{job_id} does not exist") from error
            raise CommandError(f"ReleaseDeployJob #{job_id} is {current_status}, not pending")

        job = execute_release_deploy_job(job)
        self.stdout.write(f"ReleaseDeployJob #{job.pk}: {job.status}")
        if job.status != ReleaseDeployJobStatus.SUCCEEDED:
            raise CommandError(job.error_message or "release deployment failed")
