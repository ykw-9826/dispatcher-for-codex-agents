"""Fixed Chinese operational templates. Full envelopes stay in the local ledger."""

from datetime import UTC, datetime

TITLES = {
    "turn_started": "本轮开始",
    "turn_completed": "本轮回复结束",
    "session_ended": "主线程结束",
    "interrupted": "任务中断",
    "subagent_stopped": "原生子线程结束",
    "batch_completed": "子任务完成",
    "batch_failed": "任务失败",
    "shard_failed": "任务失败",
    "explicit_retry_required": "需要操作",
    "human_action_required": "需要操作",
    "delivery_test": "通知测试",
    "event_acceptance_completed": "事件驱动验收完成",
}


def present(event) -> dict[str, str]:
    title = "[DCA] " + TITLES[event.kind]
    state = {
        "STARTED": "已开始",
        "COMPLETED": "已结束（不代表结果已获批准）",
        "FAILED": "失败，等待处理",
        "INTERRUPTED": "已中断",
        "REQUIRED": "需要人工操作",
        "TEST": "工具验收",
    }[event.status]
    metrics = event.metrics
    progress = (
        f"{int(metrics.get('success', 0))}/{int(metrics['planned'])}"
        if "planned" in metrics
        else "未提供"
    )
    elapsed = (
        f"{metrics['elapsed_seconds']:.1f} 秒"
        if "elapsed_seconds" in metrics
        else "未提供"
    )
    todo = "查看本地结果；不自动进入下一阶段"
    if event.kind in {
        "batch_failed",
        "shard_failed",
        "explicit_retry_required",
        "human_action_required",
    }:
        todo = "检查本地失败记录；重试必须明确批准"
    if event.kind == "event_acceptance_completed":

        def finding(key):
            return {1: "已验证", 0: "未通过"}.get(metrics.get(key), "未测试")

        todo = (
            f"同 thread 续接：{finding('same_thread_verified')}；"
            f"等待期间主模型逻辑请求：{metrics.get('wait_model_requests', 'UNKNOWN')}；"
            f"重复事件抑制：{finding('duplicate_suppression_verified')}；"
            "物理 API 请求总数：UNKNOWN；IDE 原窗口：不支持安全自动唤醒"
        )
    # No task/run/session/attempt IDs or caller text are sent to external sinks.
    body = "\n".join(
        (
            "任务：DCA 工具任务",
            "状态：" + state,
            "进度：" + progress,
            "耗时：" + elapsed,
            "待办：" + todo,
            "时间："
            + datetime.fromtimestamp(event.occurred_at, UTC).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
        )
    )
    return {"title": title, "body": body}
