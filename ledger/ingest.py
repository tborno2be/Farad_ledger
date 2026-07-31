"""ingest: 已完成 measurement 的 data.csv → parquet（进本 repo data/）+ SQLite 登记。

parquet schema（定死）：长表四列 scan:int32, e_v:float64, i_a:float64, t_s:float64。
file metadata：technique / parameters_json / measurement_id / source_csv / ingested_at /
source_git_hash。CA 的恒电位存 metadata['potential_v']，t_s 是时间轴。

采集仍写 CSV（现场事实源）；ingest 在 session 结束后运行，采集中不碰 git。
"""
from __future__ import annotations

import csv as _csv
import json
import logging
import traceback
from pathlib import Path

from .api import Ledger, now

CURVE_FIELDS = ("scan", "e_v", "i_a", "t_s")


# ── CSV 解析（独立实现，不依赖 CVLabtest）────────────────────────────────────
def parse_csv(csv_path, technique):
    """returns (rows list[(scan,e,i,t)], meta dict)。
    CV: scan,potential,current,time 长表（scan 空 = 平衡段，跳过）。
    LSV/CA: '#k=v' 头 + 两列（LSV: E,i；CA: t,i，potential 在头里）。"""
    tech = technique.upper()
    rows, meta = [], {}
    if tech == "CV":
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                if not r["scan"]:
                    continue
                rows.append((int(r["scan"]), float(r["potential"]), float(r["current"]),
                             float(r["time"]) if r.get("time") else float("nan")))
        return rows, meta
    lines = Path(csv_path).read_text(encoding="utf-8").splitlines()
    j = 0
    while j < len(lines) and lines[j].startswith("#"):
        for tok in lines[j][1:].split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                meta[k] = v
        j += 1
    j += 1                                        # 列名行
    for ln in lines[j:]:
        if not ln.strip():
            continue
        a, b = ln.split(",")
        if tech == "LSV":
            rows.append((0, float(a), float(b), float("nan")))
        else:                                     # CA: t,i
            rows.append((0, float("nan"), float(b), float(a)))
    return rows, meta


def write_parquet(out_path, rows, file_meta: dict):
    import pyarrow as pa
    import pyarrow.parquet as pq
    cols = list(zip(*rows)) if rows else [[], [], [], []]
    table = pa.table({
        "scan": pa.array(cols[0], pa.int32()),
        "e_v": pa.array(cols[1], pa.float64()),
        "i_a": pa.array(cols[2], pa.float64()),
        "t_s": pa.array(cols[3], pa.float64()),
    })
    table = table.replace_schema_metadata(
        {k: str(v) for k, v in file_meta.items() if v is not None})
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(out_path))
    return out_path


# ── 入口 ─────────────────────────────────────────────────────────────────────
def ingest_measurement(led: Ledger, csv_path, *, session_id, state_id, technique,
                       role="sample", parameters=None, data_dir, name=None,
                       ocp_id=None, bundle_id=None, acquisition_status="complete",
                       source_git_hash=None) -> dict:
    """一条已完成 measurement：登记 SQLite 行 + 曲线转 parquet。
    data_dir = Farad_ledger/data/<experiment_name>；返回 {measurement_id, parquet}。"""
    csv_path = Path(csv_path)
    rows, meta = parse_csv(csv_path, technique)
    mid = led.add_measurement(session_id, state_id, technique.upper(), ocp_id=ocp_id,
                              bundle_id=bundle_id, parameters=parameters, role=role,
                              started_at=None, notes=str(csv_path))   # notes = 源 CSV，
                              # 供 GUI 事后把分析结果挂回这条 measurement
    # 附件内容先进库（防误删的源文件层，优先级最高）：raw.txt / script.mscr /
    # data.csv 的本体存 artifact 表——cvlab.sqlite 自足，磁盘文件全丢也能重建。
    n_art = 0
    run_dir = csv_path.parent
    for kind, fname in (("raw", "raw.txt"), ("mscript", "script.mscr"),
                        ("csv", csv_path.name)):
        p = run_dir / fname
        if p.exists():
            led.add_artifact(kind, fname, p.read_bytes(), measurement_id=mid)
            n_art += 1
    # parquet 是可重建缓存：写失败（如 pyarrow 未装）只记日志，不影响
    # 状态落定和 artifact——data_path 留空 = 缓存待重建。
    stem = name or csv_path.parent.name
    out = Path(data_dir) / f"m{mid:06d}_{technique.upper()}_{stem}.parquet"
    fmeta = {"technique": technique.upper(), "measurement_id": mid,
             "parameters_json": json.dumps(parameters or {}, ensure_ascii=False),
             "source_csv": str(csv_path), "ingested_at": now(),
             "source_git_hash": source_git_hash, **meta}
    data_path = None
    try:
        write_parquet(out, rows, fmeta)
        data_path = str(out)
    except Exception:                                      # noqa: BLE001
        logging.getLogger("farad_ledger").error(
            "parquet cache write failed for measurement %s (curve is safe in "
            "artifact/csv):\n%s", mid, traceback.format_exc())
    led.finish_measurement(mid, acquisition_status=acquisition_status,
                           data_path=data_path)
    return {"measurement_id": mid, "parquet": data_path, "n_points": len(rows),
            "artifacts": n_art}
