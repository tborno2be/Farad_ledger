"""Ledger: SQLite 的唯一读写入口（薄函数层，schema 见 ../schema.sql）。

约定：时间戳一律 ISO-8601 字符串（now() 生成）；布尔 0/1；所有 insert 返回新行 id。
分析结果映射（find_peak couples / is_clean / cv_window -> 结果表）也在这里，
这样 CVLabtest 的 analysis.py 保持纯计算。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))   # 幂等
    _migrate(con)
    return con


def _migrate(con) -> None:
    """轻量列迁移：老库缺新列时 ALTER 补上（CREATE IF NOT EXISTS 不会改旧表）。"""
    def cols(t):
        return [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    for table, col, ddl in (
            ("well_state", "ph", "ALTER TABLE well_state ADD COLUMN ph REAL"),
            ("well_state", "ph_test_id",
             "ALTER TABLE well_state ADD COLUMN ph_test_id INTEGER "
             "REFERENCES ph_test(ph_test_id)"),
            ("wash", "cleancheck_measurement_id",
             "ALTER TABLE wash ADD COLUMN cleancheck_measurement_id INTEGER "
             "REFERENCES measurement(measurement_id)")):
        if col not in cols(table):
            con.execute(ddl)
    con.commit()


def _jd(x):
    return json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x


class Ledger:
    def __init__(self, db_path):
        self.con = connect(db_path)

    def close(self):
        self.con.close()

    def _ins(self, table, **cols):
        cols = {k: _jd(v) for k, v in cols.items() if v is not None}
        keys = ",".join(cols)
        ph = ",".join("?" * len(cols))
        cur = self.con.execute(f"INSERT INTO {table}({keys}) VALUES({ph})", list(cols.values()))
        self.con.commit()
        return cur.lastrowid

    def _upd(self, table, id_col, id_val, **cols):
        cols = {k: _jd(v) for k, v in cols.items() if v is not None}
        if not cols:
            return
        sets = ",".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE {table} SET {sets} WHERE {id_col}=?", [*cols.values(), id_val])
        self.con.commit()

    def _one(self, sql, *args):
        return self.con.execute(sql, args).fetchone()

    # ── experiment / session ────────────────────────────────────────────────
    def get_or_create_experiment(self, name) -> int:
        r = self._one("SELECT experiment_id FROM experiment WHERE experiment_name=? "
                      "AND status='active'", name)
        if r:
            return r[0]
        return self._ins("experiment", experiment_name=name, created_at=now())

    def start_session(self, experiment_id, name=None, created_via="gui",
                      electrode_setup_id=None) -> int:
        sid = self._ins("session", experiment_id=experiment_id, session_name=name,
                        created_via=created_via, created_at=now(), started_at=now(),
                        status="running", electrode_setup_id=electrode_setup_id)
        self._upd("experiment", "experiment_id", experiment_id,
                  started_at=self._one("SELECT started_at FROM experiment WHERE experiment_id=?",
                                       experiment_id)["started_at"] or now())
        return sid

    def end_session(self, session_id, status="completed", end_reason="normal_completion"):
        self._upd("session", "session_id", session_id,
                  ended_at=now(), status=status, end_reason=end_reason)

    # ── racks / wells / states ──────────────────────────────────────────────
    def get_or_create_rack(self, position_key, name=None) -> int:
        r = self._one("SELECT rack_id FROM rack WHERE position_key=?", position_key)
        return r[0] if r else self._ins("rack", name=name or position_key,
                                        position_key=position_key)

    def get_or_create_well(self, position_key, position_name) -> int:
        rid = self.get_or_create_rack(position_key)
        r = self._one("SELECT well_id FROM well WHERE rack_id=? AND position_name=?",
                      rid, position_name)
        return r[0] if r else self._ins("well", rack_id=rid, position_name=position_name)

    def get_or_create_chemical(self, name, formula=None) -> int:
        r = self._one("SELECT chemical_id FROM chemical WHERE name=?", name)
        return r[0] if r else self._ins("chemical", name=name, formula=formula,
                                        received_at=now())

    def current_state(self, experiment_id, well_id) -> int:
        """该孔当前 state；没有则自动建 version 0 'composition unknown'。"""
        r = self._one("SELECT state_id FROM well_state WHERE experiment_id=? AND well_id=? "
                      "AND ended_at IS NULL ORDER BY version DESC LIMIT 1",
                      experiment_id, well_id)
        if r:
            return r[0]
        return self._ins("well_state", experiment_id=experiment_id, well_id=well_id,
                         version=0, source_type="manual", created_at=now(),
                         notes="auto-created: composition unknown")

    def new_state(self, experiment_id, well_id, *, total_volume_ml=None,
                  solubility_m=None, ph=None, ph_test_id=None,
                  analyte=None, acid_base=None,
                  supporting=None, source_type="opentrons_protocol",
                  source_protocol=None, source_git_hash=None, notes=None) -> int:
        """冻结一个新 state（自动结束旧 state、version+1）。
        analyte / acid_base / supporting = {'chemical','formula'?,'concentration_m'?}
        或 None；solubility_m = 分析物溶解度（M）；ph = 孔溶液 pH（均可空）。"""
        def _chem(c):
            if not c or not c.get("chemical"):
                return None, None
            return (self.get_or_create_chemical(c["chemical"], c.get("formula")),
                    c.get("concentration_m"))
        a_id, a_c = _chem(analyte)
        b_id, b_c = _chem(acid_base)
        s_id, s_c = _chem(supporting)
        prev = self._one("SELECT state_id, version FROM well_state WHERE experiment_id=? "
                         "AND well_id=? ORDER BY version DESC LIMIT 1", experiment_id, well_id)
        if prev:
            self._upd("well_state", "state_id", prev["state_id"], ended_at=now())
        return self._ins("well_state", experiment_id=experiment_id, well_id=well_id,
                         version=(prev["version"] + 1 if prev else 1),
                         total_volume_ml=total_volume_ml, solubility_m=solubility_m,
                         ph=ph, ph_test_id=ph_test_id,
                         analyte_chemical_id=a_id, analyte_concentration_m=a_c,
                         acid_base_chemical_id=b_id, acid_base_concentration_m=b_c,
                         supporting_chemical_id=s_id, supporting_concentration_m=s_c,
                         source_type=source_type, source_protocol=source_protocol,
                         source_git_hash=source_git_hash, created_at=now(), notes=notes)

    def set_state_ph(self, state_id, ph_test_id):
        """把一次 ph_test 设为该 state 的正式 pH（ph_test_id 链接 + final_ph 快照）。"""
        r = self._one("SELECT final_ph FROM ph_test WHERE ph_test_id=?", ph_test_id)
        if not r:
            raise ValueError(f"ph_test {ph_test_id} 不存在")
        self._upd("well_state", "state_id", state_id,
                  ph=r["final_ph"], ph_test_id=ph_test_id)

    # ── 过程记录 ────────────────────────────────────────────────────────────
    def add_ocp(self, session_id, state_id, **f) -> int:
        return self._ins("ocp_check", session_id=session_id, state_id=state_id, **f)

    def add_measurement(self, session_id, state_id, technique, *, ocp_id=None,
                        bundle_id=None, parameters=None, role="sample", **f) -> int:
        return self._ins("measurement", session_id=session_id, state_id=state_id,
                         ocp_id=ocp_id, bundle_id=bundle_id, technique=technique,
                         parameters_json=parameters, role=role,
                         created_at=now(), **f)

    def finish_measurement(self, measurement_id, acquisition_status="complete",
                           end_reason=None, data_path=None):
        self._upd("measurement", "measurement_id", measurement_id, ended_at=now(),
                  acquisition_status=acquisition_status, end_reason=end_reason,
                  data_path=data_path)

    def add_wash(self, session_id, *, attempt=1, measurement_id=None, bundle_id=None,
                 baseline_id=None, **f) -> int:
        return self._ins("wash", session_id=session_id, attempt=attempt,
                         measurement_id=measurement_id, bundle_id=bundle_id,
                         baseline_id=baseline_id, **f)

    def add_bundle(self, session_id, bundle_type="multi_rate_cv") -> int:
        return self._ins("measurement_bundle", session_id=session_id,
                         bundle_type=bundle_type, created_at=now())

    def add_calibration(self, **f) -> int:
        return self._ins("probe_calibration", calibrated_at=f.pop("calibrated_at", now()), **f)

    def add_ph_test(self, state_id, calibration_id=None, *, session_id=None, **f) -> int:
        return self._ins("ph_test", session_id=session_id, state_id=state_id,
                         calibration_id=calibration_id, **f)

    # ── electrodes / maintenance（GUI2 polish 标记）──────────────────────────
    _ELECTRODE_PROPS = ("material", "diameter_mm", "area_cm2",
                        "reference_fill_chemical", "reference_fill_concentration_m",
                        "serial_number", "notes")

    def get_or_create_electrode(self, name, electrode_type="working") -> int:
        r = self._one("SELECT electrode_id FROM electrode WHERE name=?", name)
        return r[0] if r else self._ins("electrode", name=name,
                                        electrode_type=electrode_type)

    def upsert_electrode(self, name, electrode_type="working", **props) -> int:
        """按 name 建/更新电极；props 只覆盖非 None 的 _ELECTRODE_PROPS 字段。"""
        cols = {k: v for k, v in props.items()
                if k in self._ELECTRODE_PROPS and v is not None}
        r = self._one("SELECT electrode_id, electrode_type FROM electrode WHERE name=?",
                      name)
        if r:
            upd = dict(cols)
            if electrode_type and electrode_type != r["electrode_type"]:
                upd["electrode_type"] = electrode_type
            if upd:
                self._upd("electrode", "electrode_id", r["electrode_id"], **upd)
            return r["electrode_id"]
        return self._ins("electrode", name=name, electrode_type=electrode_type, **cols)

    def find_or_create_setup(self, we, re_, ce) -> int:
        """we/re_/ce = electrode NAMES。组合没有名字：内容就是每个 role 的
        electrode_id，同一『三支组合』唯一，电脑只认 setup_id。"""
        wid = self.get_or_create_electrode(we, "working")
        rid = self.get_or_create_electrode(re_, "reference")
        cid = self.get_or_create_electrode(ce, "counter")
        r = self._one("SELECT setup_id FROM electrode_setup WHERE working_electrode=? "
                      "AND reference_electrode=? AND counter_electrode=? AND active=1",
                      wid, rid, cid)
        return r[0] if r else self._ins("electrode_setup", working_electrode=wid,
                                        reference_electrode=rid, counter_electrode=cid)

    def add_maintenance(self, electrode_name, *, electrode_type="working",
                        maintenance_type="polish", method="manual",
                        status="completed", trigger_wash_id=None, notes=None) -> int:
        eid = self.get_or_create_electrode(electrode_name, electrode_type)
        return self._ins("electrode_maintenance", electrode_id=eid,
                         maintenance_type=maintenance_type, method=method, status=status,
                         trigger_wash_id=trigger_wash_id, completed_at=now(), notes=notes)

    # ── artifacts（文件内容进库，防误删；kind: raw/csv/mscript）──────────────
    def add_artifact(self, kind, filename, content: bytes, *,
                     measurement_id=None, ocp_id=None) -> int:
        import hashlib
        return self._ins("artifact", measurement_id=measurement_id, ocp_id=ocp_id,
                         kind=kind, filename=str(filename), content=content,
                         sha256=hashlib.sha256(content).hexdigest(), created_at=now())

    def get_artifact(self, measurement_id, kind) -> bytes | None:
        r = self._one("SELECT content FROM artifact WHERE measurement_id=? AND kind=? "
                      "ORDER BY artifact_id DESC LIMIT 1", measurement_id, kind)
        return r[0] if r else None

    def restore_artifacts(self, measurement_id, out_dir) -> list:
        """从库里把一条 measurement 的全部文件写回磁盘（误删恢复）。"""
        from pathlib import Path
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for r in self.con.execute("SELECT filename, content FROM artifact "
                                  "WHERE measurement_id=?", (measurement_id,)):
            (out / r["filename"]).write_bytes(r["content"])
            written.append(str(out / r["filename"]))
        return written

    def measurement_by_csv(self, csv_path) -> int | None:
        """由源 CSV 路径反查 measurement（ingest 时把源 CSV 存进 notes）。"""
        r = self._one("SELECT measurement_id FROM measurement WHERE notes=? "
                      "ORDER BY measurement_id DESC LIMIT 1", str(csv_path))
        return r[0] if r else None

    # ── analysis（版本化 + is_current 翻转）─────────────────────────────────
    def new_analysis(self, analysis_type, *, algorithm_name=None, algorithm_version=None,
                     code_version=None, parameters=None,
                     inputs=()) -> int:
        """inputs = [(input_role, kind, id)], kind in measurement/bundle/wash/ocp。
        同（首个 input, analysis_type）的旧 analysis 全部 is_current=0。"""
        aid = self._ins("analysis", analysis_type=analysis_type,
                        algorithm_name=algorithm_name, algorithm_version=algorithm_version,
                        code_version=code_version, parameters_json=parameters,
                        started_at=now(), completed_at=now())
        colmap = {"measurement": "measurement_id", "bundle": "bundle_id",
                  "wash": "wash_id", "ocp": "ocp_id"}
        for role, kind, xid in inputs:
            self._ins("analysis_input", analysis_id=aid, input_role=role,
                      **{colmap[kind]: xid})
        if inputs:
            role, kind, xid = inputs[0]
            self.con.execute(
                f"""UPDATE analysis SET is_current=0 WHERE analysis_id != ? AND analysis_id IN
                    (SELECT a.analysis_id FROM analysis a
                     JOIN analysis_input i ON i.analysis_id=a.analysis_id
                     WHERE a.analysis_type=? AND i.input_role=? AND i.{colmap[kind]}=?)""",
                (aid, analysis_type, role, xid))
            self.con.commit()
        return aid

    def save_find_peak(self, measurement_id, result, *, baseline_measurement_id=None,
                       parameters=None, algorithm_version=None, code_version=None) -> int:
        """FindPeakResult（含 couples）→ peak_result / cv_peak_group / cv_scan_result /
        cv_summary_result。result 直接传 analysis.find_peak 的返回对象。"""
        inputs = [("sample", "measurement", measurement_id)]
        if baseline_measurement_id:
            inputs.append(("baseline", "measurement", baseline_measurement_id))
        aid = self.new_analysis("cv_peak_detection", algorithm_name="find_peak",
                                algorithm_version=algorithm_version, code_version=code_version,
                                parameters=parameters, inputs=inputs)
        _f = lambda v: None if v is None or (isinstance(v, float) and v != v) else float(v)
        for p in getattr(result, "peaks", []):
            branch = "anodic" if p.polarity == 1 else "cathodic"
            for r in p.per_scan:
                self._ins("peak_result", analysis_id=aid, measurement_id=measurement_id,
                          scan_number=r.scan, branch=branch,
                          ep_v=_f(r.Ep), e_sd_apex_v=_f(r.E_sd_apex),
                          integrated_ip_a=_f(r.ip), fwhm_v=_f(r.fwhm_E_mV / 1000.0),
                          asymmetry=_f(r.asym), classification=p.model
                          if p.model in ("single", "grey", "double_suspected") else "single",
                          is_truncated=int(bool(r.truncated)), pon=_f(r.pon),
                          quality_status=r.status if r.status in
                          ("success", "provisional", "reject") else "provisional")
        rtype = {0: "redox_pair", 1: "irreversible_anodic", -1: "irreversible_cathodic"}
        for c in getattr(result, "couples", []):
            gid = self._ins("cv_peak_group", measurement_id=measurement_id,
                            peak_group_order=c.pair_idx,
                            reaction_type=rtype[c.polarity if c.irreversible else 0],
                            created_by="manual" if c.source == "manual" else "automatic",
                            notes=c.reason or None)
            src = {"auto": "automatic", "manual": "manual", "adjusted": "adjusted"}
            for r in c.per_scan:
                self._ins("cv_scan_result", analysis_id=aid, measurement_id=measurement_id,
                          scan_number=r.scan, peak_group_id=gid,
                          epa_v=_f(r.Epa), epc_v=_f(r.Epc), ipa_a=_f(r.ipa), ipc_a=_f(r.ipc),
                          delta_ep_v=_f(r.dEp), e_half_v=_f(r.Ehalf),
                          dev_epa_mv=_f(r.dev_Epa_mV), dev_epc_mv=_f(r.dev_Epc_mV),
                          dev_e_half_mv=_f(r.dev_Ehalf_mV),
                          selection_source=src.get(r.source, "automatic"))
            ratio = None
            if _f(c.ipa) and _f(c.ipc):
                ratio = abs(c.ipc / c.ipa)
            self._ins("cv_summary_result", analysis_id=aid, measurement_id=measurement_id,
                      peak_group_id=gid, n_scans_total=len(c.per_scan),
                      n_scans_used=c.n_scans_used, used_scan_numbers_json=c.selected_scans,
                      epa_mean_v=_f(c.Epa), epc_mean_v=_f(c.Epc),
                      ipa_mean_a=_f(c.ipa), ipc_mean_a=_f(c.ipc),
                      delta_ep_mean_v=_f(c.dEp), e_half_mean_v=_f(c.Ehalf),
                      e_half_spread_mv=_f(c.spread_mV), abs_ipc_ipa_ratio=_f(ratio),
                      quality_status="accepted" if not c.truncated else "rejected")
        return aid

    def save_manual_peaks(self, measurement_id, pairs, *, parameters=None) -> int:
        """GUI 手动标峰/配对 → 新的 cv_peak_detection analysis（is_current 翻转，
        自动覆盖之前算法选的峰）。pairs = [{Epa,Epc,ipa,ipc,Ehalf,dEp,
        per_scan:[{scan,Epa,Epc,ipa,ipc,Ehalf}]}...]"""
        aid = self.new_analysis("cv_peak_detection", algorithm_name="manual",
                                parameters=parameters,
                                inputs=[("sample", "measurement", measurement_id)])
        _f = lambda v: None if v is None or (isinstance(v, float) and v != v) else float(v)
        for i, p in enumerate(pairs or []):
            gid = self._ins("cv_peak_group", measurement_id=measurement_id,
                            peak_group_order=i, reaction_type="redox_pair",
                            created_by="manual", notes=p.get("notes"))
            per = p.get("per_scan") or []
            for r in per:
                epa, epc = _f(r.get("Epa")), _f(r.get("Epc"))
                self._ins("cv_scan_result", analysis_id=aid, measurement_id=measurement_id,
                          scan_number=r.get("scan"), peak_group_id=gid,
                          epa_v=epa, epc_v=epc,
                          ipa_a=_f(r.get("ipa")), ipc_a=_f(r.get("ipc")),
                          delta_ep_v=(abs(epa - epc) if epa is not None and epc is not None
                                      else None),
                          e_half_v=_f(r.get("Ehalf")), selection_source="manual")
            self._ins("cv_summary_result", analysis_id=aid, measurement_id=measurement_id,
                      peak_group_id=gid, n_scans_total=len(per), n_scans_used=len(per),
                      used_scan_numbers_json=[r.get("scan") for r in per],
                      epa_mean_v=_f(p.get("Epa")), epc_mean_v=_f(p.get("Epc")),
                      ipa_mean_a=_f(p.get("ipa")), ipc_mean_a=_f(p.get("ipc")),
                      delta_ep_mean_v=_f(p.get("dEp")), e_half_mean_v=_f(p.get("Ehalf")),
                      abs_ipc_ipa_ratio=(abs(p["ipc"] / p["ipa"])
                                         if _f(p.get("ipa")) and _f(p.get("ipc")) else None),
                      quality_status="accepted")
        return aid

    def save_cottrell(self, measurement_id, fit, *, parameters=None) -> int:
        """CA Cottrell 拟合 → ca_cottrell_result（is_current 版本化同 find_peak）。
        fit = analysis.cottrell_fit 的返回 dict。"""
        aid = self.new_analysis("ca_cottrell", algorithm_name="cottrell_fit",
                                parameters=parameters,
                                inputs=[("sample", "measurement", measurement_id)])
        _f = lambda v: None if v is None or (isinstance(v, float) and v != v) else float(v)
        self._ins("ca_cottrell_result", analysis_id=aid, measurement_id=measurement_id,
                  beta=_f(fit.get("beta")), slope=_f(fit.get("slope")),
                  intercept=_f(fit.get("intercept")), r_squared=_f(fit.get("r_squared")),
                  fit_start_s=_f(fit.get("fit_start_s")), fit_end_s=_f(fit.get("fit_end_s")),
                  tail_current_a=_f(fit.get("tail_current_a")),
                  max_abs_current_a=_f(fit.get("max_abs_current_a")),
                  quality_status=fit.get("quality_status", "accepted"),
                  notes=fit.get("notes"))
        return aid

    def save_randles_sevcik(self, bundle_id, peak_group_id, fits, *,
                            parameters=None) -> int:
        """多扫速 bundle 的 Randles–Ševčík 拟合 → cv_randles_sevcik_result。
        fits = [{branch:'anodic'|'cathodic', slope, intercept, r_squared, n}...]；
        peak_group_id = 该 bundle 某 rate 的当前峰组（rate 增多时整组重存，
        is_current 以 bundle 为范围翻转，旧拟合折叠）。"""
        aid = self.new_analysis("cv_randles_sevcik", algorithm_name="randles_sevcik",
                                parameters=parameters,
                                inputs=[("subject", "bundle", bundle_id)])
        _f = lambda v: None if v is None or (isinstance(v, float) and v != v) else float(v)
        for f in fits or []:
            self._ins("cv_randles_sevcik_result", analysis_id=aid, bundle_id=bundle_id,
                      peak_group_id=peak_group_id, branch=f["branch"],
                      n_points=f.get("n"), slope=_f(f.get("slope")),
                      intercept=_f(f.get("intercept")), r_squared=_f(f.get("r_squared")),
                      quality_status=f.get("quality_status", "accepted"))
        return aid

    def save_is_clean(self, wash_id, verdict, *, baseline_measurement_id=None,
                      parameters=None) -> int:
        aid = self.new_analysis("wash_clean_check", algorithm_name="is_clean",
                                parameters=parameters, inputs=[("subject", "wash", wash_id)])
        self._ins("wash_clean_check_result", analysis_id=aid, wash_id=wash_id,
                  baseline_measurement_id=baseline_measurement_id,
                  residual_in_sample=str(getattr(verdict, "residual_in_sample", None)),
                  residual_general=str(getattr(verdict, "residual_general", None)),
                  clean_status="clean" if getattr(verdict, "clean", False) else "not_clean",
                  reason=getattr(verdict, "reason", None))
        return aid

    def save_cv_window(self, measurement_id, win, *, parameters=None) -> int:
        aid = self.new_analysis("cv_parameters_detect", algorithm_name="window_from_cv",
                                parameters=parameters,
                                inputs=[("sample", "measurement", measurement_id)])
        g = lambda side, k: getattr(getattr(win, side, None), k, None)
        self._ins("cv_window_result", analysis_id=aid, measurement_id=measurement_id,
                  scan_used=getattr(win, "scan_used", None), valid=int(bool(win.valid)),
                  lower_v=win.lower_v, upper_v=win.upper_v,
                  anodic_detected=g("anodic", "detected"), anodic_limit_v=g("anodic", "limit_v"),
                  anodic_reason=g("anodic", "reason"),
                  cathodic_detected=g("cathodic", "detected"),
                  cathodic_limit_v=g("cathodic", "limit_v"),
                  cathodic_reason=g("cathodic", "reason"),
                  clamped=int(bool(getattr(win, "clamped", False))), reason=win.reason)
        return aid
