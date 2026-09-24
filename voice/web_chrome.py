"""WEB-GOAL 真实浏览器后端（M3B）：用 Chrome DevTools Protocol 驱动真实 Edge/Chrome。

实现 :class:`voice.web_backend.BrowserBackend` 协议，与内存假后端 ``FakeDomBackend``
可互换，从而让 ``WebGoalLoop`` 的 observe→route→policy→confirm→act→verify 闭环
跑在**真实网页**上，而不只是内存假 DOM。

设计要点（与 web_backend.py 的契约对齐）：
- observe() 返回 RawDocument：document_token 变化即代表导航（loop 据此推进 revision、
  让陈旧目标失效）。这里用「URL + 导航计数」派生 token。
- 元素 backend_id 是**文档内稳定**的句柄：observe 时给每个可交互元素分配一个
  data 属性序号，act 时按该序号定位。契约要求 backend_id 在同一文档内稳定即可
  （loop 在 observe 后紧接着 act，导航后 token 变、旧 id 自然失效）。
- 选择器/坐标/JS 绝不来自模型：act 只收到代码解析出的 backend_id + 逐字文本。
- 安全：默认 headless、独立临时 user-data-dir、用完即杀进程释放端口；任何一步
  失败都返回带错误码的 ActResult / 抛可被上层降级的异常，绝不静默吞。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .web_backend import ActResult, RawDocument
from .web_contracts import CommitState, WebErrorCode, WebOperation

# 候选浏览器可执行文件（按优先级）。可用 web_browser_path 覆盖。
_BROWSER_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# 把 HTML 标签/类型映射到 web_observation 认识的角色（决定该元素允许哪些操作）。
_TAG_ROLE: Mapping[str, str] = {
    "a": "link",
    "button": "button",
    "select": "select",
    "textarea": "textbox",
    "option": "option",
}

# 页面内提取可交互元素的 JS。返回 {token 材料, title, text, origin, elements[]}。
# 只提取可交互、可见、非敏感（密码类由上层 observation 再过滤一道）的元素。
_OBSERVE_JS = r"""
(() => {
  const out = {elements: [], title: document.title || "", origin: location.origin || "",
               url: location.href || "", text: (document.body && document.body.innerText || "").slice(0, 8000)};
  const interactive = "a,button,input,select,textarea,[role],[contenteditable='true'],[onclick]";
  const nodes = Array.from(document.querySelectorAll(interactive));
  let seq = 0;
  for (const el of nodes) {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const visible = style.display !== "none" && style.visibility !== "hidden"
      && parseFloat(style.opacity) > 0.01 && rect.width > 0 && rect.height > 0;
    if (!visible) continue;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      // 密码/文件等敏感输入不暴露给模型（observation 层还有第二道过滤）
      if (["password", "file"].includes(t)) continue;
    }
    seq += 1;
    const bid = "b" + seq;
    el.setAttribute("data-muliao-bid", bid);
    let role;
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "checkbox") role = "checkbox";
      else if (t === "radio") role = "radio";
      else if (t === "search") role = "searchbox";
      else if (["button","submit","reset"].includes(t)) role = "button";
      else role = "textbox";
    } else if (el.getAttribute("contenteditable") === "true") {
      role = "contenteditable";
    } else if (el.hasAttribute("role")) {
      role = (el.getAttribute("role") || "generic").toLowerCase();
    } else {
      role = {"a":"link","button":"button","select":"select","textarea":"textbox","option":"option"}[tag] || "generic";
    }
    const label = (el.getAttribute("aria-label") || el.getAttribute("title")
      || (el.innerText || "").trim() || el.getAttribute("placeholder")
      || el.getAttribute("value") || el.getAttribute("name") || "").slice(0, 60);
    let hrefOrigin = "";
    if (tag === "a" && el.href) { try { hrefOrigin = new URL(el.href).origin; } catch (e) {} }
    out.elements.push({
      backend_id: bid,
      role: role,
      label: label,
      input_type: tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "",
      enabled: !el.disabled,
      visible: true,
      destination_origin: hrefOrigin,
      value: (el.value || "").slice(0, 200),
      checked: el.checked === true,
    });
  }
  return out;
})()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_browser(explicit: str = "") -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    for cand in _BROWSER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    which = shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")
    if which:
        return which
    return ""


@dataclass
class ChromeLaunchError(RuntimeError):
    """真实浏览器起不来（找不到可执行文件 / 端口没起 / CDP 连不上）。"""


class ChromeDomBackend:
    """真实浏览器后端，实现 BrowserBackend 协议（observe / act），外加 navigate / close。

    生命周期：构造（不起进程）→ start()（起浏览器+连 CDP）→ observe/act/navigate
    → close()（杀进程、清临时目录）。也可用 with 语句。
    """

    def __init__(
        self,
        *,
        browser_path: str = "",
        headless: bool = True,
        nav_timeout: float = 20.0,
        start_url: str = "about:blank",
    ) -> None:
        self._browser_path = _find_browser(browser_path)
        self._headless = headless
        self._nav_timeout = max(1.0, float(nav_timeout))
        self._start_url = start_url
        self._proc: subprocess.Popen | None = None
        self._ws: Any = None
        self._cdp_id = 0
        self._profile_dir = ""
        self._port = 0
        self._nav_count = 0
        self._started = False

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._started:
            return
        if not self._browser_path:
            raise ChromeLaunchError("no Edge/Chrome executable found")
        try:
            import websockets.sync.client as wsc  # 延迟导入：没装也不影响 fake 后端
        except Exception as exc:  # noqa: BLE001
            raise ChromeLaunchError(f"websockets unavailable: {exc}") from exc

        self._port = _free_port()
        self._profile_dir = tempfile.mkdtemp(prefix="muliao-web-")
        args = [
            self._browser_path,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run", "--no-default-browser-check", "--disable-gpu",
            "--remote-allow-origins=*",
        ]
        if self._headless:
            args.append("--headless=new")
        args.append(self._start_url)
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ChromeLaunchError(f"failed to launch browser: {exc}") from exc

        ws_url = self._wait_for_ws(30.0)
        self._ws = wsc.connect(ws_url, timeout=30, max_size=64 * 1024 * 1024)
        self._send("Page.enable")
        self._send("Runtime.enable")
        self._started = True

    def _wait_for_ws(self, timeout: float) -> str:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json", timeout=2
                ) as resp:
                    tabs = json.loads(resp.read().decode("utf-8"))
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    return page["webSocketDebuggerUrl"]
                last = "no page target yet"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            if self._proc is not None and self._proc.poll() is not None:
                raise ChromeLaunchError(f"browser exited early (code {self._proc.returncode})")
            time.sleep(0.3)
        raise ChromeLaunchError(f"CDP endpoint not ready: {last}")

    def _send(self, method: str, **params: Any) -> dict[str, Any]:
        if self._ws is None:
            raise ChromeLaunchError("backend not started")
        self._cdp_id += 1
        mid = self._cdp_id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise ChromeLaunchError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})

    def _eval(self, expression: str, await_promise: bool = False) -> Any:
        res = self._send("Runtime.evaluate", expression=expression,
                         returnByValue=True, awaitPromise=await_promise)
        if res.get("exceptionDetails"):
            raise ChromeLaunchError("page evaluate raised: "
                                    + str(res["exceptionDetails"].get("text", "")))
        return res.get("result", {}).get("value")

    def close(self) -> None:
        self._started = False
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass
        self._proc = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = ""

    def __enter__(self) -> "ChromeDomBackend":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- BrowserBackend 协议 ----
    def observe(self) -> RawDocument:
        if not self._started:
            self.start()
        data = self._eval(_OBSERVE_JS) or {}
        elements: list[dict[str, Any]] = []
        for raw in data.get("elements") or []:
            if isinstance(raw, Mapping):
                elements.append(dict(raw))
        url = str(data.get("url") or "")
        # document_token 变化即代表导航：URL + 本后端记录的导航次数。
        token = f"{url}#{self._nav_count}"
        return RawDocument(
            document_token=token,
            origin=str(data.get("origin") or ""),
            title=str(data.get("title") or ""),
            text=str(data.get("text") or ""),
            loading_state="complete",
            raw_elements=tuple(elements),
        )

    def navigate(self, url: str) -> ActResult:
        if not self._started:
            self.start()
        try:
            self._send("Page.navigate", url=url)
            self._nav_count += 1
            self._wait_for_load()
            return ActResult(True, CommitState.COMMITTED, navigated_to=url)
        except Exception as exc:  # noqa: BLE001
            return ActResult(False, CommitState.UNKNOWN, WebErrorCode.COMMIT_UNKNOWN,
                             detail=f"navigation failed: {exc}")

    def _wait_for_load(self) -> None:
        deadline = time.time() + self._nav_timeout
        while time.time() < deadline:
            try:
                state = self._eval("document.readyState")
                if state in ("complete", "interactive"):
                    time.sleep(0.2)  # 给渲染/脚本一点余量
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def act(
        self,
        *,
        operation: str,
        target_backend_id: str = "",
        text: str = "",
        option_id: str = "",
        dry_run: bool = True,
    ) -> ActResult:
        if not self._started:
            self.start()
        if dry_run:
            return ActResult(True, CommitState.NOT_COMMITTED, detail="dry-run: no browser side effect")

        if operation == WebOperation.NAVIGATE:
            dest = text or target_backend_id
            if not dest:
                return ActResult(False, CommitState.NOT_COMMITTED,
                                 WebErrorCode.INVALID_PROPOSAL, detail="no navigation target")
            return self.navigate(dest)

        if operation == WebOperation.WAIT:
            time.sleep(min(2.0, max(0.2, self._nav_timeout / 10)))
            return ActResult(True, CommitState.COMMITTED, detail="waited")

        if operation in (WebOperation.SCROLL_UP, WebOperation.SCROLL_DOWN):
            dy = -400 if operation == WebOperation.SCROLL_UP else 400
            self._eval(f"window.scrollBy(0, {dy}); true")
            return ActResult(True, CommitState.COMMITTED, detail="scrolled")

        if not target_backend_id:
            return ActResult(False, CommitState.NOT_COMMITTED,
                             WebErrorCode.INVALID_PROPOSAL, detail="targeted action without backend_id")

        if operation in WebOperation.TARGETED or operation == WebOperation.CLICK:
            return self._act_on_element(operation, target_backend_id, text, option_id)

        return ActResult(False, CommitState.NOT_COMMITTED,
                         WebErrorCode.INVALID_PROPOSAL, detail=f"unsupported operation: {operation}")

    def _act_on_element(self, operation: str, backend_id: str, text: str, option_id: str) -> ActResult:
        bid_json = json.dumps(str(backend_id))
        if operation == WebOperation.CLICK:
            js = (
                "(() => {const el=document.querySelector(`[data-muliao-bid=${"
                + bid_json + "}]`); if(!el) return {ok:false,reason:'stale'};"
                "el.scrollIntoView({block:'center'}); el.click(); return {ok:true};})()"
            )
            res = self._eval(js) or {}
            if not res.get("ok"):
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE,
                                 detail="element not found (stale)")
            time.sleep(0.4)
            return ActResult(True, CommitState.COMMITTED, detail="clicked")

        if operation == WebOperation.TYPE_TEXT:
            txt_json = json.dumps(str(text))
            js = (
                "(() => {const el=document.querySelector(`[data-muliao-bid=${"
                + bid_json + "}]`); if(!el) return {ok:false,reason:'stale'};"
                "el.focus(); "
                "if('value' in el){ el.value=" + txt_json + ";"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true})); }"
                "else { el.textContent=" + txt_json + ";"
                "el.dispatchEvent(new Event('input',{bubbles:true})); }"
                "return {ok:true};})()"
            )
            res = self._eval(js) or {}
            if not res.get("ok"):
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE,
                                 detail="element not found (stale)")
            return ActResult(True, CommitState.COMMITTED, detail="typed")

        if operation == WebOperation.SELECT:
            opt_json = json.dumps(str(option_id or text))
            js = (
                "(() => {const el=document.querySelector(`[data-muliao-bid=${"
                + bid_json + "}]`); if(!el) return {ok:false,reason:'stale'};"
                "const want=" + opt_json + ";"
                "let matched=false;"
                "for(const o of Array.from(el.options||[])){"
                "if(o.value===want||o.text===want){el.value=o.value;matched=true;break;}}"
                "if(!matched && want){el.value=want;}"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return {ok:true,matched:matched};})()"
            )
            res = self._eval(js) or {}
            if not res.get("ok"):
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE,
                                 detail="element not found (stale)")
            return ActResult(True, CommitState.COMMITTED,
                             detail="selected" if res.get("matched") else "select-value-set")

        return ActResult(False, CommitState.NOT_COMMITTED,
                         WebErrorCode.INVALID_PROPOSAL, detail=f"unsupported element op: {operation}")


__all__ = ["ChromeDomBackend", "ChromeLaunchError"]
