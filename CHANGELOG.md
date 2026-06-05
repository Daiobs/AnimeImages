# Changelog

## Unreleased

- Upgraded runtime baseline to Python 3.14.5 and Django 5.2.x.
- Added production-oriented environment variable settings.
- Added development, deployment, architecture, and image library documentation.
- Added image library initialization command for generated `padded` assets.
- Refactored Xiaohongshu parsing/download helpers into services.
- Changed Xiaohongshu downloads to browser ZIP responses instead of arbitrary server save paths.
- Added environment-gated local server save mode for Xiaohongshu downloads.
- Changed Xiaohongshu image downloads to preserve original files by default, with image conversion as an explicit option.
- Added final redirect host validation and temporary-file ZIP creation for Xiaohongshu downloads.
- Hid image format and quality controls from the Xiaohongshu download UI so normal downloads always use original files.
