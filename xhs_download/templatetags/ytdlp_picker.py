from django import template
from xhs_download.services.ytdlp_selection import build_picker, text
from xhs_download.services.platforms.xhs_independent import display_resource_key

register = template.Library()


def _video_key(item):
    return display_resource_key(item.get('video_url') or item.get('url'))


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
        display_keys = {c.get('display_native_resource_key') for c in group['videos']
                        if c.get('display_native_resource_key')}
        display_matches = [item for item in legacy_videos or []
                           if _video_key(item) in display_keys]
        display_video = display_matches[0] if len(display_matches) == 1 else None
        group['cover_video'] = (group['legacy_video'] if (group['legacy_video'] or {}).get('cover_url')
                                else display_video)
        group['display_native_resource_key'] = _video_key(display_video) if display_video else ''
        group['represented_native_resource_keys'] = [
            key for key in display_keys if display_video and key == _video_key(display_video)
            and any(c.get('display_native_resource_key') == key and c.get('display_same_resource') is True
                    and (c.get('can_download') or c.get('can_legacy_download')) for c in group['videos'])]
        if title and len(groups) == 1:
            group['work'] = {**group['work'], 'title': title}
    if presentation.get('groups'):
        presentation['legacy_video'] = groups[0]['legacy_video']
    associated = {group['display_native_resource_key'] for group in groups
                  if group['display_native_resource_key']}
    presentation['video_resource_count'] = presentation['video_group_count'] + sum(
        _video_key(item) not in associated for item in remaining_picker_videos(presentation, legacy_videos))
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
    display_represented = {key for group in groups for key in group.get('represented_native_resource_keys', [])}
    return [item for item in legacy_videos or []
            if item.get('asset_id') not in represented and _video_key(item) not in display_represented]
