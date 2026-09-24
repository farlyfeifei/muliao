r"""主对话模型目录 · 阿里云千问（OpenAI 兼容）。

本模块是**纯逻辑**：不碰 IO、不 import server / httpx，只描述「我们能接哪些模型」
以及「把上游探测到的真实模型与本地文档目录合并」的规则。server.py 负责真正的
HTTP 探测（GET {base}/models）与运行时活动模型切换。

为什么需要本地文档目录（DOCUMENTED）：
  上游 ``/models`` 需要一把**有效**的 key 才能列出该账号被授权的模型。key 失效或
  额度受限时，探测会失败——此时仍要让前端有可选模型，于是回落到这份按官方文档整理
  的目录（标注 source="documented"，未经验证）。一旦探测成功，真实模型会以
  source="probe" 合并进来并排在前面，文档目录里未被授权的同名项仍保留以便对照。

接入文档：https://platform.qianwenai.com/docs/developer-guides/getting-started/introduction
基址：https://maas.qianwenaiapi.com/compatible-mode/v1（兼容 OpenAI SDK）
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# 兼容 OpenAI 的阿里云百炼「Token Plan 个人版」套餐专属网关基址。
# 注意：不是通用的 maas.qianwenaiapi.com（那个会拿套餐 key 去打官方百炼，报
# invalid_api_key）。套餐 key（sk-sp- 前缀）必须配这个专属域名。
ALIYUN_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# 默认主模型：glm-5.3。
# 选型依据（实测同一提示的 total_tokens）：glm-5.3 在工具调用场景最省档之一
# （181~200 tokens，deepseek-v4-pro 320、qwen3.8-max 351），且稳定支持
# tool_calls —— 幕僚每个动作都要过门控+调工具，工具调用成本是主要开销。
DEFAULT_MODEL = "glm-5.3"

# 非对话模型（图像生成 / TTS / 实时语音）：上游 /models 会列出来，但它们
# 走不通 /chat/completions（实测 400/500），显示出来就是「不存在的模型」。
# 按用户要求：不显示。命中下列任一子串即过滤掉。
NON_CHAT_HINTS: tuple[str, ...] = (
    "image", "wan2", "tts", "audio", "realtime", "vision-gen",
    "embedding", "rerank", "ocr", "asr",
)


def is_chat_capable(model_id: str) -> bool:
    """该模型 id 是否可能用于 /chat/completions（过滤图像/TTS/实时语音等非对话模型）。"""
    low = str(model_id or "").lower()
    return not any(h in low for h in NON_CHAT_HINTS)


# 实测可用的对话模型目录（逐个打过 /chat/completions 验证 200，非猜测）。
#   id    —— 传给上游 chat.completions 的 model 字段（务必逐字一致）。
#   label —— 前端展示名。
#   tier  —— 档位，用于排序：max > plus > flash/econo。
#   note  —— 一句话定位，附实测 token 消耗量级（省 token 优先）。
# 探测成功时以真实列表为准（source="probe"）；探测失败才回落到这份（已验证过的）目录。
DOCUMENTED: tuple[dict[str, str], ...] = (
    {"id": "glm-5.3", "label": "GLM-5.3", "tier": "econo",
     "note": "默认档：实测最省 token 之一（工具调用 ~200），稳定支持工具调用。"},
    {"id": "glm-5.2", "label": "GLM-5.2", "tier": "econo",
     "note": "省 token 档：工具调用实测最省（~181 tokens）。"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "tier": "plus",
     "note": "对话回复最省（~134 tokens），工具调用略费。"},
    {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash", "tier": "flash",
     "note": "均衡档，工具调用与对话都可用。"},
    {"id": "qwen3.8-max", "label": "Qwen3.8 Max", "tier": "max",
     "note": "旗舰档：能力最强，token 消耗中等偏高。"},
    {"id": "qwen3.8-flash", "label": "Qwen3.8 Flash", "tier": "flash",
     "note": "千问极速档。"},
    {"id": "qwen3.7-max", "label": "Qwen3.7 Max", "tier": "max",
     "note": "千问上代旗舰。"},
    {"id": "qwen3.7-plus", "label": "Qwen3.7 Plus", "tier": "plus",
     "note": "千问均衡档，实测 token 偏费。"},
    {"id": "qwen3.6-flash", "label": "Qwen3.6 Flash", "tier": "flash",
     "note": "千问早期档，实测 token 最费，不建议默认。"},
    {"id": "deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash (0731)", "tier": "flash",
     "note": "DeepSeek 快档快照版。"},
    {"id": "auto", "label": "Auto（上游自动选）", "tier": "auto",
     "note": "由上游按请求自动挑模型，token 消耗不可控。"},
)

# tier 排序权重（数字越小越靠前）。默认档 glm-5.3 排最前。
_TIER_ORDER = {"econo": 0, "flash": 1, "plus": 2, "max": 3, "auto": 4}


def documented_ids() -> tuple[str, ...]:
    """文档目录里的模型 id（顺序与 DOCUMENTED 一致）。"""
    return tuple(m["id"] for m in DOCUMENTED)


def is_documented(model_id: str) -> bool:
    return str(model_id) in set(documented_ids())


def _tier_of(model_id: str) -> int:
    """给任意模型 id 估一个排序档：命中文档目录用其 tier；否则按名字猜，最后兜底。"""
    for m in DOCUMENTED:
        if m["id"] == model_id:
            return _TIER_ORDER.get(m["tier"], 9)
    low = model_id.lower()
    if "glm" in low:
        return 0
    if "max" in low or "pro" in low:
        return 3
    if "plus" in low:
        return 2
    if "flash" in low or "turbo" in low:
        return 1
    if low == "auto":
        return 4
    return 5


def merge_models(probed: Sequence[str]) -> list[dict[str, Any]]:
    """把上游探测到的真实模型 id 与本地文档目录合并成一份可展示列表。

    规则：
      - **非对话模型一律不显示**（图像/TTS/实时语音等，走不通 chat.completions）。
      - 探测到的模型 source="probe"（真实可用，排在前）。
      - 文档目录里、但没被探测到的，source="documented"（已验证过但当前未探到，排在后）。
      - 同名去重：探测结果优先，并继承文档目录的 label/tier/note。
      - 同 source 内按 tier 权重再按 id 排序，稳定可预期。
    """
    doc_by_id = {m["id"]: m for m in DOCUMENTED}
    probed_ids: list[str] = []
    seen: set[str] = set()
    for raw in probed or ():
        mid = str(raw or "").strip()
        if not mid or mid in seen:
            continue
        if not is_chat_capable(mid):     # 过滤图像/TTS/实时语音等非对话模型
            continue
        seen.add(mid)
        probed_ids.append(mid)

    rows: list[dict[str, Any]] = []
    for mid in probed_ids:
        doc = doc_by_id.get(mid)
        rows.append({
            "id": mid,
            "label": (doc or {}).get("label") or mid,
            "tier": (doc or {}).get("tier") or "unknown",
            "note": (doc or {}).get("note") or "上游授权模型（实探）。",
            "source": "probe",
            "verified": True,
        })
    for mid, doc in doc_by_id.items():
        if mid in seen or not is_chat_capable(mid):
            continue
        rows.append({
            "id": mid,
            "label": doc.get("label") or mid,
            "tier": doc.get("tier") or "unknown",
            "note": doc.get("note") or "",
            "source": "documented",
            "verified": False,
        })

    rows.sort(key=lambda r: (0 if r["source"] == "probe" else 1,
                             0 if r["id"] == DEFAULT_MODEL else 1,   # 默认模型置顶
                             _tier_of(r["id"]), r["id"]))
    return rows


def normalize_probed_payload(payload: Any) -> list[str]:
    """从 OpenAI 兼容的 /models 响应里抽出模型 id 列表。

    兼容两种常见形态：
      {"data": [{"id": "..."}, ...]}   （OpenAI 标准）
      [{"id": "..."}, ...] 或 ["...", ...]（少数网关）
    任何异常都吞成空列表，绝不抛——探测失败不应拖垮 /api/models。
    """
    out: list[str] = []
    try:
        items: Iterable[Any]
        if isinstance(payload, Mapping):
            data = payload.get("data")
            items = data if isinstance(data, list) else []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        for it in items:
            if isinstance(it, Mapping):
                mid = it.get("id") or it.get("model") or it.get("name")
                if mid:
                    out.append(str(mid))
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
    except (TypeError, ValueError, AttributeError):
        return []
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for mid in out:
        if mid not in seen:
            seen.add(mid)
            uniq.append(mid)
    return uniq
