"""replay_edits: 把 data/edit_log.jsonl（dataset viewer 的手动编辑记录）重放进库。

跨机重建链条的最后一步：
  python -m ledger.seed
  python -m ledger.backfill_2026_07 <CVLabtest路径>
  python -m ledger.backfill_couples <CVLabtest路径>
  python -m ledger.replay_edits

幂等：update 按 pk 覆盖（日志按时间顺序，最后写入生效）；
      insert 用 INSERT OR IGNORE 并带上原 id（外键引用保持稳定）。
用法：python -m ledger.replay_edits [db路径] [日志路径]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(db, log):
    log = Path(log)
    if not log.exists():
        print("无编辑日志，跳过")
        return
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys = ON")
    n_upd = n_ins = n_fail = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        t = e["table"]
        try:
            pk = next(r[1] for r in con.execute(f"PRAGMA table_info({t})") if r[5])
            if e["action"] == "update":
                ch = {k: v["new"] for k, v in e["changes"].items()}
                sets = ",".join(f"{k}=?" for k in ch)
                con.execute(f"UPDATE {t} SET {sets} WHERE {pk}=?",
                            [*ch.values(), e["id"]])
                n_upd += 1
            elif e["action"] == "insert":
                vals = dict(e["values"])
                vals[pk] = e["id"]
                keys = ",".join(vals)
                ph = ",".join("?" * len(vals))
                con.execute(f"INSERT OR IGNORE INTO {t}({keys}) VALUES({ph})",
                            list(vals.values()))
                n_ins += 1
        except sqlite3.Error as exc:
            n_fail += 1
            print(f"  跳过 {e.get('ts')} {t} {e.get('id')}: {exc}")
    con.commit()
    con.close()
    print(f"编辑重放: update {n_upd}, insert {n_ins}, 失败 {n_fail}")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "cvlab.sqlite")
    log = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "data" / "edit_log.jsonl")
    main(db, log)
