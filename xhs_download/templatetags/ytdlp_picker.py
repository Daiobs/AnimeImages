from django import template
from xhs_download.services.ytdlp_selection import build_picker

register = template.Library()


@register.simple_tag
def ytdlp_presentation(candidates, route=None, legacy_videos=None):
    preferences = route.get('format_preferences', {}) if isinstance(route, dict) else {}
    if candidates and candidates[0].get('provider_id') in {'tiantianjiexi_generic', 'kukutool_generic'}:
        # A pre-existing manifest may permit a non-browser delivery path.
        # Do not replace that authorized UI with a narrower direct-fetch action.
        if legacy_videos:
            return None
        from xhs_download.services.service_format_selection import build_service_picker
        return build_service_picker(candidates, preferences)
    return build_picker(candidates, preferences=preferences)
