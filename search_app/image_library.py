from dataclasses import dataclass
from pathlib import Path

from PIL import Image


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PADDED_DIR_NAME = "padded"


@dataclass
class ImageLibraryResult:
    scanned: int = 0
    generated: int = 0
    skipped_existing: int = 0


def is_supported_image(path):
    return path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def is_inside_padded_dir(path):
    return PADDED_DIR_NAME in path.parts


def pad_image(source_path, output_path, background=(255, 255, 255)):
    with Image.open(source_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        square_size = max(width, height)
        padded = Image.new("RGB", (square_size, square_size), background)
        padded.paste(image, ((square_size - width) // 2, (square_size - height) // 2))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        padded.save(output_path)


def iter_character_dirs(images_root):
    if not images_root.exists():
        return

    for category_dir in sorted(images_root.iterdir()):
        if not category_dir.is_dir():
            continue

        for character_dir in sorted(category_dir.iterdir()):
            if character_dir.is_dir():
                yield character_dir


def iter_source_images(character_dir):
    for image_path in sorted(character_dir.rglob("*")):
        if not is_supported_image(image_path):
            continue
        if is_inside_padded_dir(image_path.relative_to(character_dir)):
            continue
        yield image_path


def get_padded_output_path(character_dir, source_path):
    relative_path = source_path.relative_to(character_dir)
    return character_dir / PADDED_DIR_NAME / relative_path


def initialize_image_library(images_root, character_dir=None, force=False, dry_run=False):
    images_root = Path(images_root)
    targets = [Path(character_dir)] if character_dir else list(iter_character_dirs(images_root))
    result = ImageLibraryResult()

    for target_dir in targets:
        for source_path in iter_source_images(target_dir):
            result.scanned += 1
            output_path = get_padded_output_path(target_dir, source_path)

            if output_path.exists() and not force:
                result.skipped_existing += 1
                continue

            if not dry_run:
                pad_image(source_path, output_path)
            result.generated += 1

    return result
