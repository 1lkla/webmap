#!/usr/bin/env python3
"""webmap adapter 漂移校验 (build order step 6 / ADAPTER_DESIGN §6).

强制"单一真源"不变量:Phase 0/4/5 与只读纪律只存在于 core/webmap_playbook.md,
adapter 只能 *引用* 正文,不能夹带正文副本。

两种合规模式,逐 adapter 自动判定:
  A. 引用模式(当前参考实现):adapter 文首引用 playbook 路径,且不含正文长句副本。
  B. 内联模式(未来 build step 渲染):adapter 用标记包住 playbook 全文,内容须逐字相等。
       <!-- BEGIN PLAYBOOK INLINE --> ... <!-- END PLAYBOOK INLINE -->

无第三方依赖。用法:python3 core/check_adapters.py   (失败 exit 1)
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # skills/webmap/
PLAYBOOK = ROOT / "core" / "webmap_playbook.md"
CONTRACT = ROOT / "core" / "CAPABILITY_CONTRACT.md"

# 受检 adapter 指令文件(承载触发/绑定的那几个)。
ADAPTER_FILES = [
    ROOT / "adapters" / "claude-code" / "SKILL.md",
    ROOT / "adapters" / "codex" / "prompts" / "webmap.md",
    ROOT / "adapters" / "codex" / "AGENTS.md.fragment",
    ROOT / "adapters" / "_template" / "INSTRUCTION.md",
]

PRIMITIVES = ["run", "read", "write", "ask"]
DUP_MIN_LEN = 40          # 只把"正文长句"算作可疑副本,避免误伤短纪律提醒
DUP_THRESHOLD = 3         # 命中 >=3 条长句 => 判定为夹带正文副本
INLINE_BEGIN = "<!-- BEGIN PLAYBOOK INLINE -->"
INLINE_END = "<!-- END PLAYBOOK INLINE -->"

errors: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def playbook_long_lines(text: str) -> set[str]:
    """正文里足够长、足够独特的行,作为副本指纹。"""
    out = set()
    for raw in text.splitlines():
        s = raw.strip()
        if len(s) >= DUP_MIN_LEN and not s.startswith(("#", "```", "|", ">", "<!--")):
            out.add(s)
    return out


def check_core() -> str | None:
    if not PLAYBOOK.exists():
        fail(f"缺正文真源: {PLAYBOOK.relative_to(ROOT)}")
        return None
    if not CONTRACT.exists():
        fail(f"缺能力契约: {CONTRACT.relative_to(ROOT)}")
    return PLAYBOOK.read_text(encoding="utf-8")


def check_adapter(path: Path, fingerprints: set[str], playbook_text: str) -> None:
    rel = path.relative_to(ROOT)
    if not path.exists():
        fail(f"缺 adapter 文件: {rel}")
        return
    text = path.read_text(encoding="utf-8")

    # 模式 B:内联渲染 —— 标记内容须 == playbook
    if INLINE_BEGIN in text:
        if INLINE_END not in text:
            fail(f"{rel}: 有 BEGIN 内联标记但缺 END")
            return
        inner = text.split(INLINE_BEGIN, 1)[1].split(INLINE_END, 1)[0].strip()
        if inner != playbook_text.strip():
            fail(f"{rel}: 内联正文与 webmap_playbook.md 不一致(漂移)")
        return

    # 模式 A:引用模式
    if "webmap_playbook.md" not in text:
        fail(f"{rel}: 未引用 core/webmap_playbook.md(既不内联也不引用 => 正文来源不明)")

    # 单一真源:不得夹带正文长句副本
    hits = sorted(s for s in fingerprints if s in text)
    if len(hits) >= DUP_THRESHOLD:
        sample = " | ".join(h[:30] + "…" for h in hits[:3])
        fail(f"{rel}: 疑似夹带正文副本({len(hits)} 条长句命中,如:{sample})—— 应引用而非复制")

    # 四原语绑定齐全(在绑定表里出现)
    missing = [p for p in PRIMITIVES if not re.search(rf"`{p}`", text)]
    if missing:
        # AGENTS.md.fragment 是常驻说明、非完整绑定表,降级为 warning
        if path.name == "AGENTS.md.fragment":
            warnings.append(f"{rel}: 未列全原语绑定 {missing}(fragment 可接受)")
        else:
            fail(f"{rel}: 原语绑定表缺 {missing}")


def main() -> int:
    playbook_text = check_core()
    if playbook_text is None:
        report()
        return 1
    fingerprints = playbook_long_lines(playbook_text)
    for f in ADAPTER_FILES:
        check_adapter(f, fingerprints, playbook_text)
    return report()


def report() -> int:
    for w in warnings:
        print(f"WARN  {w}")
    if errors:
        for e in errors:
            print(f"FAIL  {e}")
        print(f"\n✗ 漂移校验未通过:{len(errors)} 项")
        return 1
    print(f"✓ 漂移校验通过:{len(ADAPTER_FILES)} 个 adapter 引用单一真源,无正文副本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
