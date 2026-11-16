# agy-writer

让 Claude Code 把一个边界清楚的**写作任务**（起草、改写、润色、压缩、翻译）交给本机的 Antigravity CLI（`agy`）去做，并为每次运行留下完整记录。做法照搬 `codex-worker`：wrapper + 固定前言 + Claude Code 子代理。Python 3.9+，只用标准库。

默认模型 `gemini-4-hydrogen`（也可用 `gemini-3.8-flash`）。推理强度没有默认值，每个任务按需在 low / medium / high / xhigh 中选择。

## 组成

| 文件 | 作用 |
|---|---|
| `agy_write.py` | wrapper：限制模型和推理强度，控制并发，组装 `agy` 命令，记录每次运行 |
| `preamble.md` | 每个任务前自动加上的写作规则（可在配置里换成自己的） |
| `agent/agy-writer.md` | Claude Code 子代理模板，由 `install.py` 填入本机路径 |
| `install.py` | 把子代理装到 `~/.claude/agents/`（所有项目可用）或某个项目的 `.claude/agents/` |
| `tests/` | 用假的 agy 测试 wrapper，不联网、不耗额度 |

## 安装

```
python3 install.py --check
python3 install.py                  # 或 python3 install.py --project <项目路径>
```

然后重启 Claude Code 或新开会话。之后可以让 Claude 用 `agy-writer` 子代理派写作任务，或直接运行：

```
python3 agy_write.py --name essay-intro --effort medium --prompt-file brief.md
```

## 用法

```
python3 agy_write.py --name <slug> --effort <low|medium|high|xhigh> [--model <id>]
                     [--continue | --resume <conversation_id>] [--add-dir <目录>]...
                     [--with <额外前言名>]... [--timeout <秒>] [--wait <秒>]
                     [--root <目录>] [--prompt-file <文件>]      # 也可以从标准输入传入任务
```

- **模型**：只接受 `allowed_models` 里的模型，第一个是默认值。
- **推理强度**：`--effort` 必须给，没有默认值。参考：`low` 用于校对、改格式、小修小补；`medium` 用于常规起草、改写、翻译、压缩短文；`high` 用于长文、搭论证和结构、风格要求严格的稿子；`xhigh` 用于最难的稿子，比如要综合大量材料的长章节、微妙的论证、最看重质量的终稿润色。总调度没指定时，子代理按简报自己选，并在汇报里说明。
- **模型与强度的对应**：agy 的模型名带强度后缀，wrapper 会传如 `--model gemini-4-hydrogen-xhigh --effort xhigh`。每个模型能用哪几档写在 `model_efforts` 里：hydrogen 有 low 到 xhigh 四档，flash 只有 low 到 high 三档，选了没有的档会报错（退出码 2）。
- **权限**：`--mode accept-edits` 加 `--sandbox`。agy 可以读写文件、可以联网搜索（`search_web`），但无头模式下**shell 命令一律被拒**，除非你在 agy 自己的 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 里放行。被拒的操作记在 `meta.json` 的 `denied_actions`。
- **任务怎么传**：任务通过标准输入以一条 stream-json 消息交给 agy（`{"event":"user","message":{"content":...}}`），没有命令行长度限制。
- **继续写**：`--continue` 接着同一个 `--name` 最近一次的对话（记在 `<runs_dir>/sessions.json`），适合“按意见改第二稿”；`--resume <id>` 接指定对话。
- **工作目录外的文件**：用 `--add-dir` 加进来。
- **退出码**：一般与 agy 相同；agy 退出码为 0 但结果不是 `SUCCESS` 时返回 1；`2` 是参数或规则错误；`75` 是并发名额已满，可加 `--wait 600`。

## 配置

三层，后者覆盖前者：内置默认值 → `~/.agy-writer/config.json` → `<根目录>/.agy-writer.json`。

```json
{
  "allowed_models": ["gemini-4-hydrogen", "gemini-3.8-flash"],
  "model_efforts": {
    "gemini-4-hydrogen": ["low", "medium", "high", "xhigh"],
    "gemini-3.8-flash": ["low", "medium", "high"]
  },
  "max_concurrent": 4,
  "mode": "accept-edits",
  "sandbox": true,
  "timeout": 1800,
  "agy_bin": "",
  "runs_dir": ".agy_runs",
  "preamble": "",
  "extra_preambles": {"style": "STYLE.md"},
  "lock_dir": "",
  "track_changes": true,
  "max_scan_files": 20000,
  "exclude_from_changes": [".git/", "node_modules/", ".venv/", "__pycache__/"]
}
```

每次运行的记录在 `<runs_dir>/<UTC时间>_<name>/`：`prompt.md`、`stdin.jsonl`、`command.txt`、`events.jsonl`、`last_message.md`、`stderr.log`、`meta.json`。`meta.json` 记录模型、强度、状态、token 用量、对话 id、agy 写过的文件（`files_written_by_agy`，来自事件流）、运行前后有变化的文件（`changed_files`，只比较大小和修改时间；文件数超过 `max_scan_files` 时为 `null`）、被拒的操作和工具错误。

## 风格

agy 每次运行从零开始。把固定的风格要求写进根目录的 `AGENTS.md`（前言要求它遵守），或写成 `STYLE.md` 再在配置里登记为 `extra_preambles`，用 `--with style` 加进任务。

## 测试

```
python3 -m unittest discover -s tests -v
```

7 项测试在 macOS（Python 3.14）上通过；用真实的 agy 1.5.9 跑通过冒烟测试：gemini-3.8-flash-high 写 150 字散文（约 75 秒），gemini-4-hydrogen-xhigh 精简一句话（约 16 秒）。Windows 未验证。
