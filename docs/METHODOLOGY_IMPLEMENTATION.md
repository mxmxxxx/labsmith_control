# LabSmith Control — 方法实现说明（Methodology）

本文档整理本项目中 **方法（Method）** 部分可引用的技术实现与原理，涵盖硬件控制库、工作流编排、图形化界面及验证与兼容性设计。适用于课程/项目报告中的 *Methods* 或 *System Design* 章节。

---

## 1. 总体架构

系统采用 **分层架构**，自底向上分为四层：

| 层级 | 模块 | 职责 |
|------|------|------|
| 硬件抽象 | `uProcess_x64`（`.pyd`） | 经 COM 与 LabSmith 板卡通信，下发底层命令 |
| 设备与板卡 | `LabsmithBoard`、`CSyringe`、`CManifold` 等 | 连接管理、设备枚举、运动/阀控、事件监听 |
| 逻辑与兼容 | `device_registry.py`、流程步骤迁移函数 | 设备名解析、旧版 JSON 兼容 |
| 表示与交互 | `labsmith_gui.py`（PyQt6） | 手动控制、线性流程设计器、流程图编辑器 |

**设计原则：**

- **UI 与硬件解耦**：设备解析放在 `device_registry`，不依赖 Qt；板卡 API 不依赖 GUI。
- **单一执行入口**：Flow Designer 与 Flow Graph 最终都调用 `_execute_one_flow_step()`，保证“表格式步骤”与“图节点”行为一致。
- **可中断的长时操作**：通过 `poll_hook` 与 `cancel_requested` 在阻塞轮询中协作式取消，避免界面假死。

```
┌─────────────────────────────────────────────────────────┐
│  PyQt6 GUI: Manual │ Flow Designer │ Flow Graph         │
└──────────────────────────┬──────────────────────────────┘
                           │ _execute_one_flow_step / Move / MoveParallel
┌──────────────────────────▼──────────────────────────────┐
│  LabsmithBoard (+ CSyringe, CManifold, …)               │
│  · connected_devices()  · MoveParallel  · MoveWait        │
└──────────────────────────┬──────────────────────────────┘
                           │ CmdSetFlowrate, CmdMoveToVolume, SwitchValves, …
┌──────────────────────────▼──────────────────────────────┐
│  uProcess_x64 / COM                                       │
└───────────────────────────────────────────────────────────┘
```

---

## 2. 硬件连接与设备抽象

### 2.1 连接建立

- 用户选择 **COM 端口**（`pyserial` 枚举或手动输入），GUI 将端口解析为整数索引后构造 `LabsmithBoard(port)`。
- 默认在 **工作线程**（`ConnectWorker` + `QThread`）中连接，避免阻塞主界面；可通过环境变量 `LABSMITH_SYNC_CONNECT=1` 回退到 GUI 线程同步连接（应对部分 COM 驱动的线程亲和性问题）。
- 连接成功后调用 `_populate_device_names()`，从 `board.connected_devices()` 填充下拉框。

### 2.2 已连接设备注册表（`connected_devices`）

`LabsmithBoard.connected_devices()` 返回 **纯字典列表**，每项包含：

- `type`: `"syringe"` 或 `"manifold"`
- `index`: 在 `SPS01` / `C4VM` 数组中的下标
- `addr`: 硬件地址（优先 `add_syr` / `add_man`）
- `name`: 当前逻辑名称（`CSyringe.name` / `CManifold.name`）

该方法 **只读缓存属性**，不额外调用 `GetName()` 等 COM 接口，适合 UI 高频刷新。

### 2.3 设备引用解析（`device_registry.resolve_device_ref`）

工作流 JSON 中可能保存 **当前名称、地址字符串或旧版命名**（如 `SPS01_1`、`C4VM_10`）。解析顺序为：

1. **精确名称匹配**（`matched_via: exact`）
2. **地址字符串匹配**（`matched_via: addr`）
3. **遗留索引回退**（`matched_via: legacy_index`）  
   - `SPS01_N` → 第 `N-1` 个已连接注射泵  
   - `C4VM_N` → 第一个已连接歧管（兼容旧脚本）
4. 无法解析则返回 `None`，运行前校验报错并列出候选设备

运行前 `_normalize_device_refs_for_run()` 可将 `legacy_index` / `addr` 匹配结果 **写回步骤中的名称**，便于用户保存后不再依赖兼容映射。

---

## 3. 注射泵运动控制

### 3.1 单泵运动（`Move`）

对指定 `namedevice` 在 `SPS01` 列表中查找索引，调用 `CSyringe.MoveTo(flowrate, volume)`。`MoveTo` 内部会：

- 检查在线、非堵转、非运动中
- 校验体积/流速相对 `maxVolume`、`minFlowrate`/`maxFlowrate`
- 调用 `CmdSetFlowrate` 与 `CmdMoveToVolume`
- 在 `Updating()` 轮询中等待完成，并周期性调用 `poll_hook`、检查 `cancel_requested`

### 3.2 多泵并行运动（`MoveParallel`）

**应用场景**：Flow Designer / Flow Graph 中一个 **Move syringe** 步骤配置 **2～4 路** 注射泵同时运行（不同流速、体积）。

**原理**：

1. 校验泵数量、名称非空、**同一步骤内名称不重复**。
2. 对每路泵：`UpdateStatus` → 检查在线/完成/非堵转 → `CmdSetFlowrate`（全部泵先设流速）。
3. 再对各泵依次 `CmdMoveToVolume`（带有限次重试），启动位移。
4. **统一轮询循环**：直至所有目标泵 `FlagIsDone` 或超时（默认 2 小时）或用户 `StopBoard` 设置 `cancel_requested`。
5. 循环内调用 `poll_hook()`（通常为 `QApplication.processEvents`），保持 UI 响应。

GUI 侧约定：

- 单泵：调用 `board.Move(name, flow, vol)`。
- 多泵：解析 `step["pumps"]` 为 `(resolved_name, flowrate, volume)` 列表后调用 `board.MoveParallel(moves)`。

硬件与脚本层上限为 **4 路并行**，与 `MoveWait` 及历史 MATLAB 多泵语义一致。

### 3.3 多泵等待脚本（`MoveWait`）— 数据驱动重构

`MoveWait(time, d1, v1, …, d8, v8)` 保留 **对外参数签名**（兼容 `MoveWaitScript.py` 等旧脚本），内部将 `(device, volume)` 对折叠为列表 `pumps = [(index, name, volume), …]`，上限 4 泵。

**重构要点（Phase 1 终端审查）**：

- 用 **单一监听器** `CheckFirstDoneStopPauseWait` 处理 N 泵完成/停止/暂停/倒计时，替代原先按泵数分叉、易漏分支的 `elif` 结构。
- 在发运动命令 **之前** 注册 listener，避免快泵先完成而监听器未就绪的竞态。
- 修复原实现中的 NameError、未加括号的板卡方法引用、`for j in len(...)` 等缺陷（由 `test_data/run_tests.py` 静态检查回归）。

---

## 4. 工作流步骤数据模型

### 4.1 步骤类型

| 类型 | 字段（核心） | 运行时行为 |
|------|----------------|------------|
| `Move syringe` | `pumps[]`: `{syringe, flowrate, volume}` | 1 泵 → `Move`；2–4 泵 → `MoveParallel` |
| `Wait` | `seconds` | `interruptible_sleep`（分段 sleep + `processEvents`） |
| `Switch valves` | `manifold`, `v1`–`v4` | `CManifold.SwitchValves` |
| `Stop board` | （无） | `StopBoard()` |

### 4.2 规范化与向后兼容（`migrate_flow_step_inplace`）

**规范形式**：`Move syringe` 使用 **`pumps` 列表**（最多 4 项）。

**迁移来源**：

- 旧独立类型 `"Move 2 syringes"` → 合并为 `"Move syringe"` 并启用第二泵字段。
- 扁平字段 `syringe` / `flowrate` / `volume` + 可选 `enable_second_syringe` / `syringe_2` … → 构建 `pumps`。
- 已有 `pumps` 数组 → 截断、类型规范化后写回。

**镜像字段**（`sync_legacy_move_syringe_mirror`）：将前两路泵同步到 `syringe`、`syringe_2` 等，使旧版只读前两路的工具仍能识别 JSON。

### 4.3 持久化格式

- **Flow Designer**：`{ "version": 1, "type": "flow_designer", "steps": [ … ] }`
- **Flow Graph**：`{ "version": 1, "type": "flow_graph", "nodes": [ … ], "edges": [ { "src_id", "dst_id" } ] }`

加载时校验 `version`、必填字段、`MAX_FLOW_ITEMS`（1000）上限；`Move syringe` 接受 `pumps` 或遗留扁平字段（经迁移后入表）。

---

## 5. 执行前验证与运行管线

### 5.1 校验层次

1. **结构校验**（Flow Graph）：单链拓扑 — 每节点最多 1 入 1 出、恰好 1 个起点 1 个终点、边数 = 节点数 − 1、无环。
2. **设备引用校验**（`_validate_device_refs`）：名称非空、已连接设备可解析、并行泵名称不重复；遗留索引匹配时产生 **警告** 而非静默通过。
3. **数值校验**（`_validate_steps_for_run`）：`flowrate`/`volume`/`seconds` 为有限正数；拒绝 `bool` 冒充数值；阀位 `v1`–`v4` ∈ {0, 1}。

### 5.2 运行管线（Flow / Graph 共用）

```
连接板卡 → _normalize_device_refs_for_run（可选映射提示）
         → _validate_steps_for_run +（图）_validate_graph_for_run
         → _prepare_board_for_run（安装 poll_hook，清除 cancel 标志）
         → 按序 _execute_one_flow_step（行/节点高亮：运行中/成功/失败/取消）
         → _finalize_board_after_run
```

**单步运行**、**自动填充设备名**（`_autofill_flow_device_refs`）复用同一解析与迁移逻辑。

---

## 6. 图形化界面实现

### 6.1 Flow Designer（线性流程）

- **模型**：`flow_steps: list[dict]`，表格展示序号、类型、摘要。
- **参数编辑**：右侧 `QFormLayout` 按类型动态生成控件；`Move syringe` 支持 **Add/Remove parallel pump**（1～4 路）。
- **摘要列**：`_describe_step()` 显示完整泵参数（含 `||` 连接的多泵说明）。

### 6.2 Flow Graph（可视化流程图）

**画布模型**：

- **节点**：`graph_nodes` 中每项含步骤字典 + `QGraphicsRectItem` + 入/出端口 `NodePortItem` + 边列表 `incoming`/`outgoing`。
- **边**：`ArrowEdge`（三次 Bézier + 箭头）；**仅描边曲线、箭头三角填充**，避免合并路径误填充为“色块”。
- **连线交互**：自右端口拖至左端口；**单链模式**下新边会替换同节点已有入/出边。

**执行顺序**：`_get_graph_execution_order()` 对边做 **拓扑排序**（Kahn 算法），得到唯一线性序；与“仅按创建顺序”不同，顺序由连线拓扑决定。

**布局与可读性**：

- 画布 `QGraphicsView` 在布局中 **stretch=1** 占满中央区域。
- 节点标签使用 **`_graph_node_summary_text()`** 短摘要（多泵显示 `Move syringe (N pumps)`），避免长文本溢出 150×50 矩形。
- 加载图后 `_graph_fit_view()` 自动缩放至包含所有节点；`Ctrl+滚轮` 缩放。

**参数侧栏**：选中节点后在右侧 **Module parameters** 编辑，字段与 Flow Designer 一致（数据仍存于节点字典，与 `flow_steps` 独立）。

### 6.3 手动控制与状态

- 流速/体积：**滑块 + 数值框** 双向绑定；连接后按硬件 `minFlowrate`/`maxFlowrate`/`maxVolume` 钳位。
- 歧管：四路阀位滑块 → `SwitchValves`。
- 可选 **定时刷新** 注射泵/歧管状态（`UpdateStatus`），显示在线、运动、堵转、阀位等。

### 6.4 连接与日志

- **输出日志**：GUI 尾随 `logs/OUTPUT.txt`（与库内 `output_log` 一致）。
- **忙状态**：长流程显示 busy 条；Stop 触发 `StopBoard` → `cancel_requested`，运行循环检测 `_run_cancelled()` 并将当前行/节点标为取消色。

---

## 7. 关键算法与数据结构小结

| 问题 | 方法 |
|------|------|
| 设备名不一致 | 分层解析 + 运行前规范化 |
| 多泵一步 | `pumps[]` → `MoveParallel`（2–4）或 `Move`（1） |
| 旧 JSON | `migrate_flow_step_inplace` + 镜像字段 |
| 图执行顺序 | 有向边拓扑排序 + 单链结构校验 |
| UI 阻塞 | `poll_hook` + `interruptible_sleep` + 后台 Connect |
| 边绘制伪影 | 曲线 `NoBrush`，箭头单独 `fillPath` |

---

## 8. 测试与质量保证

`Python/test_data/run_tests.py` 在无 Qt、无硬件环境下验证：

- **MoveWait** 重构后的源码契约（统一 `pumps` 列表、无遗留四分支）。
- JSON 往返、缺字段跳过、断边检测。
- 校验逻辑（NaN/Inf、bool 拒绝、多泵重复名）。
- 设备解析与演示场景 JSON 完整性。

GUI 相关多为 **源码存在性/模式检查**；核心数值与迁移逻辑与 `labsmith_gui` 中的实现保持平行一致。

---

## 9. 报告撰写建议（可直接引用的表述）

**英文简短版（Methods 段落示例）：**

> We implemented a layered Python control stack on the vendor uProcess COM API, exposing syringe and manifold devices through `LabsmithBoard` and device-specific wrapper classes. Workflow automation is defined as versioned JSON steps executed by a shared interpreter (`_execute_one_flow_step`). Parallel aspiration/dispense on up to four syringes is handled by `MoveParallel`, which sets flow rates, issues volume moves, and polls completion cooperatively with the GUI event loop via `poll_hook` and `cancel_requested`. Device references in saved protocols are resolved through a deterministic registry (exact name, address, legacy index). The PyQt6 front-end provides manual control, a linear flow designer, and a single-chain flow graph editor with topological execution order, pre-run validation, and backward-compatible migration of legacy dual-pump step formats into a canonical `pumps` list.

**中文简短版：**

> 本系统在 uProcess 硬件接口之上实现分层 Python 控制库，并通过 PyQt6 提供手动控制、线性流程设计与单链流程图三种交互方式。实验步骤以 JSON 描述，经统一执行器下发至板卡；多泵并行步骤（最多四路）由 `MoveParallel` 同步设流速、启动体积运动并轮询完成。设备名称通过可复现的解析链兼容新旧命名。流程图按有向边拓扑排序执行，运行前进行结构与数值校验，并支持旧版双泵字段自动迁移为 `pumps` 列表。

---

## 10. 主要源文件索引

| 文件 | 内容 |
|------|------|
| `Python/LabsmithBoard.py` | 连接、Move、MoveParallel、MoveWait、Stop、设备列表 |
| `Python/CSyringe.py` / `CManifold.py` | 单设备命令与状态轮询 |
| `Python/device_registry.py` | 设备引用解析 |
| `Python/labsmith_gui.py` | GUI、流程/图、迁移、校验、执行 |
| `Python/test_data/run_tests.py` | 自动化回归测试 |
| `Python/test_data/*.json` | 示例与演示流程 |

---

*文档版本：与当前代码库一致（含多泵 `pumps` 模型、Flow Graph 布局与边绘制修复）。若实现变更，请同步更新本节与 `README.md`。*
