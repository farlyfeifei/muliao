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

# 兼容 OpenAI 的阿里云千问网关基址（官方文档给出的 base_url）。
ALIYUN_BASE = "https://maas.qianwenaiapi.com/compatible-mode/v1"

# 默认主模型：能力档最高的一档，作为开箱默认（可被配置/前端切换覆盖）。
DEFAULT_MODEL = "qwen3.8-max"

# 按官方文档整理的可选模型目录。
#   id    —— 传给上游 chat.completions 的 model 字段（务必逐字一致）。
#   label —— 前端展示名。
#   tier  —— 档位，仅用于排序与说明：max > plus > flash。
#   note  —— 一句话定位。
# 这些是**文档目录**，未经该 key 实探验证；探测成功后真实列表会合并进来。
DOCUMENTED: tuple[dict[str, str], ...] = (
    {"id": "qwen3.8-max", "label": "Qwen3.8 Max", "tier": "max",
     "note": "旗舰档：最强推理与写作，成本最高。"},
    {"id": "qwen3.7-plus", "label": "Qwen3.7 Plus", "tier": "plus",
     "note": "均衡档：能力与成本折中，日常主力。"},
    {"id": "qwen3.8-flash", "label": "Qwen3.8 Flash", "tier": "flash",
     "note": "极速档：低延迟低成本，适合轻量任务。"},
)

# tier 排序权重（数字越小越靠前）。
_TIER_ORDER = {"max": 0, "plus": 1, "flash": 2}


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
    if "max" in low:
        return 0
    if "plus" in low:
        return 1
    if "flash" in low or "turbo" in low:
        return 2
    return 5


def merge_models(probed: Sequence[str]) -> list[dict[str, Any]]:
    """把上游探测到的真实模型 id 与本地文档目录合并成一份可展示列表。

    规则：
      - 探测到的模型 source="probe"（真实可用，排在前）。
      - 文档目录里、但没被探测到的，source="documented"（未验证，排在后）。
      - 同名去重：探测结果优先，并继承文档目录的 label/tier/note。
      - 同 source 内按 tier 权重再按 id 排序，稳定可预期。
    """
    doc_by_id = {m["id"]: m for m in DOCUMENTED}
    probed_ids: list[str] = []
    seen: set[str] = set()
    for raw in probed or ():
        mid = str(raw or "").strip()
        if mid and mid not in seen:
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
        if mid in seen:
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
