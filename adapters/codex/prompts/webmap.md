# webmap (Codex 自定义 prompt)

安装:把本文件放到 `~/.codex/prompts/webmap.md`。用户输入 `/webmap <url> [flags]` 即注入本 prompt。

你要对用户给出的 `<url>` 执行 webmap:未知 Web 系统的**架构/功能分析**(路由+API+功能全集 →
一份 Markdown 架构报告)。

## 第一步:读正文并照做

`Read` 本仓库的 `skills/webmap/core/webmap_playbook.md` 并**逐字遵循**(Phase 0 范围/会话 →
Phase 0.5–3 单次机械爬取 → Phase 4 分批标注 → Phase 5 报告 → 只读纪律 → 覆盖率诚实口径)。
正文是 IDE 无关单一真源,本 prompt 只补 Codex 的原语绑定与**沙箱前置**。

## 原语绑定(Codex)

| 契约原语 | Codex 机制 |
|---|---|
| `run` | `shell` 工具 |
| `browser(py)` | `shell` 执行 `browser-harness <<'WEBMAP_EOF' … WEBMAP_EOF` |
| `read` | `shell`(`cat`/`sed -n`)或 Codex 文件读 |
| `write` | `apply_patch` |
| `ask` | 普通对话发问(Codex 无独立结构化提问工具) |

机械爬取整段走**一次** `shell`(daemon 跨调用不保活):

```bash
WEBMAP_URL="<url>" WEBMAP_FLAGS="<flags>" \
  browser-harness < skills/webmap/core/webmap_crawl.py
```

## ★沙箱前置(Codex 的关键摩擦,启动即确认)

Codex 默认 `workspace-write` 沙箱**网络关闭 + 进程隔离**,会同时挡住 browser-harness 的三件事:
连用户已运行的 Chrome(localhost CDP)、`http_get` 拉 chunk(外网)、起 daemon 子进程。

- 跑 webmap **必须**以放行网络+主机访问的审批档启动(`--full-auto`,或等价的
  `danger-full-access` 档 / 对工作目录开 network 的 config)。
- 若浏览器连接失败/超时 → 判定为沙箱受限:**`ask`(对话告知)用户"当前 Codex 沙箱挡住了浏览器连接,
  请以放行网络+主机的审批档重跑",然后停。** 不静默降级、不伪造授权、不绕过沙箱。
- `browser-harness` 须已在用户机 `$PATH`;Codex 不负责安装。

## 接入校验

首次接入跑 `webmap_ADAPTER_DESIGN.md` §5 的 4 步冒烟探针;第 3 步(`browser`→CDP)在受限沙箱下会暴露断网。
