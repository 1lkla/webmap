# webmap — 能力契约 (Capability Contract)

给 adapter 实现者:每个目标 IDE 必须把下面 4 个原语绑定到它的原生工具。webmap 只用这几个。
设计依据见 `webmap_ADAPTER_DESIGN.md` §1。

## 原语

| 原语 | 签名 | 语义 | 失败处理 |
|---|---|---|---|
| `run` | `run(cmd, *, timeout) -> {stdout, stderr, exit}` | 执行一条 shell 命令(承载 browser-harness) | 非 0 退出/超时:读 stderr,按场景 `ask` 或如实报错 |
| `read` | `read(path) -> text` | 读本地文件 | 文件缺失:报明,不臆造内容 |
| `write` | `write(path, text) -> void` | 新建/覆盖本地文件(自建父目录) | 仅允许写 `--out` 目录;越界拒绝 |
| `ask` | `ask(question) -> answer` | 人在环:授权确认 / 登录墙 / 沙箱升权 / 越界确认 | 无应答即停,不擅自继续 |

## 派生便捷式(非独立原语)

```
browser(py) ≡ run("browser-harness <<'WEBMAP_EOF'\n" + py + "\nWEBMAP_EOF")
```

两条硬约束,adapter 文档必须给显式样例:
1. **必须用 heredoc**(防 shell 引号污染内嵌 JS/Python 串)。
2. **整段一次跑完**——沙箱型 driver 在工具调用间回收 daemon 子进程,不可拆成两次调用
   (见 `webmap_ADAPTER_DESIGN.md` §4.1)。

## 阶段 → 原语映射

| Phase | 原语 |
|---|---|
| 0 范围/会话 | `ask`, `browser`(探页) |
| 0.5–3 机械爬取 | **单次 `run`/`browser`**(`browser-harness < core/webmap_crawl.py`) |
| 4 标注(LLM,分批) | `read` + `write` |
| 5 报告(LLM) | `read` + `write` |
| query | `read`(开 `--graph` 才转交 graphify) |

## 接入校验

绑定完成后,跑 `webmap_ADAPTER_DESIGN.md` §5 的 4 步冒烟探针;任一步失败 → 该 driver 未就绪。
缺 `run`(纯聊天/无终端)→ 不在适配范围。
