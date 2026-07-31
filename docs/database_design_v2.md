# CVLab SQLite 数据库设计 · v2

两条设计哲学：**让数据自己 reveal 实际关系**（关系靠外键链条体现，不冗余存）；**尽可能精简简明**（只记录事实，不记录可推导或暂时不需要的东西）。

## v1 → v2 改动

背景：电脑 A（Opentrons）通过 HTTP 指挥电脑 B（Dobot + EmStat4X）；dispense、pH、电化学代码同一个 repo，git 同步；**项目通过读 dispense 代码得知每个孔里有什么**。

1. **新增 `stock_solution` 表**：stock rack 上每个位置放的是什么。dispense 代码只写"从哪个 stock 孔取多少 µL"，浓度换算靠它。模拟器"选 stock 在哪"改的就是这张表对应的配置。
2. **`well_state` 增加来源字段**（source_type / source_protocol / source_git_hash）：state 由解析 dispense 代码自动生成时，记录是哪份协议、哪个 git 版本算出来的。手动登记走 source_type=manual，version 0 unknown 规则保留。
3. **解析约定**：dispense 协议里配方必须写成声明式（每孔体积 dict + stock 映射），OT-2 的 `run()` 和 parser 读同一份数据。不做通用 Python 代码解析。
4. **`ph_test.session_id` 可空**：pH 由 A 在 B 的 session 之外执行，靠 state_id 挂链。
5. **`session` 加 `created_via`**（gui / remote_api）：区分人在 GUI 点的还是 A 远程发起的。
6. **单一写者规则**：SQLite 文件只在 B 上、只有 B 写。A 产生的数据（well_state、ph_test、probe_calibration）通过 B 的 API 提交入库。
7. **模拟器不入库**：dispense 模拟属于工具，只有真实执行产生的 state 写表。
8. **新增 `artifact` 表（内容进库，防误删）**：raw.txt / data.csv / script.mscr 的文件
   内容本体存进库（kind 为值，CV/CA/LSV 不分表；OCP 经 ocp_id 同表）。cvlab.sqlite
   由此自足：磁盘文件全丢可用 `restore_artifacts()` 完整重建，parquet 是可由库内
   csv 重新生成的缓存。字段：artifact_id / measurement_id⊕ocp_id / kind(raw,csv,
   mscript) / filename / content(BLOB) / sha256 / created_at。

## v1 定稿改动（保留）

1. `well_state` 挂 `well_id + experiment_id`，version = 该孔第几个状态，跨 session 连续；version 0 = composition unknown。
2. `measurement` 无 background_measurement_id（baseline 以 `analysis_input` 为准）；`ocp_id` 可空。
3. `wash` 一行 = 一次 attempt；not_clean 按流程重试，用尽才停 session。
4. 只有一支 pH probe，不建 probe 表。
5. `cv_window_result`（CV 缩圈）、结果表字段对齐 find_peak / is_clean 实际输出（e_sd_apex_v、pon、三态 selection_source、带符号 mV 偏差、e_half_spread_mv、residual_in_sample/general）。
6. `analysis.is_current` 唯一范围 =（目标, analysis_type），partial unique index 实现。

## 跨机器架构

```
电脑 A（Opentrons）                电脑 B（CV station）
├── dispense 协议（声明式配方）      ├── Dobot + EmStat4X + GUI (Flask)
├── dispense 模拟器（工具，不入库）   ├── SQLite（唯一写者）
├── pH 模块（probe 串口在 A）        └── API：start_batch / status /
└── master 脚本：dispense → pH →         submit_state / submit_ph / submit_cal
    POST 给 B 测电化学 → 轮询 status
同一个 repo，git 同步；A 只开一个 VSCode。
```

配液信息流：dispense 配方（体积 dict）+ `stock_solution`（stock 孔 → chemical + 浓度）→ parser 算出每孔最终组成 → 经 API 写成 `well_state` + `well_state_component`。

---

## 第一层：物理与化学资产

只记录物理对象本身，不记录它在某次实验中的角色、内容或历史。

### rack

两个物理 rack：sample rack 和 rinse rack。

| **字段** | **意思** |
| --- | --- |
| `rack_id` | 这一个 rack 的唯一编号 |
| `name` | 人能看懂的名字 |
| `position_key` | 对应 `position.json` 里的 key |
| `description` | 备注（建议是 design 的 version，或实际 Fusion 360 设计稿名字） |
| `active` | 是否仍在使用 |

**暂时不记录**：某次 session 中被用作 sample 还是 rinse；里面当前有什么液体；当前属于哪个 run。

### well

rack 上的 A1–B4。

| **字段** | **意思** |
| --- | --- |
| `well_id` | 物理孔的唯一编号 |
| `rack_id` | 它属于哪个 rack |
| `position_name` | A1、A2、B4 |
| `default_role` | 默认职责，例如 sample、standard、rinse |
| `active` | 是否可用 |
| `notes` | 备注 |

**暂时不记录**：analyte、浓度、pH、体积、当前 session 的实际 role（这些属于 well_state）。

### station

不属于 rack 的独立机器人目标位置。目前暂定：wash station of CV、wash station of pH probe、pH probe station。

| **字段** | **意思** |
| --- | --- |
| `station_id` | 唯一编号 |
| `name` | 人能理解的名称 |
| `position_key` | `position.json` key |
| `station_type` | wash、safe、approach、parking 等 |
| `active` | 是否仍使用 |
| `notes` | 备注 |

**暂时不记录**：某次 wash 做了多久、哪次 event 去了这里、实际运动轨迹。

### electrode

真实存在、跨 session 使用的 WE / RE / CE。

| **字段** | **意思** |
| --- | --- |
| `electrode_id` | 唯一编号 |
| `name` | 人能识别的名称 |
| `electrode_type` | working / reference / counter |
| `material` | glassy carbon、Pt、Ag/AgCl |
| `diameter_mm` | 直径 |
| `area_cm2` | 面积 |
| `reference_fill_chemical` | RE 内部填充液 |
| `reference_fill_concentration_m` | 填充液浓度 |
| `serial_number` | 有的话记录 |
| `active` | 是否仍在使用 |
| `notes` | 备注 |

**暂时不记录**：哪个 session 用了它、polish 时间、使用次数、是否 clean（前两者查 session.electrode_setup_id 和 electrode_maintenance）。

### electrode_setup

一套可重复使用的电极配置。一行 = 一套完整配置，不专属于某一套实验。

| **字段** | **描述** |
| --- | --- |
| `setup_id` | 配置编号 |
| `setup_name` | 配置名称 |
| `working_electrode` | Working electrode ID |
| `reference_electrode` | Reference electrode ID |
| `counter_electrode` | Counter electrode ID |
| `active` | 是否当前默认配置 |
| `notes` | 备注 |

### chemical

实验室中长期存在的化学物质。一行 = 一种化学品；不属于某个 session 或某个孔。

| **字段** | **描述** |
| --- | --- |
| `chemical_id` | 化学品唯一编号 |
| `name` | 名称 |
| `formula` | 化学式 |
| `storage_location` | 在 Greenway group lab 里的储存位置 |
| `received_at` | 进入 lab 的时间 |
| `notes` | 备注 |

### stock_solution（v2 新增）

Opentrons stock rack 上某个位置、某段时间内放的一种母液。一行 = 一次摆放/配制。dispense 代码只记录"从哪个 stock 位取多少 µL"；从体积换算到每孔最终浓度，靠这张表。换母液或挪位置就写新行、旧行 active=0——模拟器里"选 stock solution 在哪"对应的就是这张表的当前 active 内容。

| **字段** | **描述** |
| --- | --- |
| `stock_id` | 唯一编号 |
| `deck_slot` | 在 Opentrons deck 的哪个 slot（例如 5） |
| `well_name` | labware 上的位置（A1、B2…） |
| `chemical_id` | 是哪种化学品 |
| `concentration_m` | 母液浓度 |
| `solvent_chemical_id` | 溶剂（一般是水），可空 |
| `prepared_at` | 配制/摆放时间 |
| `active` | 是否当前在位 |
| `notes` | 备注 |

### probe_calibration

只有一支 pH probe，因此不建 probe 表，calibration_id 即可唯一定位。校准在电脑 A 上执行，经 API 入库。

| **字段** | **描述** |
| --- | --- |
| `calibration_id` | Calibration 唯一编号 |
| `calibrated_at` | 校准时间 |
| `buffer_1_ph` | 第一个 buffer 的 pH，目前 4.01 |
| `buffer_1_voltage` | 第一个 buffer 的探头读数 |
| `buffer_2_ph` | 第二个 buffer 的 pH，目前 9.21 |
| `buffer_2_voltage` | 第二个 buffer 的探头读数 |
| `conversion_slope` | 计算得到的 m |
| `conversion_intercept` | 计算得到的 c |
| `status` | complete / failed |
| `notes` | 备注 |

---

## 第二层：执行流

### experiment

一批在人的实验逻辑中属于同一个整体的测试。一个 experiment 可以包含多个 session；也是 A 发起的一整套 dispense → pH → 电化学流程的容器。

| **字段** | **描述** |
| --- | --- |
| `experiment_id` | Experiment 唯一编号 |
| `experiment_name` | 人可以理解的实验名称 |
| `created_at` | Experiment 建立时间 |
| `started_at` | 第一条 session 开始的时间 |
| `ended_at` | 整个 experiment 被认为结束的时间 |
| `status` | active / completed / abandoned |
| `notes` | 备注 |

### session

一次通过 GUI 或远程 API 建立并执行的连续自动运行（电脑 B 上）。一行 = 一个 session。

结束条件：用户取消或停止；设备或机器人报错；clean check 重试用尽、需人工处理；更换 electrode setup；自动执行被其他原因中断。之后再次 Add Run（或 A 再次 POST）创建新 session，可属于原 experiment。

| **字段** | **描述** |
| --- | --- |
| `session_id` | Session 唯一编号 |
| `experiment_id` | 该 session 属于哪个 experiment |
| `session_name` | 人可以理解的名称 |
| `created_via` | gui / remote_api（v2 新增：人点的还是 A 发起的） |
| `created_at` | 建立 session 的时间 |
| `started_at` | 自动执行实际开始时间 |
| `ended_at` | 自动执行完成或中断时间 |
| `status` | Session 最终状态 |
| `end_reason` | Session 为什么结束 |
| `electrode_setup_id` | 本 session 使用的电极配置 |
| `notes` | 备注 |

**status**：`planned` / `running` / `completed` / `cancelled` / `stopped` / `failed`。

**end_reason**：`normal_completion` / `user_cancelled` / `electrode_maintenance_required` / `clean_check_failed` / `device_error` / `robot_error` / `connection_lost` / `electrode_setup_changed` / `unknown`。

### session_step

一个 session 中正式建立的、有明确顺序的执行步骤。一行 = 一项计划操作。可组织 pH test（若纳入 B 的时间线）、OCP check、formal measurement、wash 等。

| **字段** | **描述** |
| --- | --- |
| `step_id` | 这一条计划步骤的唯一编号 |
| `session_id` | 属于哪个 session |
| `step_order` | 在该 session 中的顺序，从 1 开始 |
| `step_type` | ph_test / ocp_check / measurement / wash 等 |
| `ph_test_id` | 该步骤对应 pH test 时填写 |
| `ocp_id` | 该步骤对应 OCP check 时填写 |
| `measurement_id` | 该步骤对应正式 measurement 时填写 |
| `wash_id` | 该步骤对应 wash 时填写 |
| `step_status` | 这项计划步骤最终是否执行 |
| `created_at` | 创建该步骤的时间 |
| `started_at` | 该步骤实际开始时间 |
| `ended_at` | 该步骤实际结束时间 |
| `skip_reason` | 未执行或跳过的原因：手动或自动 |
| `data_path` | 该步骤数据目录（有则填） |
| `notes` | 备注 |

**step_status**：`planned` / `started` / `completed`（结束，不代表科学上成功）/ `cancelled` / `skipped` / `blocked`。

**多扫速 bundle 的表现**：bundle 不单独占 step。BND001 含三个扫速时：

```
Step 1  OCP001
Step 2  M001  20 mV/s
Step 3  OCP002
Step 4  M002  50 mV/s
Step 5  OCP003
Step 6  M003  100 mV/s
Step 7  WSH001
```

M001–M003 与 WSH001 的 bundle_id = BND001。`session_step` 负责顺序，`measurement_bundle` 负责科学分组。搅拌目前不建 step。

### measurement_bundle

由 GUI/API 明确创建、视为整体的一组正式 measurement。目前用途是多扫速 CV：每个扫速一条独立 measurement、各有 OCP，靠 bundle_id 归组。不是通用分组。

| **字段** | **描述** |
| --- | --- |
| `bundle_id` | Bundle 唯一编号 |
| `session_id` | Bundle 属于哪个 session |
| `bundle_type` | Bundle 类型，目前例如 multi_rate_cv |
| `created_at` | Bundle 建立时间 |
| `notes` | 备注 |

---

## 第三层：过程记录

### well_state

某个孔在某次配液完成之后形成的完整化学体系。孔内组成变化后、下一次测试前建新 state；组成不变则沿用。挂 `well_id + experiment_id`，`version` 是该孔的第几个状态（跨 session 连续），version 0 = composition unknown。

**v2 新增来源字段**：state 正常情况下由解析 dispense 协议自动生成（配方 dict + stock_solution → 每孔最终组成），来源三字段记录可追溯性。手动登记时 source_type=manual。

| **字段** | **描述** |
| --- | --- |
| `state_id` | Well state 唯一编号 |
| `experiment_id` | 属于哪个 experiment |
| `well_id` | 属于哪个物理孔 |
| `version` | 该孔的第几个状态（0 = composition unknown） |
| `total_volume_ml` | 当前总体积 |
| `source_type` | opentrons_protocol / manual（v2） |
| `source_protocol` | dispense 协议文件名/路径（v2，manual 时为空） |
| `source_git_hash` | 该协议所在 repo 的 git commit（v2，manual 时为空） |
| `created_at` | 该状态被冻结并开始用于测试的时间 |
| `ended_at` | 下一次孔内组成发生变化的时间 |
| `notes` | 备注 |

### well_state_component

一行 = 一种 chemical 在一个已冻结、即将接受测试的 well state 中的状态。自动生成时由 parser 从"体积 × stock 浓度 ÷ 总体积"算出。

| **字段** | **描述** |
| --- | --- |
| `state_component_id` | Component 记录唯一编号 |
| `state_id` | 属于哪个 well state |
| `chemical_id` | 此时孔内存在的 chemical |
| `component_role` | analyte / supporting_electrolyte / solvent / acid / base / other |
| `concentration_m` | 该 chemical 在完整体系中的最终浓度 |
| `solubility_status` | **仅 analyte 填写**：analyte 溶在整个体系的 solubility |
| `notes` | 备注 |

### ocp_check

每次正式测量前的 OCP 稳定性检查（不算正式测量）。

| **字段** | **描述** |
| --- | --- |
| `ocp_id` | OCP 检查编号 |
| `session_id` | 属于哪个 session |
| `state_id` | 检查的是哪个 well state |
| `started_at` | OCP 开始时间 |
| `ended_at` | OCP 结束时间 |
| `elapsed_s` | 实际等待总时长 |
| `final_ocp_v` | 最终 OCP |
| `slope_v_s` | 最终稳定性斜率 |
| `residual` | 稳定性残差 |
| `settling_status` | settled / timeout_unsettled / stopped / failed |
| `proceed_to_measurement` | 最终是否继续对应正式测量 |
| `data_path` | 数据目录 |
| `notes` | 备注 |

超过 120 s 仍继续约定测量 → `timeout_unsettled`；人工取消 → `stopped`；设备异常 → `failed`。

### measurement

一次正式的实验测试（CV、CA、LSV 等）。不含 OCP 前置检查、clean check、wash、GUI 暂时状态。只记录最终可确认的数据采集事实。`ocp_id` 可空（baseline CV、部分 adhoc 无前置 OCP）。分析实际用的 baseline 记录在 `analysis_input`。

| **字段** | **描述** |
| --- | --- |
| `measurement_id` | 正式测试唯一编号 |
| `session_id` | 属于哪个 session |
| `state_id` | 测量哪个 well state |
| `ocp_id` | 本次测量前对应的 OCP；无则为空 |
| `bundle_id` | 所属多扫速 bundle；普通测试为空 |
| `technique` | CV / CA / LSV 等 |
| `parameters_json` | 本次实际使用的测试参数 |
| `role` | sample 测量或 baseline 采集 |
| `acquisition_status` | not_started / partial / complete / failed / unknown |
| `end_reason` | 测试正常结束、取消或异常的具体原因 |
| `created_at` | 正式测试记录建立时间 |
| `started_at` | 正式采集开始时间 |
| `ended_at` | 正式采集完成或终止时间 |
| `data_path` | 数据文件所在目录 |
| `notes` | 备注 |

### wash

正式测试或整个 bundle 完成后的清洗 + clean-check 判定。一行 = 一次 attempt（wash → clean check → 判定）。not_clean 按流程重试（可含人工调整 standard 扫描区间），重试用尽才停 session；不改变前面 measurement 的状态。多扫速中间不 wash，bundle 完成后一条。`measurement_id` 与 `bundle_id` 必须且只能填一个。

| **字段** | **描述** |
| --- | --- |
| `wash_id` | Wash 唯一编号 |
| `session_id` | 属于哪个 session |
| `attempt` | 同一目标之后的第几次 attempt，从 1 开始 |
| `baseline_id` | 对比判定用 baseline 的 measurement_id |
| `measurement_id` | 普通正式测试之后的 wash 时填写 |
| `bundle_id` | 多扫速 bundle 之后的 wash 时填写 |
| `started_at` | 清洗开始时间 |
| `ended_at` | Clean check 及判定完成时间 |
| `wash_protocol` | 使用的清洗流程或配置 |
| `cleancheck_parameters_json` | Clean-check CV 实际使用的参数 |
| `acquisition_status` | Clean-check CV 的采集情况 |
| `clean_status` | clean / not_clean / not_determined |
| `end_reason` | Wash 正常结束或异常的原因 |
| `data_path` | Wash 与 clean-check 数据目录 |
| `notes` | 备注 |

### ph_test

一次针对某个 well_state 实际执行的 pH 测量。**v2：`session_id` 可空**——pH 由电脑 A 在 B 的 session 之外执行（dispense 之后、电化学之前），靠 state_id 挂链；若未来纳入 B 的时间线才填 session_id。

| **字段** | **描述** |
| --- | --- |
| `ph_test_id` | 一次 pH 测量的唯一编号 |
| `session_id` | 属于哪个 session；A 独立执行时为空（v2） |
| `state_id` | 测量的是哪个 well state |
| `calibration_id` | 本次测量使用哪次 calibration |
| `started_at` | 探头开始读取该样品的时间 |
| `ended_at` | 得到结果或终止的时间 |
| `final_ph` | 最终计算出的 pH |
| `final_voltage` | 用于计算最终 pH 的最后一次探头电压读数 |
| `stability_required` | 需要连续满足稳定条件的次数，例如 2 |
| `stability_threshold_v` | 相邻电压读数允许的最大变化，目前 0.02 |
| `reading_interval_s` | 两次读数间隔，目前约 5 秒 |
| `acquisition_status` | complete / stopped / failed |
| `end_reason` | stable / user_cancelled / probe_error / calibration_error |
| `data_path` | 本次测量的原始探头读数文件路径 |
| `notes` | 备注 |

### electrode_maintenance

对某一支具体电极执行的一次维护操作。不限定 WE，不限定 polish；WE/RE/CE 都通过 electrode_id 引用 electrode 表。

| **字段** | **描述** |
| --- | --- |
| `maintenance_id` | 维护记录唯一编号 |
| `electrode_id` | 被维护的是哪一支电极 |
| `maintenance_type` | polish / clean / refill / inspect / other |
| `trigger_wash_id` | 由某次 wash 触发时引用该 wash；其他情况为空 |
| `requested_at` | 判断需要维护的时间，可为空 |
| `started_at` | 实际开始维护的时间，可为空 |
| `completed_at` | 实际完成维护的时间，可为空 |
| `method` | manual / automated |
| `protocol` | 使用的维护方法或 protocol 名称 |
| `status` | requested / in_progress / completed / cancelled / failed / unknown |
| `notes` | 备注 |

同一次操作 polish 两支电极 → 两行。人工 GUI 外 polish：`status=completed, method=manual`；系统提示型：先 `requested`、确认后 `completed`。

Session 不加 polish 字段；通过 electrode_setup_id + 本表查询"某 session 开始前某电极最近一次维护"。人工 polish 不写 session_step（发生在两个 session 之间）；旧 session 未执行步骤记 `blocked / electrode_maintenance_required`。

三者职责：session = 一次连续自动运行；session_step = 计划顺序；electrode_maintenance = 对具体电极的维护历史。

---

## 第四层：分析（版本化）

### analysis

一行 = 一次实际执行的分析。相同数据重跑时，算法版本、参数、background、baseline 策略任一不同都建新行，不覆盖。人工标峰 = 一次新 analysis（manual_picks 进 parameters_json），is_current 翻转。

| **字段** | **描述** |
| --- | --- |
| `analysis_id` | 分析记录唯一编号 |
| `analysis_type` | 分析类型 |
| `algorithm_name` | 使用的函数或算法名称 |
| `algorithm_version` | 算法版本 |
| `code_version` | 可选，Git commit 或发布版本 |
| `parameters_json` | 本次分析实际使用的参数（含 manual_picks、pair_tol_v 等） |
| `started_at` | 分析开始时间 |
| `completed_at` | 分析结束时间 |
| `status` | complete / failed / partial |
| `is_current` | 是否为当前推荐使用的分析结果 |
| `notes` | 备注 |

**analysis_type**：`cv_peak_detection` / `cv_measurement_summary` / `cv_randles_sevcik` / `cv_parameters_detect` / `wash_clean_check` / `ca_cottrell`。

**is_current**：唯一范围 =（目标, analysis_type），SQLite partial unique index 实现。

### analysis_input

一行 = 一个分析输入。扣背景需要 sample + baseline；Randles 需要 bundle；clean check 需要 clean check + baseline。

| **字段** | **描述** |
| --- | --- |
| `analysis_input_id` | 输入记录唯一编号 |
| `analysis_id` | 属于哪次 analysis |
| `input_role` | 这个输入在分析中起什么作用（sample / baseline / …） |
| `measurement_id` | 输入是正式 measurement 时填写 |
| `bundle_id` | 输入是整个 bundle 时填写 |
| `wash_id` | 输入是 wash 时填写 |
| `ocp_id` | 输入是 OCP check 时填写 |
| `notes` | 备注 |

---

## 第五层：结果表

### peak_result

一行 = 某次分析检出的一个峰（单极性、单 scan 原始检出）。Ep 与 E_sd_apex 是刻意分开的两个物理量（相差约 28.5/n mV）。

| **字段** | **描述** |
| --- | --- |
| `peak_result_id` | 峰结果唯一编号 |
| `analysis_id` | 属于哪次 peak analysis |
| `measurement_id` | 峰来自哪条 measurement |
| `scan_number` | 来自第几个 scan |
| `branch` | anodic / cathodic |
| `peak_order` | 同一 branch 中第几个峰 |
| `ep_v` | 峰电位（电流域顶点） |
| `e_sd_apex_v` | 半微分顶点电位（≈ E½ flank） |
| `ip_a` | 峰电流 |
| `integrated_ip_a` | 半积分方法得到的峰电流 |
| `fwhm_v` | 半峰宽 |
| `asymmetry` | 不对称度 |
| `classification` | single / grey / double_suspected |
| `is_truncated` | 峰是否被扫描边界截断 |
| `pon` | peak-over-noise（拟合质量） |
| `quality_status` | success / provisional / reject |
| `notes` | 备注 |

### cv_peak_group

一行 = 一个 bundle 或普通 CV 中的一个科学峰组（一个 redox 对或一个不可逆峰）。`bundle_id` 与 `measurement_id` 必须且只能填一个。

| **字段** | **描述** |
| --- | --- |
| `peak_group_id` | 峰组唯一编号 |
| `bundle_id` | 属于哪个 multi-rate CV bundle |
| `measurement_id` | 普通单条 CV 时填写 |
| `peak_group_order` | 第几个峰组（1、2、3…） |
| `reaction_type` | redox_pair / irreversible_anodic / irreversible_cathodic |
| `label` | 可选人类可读名称，如 "Fc main pair" |
| `created_by` | automatic / manual |
| `notes` | 备注 |

### cv_scan_result

一行 = 某条 CV 的某个 scan 中的一组峰结果。deviation 为带符号 mV（本 scan 值 − 同组跨 scan 中位数）。

| **字段** | **描述** |
| --- | --- |
| `cv_scan_result_id` | 唯一编号 |
| `analysis_id` | 属于哪次单 scan 分析 |
| `measurement_id` | 对应哪条 CV |
| `scan_number` | 第几个 scan |
| `peak_group_id` | 该 scan 中的哪组峰 |
| `epa_v` | 阳极峰电位；没有则为空 |
| `epc_v` | 阴极峰电位；没有则为空 |
| `ipa_a` | 阳极峰电流；没有则为空 |
| `ipc_a` | 阴极峰电流；没有则为空 |
| `delta_ep_v` | 峰间距 Epa−Epc；单峰时为空 |
| `e_half_v` | E½；单峰时为空 |
| `dev_epa_mv` | Epa 带符号偏离（mV） |
| `dev_epc_mv` | Epc 带符号偏离（mV） |
| `dev_e_half_mv` | E½ 带符号偏离（mV） |
| `selection_source` | automatic / manual / adjusted |
| `notes` | 备注 |

人工标峰后：新建 analysis，被标 scan 行 `manual`，其余 scan 对齐同一峰写 `adjusted`，is_current 指向新结果。

### cv_summary_result

一行 = 整条 CV 中某一组峰的最终汇总（代表 scans 的平均）。代表 scan 规则：按 |E½ − 中位数| 排名，奇数 n 取 (n+1)/2，偶数 n 取 2（`closest_to_median`）。

| **字段** | **描述** |
| --- | --- |
| `cv_summary_id` | 唯一编号 |
| `analysis_id` | 属于哪次 CV 汇总分析 |
| `measurement_id` | 对应哪条 CV |
| `peak_group_id` | 整条 CV 的哪组峰 |
| `n_scans_total` | 总 scan 数 |
| `n_scans_used` | 实际用于平均的 scan 数 |
| `used_scan_numbers_json` | 例如 [2,3] 或 [2,3,4] |
| `epa_mean_v` | 阳极峰电位平均值 |
| `epc_mean_v` | 阴极峰电位平均值 |
| `ipa_mean_a` | 阳极峰电流平均值 |
| `ipc_mean_a` | 阴极峰电流平均值 |
| `delta_ep_mean_v` | 平均峰间距 |
| `e_half_mean_v` | 平均 E½ |
| `e_half_spread_mv` | 选中 scans 的 E½ MAD（mV） |
| `abs_ipc_ipa_ratio` | 平均电流比 |
| `selection_method` | 例如 closest_to_median |
| `quality_status` | accepted / rejected |
| `notes` | 备注 |

### cv_window_result

一行 = 一次 CV 缩圈窗口检测结果（analysis_type = cv_parameters_detect），字段对应 cv_window.py 的 CVWindow / CVWall。

| **字段** | **描述** |
| --- | --- |
| `cv_window_result_id` | 唯一编号 |
| `analysis_id` | 对应 cv_parameters_detect analysis |
| `measurement_id` | 用于检测的宽扫 CV |
| `scan_used` | 实际分析的 scan（默认最后一圈） |
| `valid` | 窗口是否有效 |
| `lower_v` | 建议窗口下限（含 margin） |
| `upper_v` | 建议窗口上限（含 margin） |
| `anodic_detected` | 阳极侧 wall 是否检出 |
| `anodic_limit_v` | 阳极 wall 起点电位 |
| `anodic_reason` | 阳极侧判定说明 |
| `cathodic_detected` | 阴极侧 wall 是否检出 |
| `cathodic_limit_v` | 阴极 wall 起点电位 |
| `cathodic_reason` | 阴极侧判定说明 |
| `clamped` | 是否被 safe_bounds 截断 |
| `reason` | 整体判定说明 |
| `notes` | 备注 |

### cv_randles_sevcik_result

一行 = 一个多扫速 bundle 中，一组峰、一个方向的拟合结果。

| **字段** | **描述** |
| --- | --- |
| `rs_result_id` | 唯一编号 |
| `analysis_id` | 对应 cv_randles_sevcik analysis |
| `bundle_id` | 对应多扫速 bundle |
| `peak_group_id` | 哪组峰 |
| `branch` | anodic / cathodic |
| `n_points` | 使用的扫速数量 |
| `slope` | ip 对 sqrt(scan rate) 的斜率 |
| `intercept` | 截距 |
| `r_squared` | 拟合 R² |
| `diffusion_coefficient_cm2_s` | 计算出的 D |
| `electron_number_used` | 计算时假设的 n |
| `area_cm2_used` | 使用的电极面积 |
| `concentration_m_used` | 使用的浓度（可由 well_state_component 取） |
| `quality_status` | accepted / rejected |
| `notes` | 备注 |

### wash_clean_check_result

一行 = 一次 wash attempt 的 clean-check 分析结果。`wash.clean_status` 保存流程结论；这张表保存分析依据（字段对齐 is_clean 实际输出）。

| **字段** | **描述** |
| --- | --- |
| `clean_check_result_id` | 唯一编号 |
| `analysis_id` | 对应 wash_clean_check analysis |
| `wash_id` | 对应哪次 wash attempt |
| `baseline_measurement_id` | 使用的 baseline |
| `residual_in_sample` | 样品峰位附近的残余判定 |
| `residual_general` | 全窗口的一般性残余判定 |
| `clean_status` | clean / not_clean / not_determined |
| `reason` | is_clean 给出的判定说明 |
| `notes` | 备注 |

### ca_cottrell_result

一行 = 一条 CA measurement 的 Cottrell 分析。

| **字段** | **描述** |
| --- | --- |
| `ca_result_id` | 唯一编号 |
| `analysis_id` | 对应 ca_cottrell analysis |
| `measurement_id` | 对应哪条 CA |
| `beta` | log-log 拟合指数 |
| `slope` | 拟合斜率 |
| `intercept` | 截距 |
| `r_squared` | 拟合 R² |
| `fit_start_s` | 拟合起点 |
| `fit_end_s` | 拟合终点 |
| `tail_current_a` | 尾部电流 |
| `max_abs_current_a` | 最大绝对电流 |
| `quality_status` | accepted / rejected |
| `notes` | 备注 |

---

## 附：关系总览

```
电脑 A                                  电脑 B（SQLite 唯一写者）
dispense 协议(声明式) ─┐
stock_solution ────────┼─ parser ──API──> well_state (source_* 可追溯)
                       │                   └── well_state_component → chemical
pH 模块 ───────────────┴────────API──> ph_test (session_id 可空)
probe 校准 ─────────────────────API──> probe_calibration

experiment
└── session (created_via: gui/remote_api; electrode_setup → electrode ×3)
    ├── session_step（顺序）
    ├── measurement_bundle（科学分组）
    ├── ocp_check ── measurement（state_id → well_state → well → rack）
    │                   └── wash（attempt ×N）
    │                        └── electrode_maintenance（not_clean 触发）
    └── (ph_test 若纳入 B 时间线)

analysis（版本化，is_current 每目标一条）
├── analysis_input（sample / baseline / bundle / wash / ocp）
└── 结果表：peak_result / cv_peak_group → cv_scan_result → cv_summary_result
          cv_window_result / cv_randles_sevcik_result
          wash_clean_check_result / ca_cottrell_result
```
