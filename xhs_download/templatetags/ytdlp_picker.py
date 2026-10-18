from django import template
from xhs_download.services.ytdlp_selection import build_picker, text

register = template.Library()


@register.simple_tag
def ytdlp_presentation(candidates, route=None, legacy_videos=None, display_title=None, base_filename=None):
    preferences = route.get('format_preferences', {}) if isinstance(route, dict) else {}
    if candidates and candidates[0].get('provider_id') in {'tiantianjiexi_generic', 'kukutool_generic', 'tiantianjiexi', 'kukutool'}:
        from xhs_download.services.service_format_selection import build_service_picker
        presentation = build_service_picker(candidates, preferences)
    else:
        presentation = build_picker(candidates, preferences=preferences)
    if not presentation:
        return presentation
    groups = presentation.get('groups') or [presentation]
    title = text(display_title, 300) or text(base_filename, 300)
    for group in groups:
        bindings = set(group.get('legacy_asset_ids') or [
            c.get('legacy_asset_id') for c in group['videos'] if c.get('can_legacy_download')])
        matches = [item for item in legacy_videos or [] if item.get('asset_id') and item['asset_id'] in bindings]
        group['legacy_video'] = matches[0] if len(matches) == 1 else None
        if title and len(groups) == 1:
            group['work'] = {**group['work'], 'title': title}
    if presentation.get('groups'):
        presentation['legacy_video'] = groups[0]['legacy_video']
    presentation['video_resource_count'] = presentation['video_group_count'] + len(
        remaining_picker_videos(presentation, legacy_videos))
    return presentation


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
