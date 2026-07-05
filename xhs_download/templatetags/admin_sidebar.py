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
            ("下载日志", "download_logs", "下"),
            ("解析日志", "parse_logs", "解"),
        ),
    ),
    (
        "策略",
        (
            ("Policy Engine", "policy_engine", "E"),
            ("Feedback Loop", "feedback_loop", "F"),
            ("Policy Control", "policy_control", "C"),
            ("Control Convergence", "control_convergence", "收"),
            ("Stability v6", "stability_v6", "稳"),
            ("Autonomous Evolution", "autonomous_evolution", "自"),
            ("Governance v8", "governance_v8", "治"),
        ),
    ),
    (
        "运维",
        (
            ("运维首页", "admin_console", "运"),
            ("运营看板", "ops_dashboard", "看"),
            ("Observability v2", "observability_v2", "O"),
            ("放量守卫", "rollout_guard", "放"),
            ("Self-Healing", "self_healing_run", "愈"),
        ),
    ),
)


def reverse_optional(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def item_is_active(path, url):
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
    groups = []
    for group_index, (group_label, items) in enumerate(SIDEBAR_GROUPS):
        resolved_items = []
        group_active = False
        for label, url_name, short_label in items:
            url = reverse_optional(url_name)
            if not url:
                continue
            active = item_is_active(path, url)
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
