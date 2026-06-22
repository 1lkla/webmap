# webmap

`webmap` 是一个面向已授权、已登录 Web 系统的只读架构测绘技能。它通过 `browser-harness` 控制用户本机已经登录的 Chrome，结合 Chrome DevTools Protocol（CDP）、运行时指纹、静态 JS 分析、动态路由遍历和网络观测，生成目标系统的路由、API、GraphQL、实时通信、子系统和覆盖率清单，并为后续 LLM 标注生成架构报告提供结构化输入。

> 安全边界：webmap 仅适用于自有或已获授权系统；遇到登录墙会停止并要求用户手动登录；不会输入凭据，不会点击提交、保存、删除、支付、上传等改写类控件；不会执行 DoS、爆破、批量取数或越权操作。

## 目标

webmap 的核心目标是把一个未知 Web 系统在当前登录角色下可见的内容尽可能完整地梳理出来：

- 识别系统类型：SPA、MPA 或混合架构。
- 识别前端框架：Vue、React、Angular、ASP.NET/JSP/PHP/Struts 等可见特征。
- 枚举路由：运行时路由、编译态路由、站内链接、iframe 子应用、sitemap/robots 声明路径。
- 枚举 API：REST、GraphQL、WebSocket、SSE、表单提交、ASP.NET postback、OpenAPI/Swagger 声明端点。
- 关联证据：区分静态声明、动态观测、规格声明和已确认接口。
- 输出结构化结果：`webmap.json`、`network.jsonl`、下载的 JS chunk，以及供 LLM 阶段生成的 `ARCHITECTURE_REPORT.md`。

## 项目结构

```text
webmap/
├── README.md
├── .gitignore
├── core/
│   ├── webmap_crawl.py          # 核心机械爬虫与纯函数提取器
│   ├── webmap_playbook.md       # 跨 IDE 共享的执行指令正文
│   ├── CAPABILITY_CONTRACT.md   # driver 必须绑定的 run/read/write/ask 契约
│   ├── test_extractors.py       # 纯函数离线测试
│   ├── test_smoke.py            # main()/编排层离线冒烟测试
│   └── check_adapters.py        # adapter 单一真源/漂移校验
└── adapters/
    ├── claude-code/SKILL.md     # Claude Code 适配层
    ├── codex/                   # Codex prompt 与 AGENTS 片段
    └── _template/               # 新增 driver 的模板
```

## 技术栈

### 语言与运行环境

- **Python 3**：核心实现为单文件 Python 脚本 `core/webmap_crawl.py`。
- **标准库为主**：主要使用 `json`、`os`、`re`、`time`、`base64`、`urllib.parse`，没有项目级第三方 Python 依赖。
- **browser-harness**：作为外部运行时桥接 Chrome/CDP。核心脚本假设 `new_tab`、`js`、`http_get`、`cdp`、`drain_events`、`click_at_xy`、`wait_for_load` 等 helper 由 browser-harness 注入。
- **Chrome DevTools Protocol（CDP）**：用于网络事件捕获、响应体采样、GraphQL introspection、WebSocket 帧观测等。

### 前端识别覆盖

webmap 不依赖目标项目源码，而是从浏览器运行时和下载到的 bundle 中识别：

- Vue 2 / Vue 3
- React / React Router
- Angular / RouterModule
- Webpack chunk/runtime
- Vite manifest / ESM import graph / importmap
- Module Federation remoteEntry
- iframe 子应用
- jQuery / MiniUI / ASP.NET / JSP / PHP / Struts 等 MPA 或门户型特征

### 测试方式

项目当前测试是轻量离线测试：

- `core/test_extractors.py`：覆盖路径归一、chunk 解析、API 抽取、路由抽取、OpenAPI/GraphQL 解析、合并逻辑、噪声过滤等纯函数。
- `core/test_smoke.py`：用打桩 browser helper 跑通 `main()`，验证能生成 `webmap.json`，并守住历史回归点。
- `core/check_adapters.py`：确保 adapter 只引用 `webmap_playbook.md`，不复制正文造成漂移。

运行：

```bash
python3 core/test_extractors.py
python3 core/test_smoke.py
python3 core/check_adapters.py
```

## 工作逻辑

webmap 分为两层：

1. **机械爬取层**：`core/webmap_crawl.py` 一次性在 browser-harness 中执行，生成机器可读事实。
2. **LLM 标注/报告层**：根据 `core/webmap_playbook.md`，分批读取 `webmap.json`，补充功能语义、模块划分、数据流和 Markdown 报告。

### 1. 配置加载

触发层通过环境变量传入：

```bash
WEBMAP_URL="https://target.example.com" \
WEBMAP_FLAGS="--max 200 --budget 600 --arch auto --depth active --chunks all --docs on --bodies on --sourcemaps on --out ./webmap-out" \
browser-harness < core/webmap_crawl.py
```

主要参数：

| 参数 | 含义 |
|---|---|
| `--max` | 路由遍历上限，默认近似无限，主要防止异常膨胀。 |
| `--budget` | 动态遍历阶段墙钟预算。 |
| `--arch` | `auto`、`spa`、`mpa`。 |
| `--depth` | `passive` 或 `active`；active 会点击安全白名单 widget。 |
| `--chunks` | `all` 下载所有可枚举 chunk，`loaded` 只分析已加载脚本。 |
| `--docs` | 是否读取 robots、sitemap、OpenAPI、GraphQL introspection。 |
| `--bodies` | 是否采样 JSON/text 响应体字段，默认截断 4KB。 |
| `--sourcemaps` | 是否尝试读取 sourcemap 并重抽原始源。 |
| `--cross-iframe` | 跨域 iframe 是否纳入范围，默认关闭。 |
| `--out` | 输出目录。 |

### 2. Phase 0：会话与登录墙检查

- 打开目标 URL。
- 检测页面是否为登录墙或 SSO 墙。
- 如果检测到登录态不足，立即停止，不自动登录、不猜测凭据。

### 3. Phase 1：架构指纹与静态发现

核心动作：

- 运行页面 JS 探针识别 SPA/MPA/hybrid。
- 识别 Vue/React/Angular、webpack、importmap、MiniUI、jQuery、服务端后缀等信号。
- 枚举 webpack runtime 的 chunk 文件名。
- 读取已加载 JS、Vite manifest、importmap、modulepreload/prefetch/preload 资源。
- 下载 JS 到 `out/js/`。
- 可选恢复 sourcemap，生成 `*.srcmap.js` 后再次抽取。
- 从 JS 中用正则抽取：
  - REST 调用：axios、fetch、request、service、`$service` 等模式。
  - API 映射对象：`key: "/api/path"`。
  - GraphQL `gql` tag 操作名。
  - WebSocket、SSE、事件名。
  - Worker、ESM import/export/dynamic import。
  - Vue/React/Angular 编译态路由。

### 4. Phase 2：动态遍历与网络观测

根据架构类型选择遍历方式：

- **SPA/hybrid**：使用 Vue Router 或 history/hash 路由切换。
- **MPA**：打开实际 URL，解析站内链接继续入队。
- **参数路由**：先从已见链接中收集真实 ID，再尝试访问真实实例；无法解析则标记 `param-unresolved`。
- **安全主动发现**：仅点击 tab、accordion、collapse、menu-toggle 等白名单只读控件；含保存/删除/提交/支付等文本的控件会跳过。
- **网络捕获**：从 CDP 的 `Network.requestWillBeSent`、`responseReceived`、`loadingFinished`、WebSocket 事件中收集 API、状态码、MIME、响应字段和 WS event。

### 5. Phase 3：回灌闭环与合并

- 动态阶段发现的新脚本会被加入 `seen_scripts`。
- 新脚本继续下载、分析、抽路由/API。
- 最多回灌 6 轮，直到没有新脚本或触发预算/鉴权停止。
- 静态声明、动态观测、OpenAPI/GraphQL 规格结果会合并为统一 `apis`。

API provenance 逻辑：

| 值 | 含义 |
|---|---|
| `declared-only` | 静态 JS 或页面声明发现，但本轮未实际触发。 |
| `observed-only` | 网络动态观测到，但静态未声明。 |
| `spec-declared` | OpenAPI/GraphQL introspection 规格声明。 |
| `confirmed` | 静态/规格声明与动态观测互相印证。 |

### 6. 输出文件

默认输出目录为 `./webmap-out`：

```text
webmap-out/
├── webmap.json          # 结构化主数据源
├── network.jsonl        # 动态网络观测记录
├── js/                  # 下载的 JS chunk 和 sourcemap 还原源
└── ARCHITECTURE_REPORT.md  # LLM 后续标注生成的主报告
```

`webmap.json` 顶层结构：

- `system`：host、start_url、arch、framework、version、router_mode、http_client、subsystems、crawl 参数。
- `routes`：路由清单、访问结果、来源、关联 API。
- `apis`：REST/GraphQL/WS/SSE API 清单。
- `graphql`：GraphQL 端点和 operation 聚合视图。
- `realtime`：WebSocket/SSE 聚合视图。
- `modules` / `entities`：留给 LLM 标注阶段增量补齐。
- `coverage`：覆盖率和失败计数。

## 安装与接入

### Claude Code

Claude Code 适配见：

```text
adapters/claude-code/SKILL.md
```

它将契约原语绑定为：

| 契约原语 | Claude Code 工具 |
|---|---|
| `run` | Bash |
| `browser(py)` | Bash + heredoc 调用 browser-harness |
| `read` | Read |
| `write` | Write / Edit |
| `ask` | AskUserQuestion |

### Codex

Codex 适配见：

```text
adapters/codex/prompts/webmap.md
adapters/codex/AGENTS.md.fragment
```

注意：Codex 默认沙箱可能阻断本机 Chrome/CDP、外网 chunk 下载和 daemon 子进程，通常需要以允许网络和主机访问的档位运行。

### 新增 IDE/driver

参考：

```text
adapters/_template/README.md
adapters/_template/INSTRUCTION.md
```

新增 driver 原则：

- 只改触发包装、原语绑定表、环境前置三处。
- 不复制 `core/webmap_playbook.md` 正文，避免多份指令漂移。
- 缺少终端执行能力的 IDE 不支持 webmap。

## 安全与合规原则

webmap 的设计是偏防御和架构理解的只读测绘，不是攻击扫描器：

- 仅对自有或已授权目标运行。
- 登录墙停止，要求用户自行登录。
- 不输入、读取、外发真实凭据。
- 不进行爆破、字典扫描、压力测试或 DoS。
- 不批量拉取业务数据；响应体仅截断采样用于字段推断。
- 不点击改写类控件。
- 跨域 iframe 默认不纳入范围，除非用户显式开启并确认授权。
- 只写 `--out` 输出目录。

## 优势

1. **运行时视角强**  
   不需要目标源码，直接从已登录浏览器、CDP 网络事件和实际 JS runtime 获取信息，适合黑盒或灰盒架构梳理。

2. **覆盖 SPA/MPA/hybrid**  
   同时考虑现代前端 chunk/路由和传统表单/链接/postback，不局限于单页应用。

3. **静态 + 动态证据合并**  
   不只正则扫 JS，也会动态访问路由并捕获真实请求，可区分 `declared-only` 和 `confirmed`。

4. **安全边界明确**  
   主动发现仅限只读 widget，改写类控件跳过，适合授权安全评估和系统梳理场景。

5. **对前端工程形态覆盖较广**  
   支持 webpack 多 runtime、Vite、importmap、ESM BFS、sourcemap、module federation、iframe 子系统等常见现代形态。

6. **可移植 adapter 设计**  
   通过 `run/read/write/ask` 能力契约和单一 playbook，实现跨 Claude Code、Codex 等不同 IDE/agent 的薄适配。

7. **测试覆盖关键纯函数和编排回归点**  
   当前离线测试覆盖 32 组提取器逻辑，并有 smoke 测试防止 main 入口、js 对象解析和路由 push 回归。

## 局限与缺点

1. **强依赖 browser-harness 和本机 Chrome**  
   没有 browser-harness、Chrome 未运行、CDP 不可达或沙箱隔离时无法完成核心工作。

2. **正则抽取天然不完美**  
   JS 静态分析主要靠正则和启发式规则，面对高度混淆、动态拼接、eval、运行时生成 URL、复杂自定义 HTTP 封装会漏报或低置信误报。

3. **覆盖率不是系统绝对全集**  
   分母是当前登录角色、本次会话能拉到的 JS 和能访问到的路由，不代表所有角色、所有租户、所有隐藏功能。

4. **跨域和微前端存在授权边界**  
   跨域 iframe 默认关闭；module federation 远程未初始化、remoteEntry 拉取失败或 CDN 受限时会漏掉子系统。

5. **动态遍历依赖页面状态**  
   权限、菜单可见性、feature flag、会话过期、数据为空、列表无 ID、前端异常都会影响可到达路由和 confirmed API 数量。

6. **只读策略会牺牲部分覆盖**  
   因为不会提交表单、不会保存/删除/上传，写接口和确认弹窗后的接口多数只能静态发现，无法动态确认。

7. **GraphQL introspection 可能被禁用**  
   如果目标禁用 introspection，只能依赖静态 gql tag 或动态观测，operation 覆盖会下降。

8. **eTLD+1 判断是粗略实现**  
   当前同注册域判断简单取 host 后两段，对 `co.uk` 等公共后缀场景不精确，可能影响 same/cross 分类。

9. **报告生成依赖 LLM 后处理**  
   `webmap_crawl.py` 只产出事实图谱；完整 `ARCHITECTURE_REPORT.md` 需要后续按 playbook 分批标注和归约。

10. **缺少标准包结构和 CI 配置**  
    目前是技能目录形态，不是 pip 包；没有 `pyproject.toml`、GitHub Actions、版本号和发布流程。

## 当前审查结论

该项目是一个设计目标明确、工程上偏实用的 Web 架构测绘技能。它的核心价值在于：用一个已登录浏览器会话把运行时路由、API、chunk、网络请求和声明式规格合并成结构化清单，再交给 LLM 做功能语义标注。

从实现看，项目刻意把纯函数提取器和 browser-harness 编排分开，测试也覆盖了多数关键提取逻辑，这是优点。但它仍然是启发式工具，不是形式化前端编译器或完整爬虫。它适合做授权系统梳理、渗透测试前的信息架构摸底、审计报告素材生成、迁移/接手未知系统时的快速理解；不适合声称“绝对完整枚举所有功能”，也不适合无授权目标或需要高精度源码级调用图的场景。

## 后续改进建议

- 增加 `pyproject.toml`，把测试命令和 Python 版本约束标准化。
- 增加 GitHub Actions，自动运行 `test_extractors.py`、`test_smoke.py`、`check_adapters.py`。
- 增加 JSON Schema，固定 `webmap.json` 输出契约。
- 引入更强的 JS AST 解析能力，减少正则误报/漏报。
- 为公共后缀判断引入 PSL 支持，改善 same/cross origin 分类。
- 为报告阶段提供脚本化分批标注模板，减少 LLM 手工流程差异。
- 增加示例输出目录或脱敏 demo，方便新用户理解产物。
