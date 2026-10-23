"""Approved bilingual public labels; parity checked against the SPA dictionaries."""

from __future__ import annotations

PUBLIC_LABELS = {
    "overview": ["最近在这里", "Around here lately"],
    "timeline": ["时间线", "Timeline"],
    "diary": ["日记与文章", "Diary & writing"],
    "behavior": ["过往活动", "Past activity"],
    "lifecycle": ["运行阶段", "Runtime stages"],
    "sleep": ["休息记录", "Rest & sleep"],
    "goals": ["目标与方向", "Goals & directions"],
    "projects": ["正在做的事", "Projects"],
    "predictions": ["预测与回看", "Predictions & outcomes"],
    "research": ["研究笔记", "Research notes"],
    "interactions": ["对外交流", "Public exchanges"],
    "posts": ["留言广场", "Guestbook"],
    "participate": ["留下一个想法 ↗", "Leave a thought ↗"],
    "updates": ["实例更新", "Instance updates"],
    "about": ["关于 Noyra", "About Noyra"],
    "booting": ["启动中", "Booting"],
    "orienting": ["初始化", "Orienting"],
    "active": ["活跃", "Active"],
    "paused": ["已暂停", "Paused"],
    "winding_down": ["准备休眠", "Winding down"],
    "reflective_sleep": ["反思休眠", "Reflective sleep"],
    "deep_sleep": ["深度休眠", "Deep sleep"],
    "waking": ["唤醒中", "Waking"],
    "stopped": ["已停止", "Stopped"],
    "resetting": ["重置中", "Resetting"],
    "published": ["已公开", "Published"],
    "planned": ["计划中", "Planned"],
    "running": ["进行中", "In progress"],
    "completed": ["已完成", "Completed"],
    "complete": ["已完成", "Completed"],
    "succeeded": ["成功", "Succeeded"],
    "failed": ["失败", "Failed"],
    "pending": ["待处理", "Pending"],
    "unknown": ["结果未知", "Unknown outcome"],
    "rejected": ["未执行", "Rejected"],
    "cancelled": ["已取消", "Cancelled"],
    "supported": ["得到支持", "Supported"],
    "not_supported": ["未获支持", "Not supported"],
    "partial": ["部分支持", "Partly supported"],
    "delivered": ["已送达", "Delivered"],
    "sent": ["已发送", "Sent"],
    "normal": ["正常", "Normal"],
    "limited": ["有限", "Limited"],
    "undisclosed": ["未公开", "Undisclosed"],
    "periodic": ["周期休眠", "Periodic sleep"],
    "resource": ["资源调整", "Resource adjustment"],
    "maintenance": ["维护", "Maintenance"],
}

PUBLIC_ACTIONS = {
    "observe": ["观察", "Observe"],
    "research": ["研究", "Research"],
    "reflect": ["反思", "Reflect"],
    "communicate": ["交流", "Communicate"],
    "send_message": ["发送消息", "Send message"],
    "search": ["搜索", "Search"],
    "write": ["写作", "Write"],
    "publish": ["发布", "Publish"],
}

PUBLIC_PROVENANCE = {
    "operator_summary": ["管理者整理 · 已审核", "Prepared by the operator · reviewed"],
    "subject": ["Noyra 公开内容", "Public content by Noyra"],
    "visitor": ["访客署名 · 未验证", "Visitor name · unverified"],
    "operator": ["管理者发布", "Operator publication"],
    "verified_channel": ["已验证渠道", "Verified channel"],
    "runtime": ["运行记录", "Runtime record"],
}


def public_label(value: str, *, english: bool = False) -> str:
    return (
        PUBLIC_LABELS[value][int(english)]
        if value in PUBLIC_LABELS
        else value or ("Undisclosed" if english else "未公开")
    )
