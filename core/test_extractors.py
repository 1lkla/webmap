#!/usr/bin/env python3
"""webmap_crawl.py 纯函数离线单测(无需 browser)。用法: python3 core/test_extractors.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import webmap_crawl as m

_fail = []
def eq(got, want, label):
    if got != want:
        _fail.append(f"{label}\n   got : {got!r}\n   want: {want!r}")
def ok(cond, label):
    if not cond:
        _fail.append(label)


# ---- §B path_template 归一 ----
def test_normalize():
    eq(m.normalize_path_template("/user/123/detail")[0], "/user/{id}/detail", "num→{id}")
    eq(m.normalize_path_template("/u/550e8400-e29b-41d4-a716-446655440000")[0], "/u/{uuid}", "uuid→{uuid}")
    eq(m.normalize_path_template("/f/0123456789abcdef0")[0], "/f/{hash}", "hex16→{hash}")
    eq(m.normalize_path_template("/api/role/list?page=2&size=10"), ("/api/role/list", ["page", "size"]), "query split")
    eq(m.normalize_path_template("/user/{id}")[0], "/user/{id}", "已占位不动")
    eq(m.normalize_path_template("/static")[0], "/static", "普通段保留")


# ---- §C resolve_all_chunks ----
def test_chunks():
    loaded = {
        "webpack": [{"p": "/static/js/", "files": ["app.1a2b.js", "vendor.js"]},
                    {"p": "https://cdn.x.com/assets/", "files": ["remote.js"]}],
        "vite_manifest": {"index.html": {"file": "assets/index.9f.js"}, "x": {"file": "a.css"}},
        "importmap": {"vue": "/node_modules/vue.js"},
        "loaded_js": ["https://site.com/main.js"],
        "literal_chunks": ["lazy.deadbeef.js"], "public_path": "/p/",
    }
    urls = m.resolve_all_chunks(loaded, "https://site.com")
    ok("https://site.com/static/js/app.1a2b.js" in urls, "webpack p 路径前缀")
    ok("https://cdn.x.com/assets/remote.js" in urls, "webpack CDN 绝对 p")
    ok("https://site.com/assets/index.9f.js" in urls, "vite manifest .js")
    ok(all(not u.endswith(".css") for u in urls), "css 不计入")
    ok("https://site.com/main.js" in urls, "已加载 src")
    ok("https://site.com/p/lazy.deadbeef.js" in urls, "字面量兜底用 public_path")


# ---- §D API 字面量抽取 ----
def test_api_literals():
    js = r"""
      axios.post('/api/role/add', d); service.get("/api/role/list");
      $service("delete","/api/role/123"); fetch('/api/me');
      const u = `${baseURL}/role/${type}/list`;
      new WebSocket('wss://x.com/notify'); new EventSource('/sse/stream');
      socket.on('role.updated', cb); socket.on('order_created', cb);
      element.addEventListener('click', h); x.on('change', h); y.on('aJ', h); z.on('message', h);
      const q = gql`query Roles { roles { id name } } mutation AddRole { addRole }`;
      new Worker(new URL('./w.js', import.meta.url));
      const b = baseURL; const v = VUE_APP_API;
    """
    e = m.extract_api_literals(js, "chunk-x.js")
    methods = {(r["method"], r["path"]) for r in e["rest"]}
    ok(("POST", "/api/role/add") in methods, "axios.post")
    ok(("GET", "/api/role/list") in methods, "service.get")
    ok(("DELETE", "/api/role/123") in methods, "$service delete")
    ok((None, "/api/me") in methods, "fetch")
    ok(any("/role/" in t for t in e["templates"]), "模板片段")
    ok("wss://x.com/notify" in e["ws"], "websocket")
    ok("/sse/stream" in e["sse"], "sse")
    ok("role.updated" in e["events"], "ws event")
    ok("order_created" in e["events"], "snake event 保留")
    ok(not ({"click", "change", "aJ", "message"} & set(e["events"])), "DOM/压缩/生命周期事件已过滤")
    ok({"type": "query", "name": "Roles"} in e["graphql"], "gql query")
    ok({"type": "mutation", "name": "AddRole"} in e["graphql"], "gql mutation")
    ok(any("w.js" in w for w in e["workers"]), "worker url")
    ok("VUE_APP_API" in e["base_refs"], "base ref")


def test_api_prop_paths():
    """B2: 键→路径映射 / config url 属性(不锚定 axios/request 调用点)。"""
    js = r'''var api={getList:"/api/web/detailReturnCountList",queryBankInfo:"/api/web/queryBankInfo",
             monthlyFill:"/business/statistics/projectMonth",url:"/api/sys/login"};
             var rt={path:"/views/areacode",redirect:"/entrance",icon:"/static/x.png",logo:"/img/a.svg"};'''
    e = m.extract_api_literals(js, "chunk-x.js")
    pp = set(e["prop_paths"])
    ok("/api/web/queryBankInfo" in pp, "api-map 键→路径")
    ok("/business/statistics/projectMonth" in pp, "非 /api 前缀也抓")
    ok("/api/sys/login" in pp, "config url 属性")
    ok("/views/areacode" not in pp, "路由键 path 排除")
    ok("/entrance" not in pp, "路由键 redirect 排除")
    ok("/static/x.png" not in pp and "/img/a.svg" not in pp, "静态资源排除")
    recs = m._api_records(e)
    lows = {r["path_template"] for r in recs if r.get("source") == "url-map"}
    ok("/api/web/queryBankInfo" in lows, "进 api 记录")
    ok(all(r["confidence"] == "low" for r in recs if r.get("source") == "url-map"), "url-map 低置信")


# ---- §E 路由抽取 ----
def test_routes():
    react = r"""jsxs(Route,{path:"/admin",children:[jsxs(Route,{path:"/admin/roles"})]});"""
    rs, lazy = m.extract_routes(react, "react")
    paths = {r["path"] for r in rs}
    ok("/admin" in paths and "/admin/roles" in paths, "react 编译态 Route")
    vue3 = r"""createRouter({history:h,routes:[{path:'/home'},{path:'/user/:id'}]})"""
    rs, _ = m.extract_routes(vue3, "vue3")
    ok("/home" in {r["path"] for r in rs}, "vue3 createRouter")
    ng = r"""RouterModule.forRoot([{path:'dash',loadChildren:()=>import('./d')}])"""
    rs, lazy = m.extract_routes(ng, "angular")
    ok(lazy, "angular loadChildren 检出懒模块")


# ---- §F MPA + ESM ----
def test_mpa_and_esm():
    forms = [{"action": "/User/Edit", "method": "post",
              "inputs": [{"name": "id"}, {"name": "email"}]}]
    eps = m.forms_to_endpoints(forms, "https://s.com/User")
    eq(eps[0]["method"], "POST", "form method")
    eq(eps[0]["path_template"], "/User/Edit", "form action path")
    ok("email" in eps[0]["body_params"], "form inputs→body_params")
    pb = m.postback_targets("__doPostBack('btnSave','');  '/svc/Data.asmx/Get'", "https://s.com/p.aspx")
    ok(any(x.get("note", "").startswith("__doPostBack") for x in pb), "doPostBack")
    ok(any(x["path_template"].endswith("/Get") or ".asmx" in x["path_template"] for x in pb), "asmx handler")
    eq(m.resolve_specifier("./a.js", "https://s.com/js/b.js"), "https://s.com/js/a.js", "相对 specifier")
    eq(m.resolve_specifier("vue", "https://s.com/x.js", {"vue": "/v.js"}), "https://s.com/v.js", "importmap 精确")
    eq(m.resolve_specifier("@org/pkg/x", "https://s.com/x.js", {"@org/pkg/": "/o/"}), "https://s.com/o/x", "importmap 前缀")
    eq(m.resolve_specifier("lodash", "https://s.com/x.js"), None, "裸包名无 importmap→None")


# ---- §3.5 ESM import 说明符抽取 ----
def test_esm_specifiers():
    js = r"""
      import a from './mod-a.js';
      import {b} from "../shared/mod-b.js";
      export {c} from './mod-c.js';
      import './side-effect.js';
      const lazy = () => import('./lazy-chunk.js');
      import x from 'bare-pkg';
    """
    specs = set(m.extract_import_specifiers(js))
    ok("./mod-a.js" in specs, "import-from 相对")
    ok("../shared/mod-b.js" in specs, "命名 import-from")
    ok("./mod-c.js" in specs, "export-from")
    ok("./side-effect.js" in specs, "bare import 副作用")
    ok("./lazy-chunk.js" in specs, "dynamic import()")
    ok("bare-pkg" in specs, "裸包名也抽出(解析阶段再判可达)")


# ---- §3.6 同源站内链接 → 路由候选 ----
def test_same_origin_links():
    links = ["/User/Edit/42", "Detail/7", "https://s.com/Admin/Roles",
             "https://evil.com/x", "#section", "javascript:void(0)",
             "/assets/app.css", "mailto:a@b.c", "/Report?year=2024"]
    out = m.same_origin_links(links, "https://s.com", "https://s.com/User/List")
    ok("/User/Edit/{id}" in out, "数字段归一为 {id}")
    ok("/User/Detail/{id}" in out, "相对链接按页面 URL 解析")
    ok("/Admin/Roles" in out, "同源绝对链接")
    ok(all("evil.com" not in x for x in out), "跨域丢弃")
    ok(all(x not in ("", "/section") for x in out) and "#section" not in str(out), "hash 锚点丢弃")
    ok(all(not x.endswith(".css") for x in out), "静态资源丢弃")
    ok("/Report" in out, "query 链接保留路径")


# ---- §G 声明式资源 ----
def test_declared():
    eq(m.parse_robots("Disallow: /admin\nAllow: /pub\nDisallow: /"), ["/admin", "/pub"], "robots")
    eq(m.parse_sitemap("<url><loc>https://s.com/a</loc></url><loc> https://s.com/b </loc>"),
       ["https://s.com/a", "https://s.com/b"], "sitemap loc")
    spec = {"basePath": "/api", "paths": {"/role/{id}": {
        "get": {"parameters": [{"name": "verbose"}]}, "delete": {}}}}
    eps = m.parse_openapi(spec)
    ids = {(e["method"], e["path_template"]) for e in eps}
    ok(("GET", "/api/role/{id}") in ids and ("DELETE", "/api/role/{id}") in ids, "openapi paths+methods")
    ok(all(e["provenance"] == "spec-declared" for e in eps), "openapi provenance")
    intro = {"data": {"__schema": {"queryType": {"name": "Query"},
             "types": [{"name": "Query", "fields": [{"name": "roles"}, {"name": "users"}]}]}}}
    ops = m.parse_introspection(intro)
    ok({"type": "query", "name": "roles"} in ops, "introspection ops")


# ---- §H merge + coverage ----
def test_merge_coverage():
    g1 = [{"method": "GET", "path_template": "/api/role/list", "provenance": "declared-only", "source_file": "c1.js"}]
    g2 = [{"method": "GET", "path_template": "/api/role/list", "provenance": "observed-only", "called_by_routes": ["r:/roles"]}]
    spec = [{"method": "POST", "path_template": "/api/role/add", "provenance": "spec-declared"}]
    apis = m.merge_apis(g1, g2, spec)
    listapi = next(a for a in apis if a["path_template"] == "/api/role/list")
    eq(listapi["provenance"], "confirmed", "静态有(declared)且动态触发(observed)→confirmed (§5)")
    ok("r:/roles" in listapi["called_by_routes"], "合并 called_by_routes")
    addapi = next(a for a in apis if a["path_template"] == "/api/role/add")
    eq(addapi["provenance"], "spec-declared", "仅 spec 声明、未触发→保留 spec-declared")
    eq(len(apis), 2, "去重后两个端点")
    routes = m.merge_routes(
        [{"path": "/roles", "provenance": "declared-only"}, {"path": "/billing", "provenance": "declared-only"}],
        {"/roles": {"visit_result": "visited"}})
    rmap = {r["path"]: r for r in routes}
    eq(rmap["/roles"]["provenance"], "confirmed", "访问过→confirmed")
    eq(rmap["/billing"]["visit_result"], None, "未触发保留 declared-only")
    cov = m.coverage_summary(routes, apis)
    eq(cov["routes_declared"], 2, "覆盖率路由数")
    eq(cov["visited"], 1, "覆盖率 visited")
    ok("/billing" in str(cov["declared_only_list"]) or "r:/billing" in str(cov["declared_only_list"]), "未触达清单含 billing")


# ---- §I 编排判定(纯) ----
def test_arch_login():
    ok(m.looks_like_login({"hasPwd": True, "bodyLen": 500}), "登录墙判定")
    ok(not m.looks_like_login({"hasPwd": False, "bodyLen": 50000}), "正常页非登录墙")
    eq(m.detect_arch({"spa": {"webpack": True, "vue": True}, "mpa": {}}, "auto"), "spa", "SPA 指纹")
    eq(m.detect_arch({"spa": {}, "mpa": {"forms": 3, "viewstate": True}}, "auto"), "mpa", "MPA 指纹")
    eq(m.detect_arch({"spa": {"webpack": True}, "mpa": {"forms": 2}}, "auto"), "hybrid", "混合指纹")
    eq(m.detect_arch({"spa": {}, "mpa": {}}, "spa"), "spa", "强制 arch")
    # §arch jQuery/MiniUI iframe-portal 信号(0 经典信号也能纠偏到 mpa)
    eq(m.detect_arch({"spa": {}, "mpa": {"miniui": True}}, "auto"), "mpa", "MiniUI 门户→mpa")
    eq(m.detect_arch({"spa": {}, "mpa": {"jquery": True, "iframePortal": True}}, "auto"), "mpa", "jQuery+iframe 门户→mpa")
    eq(m.detect_arch({"spa": {}, "mpa": {"jquery": True}}, "auto"), "spa", "单 jQuery 不误翻→spa")
    eq(m.detect_arch({"spa": {"webpack": True}, "mpa": {"miniui": True}}, "auto"), "hybrid", "MiniUI+webpack→hybrid")
    eq(m.detect_arch({"spa": {}, "mpa": {}}, "auto"), "spa", "无信号默认 spa 不变")


# ---- §C2 sourcemap 还原 ----
def test_sourcemap():
    eq(m.find_sourcemap_url("var a=1;\n//# sourceMappingURL=app.js.map"), "app.js.map", "末尾 smURL")
    eq(m.find_sourcemap_url("no map here"), None, "无 smURL")
    eq(m.parse_sourcemap('{"sourcesContent":["export const x=1","//c2"]}'),
       "export const x=1\n//c2", "sourcesContent 拼接")
    eq(m.parse_sourcemap('{"version":3}'), "", "无 sourcesContent→空")
    import base64 as _b64
    payload = _b64.b64encode(b'{"sourcesContent":["src!"]}').decode()
    eq(m.decode_inline_sourcemap("data:application/json;base64," + payload),
       '{"sourcesContent":["src!"]}', "内联 base64 sourcemap")
    eq(m.decode_inline_sourcemap("app.js.map"), None, "非 data: → None")


# ---- §H response_fields 抽取 ----
def test_response_fields():
    eq(m.extract_response_fields('{"code":0,"msg":"ok","data":[{"id":1,"name":"a"}]}'),
       ["code", "data", "id", "msg", "name"], "顶层 + data 列表下钻")
    eq(m.extract_response_fields('{"result":{"total":3,"page":1}}'),
       ["page", "result", "total"], "result 容器下钻")
    eq(m.extract_response_fields("not json"), [], "非 JSON→空")
    eq(m.extract_response_fields(None), [], "空样本→空")


# ---- §H graphql / realtime 投影 ----
def test_views():
    apis = [
        {"id": "a:POST /graphql", "transport": "graphql", "path_template": "/graphql",
         "operation": {"type": "query", "name": "roles"}},
        {"id": "a:POST /graphql#2", "transport": "graphql", "path_template": "/graphql",
         "operation": {"type": "mutation", "name": "addRole"}},
        {"id": "a:ws:wss://x/notify", "transport": "ws", "path_template": "wss://x/notify",
         "events": ["role.updated"]},
        {"id": "a:sse:/sse", "transport": "sse", "path_template": "/sse"},
        {"id": "a:GET /api/x", "transport": "rest", "path_template": "/api/x"},
    ]
    gv = m.graphql_view(apis)
    eq(len(gv), 1, "单 graphql 端点")
    ok({"type": "query", "name": "roles"} in gv[0]["operations"], "graphql ops 聚合")
    ok({"type": "mutation", "name": "addRole"} in gv[0]["operations"], "graphql mutation 聚合")
    rv = m.realtime_view(apis)
    kinds = {(r["transport"], r["url"]) for r in rv}
    ok(("ws", "wss://x/notify") in kinds and ("sse", "/sse") in kinds, "realtime ws+sse")
    ok(any(r.get("events") == ["role.updated"] for r in rv), "realtime 带 events")


# ---- §H merge 透传新字段 ----
def test_merge_carries_fields():
    static = [{"method": "GET", "path_template": "/api/role/list", "provenance": "declared-only",
               "source_file": "c1.js", "base_ref": "VUE_APP_API"}]
    observed = [{"method": "GET", "path_template": "/api/role/list", "provenance": "observed-only",
                 "statuses": [200], "mime": "application/json", "response_fields": ["code", "data"]}]
    apis = m.merge_apis(static, observed)
    a = apis[0]
    eq(a["statuses"], [200], "merge 保留 statuses")
    eq(a["mime"], "application/json", "merge 保留 mime")
    eq(a["response_fields"], ["code", "data"], "merge 保留 response_fields")
    eq(a["base_ref"], "VUE_APP_API", "merge 保留 base_ref")
    eq(a["source_file"], "c1.js", "merge 提供 schema 兼容 source_file")
    # 空字段不落盘
    bare = m.merge_apis([{"method": "GET", "path_template": "/api/y", "provenance": "declared-only"}])
    ok("statuses" not in bare[0] and "response_fields" not in bare[0], "空集不落盘")
    # 同端点多 graphql operation 不被合并掉
    gql = m.merge_apis([
        {"method": "POST", "path_template": "/graphql", "transport": "graphql",
         "provenance": "declared-only", "operation": {"type": "query", "name": "roles"}},
        {"method": "POST", "path_template": "/graphql", "transport": "graphql",
         "provenance": "declared-only", "operation": {"type": "mutation", "name": "addRole"}},
    ])
    eq(len({a["id"] for a in gql}), 2, "两个 graphql operation 各自保留")
    eq(len(m.graphql_view(gql)[0]["operations"]), 2, "graphql_view 聚合两个 op")


def test_merge_provenance_confirmed():
    """§5 provenance 裁决: 静态(declared/spec)∩ 动态(observed)→ confirmed;单证据态保持。"""
    # spec-declared + observed → confirmed
    a = m.merge_apis([{"method": "POST", "path_template": "/api/x", "provenance": "spec-declared"}],
                     [{"method": "POST", "path_template": "/api/x", "provenance": "observed-only"}])
    eq(a[0]["provenance"], "confirmed", "spec + observed → confirmed")
    # 纯 observed 保持 observed-only
    b = m.merge_apis([{"method": "GET", "path_template": "/api/y", "provenance": "observed-only"}])
    eq(b[0]["provenance"], "observed-only", "仅动态 → observed-only")
    # 纯 declared 保持 declared-only
    c = m.merge_apis([{"method": "GET", "path_template": "/api/z", "provenance": "declared-only"}])
    eq(c[0]["provenance"], "declared-only", "仅静态 → declared-only")


def test_merge_routes_meta():
    static = [{"path": "/admin/Roles", "provenance": "declared-only", "name": "Roles",
               "component": "Roles.vue", "meta": {"roles": ["manager"], "requiresAuth": True}}]
    routes = m.merge_routes(static, {"/admin/Roles": {"visit_result": "visited"}})
    r = routes[0]
    eq(r["name"], "Roles", "保留 name")
    eq(r["component"], "Roles.vue", "保留 component")
    eq(r["access"], "manager", "meta.roles → access")
    eq(r["meta"]["requiresAuth"], True, "保留 meta")
    eq(r["provenance"], "confirmed", "访问过→confirmed")


# ---- §3.3 _api_records: SSE / events / base_ref ----
def test_api_records():
    ex = {"source_file": "c.js", "rest": [{"method": "GET", "path": "/api/x"}],
          "templates": [], "ws": ["wss://h/ws"], "sse": ["/sse/s"],
          "events": ["msg"], "graphql": [], "workers": [], "base_refs": ["API_BASE"]}
    recs = m._api_records(ex)
    transports = {r["transport"] for r in recs}
    ok("sse" in transports, "SSE 记录不再丢")
    ws = next(r for r in recs if r["transport"] == "ws")
    eq(ws["events"], ["msg"], "WS 带 events")
    rest = next(r for r in recs if r["transport"] == "rest")
    eq(rest["base_ref"], "API_BASE", "REST 带 base_ref")


# ---- §6 指纹: framework / version / http_client / subsystems ----
def test_fingerprint():
    eq(m.derive_framework({"vue": 2}, {}, "spa"), "vue2", "vue2 框架")
    eq(m.derive_framework({}, {"spa": {"react": True}}, "spa"), "react", "react 框架")
    eq(m.derive_framework({}, {"spa": {"angular": True}}, "spa"), "angular", "angular 框架")
    eq(m.derive_framework({}, {"mpa": {"exts": ["aspx"]}}, "mpa"), "aspnet", "mpa aspx→aspnet")
    eq(m.derive_framework({}, {"mpa": {"exts": ["php"]}}, "mpa"), "php", "mpa php")
    eq(m.derive_version("vue2", {"fp": {"vue_version": "2.6.14"}}), "2.6.14", "vue version")
    eq(m.derive_version("angular", {"fp": {"ng_version": "16.1.0"}}), "16.1.0", "ng version")
    eq(m.derive_version("react", {"fp": {}}), None, "缺版本→None")
    eq(m.detect_http_client("import axios from 'axios'; axios.get('/x')"), "axios", "axios 客户端")
    eq(m.detect_http_client("require('umi-request')"), "umi-request", "umi-request 客户端")
    eq(m.detect_http_client("fetch('/api/x')"), "fetch", "fetch 兜底")
    eq(m.detect_http_client("const a=1"), None, "无客户端→None")


def test_subsystems():
    subs = m.derive_subsystems([
        {"entry": "https://cdn.x.com/app1/remoteEntry.js", "name": None},
        {"entry": "https://cdn.x.com/app1/remoteEntry.js", "name": None},  # 去重
        {"name": "checkout", "entry": None},
    ])
    eq(len(subs), 2, "去重后两个子系统")
    s0 = next(s for s in subs if s.get("entry"))
    eq(s0["origin"], "https://cdn.x.com", "远程 origin")
    eq(s0["kind"], "federation-remote", "子系统类型")
    ok(any(s.get("name") == "checkout" for s in subs), "命名远程保留")


def test_graphql_endpoints():
    groups = [[{"transport": "graphql", "path_template": "/graphql"}]]
    recorded = [{"url": "https://s.com/api/graphql?x=1"}]
    eps = m.collect_graphql_endpoints(groups, recorded)
    ok("/graphql" in eps and "/api/graphql" in eps, "源码+观测端点合并")


# ---- §4.2 落地分类: visited / blocked / redirected ----
def test_classify_landing():
    eq(m.classify_landing("/admin/roles", "/admin/roles"), "visited", "落地==意图→visited")
    eq(m.classify_landing("/403", "/admin/roles"), "blocked", "守卫 /403→blocked")
    eq(m.classify_landing("/access-denied", "/admin/x"), "blocked", "access-denied→blocked")
    eq(m.classify_landing("/home", "/admin/roles"), "redirected", "跳首页→redirected")
    eq(m.classify_landing("", "/x"), "redirected", "空落地→redirected(非 visited)")


# ---- §4.3 参数路由取实例 ----
def test_param_route():
    ok(m.is_param_template("/user/{id}"), "{id} 为参数模板")
    ok(m.is_param_template("/f/{uuid}/edit"), "{uuid} 为参数模板")
    ok(not m.is_param_template("/user/list"), "无占位非参数模板")
    concrete = {"/user/42", "/user/list", "/order/{id}"}
    eq(m.resolve_param_route("/user/{id}", concrete), "/user/42", "从观测实例取真实 id")
    eq(m.resolve_param_route("/user/{id}/edit", concrete), None, "无匹配实例→None")
    eq(m.resolve_param_route("/user/list", concrete), None, "非参数模板→None")


# ---- §4.3 concrete_same_origin_paths(保留真实 id, 含 hash 路由) ----
def test_concrete_paths():
    links = ["/user/42", "Detail/7", "https://s.com/order/9", "https://evil.com/x",
             "#/role/3", "/assets/a.css", "javascript:void(0)"]
    out = m.concrete_same_origin_paths(links, "https://s.com", "https://s.com/user/list")
    ok("/user/42" in out, "保留真实 id(不归一)")
    ok("/user/Detail/7" in out, "相对链接按页面解析")
    ok("/order/9" in out, "同源绝对链接")
    ok("/role/3" in out, "hash 路由片段")
    ok(all("evil.com" not in x for x in out), "跨域丢弃")
    ok(all(not x.endswith(".css") for x in out), "静态资源丢弃")


# ---- §3.2 federation remoteEntry exposed-map ----
def test_remote_entry():
    js = r'''var moduleMap={"./Button":()=>x,"./pages/Admin":()=>y};
             get:function(m){return moduleMap[m]()};  var o={"./Util":1,"notExposed":2};'''
    ex = m.parse_remote_entry(js)
    ok("./Button" in ex and "./pages/Admin" in ex, "暴露子模块键")
    ok("./Util" in ex, "另一个 exposed 键")
    ok(all(e.startswith("./") for e in ex), "非 ./ 键不算暴露模块")


# ---- §4.5 WS 帧 event 名抽取 ----
def test_ws_event():
    eq(m.ws_event_from_payload('{"type":"role.updated","data":{}}'), "role.updated", "type 键")
    eq(m.ws_event_from_payload('{"event":"ping"}'), "ping", "event 键")
    eq(m.ws_event_from_payload('["notify",{"id":1}]'), "notify", "socket.io 数组帧")
    eq(m.ws_event_from_payload("not json"), None, "非 JSON→None")
    eq(m.ws_event_from_payload(""), None, "空→None")


# ---- 登录检测: 段边界锚定 + 无密码框 SSO 墙 ----
def test_login_detection():
    ok(m._is_login("/login"), "/login 命中")
    ok(m._is_login("https://idp/sso?return=x"), "/sso 命中")
    ok(not m._is_login("/ssorder/list"), "/ssorder 不误命中(段边界)")
    ok(not m._is_login("/lessons"), "/lessons 不误命中")
    ok(m.looks_like_login({"hasPwd": True, "bodyLen": 500}), "有密码框短页→登录墙")
    ok(m.looks_like_login({"hasPwd": False, "bodyLen": 300, "url": "https://idp/oauth2/authorize"}),
       "无密码框但 SSO URL+极短正文→登录墙")
    ok(not m.looks_like_login({"hasPwd": False, "bodyLen": 50000, "url": "https://app/dashboard"}),
       "正常业务页非登录墙")


def test_event_noise_filter():
    # §4.5 events 噪声过滤: DOM/生命周期事件 + 压缩标识符剔除, 真实帧/消息名保留
    keep = ["role.updated", "chat:message", "order_created", "task-done", "notify", "msg"]
    drop = ["click", "change", "message", "open", "close", "error", "scroll", "aJ", "bX2", "Qz"]
    got = m._clean_events(keep + drop)
    eq(sorted(got), sorted(keep), "只留疑似 WS 帧名")
    ok(m._looks_minified_event("aJ") and not m._looks_minified_event("msg"), "压缩标识符判定")
    ok(not m._looks_minified_event("role.updated"), "带分隔符非压缩")


# ---- §4.5 origin 归属 / 第三方噪声过滤 ----
def test_classify_origin():
    eq(m.classify_origin("/api/x", "app.example.com"), "same", "相对路径=本站")
    eq(m.classify_origin("https://app.example.com/api/x", "app.example.com"), "same", "同 host=本站")
    eq(m.classify_origin("https://api.example.com/v1/x", "www.example.com"), "same", "同注册域(兄弟子域)=本站")
    eq(m.classify_origin("https://cdn.x.com/app/remoteEntry.js", "app.example.com", ["cdn.x.com"]), "same", "子系统 host=本站")
    eq(m.classify_origin("https://www.google-analytics.com/g/collect", "app.example.com"), "third-party", "GA=第三方")
    eq(m.classify_origin("https://o123.ingest.sentry.io/api/1/envelope", "app.example.com"), "third-party", "sentry=第三方")
    eq(m.classify_origin("https://connect.facebook.net/x", "app.example.com"), "third-party", "facebook=第三方")
    eq(m.classify_origin("https://api.payments.io/charge", "app.example.com"), "cross", "未知跨域=cross(不误删)")


def test_rest_absolute_url():
    # §3.3: 绝对 URL 端点(前后端分离常见)被静态抽取; classify_origin 据 host 裁决去留与归一
    js = r"""axios.get('https://api.example.com/v1/users');
             fetch('https://www.google-analytics.com/collect');
             axios.post('https://pay.other.io/charge', d);
             service.get('/api/role/list');"""
    e = m.extract_api_literals(js, "c.js")
    paths = {(r["method"], r["path"]) for r in e["rest"]}
    ok(("GET", "https://api.example.com/v1/users") in paths, "绝对 URL REST 端点被抽出")
    ok(("GET", "/api/role/list") in paths, "根相对路径仍抽出")
    recs = m._api_records(e, "www.example.com", set())
    tpls = {r["path_template"] for r in recs}
    ok("/v1/users" in tpls, "同注册域绝对 URL 归一为本站路径")
    ok("/api/role/list" in tpls, "本站相对路径保留")
    ok(all("google-analytics" not in t for t in tpls), "第三方遥测绝对 URL 被剔除")
    cross = [r for r in recs if r.get("cross_origin")]
    ok(any(r["path_template"] == "https://pay.other.io/charge" for r in cross), "独立跨域 API 保留 host 并标 cross_origin")


def test_observed_origin_filter():
    recorded = [
        {"url": "https://app.example.com/api/role/list", "method": "GET", "kind": "rest",
         "status": 200, "mime": "application/json"},
        {"url": "https://www.google-analytics.com/g/collect?v=2", "method": "POST", "kind": "rest",
         "status": 204, "mime": "text/plain"},
        {"url": "https://api.payments.io/charge", "method": "POST", "kind": "rest",
         "status": 200, "mime": "application/json"},
    ]
    filtered = [0]
    obs = m._observed_apis(recorded, "app.example.com", set(), filtered)
    tpls = {o["path_template"] for o in obs}
    ok("/api/role/list" in tpls, "本站观测 API 保留(归一)")
    ok(all("google-analytics" not in t for t in tpls), "第三方遥测观测被剔除")
    eq(filtered[0], 1, "第三方剔除计数")
    ok(any(o.get("cross_origin") and o["path_template"] == "https://api.payments.io/charge" for o in obs),
       "未知跨域观测保留 host+cross_origin, 不伪装本站路径")
    cov = m.coverage_summary([], obs, 0, 0, filtered[0])
    eq(cov["third_party_filtered"], 1, "覆盖率记第三方剔除数")


def test_safe_widgets():
    dom = {"widgets": [
        {"id": "e1", "role": "tab", "cls": "nav-tab", "text": "概览"},
        {"id": "e2", "role": "button", "cls": "btn", "text": "删除"},
        {"id": "e3", "role": "", "cls": "accordion-header", "text": "展开详情"},
        {"id": "e4", "role": "", "cls": "menu-toggle", "text": "保存"},   # 白名单类但含改写词→拦
    ]}
    ids = {w["id"] for w in m.safe_widgets(dom)}
    eq(ids, {"e1", "e3"}, "白名单只放只读 widget, 黑名单词二次拦截")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if _fail:
        print(f"✗ {len(_fail)} 断言失败:\n" + "\n".join(f" - {f}" for f in _fail))
        sys.exit(1)
    print(f"✓ 全部通过: {len(tests)} 组测试")
