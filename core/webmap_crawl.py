#!/usr/bin/env python3
# webmap 机械爬虫 —— 单文件(经 `browser-harness < core/webmap_crawl.py` 由 stdin 执行)。
# 设计依据: webmap_DESIGN.md §1/§3/§4/§5/§6/§10。本文件承载 Phase 0.5–3,产出 webmap.json。
#
# 结构纪律(为离线可测):
#   - 模块顶层只放 *纯函数* + JS 常量,import 安全(不触碰 browser helper)。
#   - browser helper(new_tab/js/http_get/cdp/click_at_xy/wait_*)只在 main() 内引用,
#     由 browser-harness 注入为全局;离线 import 本文件不会执行它们。
#   - 纯函数有 test_extractors.py 离线单测;browser 编排靠文档化 helper 面写。
from __future__ import annotations
import json
import os
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl

# ============================================================================
# §A 配置 / flags(由触发层经环境变量传入,见 webmap_playbook.md §1)
# ============================================================================

def load_config() -> dict:
    flags = os.environ.get("WEBMAP_FLAGS", "")
    def flag(name, default):
        m = re.search(rf"--{name}\s+(\S+)", flags)
        return m.group(1) if m else default
    return {
        "url":        os.environ.get("WEBMAP_URL", "").strip(),
        "max":        int(flag("max", "0")) or None,         # 0/None = 无限
        "budget":     int(flag("budget", "0")) or None,
        "arch":       flag("arch", "auto"),                  # auto|spa|mpa
        "depth":      flag("depth", "active"),               # active|passive
        "chunks":     flag("chunks", "all"),                 # all|loaded
        "docs":       flag("docs", "on") == "on",
        "bodies":     flag("bodies", "on") == "on",
        "sourcemaps": flag("sourcemaps", "on") == "on",
        "cross_iframe": flag("cross-iframe", "off") == "on",  # §3.2 跨域 iframe 子应用入范围
        "out":        flag("out", "./webmap-out"),
    }

# ============================================================================
# §B path_template 归一(webmap_DESIGN.md §6 末)  —— 纯函数, 可测
# ============================================================================

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX16 = re.compile(r"^[0-9a-fA-F]{16,}$")
_NUM = re.compile(r"^\d+$")

def normalize_segment(seg: str) -> str:
    if _NUM.match(seg):
        return "{id}"
    if _UUID.match(seg):
        return "{uuid}"
    if _HEX16.match(seg):
        return "{hash}"
    # 长 opaque(>=24 且非词组)→ {slug}
    if len(seg) >= 24 and re.search(r"[^a-zA-Z0-9_-]", seg) is None and re.search(r"\d", seg):
        return "{slug}"
    return seg

def normalize_path_template(path: str) -> tuple[str, list[str]]:
    """返回 (归一路径, query_param 名列表)。仅作用于 path 段, 不动已有 {x} 占位。"""
    if not path:
        return path, []
    parts = urlsplit(path)
    query_params = sorted({k for k, _ in parse_qsl(parts.query)})
    segs = parts.path.split("/")
    norm = "/".join(s if (s.startswith("{") and s.endswith("}")) else normalize_segment(s)
                    for s in segs)
    return norm or "/", query_params

# ============================================================================
# §C §3.1 resolve_all_chunks —— 运行时求值已在 JS 内做完, 这里只做 URL 组装/去重/base 解析
#   入参 loaded = COLLECT_JS_AND_MANIFEST 返回的结构(见 JS 常量 §G)。  纯函数, 可测
# ============================================================================

def _abs_url(base_origin: str, p: str, name: str) -> str:
    # p 可能是整段 CDN URL、绝对路径、或相对路径;name 是 .u(i) 求得的文件名。
    if name.startswith(("http://", "https://", "//")):
        return name if not name.startswith("//") else "https:" + name
    if p.startswith(("http://", "https://")):
        return urljoin(p if p.endswith("/") else p + "/", name.lstrip("/"))
    if p.startswith("//"):
        return urljoin("https:" + (p if p.endswith("/") else p + "/"), name.lstrip("/"))
    # p 是路径前缀(如 "/static/js/")或空 → 挂 origin
    prefix = p if p.startswith("/") else "/" + p if p else "/"
    if not prefix.endswith("/"):
        prefix += "/"
    return urljoin(base_origin.rstrip("/") + prefix, name.lstrip("/"))

def resolve_all_chunks(loaded: dict, base_origin: str) -> list[str]:
    urls: set[str] = set()
    # 1) webpack 多 runtime: 每个 runtime 自带 p 前缀 + 已 .u(i) 求得的 files
    for rt in loaded.get("webpack", []):
        p = rt.get("p", "") or ""
        for name in rt.get("files", []):
            if name:
                urls.add(_abs_url(base_origin, p, name))
    # 2) vite/esm manifest: 取每条目的 file(及 css 不计 JS)
    man = loaded.get("vite_manifest") or {}
    for entry in man.values():
        f = entry.get("file") if isinstance(entry, dict) else None
        if f and f.endswith(".js"):
            urls.add(_abs_url(base_origin, "", f))
    # 3) importmap: specifier → URL 直接是脚本
    for spec_url in (loaded.get("importmap") or {}).values():
        if isinstance(spec_url, str) and spec_url.endswith(".js"):
            urls.add(urljoin(base_origin, spec_url))
    # 4) 已加载 src(performance/script/modulepreload)
    for u in loaded.get("loaded_js", []):
        if u:
            urls.add(u)
    # 5) 兜底: 字面量 chunk 名(仅补全, 非主力)
    for name in loaded.get("literal_chunks", []):
        urls.add(_abs_url(base_origin, loaded.get("public_path", ""), name))
    return sorted(urls)

# ============================================================================
# §C2 §3.4 sourcemap 还原(纯解析核; 网络拉取在 main 用 http_get)  纯函数, 可测
# ============================================================================

import base64

_RE_SMURL = re.compile(r"//[#@]\s*sourceMappingURL=([^\s'\"]+)")

def find_sourcemap_url(js_text: str) -> str | None:
    """取文件末尾的 //# sourceMappingURL=...(含 data: 内联)。"""
    last = None
    for m in _RE_SMURL.finditer(js_text):
        last = m.group(1)
    return last

def parse_sourcemap(map_text_or_obj) -> str:
    """sourcesContent 拼接成原始源文本(供 §3.3 在可读源上重抽)。无 sourcesContent 返回空串。"""
    obj = map_text_or_obj
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            return ""
    if not isinstance(obj, dict):
        return ""
    contents = obj.get("sourcesContent") or []
    return "\n".join(c for c in contents if isinstance(c, str))

def decode_inline_sourcemap(url: str) -> str | None:
    """data:application/json;base64,... 或 ;charset=utf-8,<urlencoded> 内联 sourcemap → JSON 文本。"""
    if not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    if not payload:
        return None
    if ";base64" in header:
        try:
            return base64.b64decode(payload).decode("utf-8", "ignore")
        except Exception:
            return None
    from urllib.parse import unquote
    return unquote(payload)

# ============================================================================
# §D §3.3 API 字面量抽取(REST + GraphQL + WS/SSE + 动态片段 + worker)  纯函数, 可测
# ============================================================================

# §3.3 REST: path 组兼收**根相对路径** `/...` 与**绝对 URL** `https://api.x.com/...`(前后端分离、
# API 在独立域时常见; 旧版只认 /... 直接漏掉)。归属(本站/独立域/第三方噪声)由 classify_origin 在
# _api_records 里裁决, 见 §J。
_RE_URL = r"(?:https?://|/)[^\"'`]+"
_RE_REST = [
    re.compile(r"""(?:axios|http|service|request|\$http)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*["'`](""" + _RE_URL + r""")["'`]""", re.I),
    re.compile(r"""\$service\s*\(\s*["'`](get|post|put|delete|patch)["'`]\s*,\s*["'`](""" + _RE_URL + r""")["'`]""", re.I),
    re.compile(r"""\bfetch\s*\(\s*["'`](""" + _RE_URL + r""")["'`]""", re.I),
    re.compile(r"""\brequest\s*\(\s*\{[^}]*?url\s*:\s*["'`](""" + _RE_URL + r""")["'`][^}]*?(?:method\s*:\s*["'`](get|post|put|delete|patch)["'`])?""", re.I),
]
_RE_BASEREF = re.compile(r"""\b(baseURL|BASE_URL|VUE_APP_[A-Z_]+|API_BASE|REACT_APP_[A-Z_]+)\b""")
_RE_GQL_OP = re.compile(r"""(query|mutation|subscription)\s+(\w+)""")
_RE_GQL_TAG = re.compile(r"""gql\s*`([^`]*)`""", re.S)
_RE_WS = re.compile(r"""new\s+WebSocket\s*\(\s*["'`]?([^"'`)\s]+)|(wss?://[^"'`)\s]+)""")
_RE_SSE = re.compile(r"""(?:new\s+EventSource|SockJS)\s*\(\s*["'`]([^"'`]+)["'`]""")
_RE_EVENT = re.compile(r"""\.(?:on|addEventListener)\s*\(\s*["'`]([\w.:-]+)["'`]""")
# §4.5 events 噪声过滤: `.on(`/`.addEventListener(` 命中所有 DOM 事件与压缩标识符,
# 压缩 bundle 里会污染 WS/SSE 记录(如 a:ws:aJ)。下表为 DOM/传输生命周期事件名(含
# WS 的 open/close/message/error —— 传输层事件, 不携带帧类型, 同属应剔除的泛化噪声)。
_DOM_EVENTS = frozenset("""click dblclick mousedown mouseup mousemove mouseover mouseout
mouseenter mouseleave contextmenu wheel keydown keyup keypress input change submit reset invalid
focus focusin focusout blur scroll resize load unload beforeunload error abort dragstart dragend
dragover dragenter dragleave drag drop touchstart touchmove touchend touchcancel pointerdown
pointerup pointermove pointerover pointerout pointerenter pointerleave pointercancel
gotpointercapture lostpointercapture copy cut paste select selectstart hashchange popstate
transitionend transitionstart animationstart animationend animationiteration visibilitychange
play pause ended timeupdate volumechange canplay canplaythrough loadeddata loadedmetadata progress
ratechange seeked seeking stalled suspend waiting durationchange emptied online offline message
open close readystatechange domcontentloaded storage""".split())
_RE_TEMPLATE = re.compile(r"""`([^`]*\$\{[^`]*)`""")          # 含 ${} 的模板串
_RE_WORKER = re.compile(r"""new\s+(?:Shared)?Worker\s*\(\s*(?:new\s+URL\s*\(\s*)?["'`]([^"'`]+)|importScripts\s*\(\s*["'`]([^"'`]+)["'`]""")
# §3.3 B2: 真实应用常把端点存成"键: 路径"映射或 config 对象的 url 属性, 不锚定 axios/request
# 调用点(国产企业级 Vue 典型: api/*.js 导出 { queryBankInfo: '/api/web/queryBankInfo' })。
# 捕获 `key:"/path"`(键可带引号), 用 blocklist 排除路由/资源键, 排除静态资源扩展名。低置信。
_RE_PROP_PATH = re.compile(r"""["'`]?(\w+)["'`]?\s*:\s*["'`](/[A-Za-z][\w\-./{}$:%]*)["'`]""")
_PROP_PATH_SKIP = {"path", "redirect", "redirectto", "component", "to", "href", "src",
                   "icon", "image", "img", "logo", "activemenu", "name", "key", "label",
                   "title", "id", "ref", "class", "classname", "tag", "type"}
_ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|css|js|mjs|ico|woff2?|ttf|eot|map|json|html?)$", re.I)

def _looks_minified_event(tok: str) -> bool:
    # 压缩标识符(如 aJ, bX2): 短、无分隔符且含大写。真实事件名多小写或带 . : _ - 分隔。
    return not re.search(r"[.:_/-]", tok) and len(tok) <= 4 and any(c.isupper() for c in tok)

def _clean_events(events) -> list:
    # 剔除 DOM/传输生命周期事件与压缩噪声, 仅留疑似 WS/socket.io 帧/消息名(§4.5)。
    return [e for e in events
            if e and e.lower() not in _DOM_EVENTS and not _looks_minified_event(e)]

# §4.5 第三方噪声过滤: 动态观测把每个 requestWillBeSent 都收成 API, 会混入 analytics/埋点/错误上报/
# 广告/公共 CDN 等"非该系统"的 JSON 端点; 归一后还丢了 host, 伪装成本站路径并标 confidence:high,
# 削弱清单精度。下表 host 命中即从 API 清单剔除(计入 coverage.third_party_filtered)。注意只收**确定的**
# 遥测/分析/广告/CDN 名单, 不含 googleapis/firebase 等可能承载真实后端的域(宁可保留也不误删)。
_THIRD_PARTY_HOST_RE = re.compile(
    r"(?:^|\.)(?:"
    r"google-analytics|googletagmanager|googlesyndication|googleadservices|doubleclick|analytics\.google|"
    r"sentry|bugsnag|rollbar|"
    r"datadoghq|newrelic|nr-data|"
    r"segment|mixpanel|amplitude|heap\.io|"
    r"hotjar|fullstory|logrocket|clarity\.ms|mouseflow|"
    r"intercom|drift\.com|zendesk|"
    r"facebook|fbcdn|"
    r"jsdelivr|unpkg|cdnjs|"
    r"hm\.baidu|cnzz|umeng|growingio|talkingdata"
    r")(?:$|[.:])", re.I)

def _registrable(host: str) -> str:
    """粗略 eTLD+1(取末两段)做同站启发; 无公共后缀表, co.uk 等多段后缀不精确(已知近似)。"""
    h = (host or "").split(":")[0].lower()
    parts = [p for p in h.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else h

def classify_origin(url: str, base_host: str, sub_hosts=()) -> str:
    """API host 归属(§4.5)。返回 'same' | 'cross' | 'third-party':
       same        — 相对路径 / 同 host / 子系统 host / 同注册域(api.x.com↔www.x.com);
       third-party — 命中已知遥测/分析/广告/公共 CDN 名单(确定噪声, 应从清单剔除);
       cross       — 其他跨域 host(可能是独立后端 API 域, 保留但标记, 不可误删)。"""
    host = (urlsplit(url).netloc or "").split("@")[-1].split(":")[0].lower()
    if not host:                                       # 相对路径 = 本站
        return "same"
    base = (base_host or "").split(":")[0].lower()
    subs = {(s or "").split(":")[0].lower() for s in (sub_hosts or ())}
    if host == base or host in subs:
        return "same"
    if base and _registrable(host) == _registrable(base):   # 兄弟子域(api.x.com / www.x.com)
        return "same"
    if _THIRD_PARTY_HOST_RE.search(host):
        return "third-party"
    return "cross"

def extract_api_literals(js_text: str, source_file: str = "?") -> dict:
    rest, templates, ws, sse, events, gql, workers, base_refs = [], [], [], [], [], [], [], set()
    for rx in _RE_REST:
        for m in rx.finditer(js_text):
            g = m.groups()
            if rx is _RE_REST[2]:      # fetch('/x')
                rest.append({"method": None, "path": g[0]})
            elif rx is _RE_REST[3]:    # request({url,method})
                rest.append({"method": (g[1] or "").upper() or None, "path": g[0]})
            else:
                rest.append({"method": g[0].upper(), "path": g[1]})
    for m in _RE_BASEREF.finditer(js_text):
        base_refs.add(m.group(1))
    for m in _RE_GQL_TAG.finditer(js_text):
        for op in _RE_GQL_OP.finditer(m.group(1)):
            gql.append({"type": op.group(1), "name": op.group(2)})
    for m in _RE_WS.finditer(js_text):
        ws.append(m.group(1) or m.group(2))
    for m in _RE_SSE.finditer(js_text):
        sse.append(m.group(1))
    for m in _RE_EVENT.finditer(js_text):
        events.append(m.group(1))
    for m in _RE_TEMPLATE.finditer(js_text):
        frag = m.group(1)
        if "/" in frag:                 # 只留像 URL 的模板
            templates.append(frag)
    for m in _RE_WORKER.finditer(js_text):
        workers.append(m.group(1) or m.group(2))
    prop_paths = []                                          # §3.3 B2: 键→路径映射 / config url 属性
    for m in _RE_PROP_PATH.finditer(js_text):
        key, p = m.group(1).lower(), m.group(2)
        if key in _PROP_PATH_SKIP or _ASSET_EXT.search(p):
            continue
        prop_paths.append(p)
    return {
        "source_file": source_file,
        "rest": _dedup_dicts(rest),
        "prop_paths": sorted(set(prop_paths)),
        "templates": sorted(set(templates)),
        "ws": sorted({w for w in ws if w}),
        "sse": sorted(set(sse)),
        "events": sorted(set(_clean_events(events))),
        "graphql": _dedup_dicts(gql),
        "workers": sorted({w for w in workers if w}),
        "base_refs": sorted(base_refs),
    }

def _hashable(v):
    if isinstance(v, (list, tuple)):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    return v

def _dedup_dicts(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = tuple(sorted((k, _hashable(v)) for k, v in it.items()))
        if key not in seen:
            seen.add(key); out.append(it)
    return out

# ============================================================================
# §E §3.2 多框架路由抽取(编译态正则; Vue 运行时树由 JS 常量另取)  纯函数, 可测
# ============================================================================

# Vue3: createRouter({... routes:[ {path:'...', ...} ]})  → 抓 path:'...'
_RE_VUE3_PATH = re.compile(r"""path\s*:\s*["'`]([^"'`]+)["'`]""")
# React Router 编译态: jsxs(Route,{path:...}) / createElement(Route,{path}) / createBrowserRouter([{path}])
_RE_REACT_ROUTE = re.compile(r"""(?:Route\s*,\s*\{[^}]*?path\s*:\s*["'`]([^"'`]+)["'`]|createBrowserRouter\s*\()""")
# Angular: RouterModule.forRoot/forChild([...{path:'x', loadChildren:...}])
_RE_NG_PATH = re.compile(r"""\{[^{}]*?path\s*:\s*["'`]([^"'`]*)["'`][^{}]*?\}""")
_RE_NG_LOADCHILDREN = re.compile(r"""loadChildren\s*:\s*[^,}]+""")

def extract_routes(js_text: str, framework_hint: str | None = None) -> list[dict]:
    """从编译后 JS 抓声明路由。返回 [{path, source}], 去重。框架未知则全跑、合并。"""
    paths: set[str] = set()
    hint = (framework_hint or "").lower()
    def add(p):
        if p and (p.startswith("/") or p in ("", "*") or not p.startswith("http")):
            paths.add(p)
    if hint in ("", "react") or "Route" in js_text or "createBrowserRouter" in js_text:
        for m in _RE_REACT_ROUTE.finditer(js_text):
            if m.group(1):
                add(m.group(1))
    if hint in ("", "vue3", "vue") or "createRouter" in js_text:
        for m in _RE_VUE3_PATH.finditer(js_text):
            add(m.group(1))
    if hint in ("", "angular") or "RouterModule" in js_text:
        for m in _RE_NG_PATH.finditer(js_text):
            add(m.group(1))
    out = []
    has_lazy = bool(_RE_NG_LOADCHILDREN.search(js_text))
    for p in sorted(paths):
        out.append({"path": p if p.startswith("/") else "/" + p, "source": "static"})
    return out, has_lazy

# ============================================================================
# §F §3.6 MPA 端点抽取  +  §3.5 ESM specifier 解析  纯函数, 可测
# ============================================================================

def forms_to_endpoints(forms: list[dict], page_url: str) -> list[dict]:
    """每个 <form action+method+inputs> 本身即一个端点(webmap_DESIGN.md §3.6)。"""
    out = []
    for f in forms or []:
        action = f.get("action") or page_url
        method = (f.get("method") or "GET").upper()
        path = urlsplit(urljoin(page_url, action)).path or "/"
        tpl, qp = normalize_path_template(path)
        out.append({
            "method": method, "path_template": tpl,
            "body_params": [i.get("name") for i in f.get("inputs", []) if i.get("name")],
            "query_params": qp, "transport": "rest", "source": "form", "confidence": "high",
        })
    return out

_RE_DOPOSTBACK = re.compile(r"""__doPostBack\s*\(\s*["']([^"']+)["']""")
_RE_ASMX = re.compile(r"""["'`]([^"'`]+\.(?:asmx|ashx|axd)(?:/[^"'`]*)?)["'`]""", re.I)

def postback_targets(dom_text_blob: str, page_url: str) -> list[dict]:
    """ASP.NET __doPostBack 目标 + .asmx/.ashx/.axd 引用(webmap_DESIGN.md §3.6)。"""
    out = []
    for m in _RE_DOPOSTBACK.finditer(dom_text_blob):
        out.append({"method": "POST", "path_template": urlsplit(page_url).path or "/",
                    "transport": "rest", "source": "postback",
                    "note": f"__doPostBack target={m.group(1)}", "confidence": "low"})
    for m in _RE_ASMX.finditer(dom_text_blob):
        tpl, qp = normalize_path_template(urlsplit(urljoin(page_url, m.group(1))).path)
        out.append({"method": None, "path_template": tpl, "query_params": qp,
                    "transport": "rest", "source": "handler", "confidence": "medium"})
    return _dedup_dicts(out)

_RE_ESM_FROM = re.compile(r"""(?:\bimport\b|\bexport\b)[^;'"()]*?\bfrom\s*["']([^"']+)["']""")
_RE_ESM_BARE = re.compile(r"""(?:^|[;\s])import\s*["']([^"']+)["']""")          # import "side-effect"
_RE_ESM_DYNAMIC = re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)""")      # import('lit')

def extract_import_specifiers(js_text: str) -> list[str]:
    """静态 import/export-from + bare import + 字面 dynamic import() 的说明符
    (webmap_DESIGN.md §3.5 ESM 图 BFS 的输入)。返回去重排序的说明符列表。"""
    specs: set[str] = set()
    for rx in (_RE_ESM_FROM, _RE_ESM_BARE, _RE_ESM_DYNAMIC):
        for m in rx.finditer(js_text):
            if m.group(1):
                specs.add(m.group(1))
    return sorted(specs)

def same_origin_links(links: list[str], base_origin: str, page_url: str) -> list[str]:
    """<a href> → 同源路由路径模板候选(webmap_DESIGN.md §3.6 MPA 路由 = 站内链接 ∪ 菜单 ∪ sitemap)。
    丢弃 hash/伪协议/跨域/资源链接;按页面 URL 解析相对路径, 归一为 path_template, 去重排序。"""
    out: set[str] = set()
    host = urlsplit(base_origin).netloc
    for href in links or []:
        if not href:
            continue
        h = href.strip()
        if h.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:")):
            continue
        u = urljoin(page_url, h)
        sp = urlsplit(u)
        if sp.scheme and sp.scheme not in ("http", "https"):
            continue
        if sp.netloc and sp.netloc != host:
            continue
        if _ASSET_EXT.search(sp.path or ""):                  # 静态资源(.png/.css/.js…)不是路由
            continue
        tpl, _ = normalize_path_template(sp.path or "/")
        out.add(tpl)
    return sorted(out)

def concrete_same_origin_paths(links: list[str], base_origin: str, page_url: str) -> set[str]:
    """同 same_origin_links, 但**保留真实 id**(不归一), 供 §4.3 参数路由取实例。
    既收 pathname(history 路由), 也收 hash 片段(#/user/42 的 hash 路由)。"""
    out: set[str] = set()
    host = urlsplit(base_origin).netloc
    for href in links or []:
        if not href:
            continue
        h = href.strip()
        if h.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
            continue
        if h.startswith("#"):                          # 纯 hash 锚点 / hash 路由
            frag = h[1:]
            if frag.startswith("/"):
                out.add(frag)
            continue
        u = urljoin(page_url, h)
        sp = urlsplit(u)
        if sp.scheme and sp.scheme not in ("http", "https"):
            continue
        if sp.netloc and sp.netloc != host:
            continue
        if sp.fragment.startswith("/"):                # hash 路由(history.origin + #/path)
            out.add(sp.fragment)
        if sp.path and not _ASSET_EXT.search(sp.path):
            out.add(sp.path)
    return out

# §4.3 参数路由: {id}/{uuid}/{hash}/{slug}/{var} 占位段须取真实实例才能进详情页
_PARAM_SEG_RE = re.compile(r"\{(?:id|uuid|hash|slug|var)\}")
# §4.2 守卫拦截落地页(被拦 → blocked, 区别于普通 redirected)
_GUARD_BLOCK_RE = re.compile(
    r"/(?:40[13]|forbidden|denied|access-?denied|unauthor|no-?auth|noaccess|error/40)", re.I)

def is_param_template(tpl: str) -> bool:
    return bool(_PARAM_SEG_RE.search(tpl or ""))

def resolve_param_route(template: str, concrete_paths) -> str | None:
    """从本会话观测到的具体路径里取一个能归一回该模板的真实实例(§4.3)。
    取不到 → None(调用方标 param-unresolved)。"""
    tnorm = normalize_path_template(template)[0]
    if not is_param_template(tnorm):
        return None
    for p in sorted(concrete_paths):
        if p and p != template and normalize_path_template(p)[0] == tnorm:
            return p
    return None

def classify_landing(landed: str, intended: str) -> str:
    """落地 vs 意图 → visited|blocked|redirected(§4.2)。auth/param 由调用方先判。"""
    if _same_route(landed, intended):
        return "visited"
    if landed and _GUARD_BLOCK_RE.search(str(landed)):
        return "blocked"
    return "redirected"

# §3.2 module federation: remoteEntry.js 暴露的 exposed-module map 键(约定 "./X")
_RE_EXPOSED = re.compile(r"""["'](\./[\w][\w/.\-]*)["']\s*:""")

def parse_remote_entry(js_text: str) -> list[str]:
    """从 remoteEntry.js 抽 exposed-module map(还原远程暴露的子模块, §3.2)。去重排序。"""
    return sorted({m.group(1) for m in _RE_EXPOSED.finditer(js_text or "")})

def ws_event_from_payload(payload: str) -> str | None:
    """从 WS 帧 payload(JSON)抽 event/type 名(§4.5)。非 JSON / 无类型键 → None。"""
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    if isinstance(obj, dict):
        for k in ("event", "type", "action", "cmd", "method", "topic", "channel"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    if isinstance(obj, list) and obj and isinstance(obj[0], str):    # socket.io ["event",data]
        return obj[0]
    return None

def resolve_specifier(spec: str, base_url: str, importmap: dict | None = None) -> str | None:
    """ESM import 说明符 → 绝对 URL(webmap_DESIGN.md §3.5 BFS 的纯解析核)。"""
    importmap = importmap or {}
    if spec in importmap:                       # 精确 importmap 命中
        return urljoin(base_url, importmap[spec])
    if spec.startswith(("./", "../", "/")):     # 相对/绝对路径
        u = urljoin(base_url, spec)
        if not urlsplit(u).path.endswith((".js", ".mjs", ".ts")):
            pass                                # 可能省略扩展名; 由调用方探测
        return u
    if spec.startswith(("http://", "https://")):
        return spec
    # 裸说明符: importmap 前缀匹配(trailing-slash 映射)
    for k, v in importmap.items():
        if k.endswith("/") and spec.startswith(k):
            return urljoin(base_url, v + spec[len(k):])
    return None                                 # 裸包名无 importmap → 无法解析(如实漏)

# ============================================================================
# §G §3.7 声明式资源解析(robots/sitemap/openapi/introspection)  纯函数, 可测
#   网络拉取在 main() 用 http_get; 这里只解析文本。
# ============================================================================

def parse_robots(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:Disallow|Allow)\s*:\s*(\S+)", line, re.I)
        if m and m.group(1) not in ("/", "*"):
            out.append(m.group(1))
    return sorted(set(out))

def parse_sitemap(xml: str) -> list[str]:
    return sorted(set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, re.I)))

def parse_openapi(spec: dict) -> list[dict]:
    """OpenAPI/Swagger → endpoints(provenance=spec-declared, confidence=high)。"""
    out = []
    base = ""
    if isinstance(spec.get("servers"), list) and spec["servers"]:
        base = (spec["servers"][0] or {}).get("url", "") if isinstance(spec["servers"][0], dict) else ""
    elif spec.get("basePath"):
        base = spec["basePath"]
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        full = (base.rstrip("/") + path) if base else path
        tpl, _ = normalize_path_template(full)
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "head", "options"):
                continue
            params = [p.get("name") for p in (op.get("parameters") or []) if isinstance(p, dict) and p.get("name")] if isinstance(op, dict) else []
            out.append({"method": method.upper(), "path_template": tpl,
                        "query_params": sorted(set(params)), "transport": "rest",
                        "provenance": "spec-declared", "confidence": "high",
                        "source": "openapi"})
    return _dedup_dicts(out)

def parse_introspection(result: dict) -> list[dict]:
    """GraphQL introspection → operations(webmap_DESIGN.md §3.7)。"""
    schema = (result.get("data") or {}).get("__schema") or result.get("__schema") or {}
    ops = []
    for kind, key in (("query", "queryType"), ("mutation", "mutationType"), ("subscription", "subscriptionType")):
        tname = (schema.get(key) or {}).get("name") if isinstance(schema.get(key), dict) else None
        if not tname:
            continue
        for t in schema.get("types") or []:
            if t.get("name") == tname:
                for fld in t.get("fields") or []:
                    ops.append({"type": kind, "name": fld.get("name")})
    return _dedup_dicts(ops)

# ============================================================================
# §H §5 CORRELATE: merge + coverage  纯函数, 可测
# ============================================================================

def _api_id(method, tpl, transport="rest"):
    if transport != "rest":
        return f"a:{transport}:{tpl}"
    return f"a:{method or 'ANY'} {tpl}"

def merge_apis(*groups: list[dict]) -> list[dict]:
    """按 (method, path_template) 去重合并。provenance 据"证据集合"判定(webmap_DESIGN.md §5):
    静态(declared-only / spec-declared)且动态(observed-only)都出现过 → confirmed;
    否则取该 id 唯一的证据态。每个贡献记录的 provenance 收进 `_provs` 集, 末尾统一裁决。"""
    by_id: dict[str, dict] = {}
    for group in groups:
        for a in group or []:
            method = a.get("method")
            tpl = a.get("path_template") or a.get("path") or ""
            if not tpl:
                continue
            if "path" in a and "path_template" not in a:
                tpl, qp = normalize_path_template(tpl)
                a = {**a, "path_template": tpl, "query_params": sorted(set((a.get("query_params") or []) + qp))}
            transport = a.get("transport", "rest")
            aid = _api_id(method, tpl, transport)
            op = a.get("operation")
            if transport == "graphql" and op:        # 同端点不同 operation 不可合并
                aid += "#" + str(op.get("type")) + ":" + str(op.get("name"))
            prov = a.get("provenance", "declared-only")
            if aid not in by_id:
                by_id[aid] = {**a, "id": aid, "path_template": tpl, "provenance": prov,
                              "_provs": set(), "source_files": set(), "called_by_routes": set()}
            cur = by_id[aid]
            cur["_provs"].add(prov)
            for f in [a.get("source_file"), a.get("source")]:
                if f:
                    cur["source_files"].add(f)
            for r in a.get("called_by_routes", []):
                cur["called_by_routes"].add(r)
            cur["query_params"] = sorted(set(cur.get("query_params") or []) | set(a.get("query_params") or []))
            cur["body_params"] = sorted(set(cur.get("body_params") or []) | set(a.get("body_params") or []))
            # 动态观测侧字段(webmap_DESIGN.md §6): 状态/mime/响应字段/base_ref/事件 求并
            cur["response_fields"] = sorted(set(cur.get("response_fields") or []) | set(a.get("response_fields") or []))
            cur["statuses"] = sorted(set(cur.get("statuses") or []) | set(a.get("statuses") or []))
            cur["events"] = sorted(set(cur.get("events") or []) | set(a.get("events") or []))
            if a.get("mime") and not cur.get("mime"):
                cur["mime"] = a["mime"]
            if a.get("base_ref") and not cur.get("base_ref"):
                cur["base_ref"] = a["base_ref"]
    out = []
    for a in by_id.values():
        provs = a.pop("_provs")                       # §5 provenance 裁决: 静态∩动态 → confirmed
        has_obs = "observed-only" in provs
        if "confirmed" in provs or (has_obs and ("declared-only" in provs or "spec-declared" in provs)):
            a["provenance"] = "confirmed"
        elif "spec-declared" in provs:
            a["provenance"] = "spec-declared"
        elif "declared-only" in provs:
            a["provenance"] = "declared-only"
        elif has_obs:
            a["provenance"] = "observed-only"
        a["source_files"] = sorted(a["source_files"])
        if a["source_files"]:                       # schema §6 字段名 source_file(代表值)
            a.setdefault("source_file", a["source_files"][0])
        a["called_by_routes"] = sorted(a["called_by_routes"])
        for k in ("response_fields", "statuses", "events"):  # 空集不落盘
            if not a.get(k):
                a.pop(k, None)
        out.append(a)
    return sorted(out, key=lambda x: x["id"])

def _enrich_route(node: dict, r: dict) -> None:
    """把 ROUTER_DETECT_JS / 编译态抽到的路由元数据并进节点(webmap_DESIGN.md §6 routes)。"""
    for k in ("name", "title", "component", "chunk"):
        if r.get(k) and not node.get(k):
            node[k] = r[k]
    if r.get("meta") and not node.get("meta"):
        node["meta"] = r["meta"]
        roles = (r["meta"] or {}).get("roles")
        if roles and not node.get("access"):
            node["access"] = ",".join(roles) if isinstance(roles, list) else str(roles)
    if r.get("feature_flags") and not node.get("feature_flags"):
        node["feature_flags"] = r["feature_flags"]


def merge_routes(static_routes: list[dict], visited: dict[str, dict]) -> list[dict]:
    """声明路由 ∪ 动态结果; provenance/visit_result 合并, 保留 name/component/meta/access。"""
    by_path: dict[str, dict] = {}
    for r in static_routes or []:
        tpl, _ = normalize_path_template(r.get("path", ""))
        node = by_path.setdefault(tpl, {"path": tpl, "provenance": r.get("provenance", "declared-only"),
                                        "visit_result": None, "source": r.get("source", "static")})
        _enrich_route(node, r)
    for path, info in (visited or {}).items():
        tpl, _ = normalize_path_template(path)
        node = by_path.setdefault(tpl, {"path": tpl, "provenance": "observed-only",
                                        "visit_result": None, "source": "dynamic"})
        node["visit_result"] = info.get("visit_result")
        if info.get("visit_result") == "visited":
            node["provenance"] = "confirmed" if node["provenance"] in ("declared-only", "observed-only") else node["provenance"]
    for r in by_path.values():
        r.setdefault("id", "r:" + r["path"])
    return sorted(by_path.values(), key=lambda x: x["path"])

def coverage_summary(routes: list[dict], apis: list[dict],
                     chunks_failed: int = 0, docs_failed: int = 0,
                     third_party_filtered: int = 0) -> dict:
    def count(items, key, val):
        return sum(1 for i in items if i.get(key) == val)
    declared_only = [a["id"] for a in apis if a.get("provenance") in ("declared-only", "spec-declared")]
    declared_only += [r["id"] for r in routes if r.get("visit_result") in (None, "blocked", "redirected", "auth-redirect", "param-unresolved")]
    return {
        "routes_declared": len(routes),
        "visited": count(routes, "visit_result", "visited"),
        "redirected": count(routes, "visit_result", "redirected"),
        "blocked": count(routes, "visit_result", "blocked"),
        "param_unresolved": count(routes, "visit_result", "param-unresolved"),
        "apis_total": len(apis),
        "confirmed": count(apis, "provenance", "confirmed"),
        "declared_only": count(apis, "provenance", "declared-only"),
        "observed_only": count(apis, "provenance", "observed-only"),
        "spec_declared": count(apis, "provenance", "spec-declared"),
        "chunks_fetch_failed": chunks_failed,
        "doc_fetch_failed": docs_failed,
        "third_party_filtered": third_party_filtered,   # §4.5 剔除的已知第三方遥测/分析端点数
        "declared_only_list": sorted(set(declared_only)),
    }

def extract_response_fields(sample) -> list[str]:
    """从响应体样本(JSON 文本/对象)抽字段名; data/result 等容器下钻一层(webmap_DESIGN.md §5)。"""
    if not sample:
        return []
    if isinstance(sample, str):
        try:
            sample = json.loads(sample)
        except Exception:
            return []
    fields: set[str] = set()
    def harvest(obj):
        if isinstance(obj, dict):
            fields.update(str(k) for k in obj.keys())
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            fields.update(str(k) for k in obj[0].keys())
    harvest(sample)
    if isinstance(sample, dict):
        for container in ("data", "result", "rows", "list", "items", "records", "content"):
            if container in sample:
                harvest(sample[container])
    return sorted(fields)

def graphql_view(apis: list[dict]) -> list[dict]:
    """从合并后的 apis 投影顶层 graphql[](endpoint+operations, webmap_DESIGN.md §6)。"""
    by_ep: dict[str, dict] = {}
    for a in apis:
        if a.get("transport") != "graphql":
            continue
        ep = a.get("path_template", "/graphql")
        node = by_ep.setdefault(ep, {"endpoint": ep, "operations": []})
        op = a.get("operation")
        if op and op not in node["operations"]:
            node["operations"].append(op)
    return sorted(by_ep.values(), key=lambda x: x["endpoint"])

def realtime_view(apis: list[dict]) -> list[dict]:
    """从合并后的 apis 投影顶层 realtime[](ws/sse + events, webmap_DESIGN.md §6)。"""
    out, seen = [], set()
    for a in apis:
        if a.get("transport") not in ("ws", "sse"):
            continue
        url = a.get("path_template")
        key = (a["transport"], url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"transport": a["transport"], "url": url, "events": a.get("events", [])})
    return sorted(out, key=lambda x: (x["transport"], x["url"] or ""))

_HTTP_CLIENT_SIGNS = [
    ("axios", re.compile(r"""\b(?:from\s+["']axios["']|require\(\s*["']axios["']|axios\.create\s*\()""")),
    ("umi-request", re.compile(r"""["']umi-request["']""")),
    ("superagent", re.compile(r"""["']superagent["']""")),
    ("ky", re.compile(r"""\b(?:from\s+["']ky["']|require\(\s*["']ky["'])""")),
    ("got", re.compile(r"""\b(?:from\s+["']got["']|require\(\s*["']got["'])""")),
    ("jquery", re.compile(r"""\$\.ajax\s*\(|jQuery\.ajax\s*\(""")),
]

def detect_http_client(js_text: str) -> str | None:
    """从 chunk 源码识别 HTTP 客户端库(webmap_DESIGN.md §6 http_client)。优先级按列表序。"""
    for name, rx in _HTTP_CLIENT_SIGNS:
        if rx.search(js_text):
            return name
    if re.search(r"\bfetch\s*\(\s*[\"'`]/", js_text):
        return "fetch"
    return None

def derive_subsystems(remotes: list[dict]) -> list[dict]:
    """COLLECT 的 federation remotes → subsystems[](name/origin/entry, webmap_DESIGN.md §3.2)。去重。"""
    out, seen = [], set()
    for r in remotes or []:
        entry = r.get("entry")
        name = r.get("name")
        origin = None
        if entry:
            sp = urlsplit(entry)
            if sp.scheme and sp.netloc:
                origin = "{0.scheme}://{0.netloc}".format(sp)
            if not name:
                name = (sp.path.rsplit("/", 2)[-2] if "/" in sp.path.strip("/") else sp.netloc) or sp.netloc
        key = (name, entry, origin)
        if key in seen or not (name or entry):
            continue
        seen.add(key)
        node = {"kind": "federation-remote"}
        if name: node["name"] = name
        if origin: node["origin"] = origin
        if entry: node["entry"] = entry
        out.append(node)
    return out

_MPA_FRAMEWORK = {"aspx": "aspnet", "asmx": "aspnet", "ashx": "aspnet",
                  "jsp": "jsp", "jspx": "jsp", "do": "struts", "action": "struts", "php": "php"}

def derive_framework(blueprint: dict, arch_probe: dict, arch: str) -> str | None:
    """机械框架指纹(webmap_DESIGN.md §6)。SPA: vue2/3·react·angular;MPA: 据链接后缀。"""
    v = blueprint.get("vue")
    if v == 2:
        return "vue2"
    if v == 3:
        return "vue3"
    spa = arch_probe.get("spa", {})
    if spa.get("react"):
        return "react"
    if spa.get("angular"):
        return "angular"
    if spa.get("vue"):
        return "vue"
    if arch in ("mpa", "hybrid"):
        for ext in arch_probe.get("mpa", {}).get("exts", []):
            if ext in _MPA_FRAMEWORK:
                return _MPA_FRAMEWORK[ext]
    return None

def derive_version(framework: str | None, arch_probe: dict) -> str | None:
    """从运行时指纹取版本(webmap_DESIGN.md §6 version)。取不到→None。"""
    fp = arch_probe.get("fp", {})
    if framework in ("vue2", "vue3", "vue"):
        return fp.get("vue_version")
    if framework == "react":
        return fp.get("react_version")
    if framework == "angular":
        return fp.get("ng_version")
    return None

# ============================================================================
# §I JS 常量(browser 内求值; 离线不可测, 按文档化 helper 面写)
# ============================================================================

PAGE_PROBE_JS = r"""
(() => ({ url: document.URL, title: document.title,
  bodyLen: (document.body && document.body.innerText || '').length,
  hasPwd: !!document.querySelector('input[type=password]'),
  forms: document.querySelectorAll('form').length }))()
"""

ARCH_PROBE_JS = r"""
(() => {
  const w = window;
  const appEl = document.getElementById('app') || document.querySelector('#app');
  const ngEl = document.querySelector('[ng-version]');
  const spa = {
    webpack: Object.keys(w).some(k => /^webpackChunk/.test(k)) || !!w.webpackJsonp,
    vue: !!(w.__VUE__ || (appEl||{}).__vue__ || (appEl||{}).__vue_app__),
    react: !!(document.querySelector('[data-reactroot],#root')),
    angular: !!(w.ng || ngEl),
    importmap: !!document.querySelector('script[type=importmap]'),
    chunks: [...document.scripts].filter(s => /\.chunk\.js|[-.][0-9a-f]{6,}\.js/.test(s.src||'')).length,
  };
  // §6 version / http_client 机械指纹(运行时暴露时取, 否则 null → 由 chunk 扫描兜底)
  let vueVer = null;
  try { vueVer = (w.Vue && w.Vue.version) ||
    (appEl && appEl.__vue__ && appEl.__vue__.$options && appEl.__vue__.$options._base && appEl.__vue__.$options._base.version) || null; } catch(e) {}
  const fp = {
    vue_version: vueVer,
    react_version: (w.React && w.React.version) || null,
    ng_version: (ngEl && ngEl.getAttribute('ng-version')) || null,
    http_client: w.axios ? 'axios' : (w.$ && w.$.ajax ? 'jquery' : null),
  };
  // mpa 服务端框架后缀(供 framework 推断)
  const exts = new Set();
  [...document.querySelectorAll('a[href]')].forEach(a => {
    const m = /\.(aspx|asmx|ashx|jsp|jspx|php|do|action)(\?|#|$)/i.exec(a.getAttribute('href')||'');
    if (m) exts.add(m[1].toLowerCase());
  });
  // §arch jQuery/MiniUI iframe-portal 信号(国产企业级门户: 干净 REST URL、无 .aspx/.jsp、无 __VIEWSTATE,
  // 旧逻辑会把这类 0 信号站点误判 spa; 用 portal 信号纠偏到 mpa/hybrid)
  const iframePortal = [...document.querySelectorAll('iframe[src]')].filter(f => {
    try { return new URL(f.getAttribute('src'), location.href).origin === location.origin; } catch (e) { return false; }
  }).length > 0;
  const mpa = {
    forms: [...document.querySelectorAll('form[method]')].filter(f => /post/i.test(f.method)).length,
    viewstate: !!document.querySelector('#__VIEWSTATE') || /__doPostBack/.test(document.documentElement.innerHTML),
    serverExt: exts.size > 0,
    exts: [...exts],
    jquery: !!(w.jQuery || (w.$ && w.$.fn && w.$.fn.jquery)),
    miniui: !!w.mini || [...document.scripts].some(s => /mini(-all|-min)?\.js|miniui/i.test(s.src||'')) || !!document.querySelector('[mini-options]'),
    iframePortal: iframePortal,
  };
  return { spa, mpa, fp };
})()
"""

COLLECT_JS_AND_MANIFEST = r"""
(() => {
  const out = { webpack: [], vite_manifest: null, importmap: null,
                loaded_js: [], literal_chunks: [], public_path: '', remotes: [] };
  // 已加载 JS
  const perf = performance.getEntriesByType('resource')
      .filter(r => /\.js(\?|$)/.test(r.name)).map(r => r.name);
  const srcs = [...document.querySelectorAll('script[src],link[rel=modulepreload],link[rel=prefetch],link[rel=preload][as=script]')]
      .map(s => s.src || s.href).filter(Boolean);
  out.loaded_js = [...new Set([...perf, ...srcs])];
  // webpack: 枚举所有 runtime —— 往每个 webpackChunk* 数组 push 一个探针 chunk,
  // 其第三元(runtime 回调)被 webpack runtime 同步以该 runtime 的 __webpack_require__ 调用;
  // 据此用 chunk-map 对象键(.u 源码里的哈希表)得 id 全集, 调 .u(i) 求真实懒 chunk 文件名,
  // 前缀用该 runtime 的 .p(可能是整段 CDN URL)。多 runtime 各算各的(webmap_DESIGN.md §3.1)。
  const reqs = [];
  try {
    const seen = new Set();
    const idsFromMaps = (req) => {                // 从 .u/.miniCssF 源码 union 所有对象字面量键
      const ids = new Set();
      for (const fn of [req.u, req.miniCssF]) {
        if (typeof fn !== 'function') continue;
        const objs = (fn + '').match(/\{[^{}]*\}/g) || [];
        for (const o of objs) {
          const keys = o.match(/(?:[,{]\s*)(\d+|"[^"]*"|'[^']*')\s*:/g) || [];
          for (const km of keys) ids.add(km.replace(/^[,{\s]+/, '').replace(/\s*:$/, '').replace(/^['"]|['"]$/g, ''));
        }
      }
      return [...ids];
    };
    const collect = (req) => {
      if (!req || seen.has(req) || typeof req.u !== 'function') return;
      seen.add(req); reqs.push(req);
      const files = [];
      for (const i of idsFromMaps(req)) { try { const f = req.u(i); if (f) files.push(f); } catch (e) {} }
      out.webpack.push({ p: (typeof req.p === 'string' ? req.p : ''), files: [...new Set(files)] });
    };
    for (const k of Object.keys(window)) {
      if (!/^webpackChunk/.test(k)) continue;
      const arr = window[k];
      if (!Array.isArray(arr) || typeof arr.push !== 'function') continue;
      try {
        arr.push([['__webmap_probe_' + Math.random().toString(36).slice(2)], {},
                  (req) => { try { collect(req); } catch (e) {} }]);
      } catch (e) {}
    }
    // 兜底: 老构建把 require 直接挂到 window(webpack4 / 部分模板)
    if (typeof window.__webpack_require__ === 'function') collect(window.__webpack_require__);
  } catch (e) { out.webpack_err = String(e); }
  // vite manifest / importmap
  const im = document.querySelector('script[type=importmap]');
  if (im) { try { out.importmap = (JSON.parse(im.textContent).imports) || null; } catch (e) {} }
  out.public_path = (window.__webpack_public_path__ || '');
  // §3.2 module federation: 尽力读容器 runtime map + remoteEntry 脚本
  try {
    const names = new Set();
    // 钩出的每个 runtime 各查 federation.remotes(window.__webpack_require__ 在 webpack5 通常未暴露)
    for (const req of [...reqs, window.__webpack_require__]) {
      if (req && req.federation && req.federation.remotes) {
        for (const k of Object.keys(req.federation.remotes)) names.add(k);
      }
    }
    // 已加载 remoteEntry 脚本 = 远程容器入口
    const entries = out.loaded_js.filter(u => /remoteEntry(\.js)?(\?|$)|remote-entry/i.test(u));
    for (const u of entries) out.remotes.push({ entry: u, name: null });
    for (const n of names) out.remotes.push({ name: n, entry: null });
  } catch (e) { out.federation_err = String(e); }
  return out;
})()
"""

ROUTER_DETECT_JS = r"""
(() => {
  const root = document.getElementById('app') || document.querySelector('#app,[data-reactroot],#root');
  const out = { vue: null, routes: [], mode: null, feature_flags: [] };
  const walk = (rs, parent) => rs && rs.forEach(r => {
    const path = (parent ? parent.replace(/\/$/, '') + '/' : '') + (r.path || '');
    out.routes.push({ path, name: r.name || null,
      meta: r.meta || null, redirect: r.redirect || null,
      component: (r.component && (r.component.name || (r.component.toString().match(/import\(["'`]([^"'`]+)/)||[])[1])) || null });
    if (r.children) walk(r.children, path);
  });
  const vm = root && root.__vue__;
  if (vm && vm.$router) {
    out.vue = 2;
    const r = vm.$router;
    out.mode = r.mode || (r.options && r.options.mode) || null;
    walk(r.options && r.options.routes, '');
  }
  if (root && root.__vue_app__) out.vue = 3;
  // §4.1: 非 Vue(React/Angular)运行时无干净 mode → 据当前 URL 是否 hash 路由探测
  if (!out.mode) out.mode = /^#\//.test(location.hash || '') ? 'hash' : 'history';
  // §4.4 feature-flag 线索: 从 Vuex $store.state / 全局 config 收布尔 flag 键(只读, 不强开)
  try {
    const stores = [];
    if (vm && vm.$store && vm.$store.state) stores.push(vm.$store.state);
    for (const k of ['__INITIAL_STATE__','__PRELOADED_STATE__','__NUXT__','CONFIG','__CONFIG__',
                     'featureFlags','FEATURE_FLAGS','flags','APP_CONFIG','env']) {
      if (window[k] && typeof window[k] === 'object') stores.push(window[k]);
    }
    const flags = new Set();
    const scan = (obj, prefix, depth) => {
      if (!obj || typeof obj !== 'object' || depth > 3) return;
      for (const k of Object.keys(obj)) {
        const v = obj[k];
        if (typeof v === 'boolean' && /(flag|enabl|feature|beta|toggle|show|visible|allow|can[A-Z])/i.test(k))
          flags.add((prefix ? prefix + '.' : '') + k);
        else if (v && typeof v === 'object' && /(flag|feature|config|toggle|setting|permission)/i.test(k))
          scan(v, (prefix ? prefix + '.' : '') + k, depth + 1);
      }
    };
    for (const s of stores) scan(s, '', 0);
    out.feature_flags = [...flags].slice(0, 50);
  } catch (e) {}
  return out;
})()
"""

# §3.2 同源 iframe 子应用枚举(默认同源入范围; 跨域需 --cross-iframe)
IFRAME_PROBE_JS = r"""
(() => [...document.querySelectorAll('iframe[src]')].map(f => {
  let sameOrigin = false;
  try { sameOrigin = !!(f.contentDocument || (f.contentWindow && f.contentWindow.document)); }
  catch (e) { sameOrigin = false; }
  return { src: f.src, sameOrigin };
}).filter(x => x.src))()
"""

# DOM 抽取 + 元素几何(供 click_at_xy); 白名单分类在 §J safe_widgets 用此输出。
EXTRACT_JS_WITH_XY = r"""
(() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return null;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return null;
    return { x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) };
  };
  let _id = 0;
  const tag = el => {
    const xy = vis(el); if (!xy) return null;
    return { id: 'e' + (_id++), tag: el.tagName.toLowerCase(),
             role: el.getAttribute('role') || '', cls: el.className && (''+el.className).slice(0,80),
             text: (el.innerText||el.value||'').trim().slice(0,40),
             type: el.getAttribute('type')||'', x: xy.x, y: xy.y };
  };
  const pick = sel => [...document.querySelectorAll(sel)].map(tag).filter(Boolean);
  const forms = [...document.querySelectorAll('form')].map(f => ({
    action: f.getAttribute('action')||'', method: f.getAttribute('method')||'GET',
    inputs: [...f.querySelectorAll('input[name],select[name],textarea[name]')].map(i => ({ name: i.name, type: i.type||'' })) }));
  return {
    links: [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href')).filter(Boolean),
    buttons: pick('button,[role=button],input[type=submit],input[type=button]'),
    widgets: pick('[role=tab],.tab,[aria-expanded],.accordion,.collapse-header,.menu-toggle,[data-toggle]'),
    forms: forms,
    blob: document.documentElement.innerHTML.slice(0, 200000),
  };
})()
"""

# §4.1: Vue → $router.push;否则按已知/探测的路由模式分支。
# 修审核 P0: 旧逻辑 `location.hash !== undefined` 恒真, 非 Vue 的 SPA(React BrowserRouter /
# Angular)被永远塞进 hash 分支, history 路由切不动 → 全标 redirected, 动态接口一个都触发不到。
# 现按 hint(blueprint.mode / 探测的真实模式)分支: 只有确为 hash 路由才设 location.hash,
# 否则走 history.pushState + popstate(React/Angular 主流)。
PUSH_ROUTE_JS = r"""(() => { const root=document.getElementById('app')||document.querySelector('#app');
  var path=%(p)r, hint=%(m)r;
  if (root && root.__vue__ && root.__vue__.$router) { root.__vue__.$router.push(path); return 'vue'; }
  var useHash = hint==='hash' || (hint!=='history' && /^#\//.test(location.hash||''));
  if (useHash) { location.hash = (path.charAt(0)==='#'?path:'#'+path); return 'hash'; }
  history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')); return 'history'; })()"""

CURRENT_ROUTE_JS = r"""(() => { const root=document.getElementById('app')||document.querySelector('#app');
  if (root && root.__vue__ && root.__vue__.$route) return root.__vue__.$route.path;
  return location.hash ? location.hash.replace(/^#/, '') : location.pathname; })()"""

CONTENT_SEL = "#app, #root, main, [role=main]"

# §3.7 #3 GraphQL introspection 只读查询(取根类型名 + 各类型字段名, 喂 parse_introspection)
GRAPHQL_INTROSPECT_QUERY = (
    "query{__schema{queryType{name} mutationType{name} subscriptionType{name} "
    "types{name fields{name}}}}"
)

# 白名单 widget 角色(webmap_DESIGN.md §4.4): 只点已知只读
SAFE_WIDGET_RE = re.compile(r"(tab|accordion|collapse|expand|menu-toggle|drawer|detail|toggle)", re.I)
BLOCK_TEXT_RE = re.compile(r"(提交|保存|删除|上传|重置|支付|确定|确认|新增|编辑|submit|save|delete|upload|reset|pay|confirm)", re.I)

def safe_widgets(dom: dict) -> list[dict]:
    """白名单分类: 只返回已知只读、且文本不含改写动词的 widget(webmap_DESIGN.md §4.4)。"""
    out = []
    for w in dom.get("widgets", []):
        role_cls = f"{w.get('role','')} {w.get('cls','')}"
        if SAFE_WIDGET_RE.search(role_cls) and not BLOCK_TEXT_RE.search(w.get("text", "")):
            out.append(w)
    return out

# ============================================================================
# §J 编排(只在 browser-harness 下执行; helper 为注入全局)
# ============================================================================

def _js(expr: str):
    """browser-harness 的 js() 已返回**解析好的对象**(dict/list/str), 不是 JSON 文本。
    历史 bug: 曾对已解析对象再套一层 JSON 解析 → 首调直接 TypeError。
    保留对 JSON 字符串契约的容错(个别 harness 版本可能回字符串)。"""
    v = js(expr)                                                           # noqa: F821
    if isinstance(v, (str, bytes, bytearray)):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v

# 登录/SSO 路径(段边界锚定: /sso 不再误命中 /ssorder;含常见 IdP 路径)
_LOGIN_PATH_RE = re.compile(
    r"/(?:login|signin|sign-in|logon|sso|saml|oauth2?|openid|cas/login|"
    r"account/login|auth/(?:login|signin)|idp|adfs)(?:[/?#]|$)", re.I)

def looks_like_login(probe: dict) -> bool:
    if probe.get("hasPwd") and probe.get("bodyLen", 999999) < 4000:
        return True
    # 无密码框的纯 SSO/IdP 跳转墙: URL 命中登录路径且正文极短(业务内容尚未渲染)
    if _LOGIN_PATH_RE.search(str(probe.get("url", ""))) and probe.get("bodyLen", 999999) < 1500:
        return True
    return False

def detect_arch(probe: dict, forced: str = "auto") -> str:
    if forced in ("spa", "mpa"):
        return forced
    spa, mpa = probe.get("spa", {}), probe.get("mpa", {})
    spa_score = sum([spa.get("webpack", False), spa.get("vue", False), spa.get("react", False),
                     spa.get("angular", False), spa.get("importmap", False), spa.get("chunks", 0) > 0])
    # jQuery/MiniUI iframe-portal 信号: MiniUI 自身即强门户信号; jQuery 需与同源内容 iframe 同时出现
    # 才算门户(jQuery 在 SPA 里也常见, 单独不足为据)。纠偏 0 信号门户被误判 spa(§arch)。
    portal = bool(mpa.get("miniui")) or (bool(mpa.get("jquery")) and bool(mpa.get("iframePortal")))
    mpa_score = sum([mpa.get("forms", 0) > 0, mpa.get("viewstate", False), mpa.get("serverExt", False), portal])
    if spa_score and mpa_score:
        return "hybrid"
    return "spa" if spa_score >= mpa_score else "mpa"


def _ingest_js(txt: str, source_name: str, base_origin: str,
               routes_s: list, apis_groups: list, seen_scripts: set, clients: set | None = None,
               module_url: str | None = None, importmap: dict | None = None,
               base_host: str = "", sub_hosts=()) -> None:
    """单个 JS 源文本 → 路由/API 记录, 并把 worker / ESM import 目标投回回灌集
    (webmap_DESIGN.md §3.3/§3.5)。module_url 给定时对该模块做 ESM 说明符 BFS 入队。
    base_host/sub_hosts 供 §4.5 REST host 归属裁决(第三方剔除 / 跨域标记)。"""
    rs, _ = extract_routes(txt)
    routes_s += [{**r, "provenance": "declared-only"} for r in rs]
    ex = extract_api_literals(txt, source_name)
    apis_groups.append(_api_records(ex, base_host, sub_hosts))
    if clients is not None:
        c = detect_http_client(txt)
        if c:
            clients.add(c)
    for w in ex.get("workers", []):
        wu = urljoin(base_origin, w)
        if wu.split("?")[0].endswith((".js", ".mjs")):
            seen_scripts.add(wu)
    # §3.5 ESM 图 BFS: 解析本模块的 import/export-from 说明符 → 绝对 URL, 脚本类的入回灌集。
    # 相对/importmap 解析需以本模块自身 URL 为基址;裸包名无 importmap → resolve 返回 None(如实漏)。
    if module_url:
        for spec in extract_import_specifiers(txt):
            ru = resolve_specifier(spec, module_url, importmap)
            if ru and ru.startswith(("http://", "https://")) and urlsplit(ru).path.endswith((".js", ".mjs")):
                seen_scripts.add(ru.split("#")[0])


def main():
    cfg = load_config()
    if not cfg["url"]:
        print(json.dumps({"error": "WEBMAP_URL 未设置"})); return
    out_dir = cfg["out"]; js_dir = os.path.join(out_dir, "js")
    os.makedirs(js_dir, exist_ok=True)

    new_tab(cfg["url"]); wait_for_load()                                    # noqa: F821
    cdp("Network.enable", maxTotalBufferSize=64_000_000, maxResourceBufferSize=16_000_000)  # noqa: F821
    probe = _js(PAGE_PROBE_JS)
    if looks_like_login(probe):
        print(json.dumps({"halted_reason": "login-wall", "msg": "登录墙 — 请先登录"})); return

    arch_probe = _js(ARCH_PROBE_JS)
    arch = detect_arch(arch_probe, cfg["arch"])
    base_origin = "{0.scheme}://{0.netloc}".format(urlsplit(cfg["url"]))
    base_host = urlsplit(cfg["url"]).netloc                  # §4.5 host 归属基准
    sub_hosts: set[str] = set()                              # §3.2 子系统 host(同源待遇, 见 classify_origin)
    third_party_filtered = [0]                               # §4.5 剔除的第三方端点计数
    # B1: --budget 只约束 Phase 2 动态遍历(开放式、按路由发请求的慢部分);静态 chunk 下载
    # 是有界集合(chunk 数固定), 不能让它吃光预算导致动态阶段 0 执行。deadline 在 Phase 2 前才设。
    deadline = None
    halted_reason = None

    routes_s: list[dict] = []
    apis_groups: list[list[dict]] = []
    chunks_failed = [0]; docs_failed = [0]
    fetched: set[str] = set()
    url_map: dict[str, str] = {}             # 已存文件名 → 源 URL(供 §3.4 sourcemap 解析)
    seen_scripts: set[str] = set()
    blueprint: dict = {}
    subsystems: list[dict] = []              # §3.2 federation 远程
    clients: set[str] = set()                # §6 http_client 候选(chunk 扫描)
    importmap: dict = {}                     # §3.5 ESM 裸说明符解析表(importmap imports)

    # Phase 0.7 声明式资源
    if cfg["docs"]:
        ds = read_declared_resources(base_origin, docs_failed)
        routes_s += ds["routes"]; apis_groups.append(ds["apis"])

    feature_flags: list[str] = []            # §4.4 全局 feature-flag 线索(显隐控制)
    # Phase 1 静态
    if arch in ("spa", "hybrid"):
        loaded = _js(COLLECT_JS_AND_MANIFEST)
        importmap = loaded.get("importmap") or {}                          # §3.5 ESM 说明符解析表
        subsystems = derive_subsystems(loaded.get("remotes", []))          # §3.2 微前端子系统
        _enrich_remotes(subsystems, fetched, chunks_failed)                # §3.2 拉 remoteEntry 解析 exposed-map
        subsystems += _collect_iframes(cfg, seen_scripts)                  # §3.2 同源 iframe 子应用
        sub_hosts = {h for s in subsystems                                 # §4.5 子系统 host 视同本站
                     for h in [urlsplit(s.get("origin") or s.get("entry") or "").netloc] if h}
        chunk_urls = (resolve_all_chunks(loaded, base_origin)
                      if cfg["chunks"] == "all" else loaded.get("loaded_js", []))  # §3.1 --chunks
        for u in chunk_urls:
            _save_js(u, js_dir, fetched, chunks_failed, url_map)
        if cfg["sourcemaps"]:
            _recover_sourcemaps(js_dir, fetched, url_map)                   # §3.4
        blueprint = _js(ROUTER_DETECT_JS)
        feature_flags = sorted(set(blueprint.get("feature_flags") or []))
        routes_s += [{"path": r["path"], "provenance": "declared-only", "source": "static",
                      "name": r.get("name"), "component": r.get("component"),
                      "meta": r.get("meta")} for r in blueprint.get("routes", [])]
        for fn in sorted(os.listdir(js_dir)):
            if fn.endswith(".js"):
                _ingest_js(_read(os.path.join(js_dir, fn)), fn, base_origin,
                           routes_s, apis_groups, seen_scripts, clients,
                           module_url=url_map.get(fn), importmap=importmap,
                           base_host=base_host, sub_hosts=sub_hosts)

    # Phase 2 遍历 + Phase 3.5 回灌(简化收敛: 见 webmap_DESIGN.md §10)
    # MPA/hybrid 无 router 配置: 把起始页本身作为遍历根入队, 才能从它的站内链接展开发现
    # (否则 routes_s 仅含 sitemap/robots, 无 sitemap 时为空 → 一页都不访问, §3.6)。
    if arch in ("mpa", "hybrid"):
        seed = urlsplit(cfg["url"]).path or "/"
        if not any(normalize_path_template(r["path"])[0] == normalize_path_template(seed)[0] for r in routes_s):
            routes_s.append({"path": seed, "provenance": "declared-only", "source": "seed"})
    if cfg["budget"]:                                       # B1: 预算从动态阶段起算
        deadline = time.time() + cfg["budget"]
    visited: dict[str, dict] = {}
    recorded: list[dict] = []
    router_mode = blueprint.get("mode")          # §4.1 Vue=$router.mode / 非 Vue=hash|history 探测值
    concrete_paths: set = set()                  # §4.3 跨 pass 累积的真实实例路径
    halted_reason = _crawl(arch, cfg, routes_s, visited, recorded, seen_scripts,
                           apis_groups, base_origin, deadline,
                           router_mode, concrete_paths) or halted_reason

    # 回灌闭环
    rounds = 0
    while rounds < 6 and not halted_reason:
        rounds += 1
        new_js = [u for u in seen_scripts if u not in fetched]
        if not new_js:
            break
        for u in new_js:
            path = _save_js(u, js_dir, fetched, chunks_failed, url_map)
            if path:
                _ingest_js(_read(path), os.path.basename(path), base_origin,
                           routes_s, apis_groups, seen_scripts, clients,
                           module_url=u, importmap=importmap,              # §3.5 ESM BFS 以脚本自身 URL 为基址
                           base_host=base_host, sub_hosts=sub_hosts)
                if cfg["sourcemaps"]:
                    _recover_sourcemaps(js_dir, fetched, url_map)          # §3.4 旁车
                    side = path[:-3] + ".srcmap.js"
                    if os.path.exists(side):                               # 回灌轮也重抽旁车原始源
                        _ingest_js(_read(side), os.path.basename(side), base_origin,
                                   routes_s, apis_groups, seen_scripts, clients,
                                   base_host=base_host, sub_hosts=sub_hosts)
        new_paths = [r for r in routes_s if normalize_path_template(r["path"])[0] not in visited]
        halted_reason = _crawl(arch, cfg, new_paths, visited, recorded, seen_scripts,
                               apis_groups, base_origin, deadline,
                               router_mode, concrete_paths) or halted_reason

    # §3.7 #3 GraphQL introspection(发现的每个端点发一次只读 POST)
    introspect_state: dict = {}
    if cfg["docs"]:
        for grp in _graphql_introspect(apis_groups, recorded, base_origin, introspect_state):
            apis_groups.append(grp)

    # Phase 3 合并 + 覆盖率
    observed = _observed_apis(recorded, base_host, sub_hosts, third_party_filtered)
    apis = merge_apis(*apis_groups, observed)
    routes = merge_routes(routes_s, visited)
    # 反向连 route.apis(webmap_DESIGN.md §6 routes.apis)
    route_by_path = {r["path"]: r for r in routes}
    for a in apis:
        for rid in a.get("called_by_routes", []):
            r = route_by_path.get(rid) or route_by_path.get(rid[2:] if rid.startswith("r:") else rid)
            if r is not None:
                r.setdefault("apis", []).append(a["id"])
    for r in routes:
        if "apis" in r:
            r["apis"] = sorted(set(r["apis"]))
    cov = coverage_summary(routes, apis, chunks_failed[0], docs_failed[0], third_party_filtered[0])
    if introspect_state.get("disabled"):
        cov["introspection_disabled"] = sorted(set(introspect_state["disabled"]))
    framework = derive_framework(blueprint, arch_probe, arch)
    http_client = (arch_probe.get("fp", {}).get("http_client")
                   or (sorted(clients)[0] if clients else None))
    data = {
        "system": {
            "host": urlsplit(cfg["url"]).netloc, "start_url": cfg["url"], "arch": arch,
            "framework": framework,
            "version": derive_version(framework, arch_probe),
            "router_mode": blueprint.get("mode") or ("server" if arch in ("mpa", "hybrid") else None),
            "http_client": http_client, "subsystems": subsystems,
            "feature_flags": feature_flags,         # §4.4 全局显隐 flag 线索(Phase 4 据此标隐藏功能)
            "crawl": {"mode": cfg["depth"], "chunks": cfg["chunks"],
                      "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "budget_sec": cfg["budget"], "halted_reason": halted_reason},
        },
        "routes": routes, "apis": apis,
        "graphql": graphql_view(apis), "realtime": realtime_view(apis),
        "modules": [], "entities": [],      # Phase 4 (LLM) 增量回写
        "coverage": cov,
    }
    _write(os.path.join(out_dir, "webmap.json"), json.dumps(data, ensure_ascii=False, indent=2))
    _write_jsonl(os.path.join(out_dir, "network.jsonl"), recorded)
    print(json.dumps({"coverage": cov, "arch": arch, "halted_reason": halted_reason},
                     ensure_ascii=False, indent=2))


# ---- 编排辅助(browser helper / fs) ----

def _api_records(extracted: dict, base_host: str = "", sub_hosts=()) -> list[dict]:
    """§3.3 抽取结果 → api 记录(REST/template/WS/SSE/GraphQL; events 与 base_ref 一并落盘)。
    REST 端点的 host 归属由 classify_origin 裁决(§4.5): 第三方遥测/CDN 剔除; 独立跨域域保留
    host 并标 cross_origin, 不归一成伪本站路径; 本站/同注册域归一为路径模板。"""
    out = []
    sf = extracted["source_file"]
    base_refs = extracted.get("base_refs", [])
    base_ref = base_refs[0] if len(base_refs) == 1 else None   # 单一 base 才可靠归属
    file_events = extracted.get("events", [])
    for r in extracted["rest"]:
        cls = classify_origin(r["path"], base_host, sub_hosts)
        if cls == "third-party":                               # §4.5 已知第三方噪声: 不计入系统 API
            continue
        tpl, qp = normalize_path_template(r["path"])
        if cls == "cross":                                     # 独立跨域域: 保留 host, 不伪装成本站路径
            tpl = "{0.scheme}://{0.netloc}".format(urlsplit(r["path"])) + tpl
        rec = {"method": r["method"], "path_template": tpl, "query_params": qp,
               "transport": "rest", "provenance": "declared-only",
               "source_file": sf, "confidence": "high"}
        if cls == "cross":
            rec["cross_origin"] = True
        if base_ref:
            rec["base_ref"] = base_ref
        out.append(rec)
    for p in extracted.get("prop_paths", []):               # §3.3 B2: 键→路径映射(无调用点锚定 → 低置信)
        tpl, qp = normalize_path_template(p)
        out.append({"method": None, "path_template": tpl, "query_params": qp,
                    "transport": "rest", "provenance": "declared-only",
                    "source_file": sf, "source": "url-map", "confidence": "low"})
    for t in extracted["templates"]:
        out.append({"method": None, "path_template": re.sub(r"\$\{[^}]+\}", "{var}", t),
                    "transport": "rest", "provenance": "declared-only",
                    "source_file": sf, "confidence": "low"})
    for w in extracted["ws"]:
        rec = {"path_template": w, "transport": "ws", "provenance": "declared-only",
               "source_file": sf, "confidence": "medium"}
        if file_events:
            rec["events"] = file_events
        out.append(rec)
    for s in extracted.get("sse", []):
        rec = {"path_template": s, "transport": "sse", "provenance": "declared-only",
               "source_file": sf, "confidence": "medium"}
        if file_events:
            rec["events"] = file_events
        out.append(rec)
    for op in extracted["graphql"]:
        out.append({"path_template": "/graphql", "transport": "graphql", "method": "POST",
                    "operation": op, "provenance": "declared-only",
                    "source_file": sf, "confidence": "medium"})
    return out

def _observed_apis(recorded: list[dict], base_host: str = "", sub_hosts=(),
                   filtered: list | None = None) -> list[dict]:
    """动态网络观测 → api 记录(带 status/mime/响应字段, webmap_DESIGN.md §4.5/§6)。
    §4.5 第三方噪声过滤: 已知遥测/分析/广告/CDN host 剔除(计入 filtered);独立跨域域保留 host
    并标 cross_origin, 不归一成伪本站路径; 本站/同注册域归一为路径模板。"""
    out = []
    for rec in recorded:
        transport = rec.get("kind", "rest")
        url = rec.get("url") or ""
        cls = classify_origin(url, base_host, sub_hosts)
        if cls == "third-party":                  # 已知第三方遥测/分析: 不计入系统 API
            if filtered is not None:
                filtered[0] += 1
            continue
        if transport in ("ws", "sse"):            # WS/SSE 不走 path_template 归一(否则 wss://h/x → /x 丢主机)
            tpl, qp = url, []
        else:
            tpl, qp = normalize_path_template(url)
            if cls == "cross":                    # 独立跨域域: 保留 host, 不伪装成本站路径
                tpl = "{0.scheme}://{0.netloc}".format(urlsplit(url)) + tpl
        api = {"method": rec.get("method"), "path_template": tpl, "query_params": qp,
               "transport": transport, "provenance": "observed-only",
               "called_by_routes": [rec["route"]] if rec.get("route") else [],
               "confidence": "high"}
        if cls == "cross":
            api["cross_origin"] = True
        if rec.get("status") is not None:
            api["statuses"] = [rec["status"]]
        if rec.get("mime"):
            api["mime"] = rec["mime"]
        if rec.get("events"):                     # §4.5 WS 帧 event 名
            api["events"] = rec["events"]
        fields = extract_response_fields(rec.get("resp_sample"))
        if fields:
            api["response_fields"] = fields
        out.append(api)
    return out

def read_declared_resources(base_origin: str, docs_failed: list) -> dict:
    routes, apis = [], []
    def get(path):
        try:
            return http_get(urljoin(base_origin, path))                    # noqa: F821
        except Exception:
            docs_failed[0] += 1; return None
    rb = get("/robots.txt")
    if rb: routes += [{"path": p, "provenance": "declared-only", "source": "robots"} for p in parse_robots(rb)]
    sm = get("/sitemap.xml")
    if sm: routes += [{"path": urlsplit(u).path, "provenance": "declared-only", "source": "sitemap"} for u in parse_sitemap(sm)]
    for spec_url in ("/swagger.json", "/swagger/v1/swagger.json", "/openapi.json", "/v2/api-docs", "/v3/api-docs", "/api-docs"):
        body = get(spec_url)
        if body:
            try:
                apis += parse_openapi(json.loads(body)); break
            except Exception:
                docs_failed[0] += 1
    return {"routes": routes, "apis": apis}

def _enrich_remotes(subsystems: list, fetched: set, chunks_failed: list) -> None:
    """§3.2: 对每个 federation 远程拉 remoteEntry.js 解析 exposed-module map(还原远程暴露子模块)。
    逐个失败计入 chunk fail, 不静默丢。"""
    for sub in subsystems:
        entry = sub.get("entry")
        if not entry or entry in fetched:
            continue
        fetched.add(entry)
        try:
            body = http_get(entry)                                          # noqa: F821
        except Exception:
            chunks_failed[0] += 1; continue
        if not body:
            chunks_failed[0] += 1; continue
        exposes = parse_remote_entry(body)
        if exposes:
            sub["exposes"] = exposes

def _collect_iframes(cfg: dict, seen_scripts: set) -> list[dict]:
    """§3.2: 同源 iframe(默认入范围)/ 跨域(--cross-iframe)子应用 → 子系统节点;
    进各 iframe target 收其已加载脚本入回灌集(由 §3.5 闭环统一抽取路由/API)。"""
    subs: list[dict] = []
    try:
        frames = _js(IFRAME_PROBE_JS) or []
    except Exception:
        return subs
    for f in frames:
        src = f.get("src") if isinstance(f, dict) else None
        if not src:
            continue
        if not (f.get("sameOrigin") or cfg["cross_iframe"]):              # 跨域默认不入范围(§0)
            continue
        sp = urlsplit(src)
        subs.append({"kind": "iframe", "origin": "{0.scheme}://{0.netloc}".format(sp), "entry": src})
        try:
            tgt = iframe_target(src)                                       # noqa: F821
            inner = js(COLLECT_JS_AND_MANIFEST, target_id=tgt)            # noqa: F821
            if isinstance(inner, (str, bytes, bytearray)):
                inner = json.loads(inner)
            for u in (inner.get("loaded_js") if isinstance(inner, dict) else None) or []:
                if u:
                    seen_scripts.add(u)
        except Exception:
            pass
    return subs

def collect_graphql_endpoints(apis_groups: list, recorded: list) -> list[str]:
    """从源码抽取(transport=graphql)+ 动态观测(url 含 graphql)汇总 GraphQL 端点路径。去重。"""
    eps = set()
    for group in apis_groups or []:
        for a in group or []:
            if a.get("transport") == "graphql" and a.get("path_template"):
                eps.add(a["path_template"])
    for rec in recorded or []:
        u = rec.get("url", "")
        if "graphql" in u.lower():
            eps.add(urlsplit(u).path or u)
    return sorted(eps)

def _graphql_introspect(apis_groups, recorded, base_origin, introspect_state):
    """§3.7 #3: 对每个发现的 GraphQL 端点发一次只读 introspection POST(页面上下文, 带会话)。
    成功 → 追加 spec-declared operation 组;禁用/失败 → 记 introspection-disabled。"""
    eps = collect_graphql_endpoints(apis_groups, recorded)
    extra = []
    for ep in eps:
        url = ep if ep.startswith(("http://", "https://")) else urljoin(base_origin, ep)
        expr = (
            "(async()=>{try{const r=await fetch(%r,{method:'POST',"
            "headers:{'content-type':'application/json'},credentials:'include',"
            "body:JSON.stringify({query:%r})});return await r.text();}"
            "catch(e){return 'ERR:'+e}})()" % (url, GRAPHQL_INTROSPECT_QUERY)
        )
        try:
            res = cdp("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)  # noqa: F821
            text = (res.get("result") or {}).get("value")
        except Exception:
            text = None
        ops = []
        if text and not str(text).startswith("ERR:"):
            try:
                ops = parse_introspection(json.loads(text))
            except Exception:
                ops = []
        if ops:
            extra.append([{"path_template": ep, "transport": "graphql", "method": "POST",
                           "operation": op, "provenance": "spec-declared",
                           "source": "introspection", "confidence": "high"} for op in ops])
        else:
            introspect_state.setdefault("disabled", []).append(ep)
    return extra

def _crawl(arch, cfg, queue, visited, recorded, seen_scripts, apis_groups, base_origin,
           deadline=None, router_mode=None, concrete=None):
    """返回 halted_reason(auth-expired / budget-exhausted)或 None(webmap_DESIGN.md §4.6/§5)。
    work 是可增长队列: MPA/hybrid 下把每页同源站内链接动态入队(§3.6 路由 = 链接 ∪ 菜单 ∪ sitemap)。
    concrete: 跨 pass 累积的真实实例路径(§4.3 参数路由取值);param 路由排到队尾, 先攒到 id 再试。"""
    limit = cfg["max"] or 10_000
    if concrete is None:
        concrete = set()
    # §4.3: 参数路由排到队尾——先访问列表/普通路由攒真实 id, 再回头取详情实例。
    norm = lambda r: normalize_path_template(r["path"] if isinstance(r, dict) else r)[0]
    work = sorted(list(queue), key=lambda r: is_param_template(norm(r)))
    enq: set[str] = set()                                                  # 已入队的链接派生路径(防重复膨胀)
    i = 0
    while i < len(work) and len(visited) < limit:
        r = work[i]; i += 1
        if deadline and time.time() > deadline:
            return "budget-exhausted"
        path = norm(r)
        if path in visited:
            continue
        # §4.3 参数路由: 取一个本会话见过的真实实例; 取不到 → param-unresolved, 不瞎 push 模板
        target = path
        if is_param_template(path):
            inst = resolve_param_route(path, concrete)
            if not inst:
                visited[path] = {"visit_result": "param-unresolved"}
                continue
            target = inst
        drain_events()                                                     # noqa: F821
        ws_state: dict = {}                        # §4.5 本路由 WS requestId→url 跨 capture 映射
        landed = _push(arch, target, base_origin, router_mode)
        recs = _capture_idle(seen_scripts, cfg, path, ws_state)
        if _is_login(landed):
            visited[path] = {"visit_result": "auth-redirect"}
            return "auth-expired"                                          # §4.6 鉴权过期 → 停, 不污染后续
        visited[path] = {"visit_result": classify_landing(landed, target)}  # §4.2 visited|blocked|redirected
        recorded.extend(recs)
        dom = _js(EXTRACT_JS_WITH_XY)
        # §4.3: 攒本页真实实例路径(供后续参数路由取值)
        concrete |= concrete_same_origin_paths(dom.get("links", []), base_origin,
                                               landed or _abs(target, base_origin))
        if arch in ("mpa", "hybrid"):
            apis_groups.append(forms_to_endpoints(dom.get("forms", []), landed or path))
            apis_groups.append(postback_targets(dom.get("blob", ""), landed or path))
            # §3.6 站内链接入队(MPA 无 router 配置, 路由主要靠链接/菜单到达)
            for tpl in same_origin_links(dom.get("links", []), base_origin, landed or _abs(path, base_origin)):
                if tpl not in visited and tpl not in enq:
                    enq.add(tpl)
                    work.append({"path": tpl, "provenance": "declared-only", "source": "link"})
        if cfg["depth"] == "active":
            _active_discover(dom, recorded, seen_scripts, cfg, path, ws_state)
            # §4.4 状态复位: Esc 关弹窗 + 重新 push 干净路由, 避免脏 UI 污染后续(SPA)
            press_key("Escape")                                            # noqa: F821
            if arch != "mpa":
                _push(arch, target, base_origin, router_mode)
        else:
            press_key("Escape")                                            # noqa: F821
    return None

def _active_discover(dom, recorded, seen_scripts, cfg, path, ws_state=None):
    exercised = set()
    for _ in range(20):                       # 安全上限
        todo = [w for w in safe_widgets(dom) if w["id"] not in exercised]
        if not todo:
            break
        for w in todo:
            click_at_xy(w["x"], w["y"]); exercised.add(w["id"])            # noqa: F821
            recorded.extend(_capture_idle(seen_scripts, cfg, path, ws_state))
        dom = _js(EXTRACT_JS_WITH_XY)

def _push(arch, path, base_origin, router_mode=None):
    if arch == "mpa":
        new_tab(_abs(path, base_origin)); wait_for_load(); wait_for_network_idle()  # noqa: F821
        return _current_url()
    js(PUSH_ROUTE_JS % {"p": path, "m": router_mode or ""})                # noqa: F821
    wait_for_network_idle()                                                # noqa: F821
    try: wait_for_element(CONTENT_SEL)                                     # noqa: F821
    except Exception: pass
    return js(CURRENT_ROUTE_JS)                                            # noqa: F821

def _capture_idle(seen_scripts, cfg, route, ws_state=None):
    """drain CDP 事件; loadingFinished 当场取 body; responseReceived 收 Script URL(§4.5);
    §4.5 WS: webSocketCreated 记 url, frameSent/Received 取帧 event 名(运行时实收发, 非静态正则)。
    ws_state(可选, 跨同一路由多次 capture 持有 requestId→url 映射)。"""
    recs = []
    pending = {}
    ws_urls = ws_state.setdefault("urls", {}) if ws_state is not None else {}
    ws_frames: dict = {}                          # 本次 drain 聚合的 WS 帧(每连接一条)
    for ev in drain_events():                                              # noqa: F821
        name, p = ev.get("method"), ev.get("params", {})
        if name == "Network.requestWillBeSent":
            pending[p["requestId"]] = {"url": p["request"]["url"], "method": p["request"].get("method"),
                                       "route": route, "kind": "rest"}
        elif name == "Network.responseReceived":
            rid = p["requestId"]; rtype = p.get("type"); mime = p.get("response", {}).get("mimeType", "")
            if rtype == "Script":
                seen_scripts.add(p["response"]["url"])
            if rid in pending:
                pending[rid]["status"] = p["response"].get("status"); pending[rid]["mime"] = mime
        elif name == "Network.loadingFinished" and cfg["bodies"]:
            rid = p["requestId"]
            if rid in pending and re.search(r"json|text", pending[rid].get("mime", "")):
                try:
                    body = cdp("Network.getResponseBody", requestId=rid)   # noqa: F821
                    pending[rid]["resp_sample"] = (body.get("body") or "")[:4096]
                except Exception:
                    pass
                recs.append(pending.pop(rid))
        elif name == "Network.webSocketCreated":
            ws_urls[p.get("requestId")] = p.get("url")
        elif name in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
            rid = p.get("requestId")
            fr = ws_frames.setdefault(rid, {"url": ws_urls.get(rid), "method": None,
                                            "route": route, "kind": "ws", "_events": set()})
            if fr["url"] is None:
                fr["url"] = ws_urls.get(rid)
            evn = ws_event_from_payload((p.get("response") or {}).get("payloadData", ""))
            if evn:
                fr["_events"].add(evn)
    recs.extend(pending.values())
    for fr in ws_frames.values():                 # WS 帧 → observed ws 记录(带 event 名)
        evs = sorted(fr.pop("_events"))
        if evs:
            fr["events"] = evs
        recs.append(fr)
    return recs

# 薄 fs / url 包装(便于测试 stub)
def _save_js(url, js_dir, fetched, chunks_failed, url_map=None):
    if url in fetched:
        return None
    fetched.add(url)
    try:
        body = http_get(url)                                               # noqa: F821
    except Exception:
        chunks_failed[0] += 1; return None
    if body is None:
        chunks_failed[0] += 1; return None
    name = re.sub(r"[^\w.-]", "_", urlsplit(url).path.rsplit("/", 1)[-1] or "chunk") + ".js" if not url.endswith(".js") else re.sub(r"[^\w.-]", "_", urlsplit(url).path.rsplit("/", 1)[-1])
    path = os.path.join(js_dir, name)
    _write(path, body)
    if url_map is not None:
        url_map[name] = url
    return path

def _recover_sourcemaps(js_dir, fetched, url_map):
    """§3.4: 对每个已存 .js 找 sourceMappingURL → 取 .map → sourcesContent 落成 *.srcmap.js,
    供下一轮抽取在可读原始源上重跑。逐个失败静默跳过(不计 chunk 失败)。"""
    for fn in sorted(os.listdir(js_dir)):
        if not fn.endswith(".js") or fn.endswith(".srcmap.js"):
            continue
        src_path = os.path.join(js_dir, fn[:-3] + ".srcmap.js")
        if os.path.exists(src_path):
            continue
        mu = find_sourcemap_url(_read(os.path.join(js_dir, fn)))
        if not mu:
            continue
        inline = decode_inline_sourcemap(mu)
        if inline is not None:
            map_text = inline
        else:
            base = url_map.get(fn) or ""
            map_url = urljoin(base, mu) if base else mu
            if not map_url.startswith(("http://", "https://")) or map_url in fetched:
                continue
            fetched.add(map_url)
            try:
                map_text = http_get(map_url)                               # noqa: F821
            except Exception:
                continue
        src = parse_sourcemap(map_text) if map_text else ""
        if src:
            _write(src_path, src)

def _read(p):
    with open(p, encoding="utf-8", errors="ignore") as f: return f.read()
def _write(p, s):
    with open(p, "w", encoding="utf-8") as f: f.write(s)
def _write_jsonl(p, rows):
    with open(p, "w", encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
def _abs(path, base_origin):
    return path if path.startswith("http") else urljoin(base_origin.rstrip("/") + "/", path.lstrip("/"))
def _current_url():
    return js("location.pathname + location.search")                      # noqa: F821
def _is_login(landed):
    return bool(landed) and bool(_LOGIN_PATH_RE.search(str(landed)))
def _same_route(landed, intended):
    return bool(landed) and normalize_path_template(str(landed))[0] == normalize_path_template(intended)[0]


# 入口: browser-harness 经 `browser-harness < webmap_crawl.py` 执行时, __name__ 为
# "browser_harness.run"(非 "__main__"); 直跑 python3 时为 "__main__"。两者都要触发 main()。
# 而 test_*.py 以 `import webmap_crawl` 载入时 __name__ == "webmap_crawl", 不触发(import 安全)。
if __name__ in ("__main__", "browser_harness.run"):
    main()
