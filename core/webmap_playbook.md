# webmap — 指令正文 (Playbook · IDE 无关单一真源)

> 这是 webmap 在**任何 driver**(Claude Code / Codex / …)下都逐字适用的指令正文。
> 各 adapter 只在它外面贴"触发 + 原语绑定 + 沙箱前置"的薄包装(见 `webmap_ADAPTER_DESIGN.md`)。
> 能力本体规格见 `webmap_DESIGN.md`;本文件是给**执行的大模型**看的操作指令,不是给爬虫实现者看的。
>
> 术语:下文用契约原语 `run / read / write / ask`(定义见 `core/CAPABILITY_CONTRACT.md`);
> `browser(py)` = 用 heredoc 单次调起 browser-harness 跑 `py`。具体绑到哪个原生工具由 adapter 指定。

---

## 0. 一句话目标

AI 控制一个**已登录**的 Chrome(via browser-harness/CDP),把一个**未知**系统的
**全部路由 + 全部 API + 全部功能(含隐藏功能)**摸清楚,讲明"每个东西是干嘛的",
输出**一份清清楚楚的 Markdown 报告**。**唯一主交付 = `ARCHITECTURE_REPORT.md`。** 图谱是可选收尾。

---

## 1. 命令面与参数(所有 driver 统一)

```
/webmap <url>
  --max <N>              路由上限(默认无限;N 仅兜底)
  --budget <sec>         墙钟预算(默认无限;到点优雅收尾并标"预算截断未跑完")
  --arch auto|spa|mpa    架构分支(默认 auto = 指纹判定;可强制)
  --depth passive|active 发现模式(默认 active;与 --arch 正交)
  --chunks all|loaded    静态分析范围(默认 all = 下载全部 chunk)
  --docs on|off          声明式资源主动读取(robots/sitemap/OpenAPI/introspection,默认 on)
  --bodies on|off        响应体捕获(默认 on,截断 4KB)
  --sourcemaps on|off    sourcemap 还原(默认 on)
  --cross-iframe on|off  跨域 iframe 子应用入范围(默认 off;同源 iframe 始终入范围,§3.2)
  --graph on|off         可选:结尾投喂 graphify(默认 off)
  --out ./webmap-out
/webmap query "<question>"   # 开了 --graph 才转交 graphify;否则对报告做检索
```

触发层把用户输入解析成这组 flag,传给 `webmap_crawl.py`(经环境变量/argv)。**解析规则跨 driver 一致**。

---

## 2. Phase 0 — 范围与会话(执行前必做,不可跳)

1. **授权确认**:确认目标 URL + 该系统的只读分析**已获授权**(仅限自有/已授权系统)。
   授权上下文不明 → `ask` 要求用户确认范围,**未确认不动手**。
2. **登录态校验**:`browser("new_tab(START); wait_for_load(); print(page_info())")`。
   若落到登录/SSO 墙 → **停止**,`ask` 提示用户先登录,**绝不输入凭据、不从截图读凭据**。
3. **产出目录**:`--out`(默认 `./webmap-out/`)。只写该目录,**禁止写入本技能自身读取路径的子目录**(防自循环)。

---

## 3. Phase 0.5–3 — 机械爬取(单次调起,不在上下文里手爬)

**整条爬取由 `webmap_crawl.py` 一次跑完**,流水线见 `webmap_DESIGN.md` §1/§3/§4:
架构指纹 → 声明式资源 → STATIC 蓝图(运行时求值枚举全部 chunk)→ DYNAMIC 行为(按 arch 分支遍历 +
全量网络捕获 + 白名单只读发现)→ JS 回灌闭环 → CORRELATE 合并 + 覆盖率。

```
run / browser:  WEBMAP_URL=<url> <flags-as-env>  browser-harness < core/webmap_crawl.py
```

**纪律(横切,见 `webmap_ADAPTER_DESIGN.md` §4):**
- **整段爬取塞进一次 `run`/`browser` 调用**——沙箱型 driver 会在工具调用间回收 daemon 子进程,
  不要把"起 daemon"与"用 daemon"拆成两次调用。
- 调起后若浏览器连接失败/超时 → 多半是沙箱断网/隔离(见 adapter 的沙箱前置):
  `ask` 提示用户改用放行网络+主机的档位重跑,**然后停**,不静默降级。
- 爬虫**机械产出** `webmap.json`(唯一真源)、`network.jsonl`、`js/`;Phase 4/5 才进上下文。

爬取返回后,先 `read` 一眼 `webmap.json` 的 `coverage` 块与 `halted_reason`,确认数据可用、未被
鉴权过期污染(主设计 §4.6);若 `halted_reason` 指示中途停 → 如实带进报告 §9,不假装跑全了。

---

## 4. Phase 4 — ANNOTATE(LLM 在上下文,纯功能,**按模块分批**)

**规模约束:真实 `webmap.json` 可能数百路由 × 数百 API × 每个 4KB 样本,一次塞进上下文会爆。
必须 map-reduce,不许"整文件读进来一把标":**

1. **预聚类(机械,爬虫已切好或此处按前缀切)**:按路径/路由 name 前缀/chunk 邻接把 routes+apis 切成
   N 个候选簇,每簇带自己的子集(每 API 只留**字段名**,不留完整 body)。
2. **逐簇标注(每批只 `read` 一个簇)**:产出该簇的 `modules / purpose / kind / 每路由每 API 一句话`,
   `write`/增量回写 `webmap.json`(用 driver 的 write 原语;Claude Code 用 Edit 增量回写)。
3. **归约**:所有簇标完,再读各簇模块摘要头部(`id/name/purpose/routes`)做一轮全局 `depends_on`
   关联 + 跨模块 `entities` 合并。`--budget` 到点则停在已标注簇,未标注簇标 `annotation: pending`。

每批补:`modules / 每路由 purpose / 每 API kind+一句话 / entities / 数据流`。
- **模块聚类命名**:按 route name/title + 组件名 + API 路径归入命名功能模块(用户与权限/文件中心/报表…),
  每个一句 `purpose` + `confidence` + `depends_on`。
- **每 API 功能**:据 method+path+params+response_fields 定 `kind`(read/create/update/delete/file/
  realtime/query)并写一句。
- **领域模型**:据 response_fields + 路径段推断实体与关系。
- **数据流**:把每个功能串成"路由 → 依次调哪些 API → 读/写什么实体"。

**置信度须如实**:`confirmed`(静态有+动态触发)端点可标"实";`declared-only`/`spec-declared` 写接口
常只有路径 → 标"据路径推断 + `confidence: low`",**不得伪装成已验证**。每个模块/角色结论附证据
(route 名/API 路径/meta)。无 sourcemap 时声明字段名为压缩名、语义为推断。

---

## 5. Phase 5 — EMIT(Markdown 报告为主)

主交付 `ARCHITECTURE_REPORT.md`,`write` 到 `--out`。结构(要有叙事,不止结构树):

1. **概览** — 系统、架构(SPA/MPA/混合)、框架/版本、路由模式、子系统(微前端/iframe)、
   是否拿到 OpenAPI/introspection 规格、本次覆盖率摘要。
2. **模块索引/TOC** — 每模块一行:名称 + 一句用途 + 路由数 + 依赖。给 30 秒全局观。
3. **功能架构树** — 核心;每叶带 `provenance/access/可见性` 标签(路由/组件/API,逐层缩进)。
4. **每功能数据流叙事** — 每个重要功能一段:用户进哪个路由 → 触发哪些接口(顺序)→ 读/写哪个实体 → 结果。
5. **API 全清单** — 按模块分组表(method/path/params/返回字段/用途/provenance);GraphQL operations、
   WS/SSE events 各自成表。
6. **领域模型** — 实体 + 字段 + 关系(尽量给 ASCII 关系图)。
7. **模块依赖** — 谁调用/跳转到谁。
8. **未触达清单(完整性,非攻击面)** — `declared-only` 路由/接口、`blocked`/`param-unresolved`/
   `template(低置信)` 项:"存在但本会话没跑到,值得人工补看"。
9. **方法与局限** — 架构分支与爬取模式、覆盖率数字、规格命中情况、**unknown-unknowns 声明**
   (federation 未初始化远程、blob/eval、SW 缓存等无法计入)、`--budget` 是否截断;
   **主动建议:按不同角色各跑一遍再合并**以逼近系统全集。

**可选 `--graph on`**:写 `raw/00_overview.md … 07_modules.md` 投喂 graphify;**默认关闭,报告不依赖它**。

完成后向用户简洁汇报:输出文件路径 + 覆盖率摘要(routes_declared/visited/declared_only、apis_total
/confirmed/declared_only)+ 一句"本次仅只读架构分析,未改服务端数据"。

---

## 6. 只读纪律(每个 driver 逐字适用,适配层不放松任何一条)

来自 `webmap_DESIGN.md` §11 与 `核心技能文件.md` 铁律:

- 仅自有/已授权系统;先确认授权;**登录墙 → 停,绝不输凭据、不从截图读凭据自行登录**。
- 主动发现**只做** 导航 + 读取 + 安全展开(白名单 widget:tab/accordion/菜单展开/只读分页);
  **改写类控件(提交/保存/删除/上传/重置/支付/弹窗确定)检测后跳过并记 `declared-only`**。
- 响应体截断(4KB)仅用于推断结构/字段,**不批量取数**。
- **不做 DoS、不做大规模/目录爆破扫描、不向第三方回连、不外发真实凭据**。
- 声明式资源(§3.7 主设计)只 GET 约定/被引用路径 + 单次 GraphQL introspection POST,**不字典爆破**。
- **只写 `--out` 目录**,禁止写入本技能自身读取路径的子目录。
- 越界即停:超出授权范围(含跨子系统)立即停手并 `ask`。

---

## 7. 覆盖率的诚实口径(不许 over-claim)

分母是"**本会话 + 当前角色可拉取到的 JS 中声明的**路由/接口",**不是系统绝对全集**。
报告须显式带 unknown-unknowns 声明:未被拉取到的代码(federation 未初始化远程、`blob:`/`eval`、
SW 缓存、跨域 iframe 子应用、仅经自定义 HTTP 封装且未触发的接口)中的路由/接口**无法计入**。
`chunks_fetch_failed` / `doc_fetch_failed` 不静默丢,如实计入并在 §9 披露。
停止规则 = **JS 回灌闭环收敛后,所有已声明路由都已尝试**(非固定计数)。
