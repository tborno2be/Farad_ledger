"""backfill_couples: 给已回填的 CV 测量补跑峰分析（couples -> 结果表）。

用法：python -m ledger.backfill_couples <CVLabtest路径> [db路径]
对每条 role=sample 的 CV（含多扫速 bundle 的每个速率），用同 session 的 baseline
做 blank 跑 find_peak，save_find_peak 落库（peak_result / cv_peak_group /
cv_scan_result / cv_summary_result）。幂等：该 measurement 已有 current 分析则跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .api import Ledger


def main(cvlab, db):
    sys.path.insert(0, str(cvlab))
    import Project.electrochem.storage as store
    import Project.electrochem.analysis as ana

    led = Ledger(db)
    con = led.con
    done = skip = fail = 0
    for s in con.execute("SELECT session_id FROM session"):
        sid = s["session_id"]
        base = con.execute(
            "SELECT measurement_id, notes FROM measurement WHERE session_id=? "
            "AND role='baseline' AND technique='CV' ORDER BY measurement_id LIMIT 1",
            (sid,)).fetchone()
        if not base or not base["notes"] or not Path(base["notes"]).exists():
            continue
        blank = store.read(base["notes"], "CV")
        for m in con.execute(
                "SELECT measurement_id, notes FROM measurement WHERE session_id=? "
                "AND role='sample' AND technique='CV'", (sid,)).fetchall():
            mid = m["measurement_id"]
            if con.execute(
                    "SELECT 1 FROM analysis a JOIN analysis_input i "
                    "ON i.analysis_id=a.analysis_id WHERE a.analysis_type="
                    "'cv_peak_detection' AND a.is_current=1 AND i.input_role='sample' "
                    "AND i.measurement_id=?", (mid,)).fetchone():
                skip += 1
                continue
            try:
                sample = store.read(m["notes"], "CV")
                res = ana.find_peak(sample, blank, beta=0.5)
                led.save_find_peak(mid, res,
                                   baseline_measurement_id=base["measurement_id"],
                                   parameters={"beta": 0.5, "backfill": True})
                done += 1
            except Exception as exc:                       # noqa: BLE001
                fail += 1
                print(f"  measurement {mid}: 分析失败 ({exc})")
    print(f"couples 回填: 新增 {done}, 已有跳过 {skip}, 失败 {fail}")
    led.close()


if __name__ == "__main__":
    cvlab = sys.argv[1] if len(sys.argv) > 1 else "../CVLabtest"
    db = sys.argv[2] if len(sys.argv) > 2 else \
        str(Path(__file__).resolve().parent.parent / "cvlab.sqlite")
    main(cvlab, db)
