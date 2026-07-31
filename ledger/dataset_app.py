"""dataset_app: Notion 式 dataset 查看/编辑器（Farad_ledger 独立，不依赖 CVLabtest）。

用法：python -m ledger.dataset_app [db路径] [端口]     默认 cvlab.sqlite / 8878
浏览器打开 http://127.0.0.1:8878

运行逻辑（对照 Notion database）：
  列类型   = PRAGMA table_info（TEXT/REAL/INTEGER）+ schema 里的 CHECK 枚举（→ select 下拉）
  relation = PRAGMA foreign_key_list（→ 可点击 chip，跳转对面行）
  rollup   = 反向外键计数（行页面里 "measurement × 12 →"）
  行即页面 = 点行号展开右侧详情面板
  视图     = 前端 localStorage 保存每张表的 筛选/排序/隐列/分组

编辑边界（provenance 保护）：
  UPDATE 走白名单 EDITABLE —— 只放人工字段（notes、库存信息、电极参数、
    experiment status、well_state 的 ph/溶解度/体积、ph_test 手动录入字段等）；
    时间戳、测量参数、分析数值、artifact 一律只读。
  INSERT 只开放实体表 INSERTABLE（chemical/electrode/stock_solution/
    probe_calibration/electrode_maintenance）——过程表只能由系统写。
  DELETE 一律不提供。
  所有改动追加记录到 data/edit_log.jsonl（时间、表、行、改了什么）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent.parent
app = Flask(__name__)
DB = ROOT / "cvlab.sqlite"
EDIT_LOG = ROOT / "data" / "edit_log.jsonl"

# ── 编辑白名单：表 -> 允许 UPDATE 的列 ────────────────────────────────────────
EDITABLE = {
    "chemical":        {"formula", "storage_location", "received_at", "notes"},
    "electrode":       {"material", "diameter_mm", "area_cm2", "reference_fill_chemical",
                        "reference_fill_concentration_m", "serial_number", "active", "notes"},
    "electrode_setup": {"active", "notes"},
    "stock_solution":  {"deck_slot", "well_name", "concentration_m", "prepared_at",
                        "active", "notes"},
    "probe_calibration": {"notes"},
    "rack":            {"name", "description", "active"},
    "well":            {"default_role", "active", "notes"},
    "station":         {"name", "station_type", "active", "notes"},
    "experiment":      {"status", "notes"},
    "session":         {"notes"},
    "measurement":     {"notes"},
    "measurement_bundle": {"notes"},
    "well_state":      {"ph", "solubility_m", "total_volume_ml", "notes"},
    "ph_test":         {"calibration_id", "started_at", "ended_at", "final_ph",
                        "final_voltage", "acquisition_status", "end_reason", "notes"},
    "ocp_check":       {"notes"},
    "wash":            {"notes"},
    "electrode_maintenance": {"performed_at", "notes"},
}
INSERTABLE = {"chemical", "electrode", "stock_solution",
              "probe_calibration", "electrode_maintenance"}
HIDDEN_TABLES = {"sqlite_sequence"}
# FK 目标表 -> 用哪列当显示名（chip 文本）
LABEL_COL = {"chemical": "name", "electrode": "name", "experiment": "experiment_name",
             "session": "session_name", "well": "position_name", "rack": "name",
             "station": "name", "measurement": "filename", "artifact": "filename"}
_OPS = {"contains": "LIKE", "=": "=", "!=": "!=", ">": ">", "<": "<",
        ">=": ">=", "<=": "<=", "empty": "IS NULL", "notempty": "IS NOT NULL"}


def _con():
    con = sqlite3.connect(str(DB), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _log_edit(entry: dict):
    EDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    with EDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _schema(con):
    """全库自省：{table: {pk, cols:[{name,type,notnull,fk,enum,editable}], count}}"""
    out = {}
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in HIDDEN_TABLES]
    for t in tables:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
        enums = {m.group(1): [v.strip().strip("'") for v in m.group(2).split(",")]
                 for m in re.finditer(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", sql, re.S)}
        fks = {r["from"]: {"table": r["table"], "column": r["to"] or "rowid"}
               for r in con.execute(f"PRAGMA foreign_key_list({t})")}
        cols, pk = [], None
        for r in con.execute(f"PRAGMA table_info({t})"):
            if r["pk"]:
                pk = r["name"]
            cols.append({"name": r["name"], "type": r["type"], "notnull": bool(r["notnull"]),
                         "pk": bool(r["pk"]), "fk": fks.get(r["name"]),
                         "enum": enums.get(r["name"]),
                         "editable": r["name"] in EDITABLE.get(t, ())})
        out[t] = {"pk": pk, "cols": cols, "insertable": t in INSERTABLE,
                  "count": con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]}
    return out


def _valid_col(con, table, col):
    return col in [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def _fk_label_sql(table):
    lc = LABEL_COL.get(table)
    return lc if lc else None


@app.get("/")
def index():
    return send_file(Path(__file__).parent / "static" / "dataset.html")


@app.get("/api/schema")
def api_schema():
    con = _con()
    try:
        return jsonify(_schema(con))
    finally:
        con.close()


@app.get("/api/table/<table>")
def api_table(table):
    """行数据 + FK 显示名。?filters=json&sort=col&dir=asc&limit=&offset="""
    con = _con()
    try:
        sch = _schema(con)
        if table not in sch:
            return jsonify({"error": "no such table"}), 404
        pk = sch[table]["pk"]
        where, args = [], []
        for f in json.loads(request.args.get("filters") or "[]"):
            col, op, val = f.get("col"), f.get("op"), f.get("val")
            if not _valid_col(con, table, col) or op not in _OPS:
                continue
            if op in ("empty", "notempty"):
                where.append(f"({col} {_OPS[op]}"
                             + (f" OR {col} = '')" if op == "empty" else f" AND {col} != '')"))
            elif op == "contains":
                where.append(f"{col} LIKE ?")
                args.append(f"%{val}%")
            else:
                where.append(f"{col} {_OPS[op]} ?")
                args.append(val)
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sort, d = request.args.get("sort"), request.args.get("dir", "asc")
        if sort and _valid_col(con, table, sort):
            sql += f" ORDER BY {sort} {'DESC' if d == 'desc' else 'ASC'}"
        elif pk:
            sql += f" ORDER BY {pk} DESC"
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        sql += f" LIMIT {limit} OFFSET {offset}"
        rows = [dict(r) for r in con.execute(sql, args)]
        # blob 不下发，换成占位
        for c in sch[table]["cols"]:
            if c["type"].upper() == "BLOB":
                for r in rows:
                    if r.get(c["name"]) is not None:
                        r[c["name"]] = f"<blob {len(r[c['name']])} B>"
        # FK 显示名：{col: {id: label}}
        fk_labels = {}
        for c in sch[table]["cols"]:
            if c["fk"] and rows:
                tgt, tc = c["fk"]["table"], c["fk"]["column"]
                lc = _fk_label_sql(tgt)
                ids = {r[c["name"]] for r in rows if r.get(c["name"]) is not None}
                if lc and ids:
                    ph = ",".join("?" * len(ids))
                    fk_labels[c["name"]] = {
                        str(r[0]): r[1] for r in con.execute(
                            f"SELECT {tc}, {lc} FROM {tgt} WHERE {tc} IN ({ph})", list(ids))}
        return jsonify({"rows": rows, "fk_labels": fk_labels})
    finally:
        con.close()


@app.get("/api/fk_options/<table>")
def api_fk_options(table):
    """FK 编辑下拉：目标表 [id, label] 列表（最多 500）。"""
    con = _con()
    try:
        sch = _schema(con)
        if table not in sch:
            return jsonify({"error": "no such table"}), 404
        pk = sch[table]["pk"]
        lc = _fk_label_sql(table) or pk
        rows = con.execute(f"SELECT {pk}, {lc} FROM {table} ORDER BY {pk} DESC LIMIT 500")
        return jsonify({"options": [[r[0], f"{r[0]} · {r[1]}" if r[1] is not None
                                     else str(r[0])] for r in rows]})
    finally:
        con.close()


@app.get("/api/row/<table>/<int:rid>")
def api_row(table, rid):
    """行即页面：整行 + 反向外键计数（rollup）。"""
    con = _con()
    try:
        sch = _schema(con)
        if table not in sch:
            return jsonify({"error": "no such table"}), 404
        pk = sch[table]["pk"]
        row = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (rid,)).fetchone()
        if not row:
            return jsonify({"error": "no such row"}), 404
        row = dict(row)
        for c in sch[table]["cols"]:
            if c["type"].upper() == "BLOB" and row.get(c["name"]) is not None:
                row[c["name"]] = f"<blob {len(row[c['name']])} B>"
        children = []
        for t2, meta in sch.items():
            for c in meta["cols"]:
                if c["fk"] and c["fk"]["table"] == table:
                    n = con.execute(f"SELECT COUNT(*) FROM {t2} WHERE {c['name']}=?",
                                    (rid,)).fetchone()[0]
                    if n:
                        children.append({"table": t2, "col": c["name"], "count": n})
        return jsonify({"row": row, "children": children})
    finally:
        con.close()


@app.patch("/api/row/<table>/<int:rid>")
def api_patch(table, rid):
    """白名单单元格编辑。body = {col: value, ...}"""
    body = request.get_json(force=True) or {}
    allowed = EDITABLE.get(table, set())
    bad = [k for k in body if k not in allowed]
    if bad:
        return jsonify({"error": f"read-only column(s): {', '.join(bad)} "
                        f"(白名单外——过程事实不可改)"}), 403
    con = _con()
    try:
        sch = _schema(con)
        pk = sch[table]["pk"]
        cols = {c["name"]: c for c in sch[table]["cols"]}
        for k, v in body.items():
            if cols[k].get("enum") and v not in (None, "") and v not in cols[k]["enum"]:
                return jsonify({"error": f"{k} 必须是 {cols[k]['enum']} 之一"}), 400
        old = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (rid,)).fetchone()
        if not old:
            return jsonify({"error": "no such row"}), 404
        sets = ",".join(f"{k}=?" for k in body)
        vals = [None if v == "" else v for v in body.values()]
        con.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", [*vals, rid])
        con.commit()
        _log_edit({"action": "update", "table": table, "id": rid,
                   "changes": {k: {"old": old[k], "new": body[k]} for k in body}})
        return jsonify({"ok": True})
    except sqlite3.Error as e:
        return jsonify({"error": str(e)}), 400
    finally:
        con.close()


@app.post("/api/row/<table>")
def api_insert(table):
    """实体表加行（'+ New'）。body = {col: value, ...}"""
    if table not in INSERTABLE:
        return jsonify({"error": f"{table} 是过程表，只能由系统写入"}), 403
    body = request.get_json(force=True) or {}
    con = _con()
    try:
        sch = _schema(con)
        pk = sch[table]["pk"]
        cols = {c["name"]: c for c in sch[table]["cols"]}
        data = {k: v for k, v in body.items()
                if k in cols and k != pk and v not in (None, "")}
        for k, v in data.items():
            if cols[k].get("enum") and v not in cols[k]["enum"]:
                return jsonify({"error": f"{k} 必须是 {cols[k]['enum']} 之一"}), 400
        keys = ",".join(data)
        ph = ",".join("?" * len(data))
        cur = con.execute(f"INSERT INTO {table}({keys}) VALUES({ph})", list(data.values()))
        con.commit()
        _log_edit({"action": "insert", "table": table, "id": cur.lastrowid, "values": data})
        return jsonify({"ok": True, "id": cur.lastrowid})
    except sqlite3.Error as e:
        return jsonify({"error": str(e)}), 400
    finally:
        con.close()


def main():
    global DB
    if len(sys.argv) > 1:
        DB = Path(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8878
    print(f"dataset viewer · db={DB} · http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
