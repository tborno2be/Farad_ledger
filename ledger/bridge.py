"""bridge: CVLabtest 与 ledger 的整条接缝，全部集中在这里。

CVLabtest 侧只做两件事（都不 import 本包）：
  1. workflow.ON_EVENT = <handler>   -- GUI 启动时 optional-import 本模块后注入
  2. GUI 的 /api/ledger/submit_* 端点把 A 发来的 JSON 转给 CVLabBridge.submit_*

事件协议（kind, payload dict），payload 全是普通类型：
  session_start     {name, created_via}
  session_end       {status, end_reason}
  ocp_done          {plate, well, elapsed_s, final_ocp_v, slope_v_s, settled,
                     proceed, data_path}
  measurement_done  {tech, plate, well, params, role, csv, status, end_reason}
  wash_done         {plate, well, attempt, clean, reason, data_path,
                     cleancheck_params, baseline_csv}
所有处理都 try/except：落库失败绝不影响跑实验（打日志即止）。
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from .api import Ledger, now
from .ingest import ingest_measurement

LOG = logging.getLogger("farad_ledger")


class CVLabBridge:
    def __init__(self, db_path, data_root, experiment_name, *, created_via="gui",
                 git_hash=None):
        """data_root = Farad_ledger/data；曲线写到 data/<experiment_name>/。"""
        self.led = Ledger(db_path)
        self.data_dir = Path(data_root) / experiment_name
        self.experiment_id = self.led.get_or_create_experiment(experiment_name)
        self.created_via = created_via
        self.git_hash = git_hash
        self.session_id = None
        self.electrode_setup_id = None   # GUI 电极页选好的 set；session_start 时落库
        self._last_ocp = {}          # (plate,well) -> ocp_id：挂到下一条 measurement
        self._last_meas = {}         # (plate,well) -> measurement_id：挂 wash
        self._last_bundle = {}       # (plate,well) -> bundle_id（CV_ms：wash 挂 bundle）
        self._bundles = {}           # bundle 事件名 -> bundle_id（一个 CV_ms item 一个）
        self._baseline_mid = None    # 最近一条 role=baseline 的 measurement_id
        self._wash_attempt = {}      # (plate,well) -> attempt 计数

    # ── 事件入口（workflow.ON_EVENT 指到这里）────────────────────────────────
    def handle(self, kind, payload: dict):
        try:
            getattr(self, f"_on_{kind}")(payload or {})
        except Exception:
            LOG.error("ledger event %s failed:\n%s", kind, traceback.format_exc())

    def _state(self, p):
        wid = self.led.get_or_create_well(p.get("plate", "?"), p.get("well", "?"))
        return self.led.current_state(self.experiment_id, wid)

    def _on_session_start(self, p):
        self.session_id = self.led.start_session(
            self.experiment_id, name=p.get("name"),
            created_via=p.get("created_via", self.created_via),
            electrode_setup_id=p.get("electrode_setup_id") or self.electrode_setup_id)

    _STATUS_OK = {"planned", "running", "completed", "cancelled", "stopped", "failed"}
    _REASON_MAP = {"not_clean": "clean_check_failed",
                   "machine_fault": "robot_error",
                   "not_connected": "device_error",
                   "step_timeout": "robot_error"}
    _REASON_OK = {"normal_completion", "user_cancelled", "electrode_maintenance_required",
                  "clean_check_failed", "device_error", "robot_error", "connection_lost",
                  "electrode_setup_changed", "unknown"}

    def _on_session_end(self, p):
        if self.session_id:
            status = p.get("status", "completed")
            reason = p.get("end_reason", "normal_completion")
            reason = self._REASON_MAP.get(reason, reason)
            self.led.end_session(
                self.session_id,
                status=status if status in self._STATUS_OK else "stopped",
                end_reason=reason if reason in self._REASON_OK else "unknown")
            self.session_id = None

    def _on_ocp_done(self, p):
        oid = self.led.add_ocp(
            self.session_id, self._state(p), started_at=None, ended_at=now(),
            elapsed_s=p.get("elapsed_s"), final_ocp_v=p.get("final_ocp_v"),
            slope_v_s=p.get("slope_v_s"),
            settling_status="settled" if p.get("settled") else "timeout_unsettled",
            proceed_to_measurement=int(bool(p.get("proceed", True))),
            data_path=p.get("data_path"))
        self._last_ocp[(p.get("plate"), p.get("well"))] = oid
        dp = p.get("data_path")
        if dp and Path(dp).is_dir():                 # OCP 文件内容也进库（防误删）
            kinds = {".csv": "csv", ".txt": "raw", ".mscr": "mscript"}
            for f in Path(dp).iterdir():
                k = kinds.get(f.suffix)
                if k:
                    self.led.add_artifact(k, f.name, f.read_bytes(), ocp_id=oid)

    def _on_measurement_done(self, p):
        key = (p.get("plate"), p.get("well"))
        # CV_ms：同一 item 的每个 rate 一条 measurement，共挂一个 bundle
        bid = None
        if p.get("bundle"):
            bid = self._bundles.get(p["bundle"])
            if bid is None:
                bid = self.led.add_bundle(self.session_id, "multi_rate_cv")
                self._bundles[p["bundle"]] = bid
        info = ingest_measurement(
            self.led, p["csv"], session_id=self.session_id, state_id=self._state(p),
            technique=p.get("tech", "CV"), role=p.get("role", "sample"),
            parameters=p.get("params"), data_dir=self.data_dir,
            ocp_id=self._last_ocp.pop(key, None), bundle_id=bid,
            acquisition_status=p.get("status", "complete"),
            source_git_hash=self.git_hash)
        if bid is not None:
            self._last_bundle[key] = bid       # wash 将挂 bundle 而非单条 measurement
            self._last_meas.pop(key, None)
        else:
            self._last_meas[key] = info["measurement_id"]
            self._last_bundle.pop(key, None)
        if p.get("role") == "baseline":
            self._baseline_mid = info["measurement_id"]
        self._wash_attempt.pop(key, None)          # 新 measurement -> attempt 归零

    def _on_wash_done(self, p):
        key = (p.get("plate"), p.get("well"))
        att = self._wash_attempt.get(key, 0) + 1
        self._wash_attempt[key] = att
        clean = p.get("clean")
        # 判定用的 standard CV（cleancheck 曲线）本体入库：role='cleancheck' 的
        # measurement + parquet + raw/mscript/csv artifact —— 磁盘丢了也能重建。
        cc_mid = None
        if p.get("csv"):
            try:
                cc = ingest_measurement(
                    self.led, p["csv"], session_id=self.session_id,
                    state_id=self._state({"plate": p.get("std_plate") or p.get("plate"),
                                          "well": p.get("std_well") or "B4"}),
                    technique="CV", role="cleancheck",
                    parameters=p.get("cleancheck_params"), data_dir=self.data_dir,
                    source_git_hash=self.git_hash)
                cc_mid = cc["measurement_id"]
            except Exception:                              # noqa: BLE001
                LOG.error("cleancheck ingest failed:\n%s", traceback.format_exc())
        bid = self._last_bundle.get(key)
        wid = self.led.add_wash(
            self.session_id, attempt=att,
            measurement_id=None if bid else self._last_meas.get(key),
            bundle_id=bid,
            baseline_id=self._baseline_mid, ended_at=now(),
            cleancheck_parameters_json=p.get("cleancheck_params"),
            acquisition_status="complete",
            clean_status={True: "clean", False: "not_clean"}.get(clean, "not_determined"),
            cleancheck_measurement_id=cc_mid,
            data_path=p.get("data_path"), notes=p.get("reason"))
        if clean is not None:
            v = type("V", (), {"clean": clean, "residual_in_sample": p.get("residual_in_sample"),
                               "residual_general": p.get("residual_general"),
                               "reason": p.get("reason")})
            self.led.save_is_clean(wid, v, baseline_measurement_id=self._baseline_mid)

    # ── 孔位组成提交（唯一来源：GUI CHEMICALS 页的手动录入）─────────────────
    def submit_state(self, d: dict) -> int:
        """{plate, well, total_volume_ml, solubility_m, ph,
            analyte:{chemical,formula?,concentration_m?}, acid_base:{…},
            supporting:{…}, source_type, notes}"""
        wid = self.led.get_or_create_well(d.get("plate", "?"), d["well"])
        return self.led.new_state(self.experiment_id, wid,
                                  total_volume_ml=d.get("total_volume_ml"),
                                  solubility_m=d.get("solubility_m"),
                                  ph=d.get("ph"),
                                  analyte=d.get("analyte"),
                                  acid_base=d.get("acid_base"),
                                  supporting=d.get("supporting"),
                                  source_type=d.get("source_type", "manual"),
                                  source_protocol=d.get("source_protocol"),
                                  source_git_hash=d.get("source_git_hash"),
                                  notes=d.get("notes"))

    def submit_calibration(self, d: dict) -> int:
        return self.led.add_calibration(**{k: d[k] for k in
            ("calibrated_at", "buffer_1_ph", "buffer_1_voltage", "buffer_2_ph",
             "buffer_2_voltage", "conversion_slope", "conversion_intercept",
             "status", "notes") if k in d})

    def submit_ph(self, d: dict) -> int:
        """{plate, well, calibration_id, final_ph, final_voltage, ...}"""
        wid = self.led.get_or_create_well(d.get("plate", "?"), d["well"])
        sid = self.led.current_state(self.experiment_id, wid)
        keys = ("started_at", "ended_at", "final_ph", "final_voltage",
                "stability_required", "stability_threshold_v", "reading_interval_s",
                "acquisition_status", "end_reason", "data_path", "notes")
        return self.led.add_ph_test(sid, d.get("calibration_id"),
                                    **{k: d[k] for k in keys if k in d})
