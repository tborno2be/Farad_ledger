"""reader: parquet 曲线 → 与 CVLabtest storage.read 完全同构的内存格式。

CV/LSV -> {scan: (E, i, t)}（numpy 数组）；CA -> (potential_V, t, i)。
GUI/analysis 拿到返回值后不需要知道数据来自 CSV 还是 parquet。
"""
from __future__ import annotations

import numpy as np


def _load(parquet_path):
    import pyarrow.parquet as pq
    t = pq.read_table(str(parquet_path))
    meta = {k.decode(): v.decode() for k, v in (t.schema.metadata or {}).items()}
    return t, meta


def metadata(parquet_path) -> dict:
    return _load(parquet_path)[1]


def read(parquet_path):
    """与 storage.read(csv, technique) 相同的返回契约（technique 从 metadata 取）。"""
    t, meta = _load(parquet_path)
    tech = meta.get("technique", "CV").upper()
    scan = t["scan"].to_numpy()
    E = t["e_v"].to_numpy()
    i = t["i_a"].to_numpy()
    ts = t["t_s"].to_numpy()
    if tech == "CA":
        return (float(meta.get("potential_V", meta.get("potential_v", "nan"))), ts, i)
    out = {}
    for s in np.unique(scan):
        m = scan == s
        out[int(s)] = (E[m], i[m], ts[m])
    return out
