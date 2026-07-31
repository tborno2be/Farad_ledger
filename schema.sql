-- Farad_ledger · CVLab SQLite schema (database_design_v2.md)
-- 约定：id 用 INTEGER PRIMARY KEY（SQLite rowid）；时间戳 TEXT ISO-8601；布尔 INTEGER 0/1。
-- 单一写者：本库只在电脑 B 上、只由 B 写入；A 侧数据经 B 的 API 提交。
-- 打开外键：每个连接执行 PRAGMA foreign_keys = ON;

-- ═══════════════ 第一层：物理与化学资产 ═══════════════

CREATE TABLE IF NOT EXISTS rack (
    rack_id        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    position_key   TEXT NOT NULL,            -- position.json key
    description    TEXT,                     -- design version / Fusion360 设计稿名
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS well (
    well_id        INTEGER PRIMARY KEY,
    rack_id        INTEGER NOT NULL REFERENCES rack(rack_id),
    position_name  TEXT NOT NULL,            -- A1..B4
    default_role   TEXT,                     -- sample / standard / rinse
    active         INTEGER NOT NULL DEFAULT 1,
    notes          TEXT,
    UNIQUE (rack_id, position_name)
);

CREATE TABLE IF NOT EXISTS station (
    station_id     INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    position_key   TEXT NOT NULL,
    station_type   TEXT,                     -- wash / safe / approach / parking
    active         INTEGER NOT NULL DEFAULT 1,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS electrode (
    electrode_id   INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,     -- 代码按 name 查重,库里也强制唯一
    electrode_type TEXT NOT NULL CHECK (electrode_type IN ('working','reference','counter')),
    material       TEXT,
    diameter_mm    REAL,
    area_cm2       REAL,
    reference_fill_chemical         TEXT,
    reference_fill_concentration_m  REAL,
    serial_number  TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    notes          TEXT
);

-- 组合不取名字:人只关心每支电极,电脑用 setup_id 标识组合;
-- 内容 = 每个 role 对应的 electrode_id,同一组合唯一。
CREATE TABLE IF NOT EXISTS electrode_setup (
    setup_id           INTEGER PRIMARY KEY,
    working_electrode  INTEGER NOT NULL REFERENCES electrode(electrode_id),
    reference_electrode INTEGER NOT NULL REFERENCES electrode(electrode_id),
    counter_electrode  INTEGER NOT NULL REFERENCES electrode(electrode_id),
    active             INTEGER NOT NULL DEFAULT 1,
    notes              TEXT,
    UNIQUE (working_electrode, reference_electrode, counter_electrode)
);

CREATE TABLE IF NOT EXISTS chemical (
    chemical_id      INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    formula          TEXT,
    storage_location TEXT,
    received_at      TEXT,
    notes            TEXT
);

-- v2：Opentrons stock rack 上某位置、某段时间的一种母液。
CREATE TABLE IF NOT EXISTS stock_solution (
    stock_id            INTEGER PRIMARY KEY,
    deck_slot           TEXT NOT NULL,       -- OT deck slot（如 '5'）
    well_name           TEXT NOT NULL,       -- labware 位置（A1、B2…）
    chemical_id         INTEGER NOT NULL REFERENCES chemical(chemical_id),
    concentration_m     REAL,
    solvent_chemical_id INTEGER REFERENCES chemical(chemical_id),
    prepared_at         TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    notes               TEXT
);

-- 只有一支 pH probe，不建 probe 表。校准在 A 上执行，经 API 入库。
CREATE TABLE IF NOT EXISTS probe_calibration (
    calibration_id       INTEGER PRIMARY KEY,
    calibrated_at        TEXT NOT NULL,
    buffer_1_ph          REAL NOT NULL,      -- 目前 4.01
    buffer_1_voltage     REAL NOT NULL,
    buffer_2_ph          REAL NOT NULL,      -- 目前 9.21
    buffer_2_voltage     REAL NOT NULL,
    conversion_slope     REAL,
    conversion_intercept REAL,
    status               TEXT NOT NULL CHECK (status IN ('complete','failed')),
    notes                TEXT
);

-- ═══════════════ 第二层：执行流 ═══════════════

CREATE TABLE IF NOT EXISTS experiment (
    experiment_id   INTEGER PRIMARY KEY,
    experiment_name TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','completed','abandoned')),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS session (
    session_id        INTEGER PRIMARY KEY,
    experiment_id     INTEGER NOT NULL REFERENCES experiment(experiment_id),
    session_name      TEXT,
    created_via       TEXT NOT NULL DEFAULT 'gui'
                      CHECK (created_via IN ('gui','remote_api')),
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    ended_at          TEXT,
    status            TEXT NOT NULL DEFAULT 'planned'
                      CHECK (status IN ('planned','running','completed','cancelled','stopped','failed')),
    end_reason        TEXT CHECK (end_reason IN
                      ('normal_completion','user_cancelled','electrode_maintenance_required',
                       'clean_check_failed','device_error','robot_error','connection_lost',
                       'electrode_setup_changed','unknown')),
    electrode_setup_id INTEGER REFERENCES electrode_setup(setup_id),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS measurement_bundle (
    bundle_id   INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES session(session_id),
    bundle_type TEXT NOT NULL DEFAULT 'multi_rate_cv',
    created_at  TEXT NOT NULL,
    notes       TEXT
);

-- ═══════════════ 第三层：过程记录 ═══════════════

-- v3：well_state 与 well_state_component 合并——一条 state 就带上孔里有什么：
-- 分析物 / 酸碱 / supporting electrolyte 三个化学品槽位（各带浓度）+ 溶解度。
-- 挂 well + experiment（不挂 session）；version 0 = composition unknown；
-- 来源三字段记录由哪份 dispense 协议、哪个 git 版本解析生成。
CREATE TABLE IF NOT EXISTS well_state (
    state_id        INTEGER PRIMARY KEY,
    experiment_id   INTEGER NOT NULL REFERENCES experiment(experiment_id),
    well_id         INTEGER NOT NULL REFERENCES well(well_id),
    version         INTEGER NOT NULL,        -- 该孔第几个状态；0 = unknown
    total_volume_ml REAL,
    solubility_m    REAL,                    -- 分析物溶解度 / M；可空（跳过）
    analyte_chemical_id        INTEGER REFERENCES chemical(chemical_id),
    analyte_concentration_m    REAL,
    acid_base_chemical_id      INTEGER REFERENCES chemical(chemical_id),
    acid_base_concentration_m  REAL,
    supporting_chemical_id     INTEGER REFERENCES chemical(chemical_id),
    supporting_concentration_m REAL,
    source_type     TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source_type IN ('opentrons_protocol','manual')),
    source_protocol TEXT,                    -- dispense 协议文件；manual 时为空
    source_git_hash TEXT,                    -- 协议所在 repo commit；manual 时为空
    created_at      TEXT NOT NULL,
    ended_at        TEXT,
    notes           TEXT,                    -- 放不进槽位的组分（如 solvent）也记在这里
    UNIQUE (experiment_id, well_id, version)
);

CREATE TABLE IF NOT EXISTS ocp_check (
    ocp_id          INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES session(session_id),
    state_id        INTEGER NOT NULL REFERENCES well_state(state_id),
    started_at      TEXT,
    ended_at        TEXT,
    elapsed_s       REAL,
    final_ocp_v     REAL,
    slope_v_s       REAL,
    residual        REAL,
    settling_status TEXT CHECK (settling_status IN
                    ('settled','timeout_unsettled','stopped','failed')),
    proceed_to_measurement INTEGER,          -- 0/1
    data_path       TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS measurement (
    measurement_id  INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES session(session_id),
    state_id        INTEGER NOT NULL REFERENCES well_state(state_id),
    ocp_id          INTEGER REFERENCES ocp_check(ocp_id),          -- 可空：baseline/adhoc
    bundle_id       INTEGER REFERENCES measurement_bundle(bundle_id),
    technique       TEXT NOT NULL,           -- CV / CA / LSV ...
    parameters_json TEXT,
    role            TEXT NOT NULL DEFAULT 'sample'
                    CHECK (role IN ('sample','baseline','cleancheck')),
    acquisition_status TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (acquisition_status IN
                    ('not_started','partial','complete','failed','unknown')),
    end_reason      TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    data_path       TEXT,
    notes           TEXT
);

-- 一行 = 一次 attempt（wash → clean check → 判定）；measurement_id XOR bundle_id。
CREATE TABLE IF NOT EXISTS wash (
    wash_id         INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES session(session_id),
    attempt         INTEGER NOT NULL DEFAULT 1,
    baseline_id     INTEGER REFERENCES measurement(measurement_id),
    measurement_id  INTEGER REFERENCES measurement(measurement_id),
    bundle_id       INTEGER REFERENCES measurement_bundle(bundle_id),
    started_at      TEXT,
    ended_at        TEXT,
    wash_protocol   TEXT,
    cleancheck_parameters_json TEXT,
    acquisition_status TEXT,
    clean_status    TEXT CHECK (clean_status IN ('clean','not_clean','not_determined')),
    cleancheck_measurement_id INTEGER REFERENCES measurement(measurement_id),
                    -- 判定用的 standard CV 本体（role='cleancheck' 的 measurement，
                    -- 曲线 parquet + 原始文件 artifact 全部入库）
    end_reason      TEXT,
    data_path       TEXT,
    notes           TEXT,
    CHECK ((measurement_id IS NULL) != (bundle_id IS NULL))
);

-- v2：session_id 可空——pH 由 A 在 B 的 session 之外执行，靠 state_id 挂链。
CREATE TABLE IF NOT EXISTS ph_test (
    ph_test_id          INTEGER PRIMARY KEY,
    session_id          INTEGER REFERENCES session(session_id),
    state_id            INTEGER NOT NULL REFERENCES well_state(state_id),
    calibration_id      INTEGER NOT NULL REFERENCES probe_calibration(calibration_id),
    started_at          TEXT,
    ended_at            TEXT,
    final_ph            REAL,
    final_voltage       REAL,
    stability_required  INTEGER,             -- 例如 2
    stability_threshold_v REAL,              -- 目前 0.02
    reading_interval_s  REAL,                -- 目前 ~5
    acquisition_status  TEXT CHECK (acquisition_status IN ('complete','stopped','failed')),
    end_reason          TEXT CHECK (end_reason IN
                        ('stable','user_cancelled','probe_error','calibration_error')),
    data_path           TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS electrode_maintenance (
    maintenance_id   INTEGER PRIMARY KEY,
    electrode_id     INTEGER NOT NULL REFERENCES electrode(electrode_id),
    maintenance_type TEXT NOT NULL CHECK (maintenance_type IN
                     ('polish','clean','refill','inspect','other')),
    trigger_wash_id  INTEGER REFERENCES wash(wash_id),
    requested_at     TEXT,
    started_at       TEXT,
    completed_at     TEXT,
    method           TEXT CHECK (method IN ('manual','automated')),
    protocol         TEXT,
    status           TEXT NOT NULL DEFAULT 'requested' CHECK (status IN
                     ('requested','in_progress','completed','cancelled','failed','unknown')),
    notes            TEXT
);

-- session_step 引用以上各过程表，放在它们之后建。
CREATE TABLE IF NOT EXISTS session_step (
    step_id        INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES session(session_id),
    step_order     INTEGER NOT NULL,         -- 从 1 开始
    step_type      TEXT NOT NULL,            -- ph_test / ocp_check / measurement / wash ...
    ph_test_id     INTEGER REFERENCES ph_test(ph_test_id),
    ocp_id         INTEGER REFERENCES ocp_check(ocp_id),
    measurement_id INTEGER REFERENCES measurement(measurement_id),
    wash_id        INTEGER REFERENCES wash(wash_id),
    step_status    TEXT NOT NULL DEFAULT 'planned' CHECK (step_status IN
                   ('planned','started','completed','cancelled','skipped','blocked')),
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    ended_at       TEXT,
    skip_reason    TEXT,
    data_path      TEXT,
    notes          TEXT,
    UNIQUE (session_id, step_order)
);

-- 附件内容表（师兄原则：同类数据同一张表、内容进库防误删）。
-- raw/csv/mscript 的文件内容本体存这里，kind 是值不是表；技术种类在 measurement.technique。
-- cvlab.sqlite 由此自足：磁盘文件全丢也能从库里完整重建（parquet 可由 csv 重新生成）。
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id    INTEGER PRIMARY KEY,
    measurement_id INTEGER REFERENCES measurement(measurement_id),
    ocp_id         INTEGER REFERENCES ocp_check(ocp_id),
    kind           TEXT NOT NULL CHECK (kind IN ('raw','csv','mscript')),
    filename       TEXT NOT NULL,
    content        BLOB NOT NULL,            -- 文件内容本体
    sha256         TEXT,                     -- 完整性校验
    created_at     TEXT,
    CHECK ((measurement_id IS NULL) != (ocp_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_artifact_meas ON artifact(measurement_id, kind);

-- ═══════════════ 第四层：分析（版本化） ═══════════════

-- is_current 唯一范围 =（目标, analysis_type）。目标经 analysis_input 表达，
-- SQLite 无法直接对此建索引 → 由 api 层在翻转 is_current 时保证唯一。
CREATE TABLE IF NOT EXISTS analysis (
    analysis_id       INTEGER PRIMARY KEY,
    analysis_type     TEXT NOT NULL CHECK (analysis_type IN
                      ('cv_peak_detection','cv_measurement_summary','cv_randles_sevcik',
                       'cv_parameters_detect','wash_clean_check','ca_cottrell')),
    algorithm_name    TEXT,
    algorithm_version TEXT,
    code_version      TEXT,                  -- git commit
    parameters_json   TEXT,                  -- 含 manual_picks、pair_tol_v 等
    started_at        TEXT,
    completed_at      TEXT,
    status            TEXT NOT NULL DEFAULT 'complete'
                      CHECK (status IN ('complete','failed','partial')),
    is_current        INTEGER NOT NULL DEFAULT 1,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS analysis_input (
    analysis_input_id INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    input_role        TEXT NOT NULL,         -- sample / baseline / ...
    measurement_id    INTEGER REFERENCES measurement(measurement_id),
    bundle_id         INTEGER REFERENCES measurement_bundle(bundle_id),
    wash_id           INTEGER REFERENCES wash(wash_id),
    ocp_id            INTEGER REFERENCES ocp_check(ocp_id),
    notes             TEXT
);

-- ═══════════════ 第五层：结果表 ═══════════════

CREATE TABLE IF NOT EXISTS peak_result (
    peak_result_id  INTEGER PRIMARY KEY,
    analysis_id     INTEGER NOT NULL REFERENCES analysis(analysis_id),
    measurement_id  INTEGER NOT NULL REFERENCES measurement(measurement_id),
    scan_number     INTEGER NOT NULL,
    branch          TEXT NOT NULL CHECK (branch IN ('anodic','cathodic')),
    peak_order      INTEGER,
    ep_v            REAL,                    -- 电流域顶点
    e_sd_apex_v     REAL,                    -- 半微分顶点 ≈ E1/2 flank
    ip_a            REAL,
    integrated_ip_a REAL,
    fwhm_v          REAL,
    asymmetry       REAL,
    classification  TEXT CHECK (classification IN ('single','grey','double_suspected')),
    is_truncated    INTEGER,
    pon             REAL,                    -- peak-over-noise
    quality_status  TEXT CHECK (quality_status IN ('success','provisional','reject')),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS cv_peak_group (
    peak_group_id    INTEGER PRIMARY KEY,
    bundle_id        INTEGER REFERENCES measurement_bundle(bundle_id),
    measurement_id   INTEGER REFERENCES measurement(measurement_id),
    peak_group_order INTEGER,
    reaction_type    TEXT NOT NULL CHECK (reaction_type IN
                     ('redox_pair','irreversible_anodic','irreversible_cathodic')),
    label            TEXT,
    created_by       TEXT NOT NULL DEFAULT 'automatic'
                     CHECK (created_by IN ('automatic','manual')),
    notes            TEXT,
    CHECK ((measurement_id IS NULL) != (bundle_id IS NULL))
);

CREATE TABLE IF NOT EXISTS cv_scan_result (
    cv_scan_result_id INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    measurement_id    INTEGER NOT NULL REFERENCES measurement(measurement_id),
    scan_number       INTEGER NOT NULL,
    peak_group_id     INTEGER NOT NULL REFERENCES cv_peak_group(peak_group_id),
    epa_v             REAL,
    epc_v             REAL,
    ipa_a             REAL,
    ipc_a             REAL,
    delta_ep_v        REAL,
    e_half_v          REAL,
    dev_epa_mv        REAL,                  -- 带符号，圈值-跨圈中位数
    dev_epc_mv        REAL,
    dev_e_half_mv     REAL,
    selection_source  TEXT NOT NULL DEFAULT 'automatic'
                      CHECK (selection_source IN ('automatic','manual','adjusted')),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS cv_summary_result (
    cv_summary_id     INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    measurement_id    INTEGER NOT NULL REFERENCES measurement(measurement_id),
    peak_group_id     INTEGER NOT NULL REFERENCES cv_peak_group(peak_group_id),
    n_scans_total     INTEGER,
    n_scans_used      INTEGER,               -- 奇数 n 取 (n+1)/2；偶数 n 取 2
    used_scan_numbers_json TEXT,             -- 例如 [2,3]
    epa_mean_v        REAL,
    epc_mean_v        REAL,
    ipa_mean_a        REAL,
    ipc_mean_a        REAL,
    delta_ep_mean_v   REAL,
    e_half_mean_v     REAL,
    e_half_spread_mv  REAL,                  -- 选中 scans 的 E1/2 MAD
    abs_ipc_ipa_ratio REAL,
    selection_method  TEXT DEFAULT 'closest_to_median',
    quality_status    TEXT CHECK (quality_status IN ('accepted','rejected')),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS cv_window_result (
    cv_window_result_id INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    measurement_id    INTEGER NOT NULL REFERENCES measurement(measurement_id),
    scan_used         INTEGER,
    valid             INTEGER,
    lower_v           REAL,
    upper_v           REAL,
    anodic_detected   INTEGER,
    anodic_limit_v    REAL,
    anodic_reason     TEXT,
    cathodic_detected INTEGER,
    cathodic_limit_v  REAL,
    cathodic_reason   TEXT,
    clamped           INTEGER,
    reason            TEXT,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS cv_randles_sevcik_result (
    rs_result_id      INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    bundle_id         INTEGER NOT NULL REFERENCES measurement_bundle(bundle_id),
    peak_group_id     INTEGER NOT NULL REFERENCES cv_peak_group(peak_group_id),
    branch            TEXT NOT NULL CHECK (branch IN ('anodic','cathodic')),
    n_points          INTEGER,
    slope             REAL,
    intercept         REAL,
    r_squared         REAL,
    diffusion_coefficient_cm2_s REAL,
    electron_number_used INTEGER,
    area_cm2_used     REAL,
    concentration_m_used REAL,
    quality_status    TEXT CHECK (quality_status IN ('accepted','rejected')),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS wash_clean_check_result (
    clean_check_result_id INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    wash_id           INTEGER NOT NULL REFERENCES wash(wash_id),
    baseline_measurement_id INTEGER REFERENCES measurement(measurement_id),
    residual_in_sample TEXT,
    residual_general  TEXT,
    clean_status      TEXT CHECK (clean_status IN ('clean','not_clean','not_determined')),
    reason            TEXT,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS ca_cottrell_result (
    ca_result_id      INTEGER PRIMARY KEY,
    analysis_id       INTEGER NOT NULL REFERENCES analysis(analysis_id),
    measurement_id    INTEGER NOT NULL REFERENCES measurement(measurement_id),
    beta              REAL,
    slope             REAL,
    intercept         REAL,
    r_squared         REAL,
    fit_start_s       REAL,
    fit_end_s         REAL,
    tail_current_a    REAL,
    max_abs_current_a REAL,
    quality_status    TEXT CHECK (quality_status IN ('accepted','rejected')),
    notes             TEXT
);

-- ═══════════════ 常用索引 ═══════════════
CREATE INDEX IF NOT EXISTS idx_session_experiment   ON session(experiment_id);
CREATE INDEX IF NOT EXISTS idx_measurement_session  ON measurement(session_id);
CREATE INDEX IF NOT EXISTS idx_measurement_state    ON measurement(state_id);
CREATE INDEX IF NOT EXISTS idx_state_well           ON well_state(well_id);
CREATE INDEX IF NOT EXISTS idx_wash_measurement     ON wash(measurement_id);
CREATE INDEX IF NOT EXISTS idx_analysis_current     ON analysis(analysis_type, is_current);
CREATE INDEX IF NOT EXISTS idx_ainput_analysis      ON analysis_input(analysis_id);
CREATE INDEX IF NOT EXISTS idx_scanresult_meas      ON cv_scan_result(measurement_id);
CREATE INDEX IF NOT EXISTS idx_step_session         ON session_step(session_id, step_order);
