#!/usr/bin/env python3
"""webmap_crawl 编排层冒烟测试(离线, 无需真实 browser)。

为什么需要它: test_extractors.py 只测纯函数, 完全绕过 main()/§J 编排层。
两个一调用即崩的 bug 因此长期潜伏:
  A1  `if __name__ == "__main__"` 守卫在 browser-harness 下永不触发(实际 __name__
      为 "browser_harness.run") → main() 从不执行。
  A2  `json.loads(js(...))` 把 js() 已解析好的对象再解析 → 首调 TypeError。

本测试用打桩的注入式 helper 跑通整条 main(), 断言它**真的写出了 webmap.json**;
js() 桩**返回对象**(真实契约), 所以若有人退回 json.loads(js(...)) 会立即崩 → 守住 A2。
末尾再静态校验 A1 的入口守卫覆盖 "browser_harness.run"。

用法: python3 core/test_smoke.py   或   pytest core/test_smoke.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webmap_crawl as m


# ----- 注入式 browser-harness helper 桩(对象返回值 = 真实 js() 契约) -----

class _Harness:
    """最小可跑通 SPA(Vue2)路径的桩。js() 按所调用的常量返回对应对象。"""
    def __init__(self):
        self.last_pushed = "/"

    def js(self, expr):
        if expr is m.PAGE_PROBE_JS:
            # 已登录: 无密码框 + 正文够长 → looks_like_login=False
            return {"url": "https://t/#/views/home", "title": "demo",
                    "bodyLen": 9000, "hasPwd": False, "forms": 0}
        if expr is m.ARCH_PROBE_JS:
            return {"spa": {"webpack": True, "vue": True, "react": False,
                            "angular": False, "importmap": False, "chunks": 3},
                    "fp": {"vue_version": "2.6.10", "react_version": None,
                           "ng_version": None, "http_client": "axios"},
                    "mpa": {"forms": 0, "viewstate": False, "serverExt": False, "exts": []}}
        if expr is m.COLLECT_JS_AND_MANIFEST:
            return {"webpack": [], "vite_manifest": None, "importmap": None,
                    "loaded_js": [], "literal_chunks": [], "public_path": "", "remotes": []}
        if expr is m.ROUTER_DETECT_JS:
            return {"vue": 2, "mode": "hash", "routes": [
                {"path": "/views/home", "name": "home", "meta": None, "component": None},
                {"path": "/views/areacode", "name": "areacode", "meta": None, "component": None},
            ]}
        if expr is m.EXTRACT_JS_WITH_XY:
            return {"links": [], "buttons": [], "widgets": [], "forms": [], "blob": ""}
        if expr is m.CURRENT_ROUTE_JS:
            return self.last_pushed                       # 落地路径 = 刚 push 的(→ visited)
        # PUSH_ROUTE_JS 把目标格式化进 `var path='...'`(P0 修复后不再是 push('字面量')):
        # 从该绑定取出落地路径, 记下并回字符串(不是对象)。
        if "$router.push" in expr or "pushState" in expr or "location.hash" in expr:
            import re
            mm = re.search(r"""\bpath\s*=\s*['"]([^'"]+)['"]""", expr)
            if mm:
                self.last_pushed = mm.group(1)
            return "vue"
        return None

    # 其余 helper 一律 no-op / 空
    def new_tab(self, *a, **k): pass
    def wait_for_load(self, *a, **k): pass
    def wait_for_network_idle(self, *a, **k): pass
    def wait_for_element(self, *a, **k): pass
    def press_key(self, *a, **k): pass
    def click_at_xy(self, *a, **k): pass
    def drain_events(self, *a, **k): return []
    def cdp(self, *a, **k): return {}
    def http_get(self, url):                              # 声明式资源探测: 全 404 → 抛错(诚实计 fail)
        raise RuntimeError("404 " + url)


def _install(h):
    for name in ("js", "new_tab", "wait_for_load", "wait_for_network_idle",
                 "wait_for_element", "press_key", "click_at_xy", "drain_events",
                 "cdp", "http_get"):
        setattr(m, name, getattr(h, name))


def test_smoke_main_writes_webmap_json():
    """A1+A2 回归: main() 在注入桩下跑通并写出结构正确的 webmap.json。"""
    out = tempfile.mkdtemp(prefix="webmap-smoke-")
    os.environ["WEBMAP_URL"] = "https://t/#/views/areacode"
    os.environ["WEBMAP_FLAGS"] = f"--max 5 --depth passive --docs on --out {out}"
    _install(_Harness())

    m.main()                                              # A1: 这一句历史上根本到不了

    p = os.path.join(out, "webmap.json")
    assert os.path.exists(p), "main() 未写出 webmap.json"
    data = json.load(open(p, encoding="utf-8"))
    assert data["system"]["arch"] == "spa"
    assert data["system"]["framework"] == "vue2"
    assert data["system"]["version"] == "2.6.10"
    assert data["system"]["router_mode"] == "hash"
    # 路由抽取到 + 动态确实访问到(_same_route 命中)
    assert data["coverage"]["routes_declared"] >= 2
    assert data["coverage"]["visited"] >= 1
    # 声明式资源探测全失败被如实计数, 不静默丢
    assert data["coverage"]["doc_fetch_failed"] >= 1
    assert "coverage" in data and "apis" in data and "routes" in data


class _MpaHarness:
    """MPA 桩: 验证起始页种子 + 站内链接入队驱动遍历(§3.6 Fix 4)。"""
    def __init__(self):
        self.nav = "/Home"                                # new_tab 落地路径
        self._first = True

    def js(self, expr):
        if expr is m.PAGE_PROBE_JS:
            return {"url": "https://t/Home", "title": "demo", "bodyLen": 9000,
                    "hasPwd": False, "forms": 2}
        if expr is m.ARCH_PROBE_JS:
            return {"spa": {"webpack": False, "vue": False, "react": False,
                            "angular": False, "importmap": False, "chunks": 0},
                    "fp": {"vue_version": None, "react_version": None,
                           "ng_version": None, "http_client": None},
                    "mpa": {"forms": 2, "viewstate": True, "serverExt": True, "exts": ["aspx"]}}
        if expr is m.EXTRACT_JS_WITH_XY:
            links = ["/User/List", "/Admin/Roles"] if self._first else []
            self._first = False
            return {"links": links, "buttons": [], "widgets": [], "forms": [], "blob": ""}
        if "location.pathname" in expr:                   # _current_url
            return self.nav
        return None

    def new_tab(self, url, *a, **k):
        from urllib.parse import urlsplit as _u
        self.nav = _u(url).path or "/"
    def wait_for_load(self, *a, **k): pass
    def wait_for_network_idle(self, *a, **k): pass
    def wait_for_element(self, *a, **k): pass
    def press_key(self, *a, **k): pass
    def click_at_xy(self, *a, **k): pass
    def drain_events(self, *a, **k): return []
    def cdp(self, *a, **k): return {}
    def http_get(self, url): raise RuntimeError("404 " + url)


def test_smoke_mpa_link_discovery():
    """§3.6 Fix 4: MPA 起始页入队 + 站内链接展开 → 链接派生路由被访问到。"""
    out = tempfile.mkdtemp(prefix="webmap-mpa-")
    os.environ["WEBMAP_URL"] = "https://t/Home"
    os.environ["WEBMAP_FLAGS"] = f"--max 10 --depth passive --docs on --out {out}"
    _install(_MpaHarness())

    m.main()

    data = json.load(open(os.path.join(out, "webmap.json"), encoding="utf-8"))
    assert data["system"]["arch"] in ("mpa", "hybrid")
    paths = {r["path"] for r in data["routes"]}
    assert "/Home" in paths, "起始页种子未入库"
    assert "/User/List" in paths and "/Admin/Roles" in paths, "站内链接未被发现/访问"
    assert data["coverage"]["visited"] >= 3, "链接派生路由未被遍历"


def test_entrypoint_guard_covers_browser_harness():
    """A1 静态守卫: 入口必须覆盖 browser-harness 的运行期 __name__。"""
    src = open(os.path.join(os.path.dirname(__file__), "webmap_crawl.py"),
               encoding="utf-8").read()
    assert "browser_harness.run" in src, "入口守卫未覆盖 browser-harness 的 __name__"


def test_js_calls_not_double_parsed():
    """A2 静态守卫: 不得再出现 json.loads(js(...)) 双重解析。"""
    src = open(os.path.join(os.path.dirname(__file__), "webmap_crawl.py"),
               encoding="utf-8").read()
    assert "json.loads(js(" not in src, "退回了 json.loads(js(...)) 双重解析(A2 回归)"


def test_push_route_no_hash_shortcircuit():
    """P0 静态守卫: PUSH_ROUTE_JS 不得退回 `location.hash !== undefined` 恒真短路;
    须按 hint(blueprint.mode / 探测模式)分支, 保留 history.pushState 路径(React/Angular)。"""
    assert "location.hash !== undefined" not in m.PUSH_ROUTE_JS, \
        "P0 回归: hash 短路条件 location.hash !== undefined 恒真"
    assert "useHash" in m.PUSH_ROUTE_JS, "P0: 缺少 hint 驱动的 hash/history 分支"
    assert "history.pushState" in m.PUSH_ROUTE_JS, "P0: 缺少 history 路由分支(React/Angular)"


if __name__ == "__main__":
    test_smoke_main_writes_webmap_json()
    test_smoke_mpa_link_discovery()
    test_entrypoint_guard_covers_browser_harness()
    test_js_calls_not_double_parsed()
    test_push_route_no_hash_shortcircuit()
    print("✓ smoke: main() 跑通并写出 webmap.json; MPA 链接发现; A1/A2/P0 守卫通过")
