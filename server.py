# 幕僚 Muliáo · 本地后端
# 职责：
#   1) 接 TypeSafe Jev（System One 模型）做结构化快判断：回合前体检 + 回合后复核 + 蜂群编排决策
#   2) 代理上游对话模型，SSE 流式转发（含思考流 reasoning_content）——前端不直连、不暴露 key
#   3) 提供静态前端 + 权限同意闸门 + 本机采集（通知/进程/窗口）
# 启动：python server.py   （默认 http://127.0.0.1:8930）
import asyncio
import copy
import json
import math
import os
import re
import sys
import time
import threading
import uuid
import webbrowser
from urllib.parse import urlsplit

from runtime_paths import config_path, data_dir


# ---- 无控制台（PyInstaller --windowed / APP 模式）下的 stdio 兜底 ----
# windowed 模式里 sys.stdout / sys.stderr 是 None，而 uvicorn 的日志格式化器会调
# sys.stderr.isatty()，直接 AttributeError 崩掉。这里给它们装一个可用的替身。
class _NullStream:
    """最小可用的假流：吞掉写入，提供 logging/uvicorn 需要的方法。"""
    def __init__(self, path=None):
        self._fh = None
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
            except Exception:
                self._fh = None

    def write(self, s):
        if self._fh is not None:
            try:
                self._fh.write(s)
            except Exception:
                pass
        return len(s)

    def writelines(self, seq):
        for s in seq:
            self.write(s)

    def flush(self):
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass

    def isatty(self):
        return False          # 关键：告诉 uvicorn 不要走彩色/终端分支

    def fileno(self):
        if self._fh is not None:
            return self._fh.fileno()
        raise OSError("no fileno")

    @property
    def encoding(self):
        return "utf-8"

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass


LOG_DIR = str(data_dir())
_LOG_PATH = os.path.join(LOG_DIR, "server.log")
_CONFIG_PATH = str(config_path())


def _load_local_config() -> dict:
    """读取仓库外的本机配置；环境变量始终具有更高优先级。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_LOCAL_CONFIG = _load_local_config()


def _setting(name: str, default: str = "") -> str:
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value
    value = _LOCAL_CONFIG.get(name, default)
    return str(value) if value is not None else default


if sys.stdout is None or not hasattr(sys.stdout, "write"):
    sys.stdout = _NullStream(_LOG_PATH)
if sys.stderr is None or not hasattr(sys.stderr, "write"):
    sys.stderr = _NullStream(_LOG_PATH)

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文/emoji 崩溃
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

import notifier
import permissions
import collectors
import machine_tools
import action_gate
from swarm import RECIPES, ROLE_POOL, SwarmOrchestrator, deterministic_fallback_plan
from swarm_api import SWARM_PLAN_QUESTIONS, SwarmService
from swarm_models import BeeSpec
from swarm_runtime import BeeRuntime

# ---- 配置（环境变量优先，其次读取仓库外 %APPDATA%\Muliao\config.json）----
# 对话模型（主模型，负责想和写）
LLM_BASE = _setting("MULIAO_LLM_BASE", "http://152.53.54.178:8318/v1")
LLM_KEY = _setting("MULIAO_LLM_KEY")
LLM_MODEL = _setting("MULIAO_LLM_MODEL", "gpt-5.6-sol-free")

# Jev（TypeSafe System One，负责快判断）
JEV_URL = _setting("MULIAO_JEV_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODELS_URL = _setting("MULIAO_JEV_MODELS_URL", "https://api.typesafe.ai/v1/models")
JEV_KEY = _setting("MULIAO_JEV_KEY")
JEV_MODEL = _setting("MULIAO_JEV_MODEL", "jev-latest")
JEV_CONSOLE = "https://console.typesafe.ai/keys"   # 额度/key 管理入口

PORT = int(os.environ.get("MULIAO_PORT", "8930"))
NO_BROWSER = os.environ.get("MULIAO_NO_BROWSER") == "1"
INSTANCE_TOKEN = os.environ.get("MULIAO_INSTANCE_TOKEN", "")
# 单轮回复 token 上限：ΔH（每轮新增前缀）主要来自 assistant 回复，封顶它可稳住命中率。
# 参谋本应简洁；160 tokens ≈ 100-150 中文字，够给「结论+依据」。
MAX_REPLY_TOKENS = int(os.environ.get("MULIAO_MAX_REPLY", "160"))
# 资源根目录：开发态 = 本文件所在目录；PyInstaller 打包态（onefile）= 运行时解压目录 sys._MEIPASS
if getattr(sys, "frozen", False):
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="幕僚 Muliáo")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

# 本机网页写接口统一防跨站：只接受固定回环 Host/Origin、同站 Fetch 与 JSON。
# 这不依赖请求 Host 动态生成信任列表，避免 DNS rebinding 绕过。
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_JSON_WRITE_EXEMPT = set()


@app.middleware("http")
async def protect_local_api(req: Request, call_next):
    host = (req.headers.get("host") or "").split(":", 1)[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"ok": False, "err": "非法 Host"}, status_code=400)
    if req.url.path.startswith("/api/") and req.method in _WRITE_METHODS:
        origin = req.headers.get("origin")
        if origin:
            try:
                origin_host = (urlsplit(origin).hostname or "").lower()
            except Exception:
                origin_host = ""
            if origin_host not in {"127.0.0.1", "localhost", "::1"}:
                return JSONResponse({"ok": False, "err": "跨源请求被拒绝"}, status_code=403)
        if (req.headers.get("sec-fetch-site") or "").lower() == "cross-site":
            return JSONResponse({"ok": False, "err": "跨站请求被拒绝"}, status_code=403)
        content_type = (req.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if req.url.path not in _JSON_WRITE_EXEMPT and content_type != "application/json":
            return JSONResponse({"ok": False, "err": "写接口只接受 application/json"}, status_code=415)
    return await call_next(req)


# Jev 连通性缓存（避免每条消息都打 /models）
_jev_health = {"ok": None, "model": None, "err": None, "checked_at": 0.0, "ms": None}

# ============ 稳定系统提示词（prompt 缓存命中率的关键）============
# 实测规律：该端点按「前缀逐字节匹配」命中缓存，TTL>65s，最小 ~128 tokens。
# system 越长越稳命中率越高——74 tokens 时多轮才 55%，~2000 tokens 时第 2 轮起稳定 95%+。
# 因此这段必须逐字节永不变：不含时间戳、随机数、动态内容。后端统一注入，前端不拼。
SYSTEM_PROMPT = (
    "你是「幕僚 Muliáo」，一名冷静、克制、重证据的中文参谋助手。\n"
    "【工作准则】1.先给结论再给依据；2.不确定就明说不确定，绝不编造无法核实的事实；"
    "3.涉及风险或不可逆操作，先提示风险并请求确认，不直接教用户执行危险动作；"
    "4.回答简洁有条理，使用中文；5.每条主张尽量可追溯到来源或推理。\n"
    "【篇幅纪律】默认回答控制在 150 字以内，只给结论与最关键的依据；"
    "用户明确要求展开时才写长，且分点陈述，不堆砌铺垫与客套。\n"
    "【判断门控】对每条用户消息，系统会先用快速判断引擎评估意图、危险等级、是否外泄、是否需澄清；"
    "危险度高或需澄清时，你应当先反问或给出选项，而不是直接执行。\n"
    "【few-shot 示例】\n"
    "用户：帮我把整个项目删掉。 幕僚：这是不可逆操作。你确认要删除当前项目的全部文件吗？"
    "我可以先帮你备份，或只删除指定子目录。请确认范围。\n"
    "用户：解释置信度门控。 幕僚：结论——置信度门控是按模型对结果的把握程度决定是否采用。"
    "依据——高于阈值自动执行，低于阈值转人工或换模型，能减少错误决策。\n"
    "用户：今天天气如何。 幕僚：我无法获取实时天气数据。建议你查看天气应用；"
    "如果你把数据贴给我，我可以帮你分析。\n"
    "【本机感知】若你被授予了本机数据工具（工具列表里会出现），当用户提到「我现在」「刚才那个」"
    "「我电脑上」「有什么消息」等指代，或问题需要真实环境信息才能回答时，**先调用相应工具读取事实再作答**，"
    "不要凭空猜测。未获授权的数据源不会出现在工具列表里——此时如实说明未授权，不要编造。\n"
    "【背景资料】以下为长期稳定的参考知识，供你回答时引用：\n"
    + "参谋系统强调证据链与可追溯性，任何结论都应有来源支撑；快判断引擎用于分流与门控，复杂推理与写作由主模型承担。" * 60
)

# ============ 缓存命中率统计（按会话）============
# {session_id: {"turns": int, "prompt_tokens": int, "cached_tokens": int, "history": [...]}}
_cache_stats: dict = {}
_cache_lock = threading.Lock()


def _record_cache(session_id: str, prompt_tokens: int, cached_tokens: int) -> dict:
    with _cache_lock:
        st = _cache_stats.setdefault(session_id, {"turns": 0, "prompt_tokens": 0, "cached_tokens": 0,
                                                  "warm_prompt": 0, "warm_cached": 0, "history": []})
        st["turns"] += 1
        st["prompt_tokens"] += prompt_tokens or 0
        st["cached_tokens"] += cached_tokens or 0
        # 稳态 = 排除首轮冷启动（第 2 轮起）的累计，反映真实长对话命中率
        if st["turns"] >= 2:
            st["warm_prompt"] += prompt_tokens or 0
            st["warm_cached"] += cached_tokens or 0
        rate = (cached_tokens / prompt_tokens) if prompt_tokens else 0.0
        st["history"].append({"turn": st["turns"], "prompt": prompt_tokens, "cached": cached_tokens, "rate": round(rate, 4)})
        st["history"] = st["history"][-60:]
        cum_rate = (st["cached_tokens"] / st["prompt_tokens"]) if st["prompt_tokens"] else 0.0
        warm_rate = (st["warm_cached"] / st["warm_prompt"]) if st["warm_prompt"] else 0.0
        return {"turn": st["turns"], "prompt": prompt_tokens, "cached": cached_tokens,
                "rate": round(rate, 4), "cum_prompt": st["prompt_tokens"],
                "cum_cached": st["cached_tokens"], "cum_rate": round(cum_rate, 4),
                "warm_rate": round(warm_rate, 4), "warm_turns": max(0, st["turns"] - 1)}


# ============ 启动预热：把稳定 system 前缀写进上游缓存 ============
def _prewarm_cache():
    """后台预热线程：用 SYSTEM_PROMPT 打一次极短请求，把这段长前缀写进上游 prompt 缓存。
    这样连首轮对话的 system 部分都能命中（实测 TTL>65s，连续对话每轮刷新）。"""
    url = LLM_BASE.rstrip("/") + "/chat/completions"
    payload = {"model": LLM_MODEL,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": "hi"}],
               "stream": False, "max_tokens": 1}
    headers = {"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60.0)
            if r.status_code == 200:
                print("🔥 缓存预热完成（system 前缀已写入上游缓存）")
                return
            print(f"⚠️  预热返回 {r.status_code}，重试…")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  预热失败：{e}，重试…")
        time.sleep(3)


def _json(obj, status=200):
    return JSONResponse(obj, status_code=status, headers={"Cache-Control": "no-store"})


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ============ Jev 调用 ============
_JEV_RETRY_STATUSES = {429}
_jev_log_lock = threading.Lock()


def _jev_error_code(status: int | None) -> str:
    if status == 402:
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    return "upstream"


def _log_jev_failure(*, code: str, status: int | None, attempts: int, ms: int,
                     error_type: str, retrying: bool) -> None:
    """写脱敏诊断：不记录 state、question 内容、API key 或响应正文。"""
    line = (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] JEV "
        f"code={code} status={status if status is not None else '-'} "
        f"attempt={attempts} ms={ms} error={error_type} retrying={int(retrying)}\n"
    )
    try:
        with _jev_log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(_LOG_PATH, "a", encoding="utf-8") as log_file:
                log_file.write(line)
    except OSError:
        pass


async def jev_ask(state: str, questions: dict, timeout: float = 25.0) -> dict:
    """调 Jev；只有「请求尚未发出」的连接失败与 429 限流会重试一次。

    读超时/5xx/529 说明请求可能已被上游处理，自动重发会重复计费，
    因此只报告诊断码（timeout/upstream），由用户决定是否重试。
    """
    body = {"state": str(state)[:24000], "model": JEV_MODEL, "questions": questions}
    headers = {"Authorization": f"Bearer {JEV_KEY}", "Content-Type": "application/json"}
    started = time.monotonic()
    max_attempts = 2

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, max_attempts + 1):
                status = None
                code = "upstream"
                error_type = "upstream"
                message = "Jev 上游异常"
                retryable = False
                try:
                    response = await client.post(JEV_URL, json=body, headers=headers)
                    status = response.status_code
                    if status == 200:
                        try:
                            payload = response.json()
                        except (TypeError, ValueError) as exc:
                            ms = round((time.monotonic() - started) * 1000)
                            _log_jev_failure(
                                code="bad_response", status=200, attempts=attempt, ms=ms,
                                error_type=type(exc).__name__, retrying=False,
                            )
                            return {
                                "ok": False, "answers": {}, "model": None, "usage": None,
                                "ms": ms, "err": "Jev 返回的 JSON 无法解析",
                                "need_topup": False, "code": "bad_response", "status": 200,
                                "attempts": attempt,
                            }
                        answers = payload.get("answers") if isinstance(payload, dict) else None
                        if not isinstance(answers, dict):
                            ms = round((time.monotonic() - started) * 1000)
                            _log_jev_failure(
                                code="bad_response", status=200, attempts=attempt, ms=ms,
                                error_type="missing_answers", retrying=False,
                            )
                            return {
                                "ok": False, "answers": {}, "model": None, "usage": None,
                                "ms": ms, "err": "Jev 返回缺少 answers",
                                "need_topup": False, "code": "bad_response", "status": 200,
                                "attempts": attempt,
                            }
                        expected_keys = set(questions)
                        missing_keys = sorted(expected_keys.difference(answers))
                        malformed_keys = []
                        for answer_id in expected_keys.intersection(answers):
                            question = questions.get(answer_id) or {}
                            answer = answers.get(answer_id)
                            answer_type = question.get("type") if isinstance(question, dict) else None
                            value_key = {"noul": "noul", "choice": "choice", "score": "score"}.get(answer_type)
                            if not isinstance(answer, dict) or value_key not in answer:
                                malformed_keys.append(answer_id)
                                continue
                            if answer.get("type") is not None and answer.get("type") != answer_type:
                                malformed_keys.append(answer_id)
                                continue
                            raw_value = answer.get(value_key)
                            if answer_type == "choice":
                                allowed = question.get("criteria") if isinstance(question.get("criteria"), dict) else {}
                                if not isinstance(raw_value, str) or not raw_value.strip() or raw_value not in allowed:
                                    malformed_keys.append(answer_id)
                            elif answer_type == "noul":
                                if isinstance(raw_value, bool):
                                    pass
                                elif not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not math.isfinite(float(raw_value)) or not 0.0 <= float(raw_value) <= 1.0:
                                    malformed_keys.append(answer_id)
                            elif answer_type == "score":
                                if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not math.isfinite(float(raw_value)) or not 0.0 <= float(raw_value) <= 10.0:
                                    malformed_keys.append(answer_id)
                            else:
                                malformed_keys.append(answer_id)
                        if missing_keys or malformed_keys:
                            ms = round((time.monotonic() - started) * 1000)
                            detail = []
                            if missing_keys:
                                detail.append("missing=" + ",".join(missing_keys))
                            if malformed_keys:
                                detail.append("malformed=" + ",".join(sorted(malformed_keys)))
                            _log_jev_failure(
                                code="bad_response", status=200, attempts=attempt, ms=ms,
                                error_type="invalid_answers", retrying=False,
                            )
                            return {
                                "ok": False, "answers": {}, "model": None, "usage": None,
                                "ms": ms, "err": "Jev answers 不完整（" + "; ".join(detail) + "）",
                                "need_topup": False, "code": "bad_response", "status": 200,
                                "attempts": attempt,
                            }
                        return {
                            "ok": True,
                            "answers": answers,
                            "model": payload.get("model"),
                            "usage": payload.get("usage"),
                            "ms": round((time.monotonic() - started) * 1000),
                            "err": None,
                            "need_topup": False,
                            "code": None,
                            "status": 200,
                            "attempts": attempt,
                        }

                    code = _jev_error_code(status)
                    error_type = f"HTTP_{status}"
                    message = f"HTTP {status}: {response.text[:300]}"
                    retryable = status in _JEV_RETRY_STATUSES
                    retry_delay = 0.45
                    if status == 429:
                        try:
                            retry_delay = min(2.0, max(0.1, float(response.headers.get("retry-after", retry_delay))))
                        except (AttributeError, TypeError, ValueError):
                            pass
                except asyncio.CancelledError:
                    raise
                except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                    code = "network"
                    error_type = type(exc).__name__
                    message = f"{type(exc).__name__}: Jev 连接建立失败"
                    retryable = True
                    retry_delay = 0.45
                except httpx.TimeoutException as exc:
                    code = "timeout"
                    error_type = type(exc).__name__
                    message = f"{type(exc).__name__}: Jev 请求超时（为避免重复计费未自动重试）"
                    retryable = False
                    retry_delay = 0.0
                except httpx.TransportError as exc:
                    code = "network"
                    error_type = type(exc).__name__
                    message = f"{type(exc).__name__}: Jev 传输中断（为避免重复计费未自动重试）"
                    retryable = False
                    retry_delay = 0.0
                except Exception as exc:  # noqa: BLE001
                    code = "upstream"
                    error_type = type(exc).__name__
                    message = f"{type(exc).__name__}: {machine_tools.sanitize_error(exc)}"
                    retry_delay = 0.0

                ms = round((time.monotonic() - started) * 1000)
                should_retry = retryable and attempt < max_attempts
                _log_jev_failure(
                    code=code, status=status, attempts=attempt, ms=ms,
                    error_type=error_type, retrying=should_retry,
                )
                if should_retry:
                    await asyncio.sleep(retry_delay)
                    continue
                return {
                    "ok": False,
                    "answers": {},
                    "model": None,
                    "usage": None,
                    "ms": ms,
                    "err": message,
                    "need_topup": status in (401, 402, 403),
                    "code": code,
                    "status": status,
                    "attempts": attempt,
                }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        ms = round((time.monotonic() - started) * 1000)
        _log_jev_failure(
            code="network", status=None, attempts=1, ms=ms,
            error_type=type(exc).__name__, retrying=False,
        )
        return {
            "ok": False, "answers": {}, "model": None, "usage": None,
            "ms": ms, "err": f"{type(exc).__name__}: Jev 客户端初始化失败",
            "need_topup": False, "code": "network", "status": None, "attempts": 1,
        }


async def jev_health(force=False) -> dict:
    """轻量探活：GET /v1/models。30 秒内复用缓存。"""
    now = time.time()
    if not force and _jev_health["checked_at"] and now - _jev_health["checked_at"] < 30:
        return _jev_health
    t = time.time()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(JEV_MODELS_URL, headers={"Authorization": f"Bearer {JEV_KEY}"})
        ms = round((time.time() - t) * 1000)
        if r.status_code == 200:
            models = [m.get("name") for m in r.json().get("models", [])]
            _jev_health.update({"ok": True, "model": JEV_MODEL, "err": None,
                                "checked_at": now, "ms": ms, "models": models, "need_topup": False})
        else:
            _jev_health.update({"ok": False, "model": None, "err": f"HTTP {r.status_code}: {r.text[:200]}",
                                "checked_at": now, "ms": ms, "need_topup": r.status_code in (401, 402, 403)})
    except Exception as e:  # noqa: BLE001
        _jev_health.update({"ok": False, "model": None, "err": f"{type(e).__name__}: {e}",
                            "checked_at": now, "ms": None, "need_topup": False})
    return _jev_health


# ============ 内置问题集（英文写题，Jev 英文最准；中文 state 照传） ============
# 回合前 · 快判断
JUDGE_Q = {
    "intent": {
        "type": "choice",
        "instructions": "What is the primary intent of this user message to an AI assistant?",
        "criteria": {
            "chat": "Small talk, greeting, or emotional expression",
            "query": "Asking for information, facts, or an explanation",
            "task": "A concrete task or operation to perform",
            "create": "Content creation, writing, generation, or design",
            "risky": "A risky, sensitive, or irreversible operation",
        },
    },
    "danger": {
        "type": "score",
        "instructions": "Rate the danger of executing this request exactly as written, from 1 (harmless) to 9 (irreversible or harmful).",
        "criteria": ["1 harmless read-only", "2 minor", "3 minor", "4 moderate", "5 moderate",
                     "6 fairly serious", "7 serious", "8 severe", "9 irreversible or harmful"],
    },
    "external": {
        "type": "noul",
        "instructions": "Does this request send data off the local machine (upload, publish, post, or send to a third party)?",
    },
    "clarify": {
        "type": "noul",
        "instructions": "Is this request too vague or underspecified, such that the assistant should ask a clarifying question before acting?",
    },
}

# 回合后 · 复核（state = 用户问题 + 幕僚回复）
REVIEW_Q = {
    "on_topic": {
        "type": "noul",
        "instructions": "Does the assistant's reply actually answer the user's question, rather than being off-topic or evasive?",
    },
    "safe": {
        "type": "noul",
        "instructions": "Is the assistant's reply safe, giving no unauthorized, harmful, or reckless execution instructions?",
    },
    "grounded": {
        "type": "noul",
        "instructions": "Is the assistant's reply restrained, not fabricating unverifiable facts or overclaiming certainty?",
    },
}


# ============ 路由 ============
@app.get("/api/instance")
async def instance_status():
    """纯本机、无外网探活的实例身份，用于单实例端口扫描。"""
    return _json({"app": "muliao", "protocol": 1,
                  "token": INSTANCE_TOKEN, "pid": os.getpid()})


@app.get("/api/status")
async def status():
    h = await jev_health()
    cons = permissions.status()
    return _json({
        "engine": "Jev (TypeSafe System One)",
        "jev_ok": bool(h.get("ok")),
        "jev_err": h.get("err"),
        "jev_need_topup": bool(h.get("need_topup")),
        "jev_model": h.get("model") or JEV_MODEL,
        "jev_ms": h.get("ms"),
        "jev_console": JEV_CONSOLE,
        "model": LLM_MODEL,
        "base": LLM_BASE,
        # 权限：未同意总条款时，前端先弹权限页
        "agreed": cons["agreed"],
        "granted_scopes": permissions.granted_scopes(),
    })


# ============ 权限同意闸门 ============
def _clear_sensitive_sessions() -> None:
    """模型可见工具集合变化后清掉含工具结果的历史并撤销相关蜂群快照。"""
    global _permission_epoch
    with _sessions_lock:
        _sessions.clear()
    with _cache_lock:
        _cache_stats.clear()
    _permission_epoch += 1


def _visible_tool_names() -> tuple[str, ...]:
    return tuple(machine_tools.available_tool_names())


def _clear_if_tool_visibility_changed(before: tuple[str, ...]) -> None:
    # voice_control 等设备能力不进入聊天/蜂群工具 schema，切换它不能造成缓存冷启动。
    if before != _visible_tool_names():
        _clear_sensitive_sessions()


@app.get("/api/permissions")
async def perm_status():
    """所有采集源的可用性 + 当前授权状态（供权限页渲染）。"""
    return _json({
        "consent": permissions.status(),
        "sources": collectors.sources(),
        "capabilities": collectors.capabilities(),
    })


@app.post("/api/permissions/agree")
async def perm_agree(req: Request):
    """用户同意隐私条款并把 scopes 保存为完整授权集合。"""
    try:
        b = await req.json()
    except Exception:
        return _json({"ok": False, "err": "请求体不是合法 JSON"}, 400)
    if not isinstance(b, dict):
        return _json({"ok": False, "err": "请求体必须是 JSON 对象"}, 400)
    all_value = b.get("all", False)
    if not isinstance(all_value, bool):
        return _json({"ok": False, "err": "all 必须是布尔值"}, 422)
    scopes = b.get("scopes", [])
    if scopes is None:
        scopes = []
    if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
        return _json({"ok": False, "err": "scopes 必须是字符串数组"}, 422)
    unknown = [s for s in scopes if s not in permissions.DATA_SCOPES]
    if unknown:
        return _json({"ok": False, "err": f"未知或不可批量授权的权限项：{unknown[0]}"}, 422)
    before_tools = _visible_tool_names()
    try:
        st = permissions.agree(agree_all=True) if all_value else permissions.agree(scopes=scopes)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "err": machine_tools.sanitize_error(e)}, 500)
    _clear_if_tool_visibility_changed(before_tools)
    return _json({"ok": True, "consent": st})


@app.post("/api/permissions/scope")
async def perm_scope(req: Request):
    """单项授权开关。body: {scope, on}"""
    try:
        b = await req.json()
    except Exception:
        return _json({"ok": False, "err": "请求体不是合法 JSON"}, 400)
    if not isinstance(b, dict) or b.get("scope") not in permissions.ALL_SCOPES:
        return _json({"ok": False, "err": "scope 无效"}, 422)
    if not isinstance(b.get("on"), bool):
        return _json({"ok": False, "err": "on 必须是布尔值"}, 422)
    before_tools = _visible_tool_names()
    try:
        st = permissions.set_scope(b["scope"], b["on"])
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "err": machine_tools.sanitize_error(e)}, 500)
    _clear_if_tool_visibility_changed(before_tools)
    return _json({"ok": True, "consent": st})


@app.post("/api/permissions/revoke")
async def perm_revoke():
    try:
        st = permissions.revoke_all()
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "err": machine_tools.sanitize_error(e)}, 500)
    _clear_sensitive_sessions()
    return _json({"ok": True, "consent": st})


@app.post("/api/permissions/purge")
async def perm_purge():
    """撤销授权 + 删除同意记录 + 清本机采集缓存。"""
    try:
        st = permissions.purge_all()
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "err": machine_tools.sanitize_error(e)}, 500)
    _clear_sensitive_sessions()
    removed = []
    # 清采集产生的本地落盘文件（journal 等），不动用户原始数据
    for p in [getattr(notifier, "_LINUX_JOURNAL", None)]:
        if p and os.path.isfile(p):
            try:
                os.remove(p); removed.append(p)
            except OSError:
                pass
    return _json({"ok": True, "consent": st, "removed": removed})


# ============ 本机采集（权限确认后才真正返回数据）============
@app.get("/api/collect/{scope}")
async def collect(scope: str, req: Request):
    """按 scope 采集。未授权返回 granted=False + 空。"""
    fn = collectors.COLLECTORS.get(scope)
    if not fn:
        return _json({"err": f"未知采集源：{scope}",
                      "known": list(collectors.COLLECTORS)}, 404)
    q = req.query_params
    kw = {}
    if "limit" in q:
        try:
            # ⚠️ 负数 limit 会被下游 `items[:-n]` 当成「从末尾砍 n 条」，
            # 恰好砍掉内存最高的进程（用户最想看的）。≤0 一律当未给。
            n = int(q["limit"])
            kw["limit"] = min(n, 500) if n > 0 else None
        except ValueError:
            pass
    if kw.get("limit") is None and "limit" in q:
        kw.pop("limit", None)
    if "since_id" in q:
        kw["since_id"] = q["since_id"]
    try:
        data = await asyncio.to_thread(fn, **kw)
    except Exception as e:  # noqa: BLE001
        # 原始异常含本机路径（用户名/库位置），脱敏后再回，即使只是本机浏览器也别泄漏
        return _json({"granted": permissions.is_granted(scope),
                      "error": machine_tools.sanitize_error(e)}, 500)
    return _json(data)


@app.get("/api/collect")
async def collect_all(req: Request):
    """一次拿全部已授权源的概要（权限页「实际能采到什么」的验证用）。"""
    out = {}
    for scope in permissions.DATA_SCOPES:
        if not permissions.is_granted(scope):
            out[scope] = {"granted": False}
            continue
        fn = collectors.COLLECTORS.get(scope)
        try:
            data = await asyncio.to_thread(fn, limit=5)
            # 只回概要，避免一次拉太多
            if isinstance(data, dict) and "items" in data:
                out[scope] = {"granted": True, "count": data.get("count"),
                              "sample": data["items"][:3]}
            else:
                out[scope] = {"granted": True, "data": data}
        except Exception as e:  # noqa: BLE001
            out[scope] = {"granted": True, "error": str(e)}
    return _json(out)


@app.post("/api/jev/recheck")
async def jev_recheck():
    h = await jev_health(force=True)
    return _json({**h, "console": JEV_CONSOLE})


@app.post("/api/judge")
async def judge(req: Request):
    """回合前快判断 / 回合后复核：Jev 一次请求并行评估多个原子问题。"""
    body = await req.json()
    state = str(body.get("state", ""))
    kind = body.get("kind", "judge")
    questions = body.get("questions") or (REVIEW_Q if kind == "review" else JUDGE_Q)

    res = await jev_ask(state, questions)
    if not res["ok"]:
        return _json({
            "engine": "Jev", "ok": False, "kind": kind, "answers": {},
            "err": res.get("err"), "code": res.get("code"), "status": res.get("status"),
            "attempts": res.get("attempts"), "need_topup": res.get("need_topup"),
            "console": JEV_CONSOLE, "ms": res.get("ms"),
        }, 200)
    return _json({
        "engine": "Jev", "ok": True, "kind": kind,
        "ms": res.get("ms"), "model": res.get("model"), "usage": res.get("usage"),
        "attempts": res.get("attempts"), "answers": res.get("answers", {}),
    })


@app.get("/api/cache/{session_id}")
async def cache_stats(session_id: str):
    with _cache_lock:
        st = _cache_stats.get(session_id)
        sys_est = len(SYSTEM_PROMPT) // 3
        if not st:
            return _json({"session_id": session_id, "turns": 0, "cum_prompt": 0,
                          "cum_cached": 0, "cum_rate": 0.0, "warm_rate": 0.0, "warm_turns": 0,
                          "history": [], "target": 0.90, "system_tokens_est": sys_est})
        cum_rate = (st["cached_tokens"] / st["prompt_tokens"]) if st["prompt_tokens"] else 0.0
        warm_rate = (st["warm_cached"] / st["warm_prompt"]) if st["warm_prompt"] else 0.0
        return _json({"session_id": session_id, "turns": st["turns"],
                      "cum_prompt": st["prompt_tokens"], "cum_cached": st["cached_tokens"],
                      "cum_rate": round(cum_rate, 4), "warm_rate": round(warm_rate, 4),
                      "warm_turns": st.get("warm_turns", max(0, st["turns"] - 1)),
                      "history": st["history"], "target": 0.90, "system_tokens_est": sys_est})


# ============ 系统通知抓取（跨平台：Windows / macOS / Linux / Android）============
@app.get("/api/notify/capabilities")
async def notify_capabilities():
    caps = await asyncio.to_thread(notifier.capabilities)
    # path-like 字段会暴露用户名与本机目录；未授权时递归抹掉。
    if not permissions.is_granted("notifications"):
        def hide_paths(obj):
            if isinstance(obj, dict):
                for key in list(obj):
                    if key in {"source", "roots", "path"}:
                        obj[key] = None if key != "roots" else []
                    else:
                        hide_paths(obj[key])
            elif isinstance(obj, list):
                for item in obj:
                    hide_paths(item)
        hide_paths(caps)
        for c in caps:
            if isinstance(c, dict):
                c["source_hidden"] = "未授权通知访问"
    return _json({"host_platform": notifier.PLATFORM, "adapters": caps,
                  "granted": permissions.is_granted("notifications")})


@app.get("/api/notify")
async def notify_list(req: Request):
    """抓系统通知。?since_id= 增量；?triage=1 时再用 Jev 批量分级。
    未授权 notifications 时返回 granted=False + 空，绝不偷抓。"""
    if not permissions.is_granted("notifications"):
        return _json({"granted": False, "items": [], "count": 0, "platforms_used": [],
                      "note": "未授权通知访问，请在权限页开启"})
    q = req.query_params
    since_id = q.get("since_id", "")
    try:
        requested_limit = int(q.get("limit", "60") or 60)
    except (TypeError, ValueError):
        return _json({"ok": False, "err": "limit 必须是整数"}, 422)
    if requested_limit <= 0:
        return _json({"ok": False, "err": "limit 必须在 1..200 之间"}, 422)
    limit = min(requested_limit, 200)
    all_platforms = q.get("all", "0") == "1"
    triage = q.get("triage", "0") == "1"
    data = await asyncio.to_thread(
        notifier.fetch_all, since_id=since_id, limit=limit, all_platforms=all_platforms)
    # 抓取期间若权限被撤销，丢弃已采内容。
    if not permissions.is_granted("notifications"):
        return _json({"granted": False, "items": [], "count": 0, "platforms_used": []})
    items = data["items"]

    triaged_n = 0
    if triage and items:
        # Jev 的强项：一个 state + 多个问题并行评估，一次往返搞定整批。
        # 把全部通知塞进 state（带索引），为每条生成 category / needs_action 两个问题。
        batch = items[:20]                     # 上限 20 条，避免 state 过长
        state = {"notifications": [
            {"index": i, "app": n["app_name"], "title": n["title"][:120], "body": n["body"][:200]}
            for i, n in enumerate(batch)
        ]}
        questions = {}
        for i in range(len(batch)):
            questions[f"cat_{i}"] = {
                "type": "choice",
                "instructions": {
                    "question": "Classify the notification with index `index` in `notifications`.",
                    "index": i,
                },
                "criteria": {
                    "agent": "From an AI agent / coding tool / task runner (task done or failed)",
                    "system": "OS-level status: power, network, update, volume, security",
                    "message": "A message or social notification from a person or messaging app",
                    "app": "A routine app notification: sync, upsell, reminder",
                },
            }
            questions[f"act_{i}"] = {
                "type": "noul",
                "instructions": {
                    "question": "Does the notification with index `index` in `notifications` need the user to act on it or look at it soon (an error, a completion, an incoming message, a security alert)?",
                    "index": i,
                },
            }
        res = await jev_ask(state, questions, timeout=45.0)
        if res["ok"]:
            a = res["answers"]
            for i, n in enumerate(batch):
                cat = a.get(f"cat_{i}") or {}
                act = a.get(f"act_{i}") or {}
                n["triage"] = {
                    "category": cat.get("choice"),
                    "category_conf": cat.get("confidence"),
                    "needs_action": act.get("noul"),
                }
                triaged_n += 1
            data["triage_ms"] = res.get("ms")
            data["triage_model"] = res.get("model")
        else:
            data["triage_err"] = res.get("err")

    return _json({**data, "items": items, "triaged": triaged_n, "granted": True})


@app.post("/api/notify/journal")
async def notify_journal(req: Request):
    """Linux/通用回写口：把抓到的通知 POST 进来落盘，供 LinuxAdapter.fetch 读取。

    ⚠️ 加固说明（曾是任意写入风险，勿回退）：
      · notify-bridge 实际是**直接写 journal 文件**（见 notify_bridge.py JournalWriter），
        不走这个 HTTP 口；前端也不调它。所以这个口纯粹是「本机手动补录 / 第三方进程写入」。
      · 服务绑定 127.0.0.1，但浏览器里任何网页都能对 127.0.0.1 发**简单请求**
        （Content-Type: text/plain 不触发预检，而 req.json() 照样解析）——
        即经典 localhost CSRF：恶意网页可往用户的通知面板注入伪造通知（钓鱼）。
      · 因此这里加三道闸：①未授权 notifications 直接拒（读取口本就要求授权）
        ②校验 Origin，跨源浏览器请求一律拒 ③字段强制 str + 长度封顶，防污染/撑爆 journal。
    """
    # ① 权限闸：读取口 /api/notify 要求 notifications 授权，写入口对齐
    if not permissions.is_granted("notifications"):
        return _json({"ok": False, "err": "未授权通知访问，拒绝写入"}, 403)

    # ② CSRF 防护：带 Origin 的请求必须同源（curl / 脚本 / bridge 不带 Origin，放行）
    origin = req.headers.get("origin")
    if origin:
        host = req.headers.get("host", "")
        allowed = {f"http://{host}", f"https://{host}",
                   "http://127.0.0.1:" + host.split(":")[-1],
                   "http://localhost:" + host.split(":")[-1]}
        if origin.rstrip("/") not in {a.rstrip("/") for a in allowed}:
            return _json({"ok": False, "err": "跨源请求被拒绝"}, 403)

    try:
        b = await req.json()
    except Exception:
        return _json({"ok": False, "err": "请求体不是合法 JSON"}, 400)
    if not isinstance(b, dict):
        return _json({"ok": False, "err": "请求体必须是 JSON 对象"}, 400)

    def _field(v, cap):
        # 强制成 str 并封顶：防写入 dict/list/超大字符串污染 journal
        if v is None:
            return ""
        s = v if isinstance(v, str) else str(v)
        s = s.replace("\r", " ").replace("\n", " ")   # journal 是 JSONL，禁止裸换行
        return s[:cap]

    app_s = _field(b.get("app"), 80)
    title = _field(b.get("title"), 200)
    body = _field(b.get("body"), 600)
    if not (title or body):
        return _json({"ok": False, "err": "title 与 body 不能同时为空"}, 400)

    try:
        os.makedirs(os.path.dirname(notifier._LINUX_JOURNAL), exist_ok=True)
        seq = time.time_ns()
        rec = {"seq": seq, "ts": time.time(), "app": app_s,
               "title": title, "body": body}
        with open(notifier._LINUX_JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return _json({"ok": True, "seq": seq})
    except Exception as e:  # noqa: BLE001
        # 脱敏：journal 路径含用户名，不泄漏给调用方
        return _json({"ok": False, "err": machine_tools.sanitize_error(e)}, 500)


# ============ 会话历史（后端权威存储）============
# 为什么后端要自己存历史：工具调用轮会产生 assistant(tool_calls) + tool(result) 消息，
# 这些消息前端不保存。若下一轮用前端传回的历史，那条 assistant 就只有纯文本，
# 导致从该点起前缀与上游缓存里的序列**分叉**，缓存全 miss（实测稳态命中率从 96% 掉到 73%）。
# 由后端持有完整序列，前缀才能逐字节稳定。前端传的 messages 只用于冷启动初始化。
_sessions: dict = {}
_sessions_lock = threading.Lock()
_session_turn_locks: dict[str, asyncio.Lock] = {}
_session_turn_locks_guard = threading.Lock()
_permission_epoch = 0
MAX_SESSION_MSGS = int(os.environ.get("MULIAO_MAX_HISTORY", "80"))


def _turn_lock(sid: str) -> asyncio.Lock:
    with _session_turn_locks_guard:
        return _session_turn_locks.setdefault(sid, asyncio.Lock())


def _permission_version() -> int:
    return _permission_epoch


# ============ 动作门控 + 高风险确认（让 Jev 参与每一次工具行为）============
# 每个工具执行前先过 action_gate.evaluate：
#   allow   → 直接执行
#   confirm → 发 tool_confirm 事件，挂起等待用户经 /api/chat/confirm 拍板，再决定执行/拒绝
#   deny    → 拒绝执行，回灌拒绝占位结果（不真正调用工具）
# 确认用 asyncio.Future 在「流生成器」与「确认 POST 端点」之间搭桥：
# 二者跑在同一事件循环，POST 端点 set_result 即可唤醒挂起的生成器。
_pending_confirms: dict[str, asyncio.Future] = {}
_pending_confirms_lock = threading.Lock()
_CONFIRM_TIMEOUT = float(os.environ.get("MULIAO_CONFIRM_TIMEOUT", "120"))


def _register_confirm(confirm_id: str) -> asyncio.Future:
    """登记一个待确认动作，返回供生成器 await 的 Future。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    with _pending_confirms_lock:
        _pending_confirms[confirm_id] = fut
    return fut


def _resolve_confirm(confirm_id: str, decision: str) -> bool:
    """确认端点调用：把用户决定送达挂起的生成器。未知/已决的 id 返回 False。"""
    with _pending_confirms_lock:
        fut = _pending_confirms.pop(confirm_id, None)
    if fut is None:
        return False
    if not fut.done():
        fut.set_result(decision)
    return True


def _discard_confirm(confirm_id: str) -> None:
    """客户端断流/取消时清理：未决的确认一律按拒绝处理，避免 Future 悬挂。"""
    with _pending_confirms_lock:
        fut = _pending_confirms.pop(confirm_id, None)
    if fut is not None and not fut.done():
        fut.set_result("deny")


async def _await_confirmation(confirm_id: str, timeout: float) -> str:
    """挂起等待用户决定。超时/取消一律视为拒绝（fail-safe）。"""
    fut = _register_confirm(confirm_id)
    try:
        decision = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        decision = "deny"
    except asyncio.CancelledError:
        _discard_confirm(confirm_id)
        raise
    finally:
        with _pending_confirms_lock:
            _pending_confirms.pop(confirm_id, None)
    return decision if decision in ("allow", "deny") else "deny"


# 控制类工具中文名（前端弹窗与 tool_gate 展示用；前端也自带一份映射）
_CONTROL_TOOL_CN = {
    "list_windows": "列出窗口", "focus_window": "聚焦窗口", "close_window": "关闭窗口",
    "open_application": "打开应用", "click_element": "点击控件", "type_text": "输入文字",
    "press_keys": "发送按键",
}


# ============ Ghost 蜂群：真实运行时装配与 API 适配 ============
_SWARM_ROLE_PROMPTS = {
    "compiler": "把目标编译成可执行约束、验收标准和清晰边界，不擅自扩展范围。",
    "investigator": "收集并区分观察、推断与待核验信息，优先保留证据来源。",
    "extractor": "从输入与证据中提取结构化事实，避免重复、遗漏和无依据补全。",
    "builder": "在任务契约内产出可用实现或结构化成果，并明确关键取舍。",
    "verifier": "独立核验结果、约束和证据，指出冲突、缺口与不可证实之处。",
    "integrator": "整合各蜂结果，解决可解决的冲突，给出简洁完整的最终答复。",
}

_swarm_plans: dict[str, dict] = {}
_swarm_active_runs: set[str] = set()
_swarm_plans_lock = threading.RLock()


def _swarm_tool_names(specs: list[dict]) -> list[str]:
    names = []
    for item in specs:
        function = item.get("function") if isinstance(item, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _swarm_permission_snapshot() -> dict:
    """同一时点记录授权 scope、工具列表和撤权版本。"""
    scopes = permissions.granted_scopes()
    specs = machine_tools.available_tool_specs()
    return {
        "permission_version": _permission_version(),
        "granted_scopes": list(scopes),
        "scopes": {scope: scope in scopes for scope in permissions.ALL_SCOPES},
        "allowed_tools": _swarm_tool_names(specs),
    }


def _swarm_llm_config() -> dict:
    return {
        "base": LLM_BASE,
        "key": LLM_KEY,
        "model": LLM_MODEL,
        "system_prompt": SYSTEM_PROMPT,
    }


async def _swarm_bee_runner(bee_id: str, context: dict):
    """把 BeeRuntime 的 emit 回调桥成 SwarmOrchestrator 可消费的异步事件流。"""
    contract = context.get("contract") if isinstance(context, dict) else {}
    if not isinstance(contract, dict):
        contract = {}
    allowed_tools = contract.get("allowed_tools") or []
    if not isinstance(allowed_tools, (list, tuple)):
        allowed_tools = []
    spec = BeeSpec(
        bee_id=str(bee_id),
        role_prompt=_SWARM_ROLE_PROMPTS.get(str(bee_id), "严格按任务契约完成被分配的工作。"),
        allowed_tools=tuple(str(name) for name in allowed_tools),
        model=LLM_MODEL,
    )
    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = context.get("cancel_event")

    async def emit(event):
        await queue.put(event)

    task = asyncio.create_task(
        _swarm_runtime.run(
            spec,
            str(context.get("goal") or ""),
            contract,
            context.get("inputs") or [],
            emit,
            cancel_event,
        )
    )
    try:
        while not task.done() or not queue.empty():
            if not queue.empty():
                yield queue.get_nowait()
                continue
            get_event = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({task, get_event}, return_when=asyncio.FIRST_COMPLETED)
            if get_event in done:
                yield get_event.result()
            else:
                get_event.cancel()
                await asyncio.gather(get_event, return_exceptions=True)
        try:
            result = await task
        except asyncio.CancelledError:
            if cancel_event is not None and cancel_event.is_set():
                return
            raise
        while not queue.empty():
            yield queue.get_nowait()
        yield {"type": "result", "result": result}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _ServerSwarmBackend:
    """补齐 SwarmService 与真实 SwarmOrchestrator 之间的参数差异。"""

    def __init__(self, orchestrator: SwarmOrchestrator):
        self.orchestrator = orchestrator

    async def run(self, plan, goal, cancel_event, run_id, **kwargs):
        service_plan = copy.deepcopy(dict(plan or {}))
        core_plan = copy.deepcopy(service_plan.pop("_orchestrator_plan", service_plan))
        confirmed = bool(service_plan.pop("_confirmed", False))
        audit = service_plan.pop("_confirmation_audit", None)
        permission_snapshot = copy.deepcopy(
            service_plan.get("permissions_snapshot") or core_plan.get("permissions_snapshot") or {}
        )
        offset = 0
        last_seq = 0
        if isinstance(audit, dict):
            offset = 1
            last_seq = 1
            yield {
                "event_id": f"evt_confirmed_{run_id}",
                "seq": 1,
                "run_id": str(run_id),
                "task_id": f"tsk_confirmed_{run_id}",
                "ts": audit.get("confirmed_at") or time.time(),
                "type": "swarm.confirmed",
                "payload": copy.deepcopy(audit),
            }
        try:
            async for raw_event in self.orchestrator.run(
                str(goal or core_plan.get("goal") or ""),
                plan=core_plan,
                permission_snapshot=permission_snapshot,
                cancel_event=cancel_event,
                confirmed=confirmed,
                run_id=str(run_id),
            ):
                event = copy.deepcopy(dict(raw_event))
                if isinstance(event.get("seq"), int):
                    event["seq"] += offset
                    last_seq = max(last_seq, event["seq"])
                if confirmed and event.get("type") == "swarm.plan":
                    payload = event.get("payload")
                    emitted_plan = payload.get("plan") if isinstance(payload, dict) else None
                    if isinstance(emitted_plan, dict):
                        emitted_plan["requires_confirmation"] = False
                if event.get("type") == "swarm.done":
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                        event["payload"] = payload
                    results = payload.get("results")
                    if isinstance(results, dict):
                        final = results.get("integrator")
                        if isinstance(final, dict) and final.get("text"):
                            payload.setdefault("final_text", str(final["text"]))
                yield _sanitize_swarm_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield {
                "event_id": f"evt_error_{run_id}_{time.time_ns()}",
                "seq": last_seq + 1,
                "run_id": str(run_id),
                "ts": time.time(),
                "type": "swarm.error",
                "payload": {
                    "status": "error",
                    "code": "server_adapter_error",
                    "message": _sanitize_swarm_error(exc),
                },
            }


def _sanitize_swarm_error(value) -> str:
    exc = value if isinstance(value, BaseException) else RuntimeError(str(value))
    message = machine_tools.sanitize_error(exc)
    message = re.sub(r"(?:~|%[A-Z_]+%)(?:[\\/][^\s\"']*)+", "<path>", message,
                     flags=re.IGNORECASE)
    message = re.sub(r"[A-Za-z]:[\\/](?:[^\s\"']+[\\/]?)+", "<path>", message)
    return message


def _sanitize_swarm_event(event: dict) -> dict:
    safe = copy.deepcopy(event)
    if safe.get("type") != "swarm.error":
        return safe
    payload = safe.get("payload")
    if not isinstance(payload, dict):
        payload = {"message": str(payload)}
        safe["payload"] = payload
    raw = payload.get("message") or payload.get("error") or "蜂群执行失败"
    payload["message"] = _sanitize_swarm_error(raw)
    payload.pop("error", None)
    return safe


def _sanitize_swarm_status(status: dict) -> dict:
    safe = copy.deepcopy(status)
    if safe.get("error") is not None:
        error = safe["error"]
        if isinstance(error, dict):
            error = copy.deepcopy(error)
            for key in ("message", "error", "detail"):
                if error.get(key):
                    error[key] = _sanitize_swarm_error(error[key])
            safe["error"] = error
        else:
            safe["error"] = _sanitize_swarm_error(error)
    return safe


def _swarm_risk_score(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(10.0, float(value)))
    return {
        "low": 1.5,
        "medium": 4.5,
        "high": 7.0,
        "critical": 9.0,
    }.get(str(value or "").strip().lower(), 4.5)


def _swarm_recipe(plan: dict) -> str:
    if plan.get("swarm_worthy") is False:
        return "single"
    task_type = str(plan.get("task_type") or "").strip().lower()
    risk = _swarm_risk_score(plan.get("risk_level"))
    if risk >= 6.0 or task_type in {"action", "sensitive", "risky"}:
        return "sensitive"
    if task_type in {"diagnose", "debug", "troubleshoot"}:
        return "diagnose"
    if task_type in {"research", "query"} and plan.get("evidence_heavy"):
        return "research"
    if task_type in {"code", "create", "build", "mixed"}:
        return "build"
    if plan.get("evidence_heavy") or plan.get("parallelizable"):
        return "research"
    return "build" if plan.get("swarm_worthy") else "single"


def _normalize_swarm_plan(plan: dict, permissions_snapshot: dict, source=None, source_ref=None) -> dict:
    normalized = copy.deepcopy(plan)
    recipe = _swarm_recipe(normalized)
    fallback = deterministic_fallback_plan(str(normalized.get("goal") or ""))
    stages = [
        {"id": f"stage_{index + 1}", "index": index, "bees": list(bees)}
        for index, bees in enumerate(RECIPES[recipe]["stages"])
    ]
    risk_level = _swarm_risk_score(normalized.get("risk_level"))
    needs_clarification = bool(normalized.get("needs_clarify"))
    confirmation_reasons = list(normalized.get("confirmation_reasons") or [])
    if recipe == "sensitive" and "sensitive_recipe" not in confirmation_reasons:
        confirmation_reasons.append("sensitive_recipe")
    if needs_clarification and "needs_clarification" not in confirmation_reasons:
        confirmation_reasons.append("needs_clarification")
    requires_confirmation = bool(
        normalized.get("requires_confirmation")
        or needs_clarification
        or risk_level >= 6.0
        or recipe == "sensitive"
    )
    swarm_worthy = bool(normalized.get("swarm_worthy")) and recipe != "single"
    normalized.update({
        "recipe": recipe,
        "recipe_id": recipe,
        "stages": stages,
        "bees": [bee for stage in stages for bee in stage["bees"]],
        "max_parallel": int(fallback.get("max_parallel", 3)),
        "risk_level": risk_level,
        "needs_clarification": needs_clarification,
        "needs_clarify": needs_clarification,
        "requires_confirmation": requires_confirmation,
        "confirmation_reasons": confirmation_reasons,
        "execution_allowed": swarm_worthy and not requires_confirmation,
        "should_execute": swarm_worthy and not requires_confirmation,
        "status": "requires_confirmation" if requires_confirmation else "planned",
        "permissions_snapshot": copy.deepcopy(permissions_snapshot),
    })
    if source is not None:
        normalized["request_source"] = str(source)[:80]
    if source_ref is not None:
        normalized["source_ref"] = str(source_ref)[:500]
    fallback_reason = normalized.get("fallback_reason")
    if fallback_reason:
        normalized["fallback_reason"] = _sanitize_swarm_error(fallback_reason)
    jev_meta = normalized.get("jev")
    if isinstance(jev_meta, dict) and jev_meta.get("error"):
        jev_meta["error"] = _sanitize_swarm_error(jev_meta["error"])
    return normalized


def _prepare_swarm_run_plan(plan: dict, confirmed: bool, permissions_snapshot: dict):
    original = copy.deepcopy(plan)
    run_plan = copy.deepcopy(original)
    run_plan["permissions_snapshot"] = copy.deepcopy(permissions_snapshot)
    core_plan = copy.deepcopy(original)
    core_plan["permissions_snapshot"] = copy.deepcopy(permissions_snapshot)
    needs_clarification = bool(
        original.get("needs_clarification") or original.get("needs_clarify")
    )
    audit = None
    if confirmed and not needs_clarification:
        reasons = list(original.get("confirmation_reasons") or [])
        audit = {
            "confirmed": True,
            "confirmed_at": time.time(),
            "required_confirmation": bool(original.get("requires_confirmation")),
            "confirmation_reasons": reasons,
        }
        run_plan["requires_confirmation"] = False
        run_plan["needs_clarify"] = False
        run_plan["execution_allowed"] = bool(run_plan.get("swarm_worthy", True))
        # SwarmService 会从风险再次推导门禁；实际风险保留在 core_plan 和审计中。
        run_plan["risk_level"] = "low"
        core_plan["requires_confirmation"] = False
        run_plan["_confirmed"] = True
        run_plan["_confirmation_audit"] = copy.deepcopy(audit)
    else:
        run_plan["_confirmed"] = False
    run_plan["_orchestrator_plan"] = core_plan
    return run_plan, audit


def _mark_swarm_confirmed(run_id: str, audit: dict | None) -> None:
    if not audit:
        return
    _swarm_service._update_run(
        str(run_id),
        status="planned",
        requires_confirmation=False,
        confirmed=True,
        confirmed_at=audit.get("confirmed_at"),
        confirmation_reasons=copy.deepcopy(audit.get("confirmation_reasons") or []),
    )


class _PermissionCancelEvent:
    """权限版本改变时让运行中的真实 runtime 立即看到取消。"""

    def __init__(self, epoch: int):
        self.epoch = int(epoch)
        self._event = asyncio.Event()

    def is_set(self):
        return self._event.is_set() or _permission_version() != self.epoch

    def set(self):
        self._event.set()

    async def wait(self):
        while not self.is_set():
            await asyncio.sleep(0.02)
        return True


_swarm_runtime = BeeRuntime(
    llm_base=LLM_BASE,
    key=LLM_KEY,
    model=LLM_MODEL,
    system_prompt=SYSTEM_PROMPT,
    tool_specs_provider=machine_tools.available_tool_specs,
    tool_executor=machine_tools.execute_tool,
)
_swarm_orchestrator = SwarmOrchestrator(
    _swarm_bee_runner,
    jev_ask,
    max_parallel=max(1, int(os.environ.get("MULIAO_SWARM_MAX_PARALLEL", "3"))),
    max_jev_calls=max(0, int(os.environ.get("MULIAO_SWARM_MAX_JEV_CALLS", "5"))),
)
_swarm_backend = _ServerSwarmBackend(_swarm_orchestrator)
_swarm_service = SwarmService(orchestrator=_swarm_backend)


def _sess_snapshot(sid: str):
    with _sessions_lock:
        s = _sessions.get(sid)
        return json.loads(json.dumps(s)) if s is not None else None


def _sess_restore(sid: str, snapshot) -> None:
    with _sessions_lock:
        if snapshot is None:
            _sessions.pop(sid, None)
        else:
            _sessions[sid] = snapshot



def _sess_realign(sid: str, client_msgs: list, last_user: str) -> None:
    """按前端可见的纯文本历史精确对齐；内容不一致就重建。"""
    client_pairs = [
        {"role": m.get("role"), "content": str(m.get("content", ""))[:8000]}
        for m in client_msgs if m.get("role") in ("user", "assistant")
    ]
    if client_pairs and client_pairs[-1]["role"] == "user" and client_pairs[-1]["content"] == last_user:
        client_pairs.pop()
    with _sessions_lock:
        s = _sessions.get(sid)
        if s is not None:
            own = [
                {"role": m.get("role"), "content": str(m.get("content", ""))[:8000]}
                for m in s["msgs"]
                if m.get("role") in ("user", "assistant") and not m.get("tool_calls")
            ]
            if own == client_pairs:
                return
        _sessions[sid] = {"msgs": client_pairs, "updated": time.time()}


def _sess_append(sid: str, msgs: list) -> None:
    with _sessions_lock:
        s = _sessions.setdefault(sid, {"msgs": [], "updated": time.time()})
        s["msgs"].extend(msgs)
        # 超长时从头部裁剪。裁剪会让该轮 miss 一次（前缀变了），之后重新稳定。
        # 切口可能落在一对 assistant(tool_calls)/tool 中间——那会产生非法序列，
        # 由 _sess_msgs() 读出时的 _sanitize_history() 统一修复。
        if len(s["msgs"]) > MAX_SESSION_MSGS:
            s["msgs"] = s["msgs"][-MAX_SESSION_MSGS:]
        s["updated"] = time.time()


def _sanitize_history(msgs: list) -> list:
    """剔除会让上游 400 的非法消息序列，返回一份可直接发送的副本。

    OpenAI 兼容端点对工具消息有两条硬校验：
      1) 每条 role=tool 的 tool_call_id，必须能在紧邻前面的 assistant.tool_calls 里找到
      2) assistant.tool_calls 里的**每个** id 都必须有对应的 tool 回复，一个都不能少
    破坏这两条的真实路径有三条：
      · MAX_SESSION_MSGS 从头裁剪时切口正好落在 tool_calls / tool 之间
      · 客户端中途断开（关页面）导致只写了 assistant(tool_calls) 没写 tool 结果
      · 上游流断在半截，某个 tool_call 只聚合出半份
    这里在**读出时**统一修复：要么整组保留，要么整组丢弃，绝不留半截。
    必须是纯函数（同输入同输出），否则前缀会抖动、缓存全 miss。
    """
    out: list = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        if not isinstance(m, dict):
            i += 1
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            ids = []
            for tc in (m.get("tool_calls") or []):
                cid = (tc or {}).get("id") if isinstance(tc, dict) else None
                if isinstance(cid, str) and cid and cid not in ids:
                    ids.append(cid)
            # 收集紧随其后的 tool 回复
            j = i + 1
            got: dict = {}
            while j < n and isinstance(msgs[j], dict) and msgs[j].get("role") == "tool":
                cid = msgs[j].get("tool_call_id")
                if isinstance(cid, str) and cid and cid not in got:
                    got[cid] = msgs[j]
                j += 1
            tcs = m.get("tool_calls") or []
            if ids and len(ids) == len(tcs) and all(c in got for c in ids):
                out.append(m)
                out.extend(got[c] for c in ids)      # 顺序与 tool_calls 对齐
            # 不配对 → 整组丢弃（assistant(tool_calls) 和它的 tool 回复都不能单独留）
            i = j
            continue
        if role == "tool":
            i += 1                                   # 孤儿 tool 消息，丢弃
            continue
        if role in ("user", "assistant", "system"):
            # content 为 None 且没有 tool_calls 的消息是空壳，上游会拒
            if m.get("content") is not None or m.get("tool_calls"):
                out.append(m)
            i += 1
            continue
        i += 1                                       # 未知 role，丢弃
    return out


def _sess_msgs(sid: str) -> list:
    with _sessions_lock:
        s = _sessions.get(sid)
        raw = list(s["msgs"]) if s else []
    return _sanitize_history(raw)


# ============ 流式 tool_calls 增量聚合 ============
class _UpstreamError(Exception):
    """上游返回非 200。单独一类是为了让它绕过通用异常处理里的脱敏与重试逻辑，
    原样把上游的状态码和响应体片段透给前端排障。"""


# 上游（OpenAI 兼容）有两种流式形状，都必须吃下：
#   A) 每个分片都带 index——实测本项目用的 152.53.54.178:8318 就是这种：
#      首片带 id+name+index，后续片只带 index+arguments 续传。
#   B) 不带 index（部分实现 / 中转代理会把它吃掉），只靠「出现新 id」标记新调用开始。
# 旧代码 `tool_acc[tc.get("index", 0)]` 在形状 B 下把**所有**分片塞进 slot 0：
# 两次并行调用的 arguments 拼成一串非法 JSON、name 被后者覆盖 → 参数串台、调错工具。
# 实测该上游确实会在**同一轮里并行返回两个调用**（index 0 和 1），所以这不是理论风险。
def _merge_tool_call_delta(acc: list, tc: dict) -> None:
    """把一个 tool_calls 增量分片并进 acc（原地修改，acc 是按调用顺序排列的槽位列表）。"""
    if not isinstance(tc, dict):
        return
    idx = tc.get("index")
    fn = tc.get("function") or {}
    if not isinstance(fn, dict):
        fn = {}
    new_id = tc.get("id") or ""
    if not isinstance(new_id, str):
        new_id = str(new_id)

    def _blank():
        return {"id": "", "name": "", "arguments": ""}

    slot = None
    if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
        # 形状 A：index 权威。但有些中转把所有调用的 index 都写成 0，
        # 此时「同一个 index 上出现了不同的 id」就是新调用的信号，必须另开一槽。
        if idx < len(acc) and new_id and acc[idx]["id"] and acc[idx]["id"] != new_id:
            acc.append(_blank())
            slot = acc[-1]
        else:
            while len(acc) <= idx:
                acc.append(_blank())
            slot = acc[idx]
    else:
        # 形状 B：没有可信 index，靠 id 判断新调用的开始
        if new_id and (not acc or acc[-1]["id"] != new_id):
            acc.append(_blank())
        elif not acc:
            acc.append(_blank())
        slot = acc[-1]

    if new_id and not slot["id"]:
        slot["id"] = new_id
    if fn.get("name") and not slot["name"]:
        slot["name"] = fn["name"]
    if fn.get("arguments"):
        slot["arguments"] += fn["arguments"]


def _finalize_tool_calls(acc: list) -> list:
    """把聚合出来的调用整理成**可安全发给上游**的形状：丢掉残废调用、补 id、id 去重。

    三种必须处理的脏输入：
      · name 为空（上游流断在半截）→ 丢掉。宁可少执行一个工具，也不能发出
        function.name 为空的 tool_call，那是非法消息。
      · id 为空 → 本地合成一个。没有 id 就无法把 tool 结果对回调用，上游会 400。
      · id 重复（中转/重放）→ 去重。两个 tool_call 同 id 会让 tool 消息对错号。
    """
    out, seen = [], set()
    for i, c in enumerate(acc):
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            cid = f"call_local{i}_{int(time.time() * 1000)}"
        while cid in seen:
            cid += "x"
        seen.add(cid)
        args = c.get("arguments")
        out.append({"id": cid, "name": name,
                    "arguments": args if isinstance(args, str) else ""})
    return out


@app.post("/api/chat")
async def chat(req: Request):
    """代理上游模型，SSE 流式转发：reasoning / delta / tool_call / cache / usage / done。

    权限在这里真正生效：只有**已授权**的采集工具会出现在 tools 列表里，
    模型可以按需调用它们读到用户电脑的真实状态；未授权的源对模型完全不可见。
    缓存友好：工具定义是稳定文本，只要授权集不变，前缀（system+tools）逐字节稳定；
    会话历史由后端持有，保证工具调用轮之后的前缀不分叉。"""
    body = await req.json()
    client_messages = body.get("messages") or []
    session_id = str(body.get("session_id") or "default")

    # 取本轮最新 user 文本
    last_user = ""
    for m in reversed(client_messages):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))[:8000]
            break

    # 会话对齐与 user 追加在流生成器拿到该 session 的独占锁后完成。
    # 这样同一会话的并发请求不会交错，失败/断流也可以整体回滚。

    turn_state = {"committed": False, "epoch": None, "confirm_id": None}

    async def gen_turn():
        _sess_realign(session_id, client_messages, last_user)
        _sess_append(session_id, [{"role": "user", "content": last_user}])
        tools = machine_tools.available_tool_specs()
        tool_names = machine_tools.available_tool_names()
        max_tool_rounds = int(os.environ.get("MULIAO_MAX_TOOL_ROUNDS", "4"))
        url = LLM_BASE.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(240.0, connect=20.0)
        sum_prompt = 0
        sum_cached = 0
        rounds = 0
        tools_used = []
        final_text = ""
        hit_max_rounds = False

        if tools:
            yield sse({"type": "tools_available", "tools": tool_names})

        async def stream_round(client, messages, payload_base, out: dict):
            """跑一次上游流式请求：边流边把 SSE 字符串 yield 出去，
            聚合结果写进 out（finish / text / calls / pt / ct）。

            为什么用 out 而不是 return：Python 的 async generator **不允许带值 return**
            （SyntaxError），而这里既要增量转发 delta，又要把聚合结果带回来。

            抽成函数是因为「空 content」要重试一次，两条路径必须共用同一套解析逻辑，
            否则重试分支很容易写出与主分支不一致的聚合规则（正是串台 bug 的温床）。
            """
            acc: list = []
            finish = None
            round_text = ""
            pt = ct = 0
            done_seen = False
            async with client.stream("POST", url, json=payload_base, headers=headers) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise _UpstreamError(f"上游请求失败（HTTP {r.status_code}）")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    p = line[5:].strip()
                    if p == "[DONE]":
                        done_seen = True
                        break
                    try:
                        j = json.loads(p)
                    except json.JSONDecodeError:
                        continue
                    ch = (j.get("choices") or [{}])[0]
                    d = ch.get("delta") or {}

                    if d.get("reasoning_content"):
                        yield sse({"type": "reasoning", "text": d["reasoning_content"]})
                    if d.get("content"):
                        round_text += d["content"]
                        yield sse({"type": "delta", "text": d["content"]})

                    for tc in (d.get("tool_calls") or []):
                        _merge_tool_call_delta(acc, tc)

                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    if j.get("usage"):
                        u = j["usage"]
                        pt = u.get("prompt_tokens") or 0
                        ct = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            if not done_seen and finish is None:
                raise _UpstreamError("上游流式响应意外中断")
            out["finish"] = finish
            out["text"] = round_text
            out["calls"] = _finalize_tool_calls(acc)
            out["pt"] = pt
            out["ct"] = ct

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for round_i in range(max_tool_rounds + 1):
                    # 每轮都从后端 store 重建：system 恒定在最前，历史只增不改。
                    # _sess_msgs 已剔除非法序列，不会把半截 tool_calls 发给上游。
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + _sess_msgs(session_id)
                    payload = {"model": LLM_MODEL, "messages": messages, "stream": True,
                               "max_tokens": MAX_REPLY_TOKENS}
                    if tools:
                        payload["tools"] = tools
                        payload["tool_choice"] = "auto"

                    out = {}
                    async for item in stream_round(client, messages, payload, out):
                        yield item
                    sum_prompt += out.get("pt") or 0
                    sum_cached += out.get("ct") or 0
                    rounds += 1
                    finish = out.get("finish")
                    round_text = out.get("text") or ""
                    calls = out.get("calls") or []

                    # 空回复自动重试：实测该模型偶尔一个 content delta 都不给，
                    # 只有 finish_reason=stop，前端就显示「（无回复）」。
                    # 没有工具调用且文本为空时重试一次（前缀逐字节相同，缓存仍命中）。
                    if not calls and not round_text.strip():
                        yield sse({"type": "notice", "message": "上游返回空内容，重试一次…"})
                        out = {}
                        async for item in stream_round(client, messages, payload, out):
                            yield item
                        sum_prompt += out.get("pt") or 0
                        sum_cached += out.get("ct") or 0
                        rounds += 1
                        finish = out.get("finish")
                        round_text = out.get("text") or ""
                        calls = out.get("calls") or []
                        if not calls and not round_text.strip():
                            # 仍为空：明确告诉前端，别让它只显示「（无回复）」
                            yield sse({"type": "empty_response",
                                       "message": "上游连续两次返回空内容"})

                    if not calls or finish != "tool_calls":
                        final_text = round_text
                        break

                    if round_i == max_tool_rounds:
                        # 最后一轮模型仍在要工具：不能静默无回复，
                        # 也不能把「没有 tool 回复的 assistant(tool_calls)」写进历史。
                        hit_max_rounds = True
                        final_text = round_text
                        break

                    # 原子写：assistant(tool_calls) 与它全部的 tool 结果**一次性**进 store。
                    # 这是本次加固的关键。旧代码先 append assistant，再逐个 append tool，
                    # 中间任何一次 yield 遇到客户端断开（GeneratorExit）就会留下
                    # 「有 tool_calls 没 tool 回复」的半截历史 → 下次请求发给上游就是
                    # 非法消息序列，大概率 400，整个会话报废。
                    results = []
                    for c in calls:
                        args = machine_tools.parse_args(c["arguments"])
                        t0 = time.time()
                        yield sse({"type": "tool_call", "id": c["id"], "name": c["name"],
                                   "args": args if args is not None else {}})
                        if args is None:
                            tool_out = json.dumps({"error": "invalid_arguments",
                                                   "hint": "工具参数不完整，已拒绝执行，请不要重试。"},
                                                  ensure_ascii=False)
                        else:
                            # ── Jev 动作门控：每个工具执行前先裁决 ──
                            is_control = machine_tools.is_control_tool(c["name"])
                            gate = await action_gate.evaluate(
                                tool_name=c["name"], args=args, user_text=last_user,
                                control=is_control, jev_ask=jev_ask,
                                permission_snapshot=_swarm_permission_snapshot(),
                            )
                            gate_ms = round((time.time() - t0) * 1000)
                            yield sse({
                                "type": "tool_gate", "id": c["id"], "name": c["name"],
                                "action": gate.action, "risk": round(gate.risk, 2),
                                "reason": gate.reason, "source": gate.source,
                                "needs_confirmation": gate.needs_confirmation,
                                "jev_ok": gate.jev_ok, "ms": gate_ms,
                            })
                            execute = False
                            if gate.action == "allow":
                                execute = True
                            elif gate.action == "deny":
                                tool_out = json.dumps({
                                    "error": "blocked_by_jev",
                                    "hint": f"Jev 门控拒绝执行此动作：{gate.reason}。不要重试。",
                                }, ensure_ascii=False)
                            else:  # confirm
                                confirm_id = uuid.uuid4().hex
                                turn_state["confirm_id"] = confirm_id
                                yield sse({
                                    "type": "tool_confirm", "confirm_id": confirm_id,
                                    "id": c["id"], "name": c["name"],
                                    "cn": _CONTROL_TOOL_CN.get(c["name"], c["name"]),
                                    "args": args, "risk": round(gate.risk, 2),
                                    "reason": gate.reason,
                                    "timeout_ms": int(_CONFIRM_TIMEOUT * 1000),
                                })
                                decision = await _await_confirmation(confirm_id, _CONFIRM_TIMEOUT)
                                turn_state["confirm_id"] = None
                                if decision == "allow":
                                    execute = True
                                else:
                                    tool_out = json.dumps({
                                        "error": "denied_by_user",
                                        "hint": "用户拒绝了此动作。不要重试，请询问用户意图。",
                                    }, ensure_ascii=False)
                            if execute:
                                tool_out = await asyncio.to_thread(machine_tools.execute_tool,
                                                                   c["name"], args)
                        ms = round((time.time() - t0) * 1000)
                        denied = (args is None or "未获用户授权" in tool_out
                                  or "unavailable_tool" in tool_out
                                  or "blocked_by_jev" in tool_out
                                  or "denied_by_user" in tool_out)
                        tools_used.append({"name": c["name"], "ms": ms, "denied": denied,
                                           "bytes": len(tool_out)})
                        yield sse({"type": "tool_result", "id": c["id"], "name": c["name"],
                                   "ms": ms, "denied": denied, "bytes": len(tool_out)})
                        results.append({"role": "tool", "tool_call_id": c["id"],
                                        "content": tool_out})
                    asst_tc = {
                        "role": "assistant",
                        "content": round_text or None,
                        "tool_calls": [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"],
                                                     "arguments": c["arguments"] or "{}"}}
                                       for c in calls],
                    }
                    if turn_state["epoch"] != _permission_version():
                        raise asyncio.CancelledError()
                    _sess_append(session_id, [asst_tc] + results)
                else:
                    hit_max_rounds = True
            if hit_max_rounds:
                note = (f"工具调用已达上限 {max_tool_rounds} 轮，我用已获取的信息作答；"
                        "如需更多请缩小问题范围。")
                yield sse({"type": "notice", "message": note})
                if not final_text.strip():
                    final_text = note

        except _UpstreamError as e:
            yield sse({"type": "error", "message": str(e)})
            return
        except asyncio.CancelledError:
            # 客户端断开（关页面 / 切会话）。不写任何半截状态，直接结束。
            raise
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001
            yield sse({"type": "error",
                       "message": f"{type(e).__name__}: {machine_tools.sanitize_error(e)}"})
            return

        # 权限集合在本轮中途变化时，丢弃本轮，防止撤权后旧工具数据重新落库。
        if turn_state["epoch"] != _permission_version():
            raise asyncio.CancelledError()
        # 最终回答写入 store（下一轮前缀的一部分）
        _sess_append(session_id, [{"role": "assistant", "content": final_text}])
        turn_state["committed"] = True

        # 跨轮累计记账：命中率 = Σcached / Σprompt（含工具轮的所有上游请求）
        if sum_prompt:
            stats = _record_cache(session_id, sum_prompt, sum_cached)
            yield sse({"type": "cache", "cache": stats})
        if tools_used:
            yield sse({"type": "tools_used", "tools": tools_used, "rounds": rounds})
        yield sse({"type": "done"})

    async def gen():
        lock = _turn_lock(session_id)
        async with lock:
            snapshot = _sess_snapshot(session_id)
            turn_state["committed"] = False
            turn_state["epoch"] = _permission_version()
            try:
                async for item in gen_turn():
                    yield item
            finally:
                # 客户端断流/取消时，若有挂起的确认，一律按拒绝收掉，避免 Future 悬挂、
                # 也避免控制类动作在无人确认的情况下被放行。
                cid = turn_state.get("confirm_id")
                if cid:
                    _discard_confirm(cid)
                    turn_state["confirm_id"] = None
                if (not turn_state["committed"]
                        and turn_state["epoch"] == _permission_version()):
                    _sess_restore(session_id, snapshot)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/chat/confirm")
async def chat_confirm(req: Request):
    """用户对一次高风险动作的裁决：body {confirm_id, decision: "allow"|"deny"}。

    把决定送达正挂起等待的 /api/chat 流生成器。未知/已决/已过期的 confirm_id
    返回 ok:false（前端据此关闭弹窗并提示）。
    """
    try:
        b = await req.json()
    except Exception:
        return _json({"ok": False, "err": "请求体不是合法 JSON"}, 400)
    if not isinstance(b, dict):
        return _json({"ok": False, "err": "请求体必须是 JSON 对象"}, 400)
    confirm_id = str(b.get("confirm_id") or "").strip()
    decision = b.get("decision")
    if not confirm_id:
        return _json({"ok": False, "err": "confirm_id 不能为空"}, 422)
    if decision not in ("allow", "deny"):
        return _json({"ok": False, "err": "decision 必须是 allow 或 deny"}, 422)
    delivered = _resolve_confirm(confirm_id, decision)
    if not delivered:
        return _json({"ok": False, "err": "该确认已失效或不存在（可能已超时/已取消）"}, 404)
    return _json({"ok": True, "confirm_id": confirm_id, "decision": decision})


async def _swarm_json_body(req: Request):
    try:
        body = await req.json()
    except Exception:
        return None, _json({"ok": False, "err": "请求体必须是有效 JSON 对象"}, 400)
    if not isinstance(body, dict):
        return None, _json({"ok": False, "err": "请求体必须是 JSON 对象"}, 400)
    return body, None


async def _wait_for_request_disconnect(req: Request) -> None:
    while not await req.is_disconnected():
        await asyncio.sleep(0.05)


async def _cancel_task(task: asyncio.Task) -> None:
    if task.done():
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        return
    task.cancel()
    # Request.is_disconnected() may be waiting on the ASGI receive channel in
    # synchronous TestClient transports. Do not hold the response open while
    # waiting for that listener to acknowledge cancellation.
    await asyncio.sleep(0)


@app.post("/api/swarm/plan")
async def swarm_plan(req: Request):
    body, error = await _swarm_json_body(req)
    if error is not None:
        return error
    goal = str(body.get("goal") or "").strip()[:24000]
    session_id = str(body.get("session_id") or "default").strip()[:200]
    if not goal:
        return _json({"ok": False, "err": "goal 不能为空"}, 400)
    permissions_snapshot = _swarm_permission_snapshot()
    planner_state = json.dumps(
        {
            "goal": goal,
            "session_id": session_id,
            "permissions_snapshot": permissions_snapshot,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_task = asyncio.create_task(jev_ask(planner_state, copy.deepcopy(SWARM_PLAN_QUESTIONS)))
    disconnect_task = asyncio.create_task(_wait_for_request_disconnect(req))
    try:
        done, _ = await asyncio.wait(
            {plan_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done and plan_task not in done:
            await _cancel_task(plan_task)
            raise asyncio.CancelledError()
        await _cancel_task(disconnect_task)
        jev_response = await plan_task
        raw_plan = _swarm_service.plan(
            goal,
            session_id,
            permissions_snapshot,
            lambda _state, _questions: jev_response,
        )
        if await req.is_disconnected():
            raw_run_id = str(raw_plan.get("run_id") or "") if isinstance(raw_plan, dict) else ""
            if raw_run_id:
                _swarm_service.cancel(raw_run_id)
            raise asyncio.CancelledError()
        plan = _normalize_swarm_plan(
            raw_plan,
            permissions_snapshot,
            source=body.get("source"),
            source_ref=body.get("source_ref"),
        )
        run_id = str(plan["run_id"])
        with _swarm_plans_lock:
            _swarm_plans[run_id] = copy.deepcopy(plan)
        # SwarmService.plan 已建状态；补齐规范化后的确认/recipe 元数据。
        _swarm_service._update_run(
            run_id,
            status=plan.get("status", "planned"),
            source=plan.get("source"),
            recipe=plan.get("recipe"),
            requires_confirmation=bool(plan.get("requires_confirmation")),
            confirmation_reasons=copy.deepcopy(plan.get("confirmation_reasons") or []),
            permission_version=permissions_snapshot["permission_version"],
        )
        return _json({"ok": True, "plan": plan})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "err": machine_tools.sanitize_error(exc)}, 500)
    finally:
        await _cancel_task(disconnect_task)
        if not plan_task.done():
            await _cancel_task(plan_task)


@app.post("/api/swarm/run")
async def swarm_run(req: Request):
    body, error = await _swarm_json_body(req)
    if error is not None:
        return error
    supplied_plan = body.get("plan")
    if not isinstance(supplied_plan, dict):
        return _json({"ok": False, "err": "plan 必须是 JSON 对象"}, 400)
    run_id = str(supplied_plan.get("run_id") or "").strip()
    if not run_id:
        return _json({"ok": False, "err": "plan.run_id 不能为空"}, 400)
    with _swarm_plans_lock:
        stored_plan = copy.deepcopy(_swarm_plans.get(run_id))
    if stored_plan is None:
        status = _swarm_service.status(run_id)
        if not status.get("found"):
            return _json({"ok": False, "err": "未知的 run_id"}, 404)
        stored_plan = copy.deepcopy(supplied_plan)
    # 服务器保存的计划是权威版本；仅 goal/session_id 允许由同 run_id 请求补齐。
    plan = stored_plan
    goal = str(body.get("goal") or plan.get("goal") or "").strip()[:24000]
    session_id = str(body.get("session_id") or plan.get("session_id") or "default").strip()[:200]
    if not goal:
        return _json({"ok": False, "err": "goal 不能为空"}, 400)
    permissions_snapshot = _swarm_permission_snapshot()
    run_plan, audit = _prepare_swarm_run_plan(
        plan,
        confirmed=body.get("confirmed") is True,
        permissions_snapshot=permissions_snapshot,
    )
    permission_cancel = _PermissionCancelEvent(permissions_snapshot["permission_version"])
    with _swarm_plans_lock:
        current = _swarm_service.status(run_id)
        if current.get("status") in {"cancelled", "completed", "failed", "skipped"}:
            return _json({
                "ok": False,
                "err": "该 run_id 已结束，不能再次执行",
                "run_id": run_id,
                "status": current.get("status"),
            }, 409)
        if run_id in _swarm_active_runs:
            return _json({
                "ok": False,
                "err": "该 run_id 正在执行",
                "run_id": run_id,
                "status": "running",
            }, 409)
        _swarm_active_runs.add(run_id)
    _mark_swarm_confirmed(run_id, audit)

    async def gen():
        try:
            async for event in _swarm_service.stream_run(
                run_plan,
                goal,
                session_id,
                _swarm_llm_config(),
                jev_ask,
                machine_tools.execute_tool,
                permission_cancel,
            ):
                yield sse(_sanitize_swarm_event(event))
        except asyncio.CancelledError:
            # 客户端断流时通知 service 的共享取消信号，避免后台蜂继续工作。
            _swarm_service.cancel(run_id)
            raise
        except GeneratorExit:
            _swarm_service.cancel(run_id)
            raise
        except Exception as exc:  # noqa: BLE001
            _swarm_service._update_run(
                run_id,
                status="failed",
                error=_sanitize_swarm_error(exc),
                finished_at=time.time(),
            )
            yield sse({
                "event_id": f"evt_error_{run_id}_{time.time_ns()}",
                "seq": 1,
                "run_id": run_id,
                "ts": time.time(),
                "type": "swarm.error",
                "payload": {
                    "status": "error",
                    "code": "server_stream_error",
                    "message": _sanitize_swarm_error(exc),
                },
            })
        finally:
            with _swarm_plans_lock:
                _swarm_active_runs.discard(run_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/swarm/{run_id}/cancel")
async def swarm_cancel(run_id: str):
    key = str(run_id or "").strip()
    if not key:
        return _json({"ok": False, "err": "run_id 不能为空"}, 400)
    requested = _swarm_service.cancel(key)
    status = _sanitize_swarm_status(_swarm_service.status(key))
    if not status.get("found"):
        return _json({"ok": False, "err": "未知的 run_id", "run_id": key}, 404)
    return _json({
        "ok": True,
        "run_id": key,
        "cancel_requested": requested,
        "status": status.get("status"),
    })


@app.get("/api/swarm/{run_id}")
async def swarm_status(run_id: str):
    status = _sanitize_swarm_status(
        _swarm_service.status(str(run_id or "").strip())
    )
    return _json(status, 200 if status.get("found") else 404)


# ---- 静态前端（必须最后挂载，/api/* 路由优先匹配）----
app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")


# ============ APP 窗口模式：用 Edge/WebView2 开一个无浏览器外壳的独立窗口 ============
def _edge_paths() -> list:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    out = []
    for base, sub in [
        (pfx86, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"Microsoft\Edge\Application\msedge.exe"),
        (local, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"Google\Chrome\Application\chrome.exe"),
        (pfx86, r"Google\Chrome\Application\chrome.exe"),
        (local, r"Google\Chrome\Application\chrome.exe"),
    ]:
        if base:
            out.append(os.path.join(base, sub))
    return [p for p in out if os.path.isfile(p)]


def open_app_window(url: str) -> bool:
    """以 --app 模式打开独立窗口（无地址栏/标签页，看起来就是桌面应用）。
    成功返回 True；找不到内核浏览器则返回 False，由调用方回退到默认浏览器。"""
    exe = _edge_paths()
    if not exe:
        return False
    try:
        import subprocess
        # 独立 user-data-dir 让它成为「幕僚自己的窗口」，不与用户日常浏览器共用会话
        profile = os.path.join(LOG_DIR, "appwindow")
        subprocess.Popen([
            exe[0], f"--app={url}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1500,940",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True
    except Exception as e:  # noqa: BLE001
        print("APP 窗口启动失败：", e)
        return False


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _is_muliao_running(port: int, token: str | None = None) -> bool:
    """无外网探活：验证端口上的服务是否为幕僚，可选校验本次启动 token。"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/instance", timeout=1) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
            return j.get("app") == "muliao" and (token is None or j.get("token") == token)
    except Exception:
        return False


def _fatal(title: str, msg: str) -> None:
    """启动彻底失败时弹一个可见的错误框——绝不能静默退出让用户以为没反应。"""
    print(f"[FATAL] {title}: {msg}")
    try:
        with open(os.path.join(LOG_DIR, "server.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[FATAL] {title}: {msg}\n")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, msg, f"幕僚 Muliáo — {title}", 0x10)   # MB_ICONERROR
            return
        except Exception:
            pass
    # 非 Windows / 弹窗失败：退回浏览器提示页
    try:
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "muliao_error.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"<meta charset=utf-8><body style='background:#000;color:#ffb454;"
                    f"font:16px/1.7 sans-serif;padding:40px'>"
                    f"<h2>幕僚 Muliáo 启动失败 — {title}</h2><pre>{msg}</pre></body>")
        webbrowser.open("file:///" + p.replace("\\", "/"))
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    import logging

    # ---- 端口协商：先复用候选区间内已有幕僚，否则取第一个空端口 ----
    target_port = None
    for cand in range(PORT, PORT + 21):
        if _port_in_use(cand) and _is_muliao_running(cand):
            print(f"幕僚已在 http://127.0.0.1:{cand} 运行，复用现有实例")
            if not NO_BROWSER:
                open_app_window(f"http://127.0.0.1:{cand}") or webbrowser.open(
                    f"http://127.0.0.1:{cand}")
            sys.exit(0)
    for cand in range(PORT, PORT + 21):
        if not _port_in_use(cand):
            target_port = cand
            if cand != PORT:
                print(f"端口 {PORT} 被占用，改用 {cand}")
            break
    if target_port is None:
        _fatal("端口被占用",
               f"端口 {PORT}~{PORT + 20} 全部被占用，无法启动。\n"
               f"请关闭占用这些端口的程序后重试。")
        sys.exit(1)

    print(f"🎖  幕僚 Muliáo → http://127.0.0.1:{target_port}")
    print(f"   对话模型 {LLM_MODEL} @ {LLM_BASE}")
    print(f"   判断引擎 Jev @ {JEV_URL}（key 前缀 {JEV_KEY[:12]}…）")
    threading.Thread(target=_prewarm_cache, daemon=True).start()

    if not NO_BROWSER:
        url = f"http://127.0.0.1:{target_port}"
        # APP 窗口优先；等服务起来再开
        def _launch():
            if not open_app_window(url):
                webbrowser.open(url)
        threading.Timer(1.4, _launch).start()

    # use_colors=False 是崩溃的另一半保险：不让 uvicorn 走 isatty 彩色分支
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    try:
        uvicorn.run(app, host="127.0.0.1", port=target_port,
                    log_level="warning", use_colors=False, access_log=False)
    except Exception as e:  # noqa: BLE001
        _fatal("服务启动失败", f"{type(e).__name__}: {e}")
        sys.exit(1)
