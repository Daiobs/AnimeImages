from django import template
from xhs_download.services.ytdlp_selection import build_picker

register = template.Library()


@register.simple_tag
def ytdlp_presentation(candidates, route=None):
    preferences = route.get('format_preferences', {}) if isinstance(route, dict) else {}
    return build_picker(candidates, preferences=preferences)
