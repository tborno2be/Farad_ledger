"""backfill: 把 Fc-3 和 MB（MB-4 + MB-6-ms 合并为一个 experiment）落进库（幂等靠先查重）。

用法：python -m ledger.backfill_2026_07 <CVLabtest路径> [db路径]
事实（Eva 口述 2026-07-30）：两批都用现役电极组 setup(WE-01/AgAgCl-01/Graphite-01)；
Fc-3 开始前 WE polish、MB 开始前 WE 再 polish；Fc-3 七孔均为 1 mM FcMeOH / 0.1 M KCl，
standard=0.1 M KCl；MB 1 mM、supporting 1 M H2SO4（acid 槽留空）、baseline=1 M H2SO4。
时间取各 run 的 ocp_log.jsonl 真实时间。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .api import Ledger
from .ingest import ingest_measurement

FCMEOH = {"chemical": "FcMeOH", "formula": "C11H12FeO"}
KCL = {"chemical": "KCl", "formula": "KCl"}
H2SO4 = {"chemical": "H2SO4", "formula": "H2SO4"}


def _times(root):
    out = {}
    p = root / "ocp_log.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            out.setdefault((e.get("name"), e.get("tech")), e["when"])
            out.setdefault(("__first__",), e["when"])
            out[("__last__",)] = e["when"]
    return out


def _do_experiment(led, name, root, data_dir, wells_state, sessions_note):
    if led._one("SELECT experiment_id FROM experiment WHERE experiment_name=?", name):
        print(f"{name}: 已存在，跳过")
        return None
    exp = led.get_or_create_experiment(name)
    setup = led.find_or_create_setup("WE-01", "AgAgCl-01", "Graphite-01")
    tms = _times(root)
    t0 = tms.get(("__first__",))
    # polish（每批开始前 WE 打磨一次）
    mid = led.add_maintenance("WE-01", electrode_type="working",
                              notes=f"before {name} (backfill)")
    led._upd("electrode_maintenance", "maintenance_id", mid,
             completed_at=t0, requested_at=t0)
    sid = led.start_session(exp, name=sessions_note, electrode_setup_id=setup)
    led._upd("session", "session_id", sid, created_at=t0, started_at=t0)
    # 孔位状态
    states = {}
    for well, kw in wells_state.items():
        wid = led.get_or_create_well("cv_deck_1", well)
        st = led.new_state(exp, wid, source_type="manual", **kw)
        led._upd("well_state", "state_id", st, created_at=t0)
        states[well] = st
    # 测量（CV/CA 单条 + CV_ms 逐速成 bundle）
    n = 0
    for d in sorted(root.iterdir()):
        nm = d.name
        if not d.is_dir() or nm == "OCP":
            continue
        if nm.startswith("CV_ms_"):
            base = nm[len("CV_ms_"):]
            well = base.split("_")[0]
            bid = led.add_bundle(sid)
            for csvf in sorted(d.glob(f"{base}_*.csv")):
                rate = csvf.stem[len(base) + 1:]
                info = ingest_measurement(
                    led, csvf, session_id=sid, state_id=states.get(well),
                    technique="CV", role="sample", bundle_id=bid,
                    parameters={"scan_rate_mv_s": float(rate)}, data_dir=data_dir)
                n += 1
            continue
        csvf = d / "data.csv"
        if not csvf.exists():
            continue
        tech = nm.split("_")[0]
        if tech not in ("CV", "CA", "LSV"):
            continue
        role = "baseline" if "baseline" in nm else "sample"
        well = "B4" if role == "baseline" else nm.split("_")[1]
        info = ingest_measurement(
            led, csvf, session_id=sid, state_id=states.get(well),
            technique=tech, role=role, data_dir=data_dir)
        w = tms.get((("baseline" if role == "baseline" else well), tech)) or t0
        led._upd("measurement", "measurement_id", info["measurement_id"],
                 created_at=w, started_at=w)
        n += 1
    led.end_session(sid)
    led._upd("session", "session_id", sid, ended_at=tms.get(("__last__",)))
    print(f"{name}: session#{sid} 落库 {n} 条测量")
    return sid


def main(cvlab, db):
    led = Ledger(db)
    exp = Path(cvlab) / "experiments"
    dd = Path(db).resolve().parent / "data"
    fc_state = {w: dict(analyte={**FCMEOH, "concentration_m": 1e-3},
                        supporting={**KCL, "concentration_m": 0.1})
                for w in ("A1", "A2", "A3", "A4", "B1", "B2", "B3")}
    fc_state["B4"] = dict(supporting={**KCL, "concentration_m": 0.1})   # standard
    _do_experiment(led, "Fc-3", exp / "Fc-3", dd / "Fc-3", fc_state,
                   "Fc-3 (backfill)")
    mb_state = {"A1": dict(analyte={"chemical": "methylene blue",
                                    "concentration_m": 1e-3},
                           supporting={**H2SO4, "concentration_m": 1.0}),
                "B4": dict(supporting={**H2SO4, "concentration_m": 1.0})}
    # MB-4 + MB-6-ms 合并为一个 experiment（两个 session，模拟追加 run 改 baseline 范围）
    exp_id = led.get_or_create_experiment("MB")
    for sub in ("MB-4", "MB-6-ms"):
        # 每个子文件夹一个 session，孔状态挂同一 experiment
        name = f"MB[{sub}]"
        if led._one("SELECT session_id FROM session WHERE session_name=?", name):
            print(f"{name}: 已存在，跳过")
            continue
        root = exp / sub
        tms = _times(root)
        t0 = tms.get(("__first__",))
        setup = led.find_or_create_setup("WE-01", "AgAgCl-01", "Graphite-01")
        if sub == "MB-4":                      # MB 批次开始前 polish 一次
            mid = led.add_maintenance("WE-01", electrode_type="working",
                                      notes="before MB (backfill)")
            led._upd("electrode_maintenance", "maintenance_id", mid,
                     completed_at=t0, requested_at=t0)
        sid = led.start_session(exp_id, name=name, electrode_setup_id=setup)
        led._upd("session", "session_id", sid, created_at=t0, started_at=t0)
        states = {}
        for well, kw in mb_state.items():
            wid = led.get_or_create_well("cv_deck_1", well)
            r = led._one("SELECT state_id FROM well_state WHERE experiment_id=? AND "
                         "well_id=? AND ended_at IS NULL ORDER BY version DESC LIMIT 1",
                         exp_id, wid)
            states[well] = r[0] if r else led.new_state(exp_id, wid,
                                                        source_type="manual", **kw)
        n = 0
        for d in sorted(root.iterdir()):
            nm = d.name
            if not d.is_dir() or nm == "OCP":
                continue
            if nm.startswith("CV_ms_"):
                base = nm[len("CV_ms_"):]
                well = base.split("_")[0]
                bid = led.add_bundle(sid)
                for csvf in sorted(d.glob(f"{base}_*.csv")):
                    ingest_measurement(led, csvf, session_id=sid,
                                       state_id=states.get(well), technique="CV",
                                       role="sample", bundle_id=bid,
                                       parameters={"scan_rate_mv_s":
                                                   float(csvf.stem[len(base)+1:])},
                                       data_dir=dd / "MB")
                    n += 1
                continue
            csvf = d / "data.csv"
            if not csvf.exists() or nm.split("_")[0] not in ("CV", "CA", "LSV"):
                continue
            role = "baseline" if "baseline" in nm else "sample"
            well = "B4" if role == "baseline" else nm.split("_")[1]
            info = ingest_measurement(led, csvf, session_id=sid,
                                      state_id=states.get(well),
                                      technique=nm.split("_")[0], role=role,
                                      data_dir=dd / "MB")
            w = tms.get((("baseline" if role == "baseline" else well),
                         nm.split("_")[0])) or t0
            led._upd("measurement", "measurement_id", info["measurement_id"],
                     created_at=w, started_at=w)
            n += 1
        led.end_session(sid)
        led._upd("session", "session_id", sid, ended_at=tms.get(("__last__",)))
        print(f"{name}: session#{sid} 落库 {n} 条测量")
    led.close()


if __name__ == "__main__":
    cvlab = sys.argv[1] if len(sys.argv) > 1 else "../CVLabtest"
    db = sys.argv[2] if len(sys.argv) > 2 else \
        str(Path(__file__).resolve().parent.parent / "cvlab.sqlite")
    main(cvlab, db)
