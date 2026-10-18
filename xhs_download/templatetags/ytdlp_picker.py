from django import template
from xhs_download.services.ytdlp_selection import build_picker

register = template.Library()


@register.simple_tag
def ytdlp_presentation(candidates, route=None, legacy_videos=None):
    preferences = route.get('format_preferences', {}) if isinstance(route, dict) else {}
    if candidates and candidates[0].get('provider_id') in {'tiantianjiexi_generic', 'kukutool_generic', 'tiantianjiexi', 'kukutool'}:
        from xhs_download.services.service_format_selection import build_service_picker
        return build_service_picker(candidates, preferences)
    return build_picker(candidates, preferences=preferences)


@register.simple_tag
def remaining_picker_videos(presentation, legacy_videos):
    if not presentation:
        return legacy_videos or []
    if not presentation.get('service_picker'):
        return []
    groups = presentation.get('groups') or [presentation]
    represented = {c.get('legacy_asset_id') for group in groups for c in group['videos']
                   if c.get('legacy_asset_id') and c.get('can_legacy_download')}
    return [item for item in legacy_videos or [] if item.get('asset_id') not in represented]
