"""seed: 把实验室现役的电极组合和用过的化学品登进库（幂等，可反复跑）。

用法（在 Farad_ledger 目录）：python -m ledger.seed [db路径，默认 cvlab.sqlite]
未知信息按约定留空：石墨对电极面积、Ag/AgCl 填充液浓度、化学品存放位置/入库时间。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .api import Ledger

# 现役三电极（2026-07 兵役状态；名字与 GUI 默认一致）
ELECTRODES = [
    dict(name="WE-01", electrode_type="working", material="Pt",
         diameter_mm=2.0, area_cm2=0.0314),        # 2 mm Pt 盘电极；面积=几何面积 πr²
    dict(name="AgAgCl-01", electrode_type="reference", material="Ag/AgCl",
         reference_fill_chemical="KCl"),          # 浓度未知，留空
    dict(name="Graphite-01", electrode_type="counter", material="graphite"),  # 面积留空
]

# 目前实验里出现过的化学品（location / received_at 未知留空）
CHEMICALS = [
    ("H2SO4", "H2SO4"),
    ("water", "H2O"),
    ("methylene blue", "C16H18ClN3S"),
    ("ferrocene", "C10H10Fe"),
]


def main(db="cvlab.sqlite"):
    led = Ledger(db)
    for e in ELECTRODES:
        eid = led.upsert_electrode(e.pop("name"), e.pop("electrode_type"), **e)
        print(f"electrode #{eid}")
    sid = led.find_or_create_setup("WE-01", "AgAgCl-01", "Graphite-01")
    print(f"electrode_setup #{sid} (WE-01 / AgAgCl-01 / Graphite-01)")
    for name, formula in CHEMICALS:
        r = led._one("SELECT chemical_id FROM chemical WHERE name=?", name)
        cid = r[0] if r else led._ins("chemical", name=name, formula=formula)
        print(f"chemical #{cid}: {name}")
    led.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         str(Path(__file__).resolve().parent.parent / "cvlab.sqlite"))
