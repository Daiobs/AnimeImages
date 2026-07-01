from .download_routing import ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP
from .routing_v3 import build_routing_intelligence_snapshot


def build_routing_policy_recommendations(snapshot=None):
    snapshot = snapshot or build_routing_intelligence_snapshot()
    rows = {row["route_type"]: row for row in snapshot.get("rows", ())}
    recommendations = []

    direct = rows.get(ROUTE_DIRECT, {})
    proxy = rows.get(ROUTE_PROXY, {})
    zip_route = rows.get(ROUTE_ZIP, {})

    if direct.get("count", 0) >= 3 and direct.get("success_rate", 0) >= 90:
        recommendations.append("DIRECT 历史成功率较高，可继续优先用于普通图片直链。")

    if proxy.get("count", 0) >= 3 and proxy.get("success_rate", 100) < 70:
        recommendations.append("PROXY 近期失败率偏高，建议暂时降低单视频代理权重。")

    if zip_route.get("actual_server_bytes", 0) > zip_route.get("estimated_server_bytes", 0) * 1.5 and zip_route.get("count", 0):
        recommendations.append("ZIP 实际服务器出网明显高于估算，建议降低 ZIP 权重并收紧单次打包限制。")

    if not recommendations:
        recommendations.append("当前路由样本不足或表现稳定，建议继续观察。")

    return recommendations
