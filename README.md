# Farad_ledger

CVLab 平台的数据层，独立于 CVLabtest repo（CVLabtest 未来可能发表，保持解耦）。

设计原则：让数据自己 reveal 实际关系；尽可能精简简明。完整设计见 `docs/database_design_v2.md`。

## 职责边界

- **这里有**：SQLite schema、写入/查询 API（`ledger/`，待建）、dispense parser 与结果表的入库映射、（未来）parquet 分析缓存与 ingest。
- **CVLabtest 里没有**：任何数据库代码。CVLabtest 只暴露 hook（session 起止、measure 前后、wash 判定），装了 Farad_ledger 就落库，没装照常运行。
- **单一写者**：`cvlab.sqlite` 只存在于电脑 B、只由 B 写。电脑 A（Opentrons/pH）产生的数据经 B 的 API 提交（submit_state / submit_ph / submit_calibration）。

## 使用

```bash
sqlite3 cvlab.sqlite < schema.sql   # 建库（幂等，均为 IF NOT EXISTS）
```

每个连接需 `PRAGMA foreign_keys = ON`。数据库文件不入 git。

## 结构

```
Farad_ledger/
├── schema.sql                 # v2 全部建表语句
├── docs/database_design_v2.md # 设计文档（字段含义、边界、关系总览）
├── ledger/                    # Python API 层（待建）
└── cvlab.sqlite               # 运行时生成，gitignore
```

## 数据存储分层（约定）

原始采集 = CSV（事实源，人可读，随 measurement.data_path）；SQLite = 元数据 + 分析结果；parquet = 跨 run 分析缓存（需要时由 ingest 生成，暂缓）。
