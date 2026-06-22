---
name: webmap
description: 未知 Web 系统的全功能架构分析——枚举全部路由 + 全部 API + 全部功能(含隐藏功能),
  说明每项用途,输出一份清楚的 Markdown 架构报告。
  Use when the user asks to map or understand an unknown, already-logged-in web app's
  architecture: enumerate all routes/APIs/features (incl. hidden/role-gated), identify
  Vue/React/Angular/微前端 or server-rendered MPA, and produce an ARCHITECTURE_REPORT.md
  via read-only browser (browser-harness/CDP) recon. Trigger: /webmap <url>
---

# webmap (Claude Code driver)

未知 Web 系统全功能架构分析。这是 webmap 的 **Claude Code 适配**——薄包装,正文共享。
能力本体见 `../../../webmap_DESIGN.md`;适配总览见 `../../../webmap_ADAPTER_DESIGN.md`。

## 原语绑定(Claude Code)

| 契约原语 | Claude Code 工具 |
|---|---|
| `run` | `Bash` |
| `browser(py)` | `Bash` + heredoc(见下) |
| `read` | `Read` |
| `write` | `Write`(首建)/ `Edit`(增量回写 `webmap.json`) |
| `ask` | `AskUserQuestion`(授权确认 / 登录墙) |

`browser(py)` 标准形(**heredoc + 单次跑完**,不要拆成多次 Bash 调用):

```bash
browser-harness <<'WEBMAP_EOF'
new_tab(START); wait_for_load(); print(page_info())
WEBMAP_EOF
```

机械爬取整段也走一次 Bash(daemon 跨调用可能不保活,务必单次):

```bash
WEBMAP_URL="<url>" WEBMAP_FLAGS="--arch auto --docs on ..." \
  browser-harness < /abs/path/skills/webmap/core/webmap_crawl.py
```

## 环境前置(Claude Code)

- `browser-harness` 已在 `$PATH`(全局 CLAUDE.md 已声明),连用户**已登录**的 Chrome。
- 若频繁权限弹窗:在项目 `.claude/settings.local.json` 的 allowlist 加 `Bash(browser-harness:*)`。
- **无沙箱断网问题**——这是基线最顺的 driver。

## 执行指令

**逐字遵循共享正文 `../../core/webmap_playbook.md`**(Phase 0 范围/会话 → Phase 0.5–3 单次机械爬取 →
Phase 4 分批标注 → Phase 5 报告 → 只读纪律 → 覆盖率诚实口径)。本文件只补 Claude Code 的原语绑定与环境前置;
**正文不在此复制**(单一真源,防漂移;`webmap_ADAPTER_DESIGN.md` §2)。

> 实现备注:正式 build step 会把 playbook 全文**内联渲染**进本 SKILL.md 以求完全自包含,并由 CI 校验
> 内联段 == playbook(`webmap_ADAPTER_DESIGN.md` §6 step 6)。在该工具就绪前,参考实现以 `Read` 正文落地。

## 接入校验

首次接入跑 `webmap_ADAPTER_DESIGN.md` §5 的 4 步冒烟探针(run / write+read / browser→CDP / ask)。
