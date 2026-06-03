from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search_app.image_library import initialize_image_library


class Command(BaseCommand):
    help = "Generate square padded images for the local static/images library."

    def add_arguments(self, parser):
        parser.add_argument(
            "--images-root",
            default=str(settings.BASE_DIR / "static" / "images"),
            help="Image library root. Defaults to static/images.",
        )
        parser.add_argument(
            "--character-dir",
            help="Process one character directory, such as static/images/原神/纳西妲.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate padded files even when they already exist.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be generated without writing files.",
        )

    def handle(self, *args, **options):
        images_root = Path(options["images_root"])
        character_dir = Path(options["character_dir"]) if options["character_dir"] else None

        if not images_root.exists():
            raise CommandError(f"Images root does not exist: {images_root}")

        if character_dir and not character_dir.exists():
            raise CommandError(f"Character directory does not exist: {character_dir}")

        result = initialize_image_library(
            images_root=images_root,
            character_dir=character_dir,
            force=options["force"],
            dry_run=options["dry_run"],
        )

        action = "Would generate" if options["dry_run"] else "Generated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {result.generated} padded image(s); "
                f"scanned {result.scanned}; skipped existing {result.skipped_existing}."
            )
        )
