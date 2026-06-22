# webmap (<IDE 名> 适配) — 通用包装骨架

> 新增一个 driver:复制本文件 → 按下面"只改三处"填 → 跑冒烟探针。其余照抄,**不要重写正文**。

## 第一步:读正文并照做

用本 IDE 的文件读机制 `Read` 仓库内 `skills/webmap/core/webmap_playbook.md` 并**逐字遵循**。
正文(Phase 0/4/5 + 只读纪律 + 覆盖率口径)是 IDE 无关单一真源,本文件只补绑定与前置。

## ① 触发包装(改这里)

用本 IDE 原生机制注册唤起方式,例:
- Cursor:`.cursor/rules/webmap.mdc`(frontmatter `description`/`globs`/手动 `@webmap`)。
- Cline / Continue:`.clinerules` / 自定义命令。
- Windsurf:其 rules 文件。
文首声明 `Read skills/webmap/core/webmap_playbook.md and follow it`。

## ② 原语绑定表(改这里)

| 契约原语 | 本 IDE 工具 | 备注 |
|---|---|---|
| `run` | `<terminal/run command 工具>` | 没有终端工具 → **不支持该 IDE** |
| `browser(py)` | `<上面的 run>` + heredoc | `browser-harness <<'WEBMAP_EOF' … WEBMAP_EOF`,**整段单次跑完** |
| `read` | `<文件读工具>` | |
| `write` | `<编辑/apply 工具>` | 仅写 `--out` 目录 |
| `ask` | `<聊天发问>` | 授权 / 登录墙 / 沙箱升权 |

机械爬取(单次 run):
```bash
WEBMAP_URL="<url>" WEBMAP_FLAGS="<flags>" browser-harness < skills/webmap/core/webmap_crawl.py
```

## ③ 环境前置核对(改这里)

逐项确认并写明:
- [ ] `browser-harness` 在用户机 `$PATH`,能连已登录 Chrome(本 adapter 不负责安装)。
- [ ] 本 IDE 是否沙箱断网/进程隔离?**是** → 写明必须放行网络+主机的档位;连接失败时 `ask` 告知改档重跑并**停**,不静默降级、不绕沙箱。
- [ ] daemon 是否跨工具调用存活?不确定 → 坚持爬取单次 run 跑完。

## 接入校验(不改,照跑)

跑 `webmap_ADAPTER_DESIGN.md` §5 的 4 步冒烟探针:
1. `run("echo webmap-probe && command -v browser-harness")`
2. `write` 一个临时文件 + `read` 回比对
3. `browser("new_tab('about:blank'); print(page_info())")` ← 沙箱断网在此暴露
4. `ask` 一次无害确认

任一步失败 → 该 driver 未就绪,如实报缺口(多半沙箱断网或 browser-harness 未装)。
