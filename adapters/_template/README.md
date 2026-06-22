# 新增一个 webmap driver — 只改三处

适配层是薄的:能力本体(`core/webmap_crawl.py`)与指令正文(`core/webmap_playbook.md`)跨 IDE 共享,
新增 driver **只动外围三处**。完整依据见 `../../webmap_ADAPTER_DESIGN.md` §3.4。

1. **触发包装** — 写该 IDE 原生指令文件(slash/prompt/rules 格式),文首 `Read core/webmap_playbook.md and follow it`。
2. **原语绑定表** — 把 `run / read / write / ask` 填到该 IDE 的原生工具,给出 `browser(py)` 的 heredoc 样例。
3. **环境前置核对** — browser-harness 是否在 PATH、是否沙箱断网/隔离、daemon 是否跨调用存活;断网就写明升权 + `ask`-then-stop 回退。

改完跑 `INSTRUCTION.md` 末尾的 4 步冒烟探针,确认四原语 + browser-harness 可达,才算该 driver 就绪。

## 现有 driver 参考

- `../claude-code/SKILL.md` — 基线参考实现(无沙箱坑,最顺)。
- `../codex/` — 沙箱型 driver 的范例(重点看沙箱升权前置)。

## 不变量(别破坏)

- **不复制正文**:Phase 0/4/5 与只读纪律只存在于 `core/webmap_playbook.md`。
- **不放松纪律**:登录墙→停、只读、只写 `--out`、不扫描/DoS,逐字适用每个 driver。
- **产物布局一致**:`webmap-out/{ARCHITECTURE_REPORT.md, webmap.json, network.jsonl, js/, raw/}`,保证跨 IDE 可比对。
- **缺 `run` 不支持**:纯聊天/无终端的 IDE 不在适配范围。
