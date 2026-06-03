# Image Library Workflow

## Directory Layout

The local image library is stored under:

```text
static/images/<category>/<character>/
```

`static/images/` is intentionally treated as a local or external asset library. It is large and should not be committed to the normal Git repository. Use a separate backup, Git LFS, object storage, or a packaged asset archive if the image library needs to be shared.

Images can be placed directly under a character directory:

```text
static/images/单人IP/春日野穹/source.jpg
static/images/单人IP/春日野穹/padded/source.jpg
```

Images can also be grouped by posture or another folder:

```text
static/images/原神/纳西妲/站/source.jpg
static/images/原神/纳西妲/坐/source.jpg
static/images/原神/纳西妲/padded/站/source.jpg
static/images/原神/纳西妲/padded/坐/source.jpg
```

## Padded Images

`padded` contains generated square images. The original image is centered on a white square canvas. Search results read these generated files so the grid displays consistently.

Do not manually edit generated `padded` images unless you intentionally want to override the generated output.

## Initialize or Refresh

Preview what would be generated:

```bash
python manage.py init_image_library --dry-run
```

Generate missing padded images:

```bash
python manage.py init_image_library
```

Regenerate existing padded images:

```bash
python manage.py init_image_library --force
```

Process one character directory:

```bash
python manage.py init_image_library --character-dir "static/images/原神/纳西妲"
```

## Rules

- Source images under any `padded` directory are skipped.
- Existing padded files are kept unless `--force` is used.
- Supported extensions: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`.
- Animated images are not preserved as animations; they are opened by Pillow as normal images.
