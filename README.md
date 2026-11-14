# 空想增量（Kongxiang Incremental）插件

在 N.E.K.O 插件面板里玩《空想增量》。

## 结构

```
kongxiang_incremental/
├── plugin.toml          # 插件清单（SDK 0.1.x）
├── __init__.py          # 插件后端：静态 UI + 状态/动作中转 + 两个 llm_tool
└── static/              # 游戏本体（index.html / src / css / dist）
```

## 工作原理（MVP）

- `register_static_ui("static")` 把 `static/index.html` 挂到
  `/plugin/kongxiang_incremental/ui/`；
- 游戏页的 `src/neko/plugin-bridge.js` 每 3 秒把 `nekoSnapshotForHost(state)`
  通过 `POST /runs` 同步给插件的 `sync_state`；
- 猫咪通过 `kongxiang_state` 读快照，通过 `kongxiang_action` 排动作；
- 游戏页每 2 秒 `poll_actions` 领取动作并执行，再 `report_result` 回执。

## 开发

```bash
uv run neko-plugin check kongxiang_incremental
```

完整插件市场发布流程见 N.E.K.O 仓库 `docs/zh-CN/plugins/cli.md`。

## License

MIT（见 `LICENSE`）。`static/` 内含《空想增量》游戏本体，同样以 MIT 发布；
其第三方库 `dist/break_eternity.js` 遵循其原始许可。

## 实测记录（2026-11-14）

- 插件 UI：`/plugin/kongxiang_incremental/ui/` 200；main.js / css / esm MIME 正确。
- 动作闭环 A（手动）：`queue_action` → `poll_actions` → 页面执行（计数频率 → 8 级）→ `report_result`。
- 动作闭环 B（真模型）：原生聊天「帮我升一下计数频率」→ Main 调用 `kongxiang_action`
  （11:25:14 回调 HTTP 200）→ 插件队列 → 页面执行 → `results=2`，
  页面快照 `multipliers.countFreq = 1.6`（= 8 级 × 1.075）。
- 结论：聊天 → 模型调工具 → 插件排队 → 页面执行 → 回执，全链路打通。
