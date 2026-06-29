from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return ""
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""
