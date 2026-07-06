from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


SIDEBAR_GROUPS = (
    (
        "总览",
        (
            ("控制平面", "control_plane", "控"),
            ("后台首页", "admin:index", "首"),
        ),
    ),
    (
        "Provider",
        (
            ("Provider 管理", "provider_settings", "P"),
            ("Provider 健康测试", "provider_health_tests", "测"),
            ("Provider 审计日志", "provider_audit_logs", "审"),
        ),
    ),
    (
        "下载",
        (
            ("下载限流", "admin:download_console_downloadruntimesetting_changelist", "限"),
            ("批量解析配置", "admin:xhs_download_batchparseruntimesetting_changelist", "批"),
            ("日志中心", "logs_center", "志"),
        ),
    ),
    (
        "策略",
        (
            ("策略中心", "policy_center", "策"),
            ("Policy Engine", "policy_engine", "E"),
            ("Policy Control", "policy_control", "C"),
        ),
    ),
    (
        "运维",
        (
            ("运营看板", "ops_dashboard", "看"),
            ("放量守卫", "rollout_guard", "放"),
            ("Self-Healing", "self_healing_run", "愈"),
        ),
    ),
)

ADMIN_TITLE_MAP = {
    "Site administration": "后台首页",
    "Django site admin": "后台首页",
    "Django administration": "后台首页",
    "Log in": "登录后台",
    "Password change": "修改密码",
    "Password change successful": "密码修改成功",
}


def reverse_optional(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def current_view_name(request):
    resolver_match = getattr(request, "resolver_match", None)
    return str(getattr(resolver_match, "view_name", "") or "")


def item_is_active(path, url, *, url_name="", view_name=""):
    if view_name and url_name and view_name == url_name:
        return True
    if not path or not url:
        return False
    if path == url:
        return True
    if url != "/admin/" and path.startswith(url):
        return True
    return False


@register.inclusion_tag("admin/sidebar_menu.html", takes_context=True)
def admin_sidebar_menu(context):
    request = context.get("request")
    path = getattr(request, "path", "") or ""
    view_name = current_view_name(request)
    groups = []
    for group_index, (group_label, items) in enumerate(SIDEBAR_GROUPS):
        resolved_items = []
        group_active = False
        for label, url_name, short_label in items:
            url = reverse_optional(url_name)
            if not url:
                continue
            active = item_is_active(path, url, url_name=url_name, view_name=view_name)
            group_active = group_active or active
            resolved_items.append(
                {
                    "label": label,
                    "url": url,
                    "short_label": short_label,
                    "active": active,
                }
            )
        if resolved_items:
            groups.append(
                {
                    "label": group_label,
                    "key": f"group-{group_index}",
                    "active": group_active,
                    "items": resolved_items,
                }
            )
    return {"sidebar_groups": groups}


@register.simple_tag
def admin_url_optional(name):
    return reverse_optional(name)


@register.simple_tag
def admin_display_title(title):
    value = str(title or "").strip()
    return ADMIN_TITLE_MAP.get(value, value or "后台首页")
