"""Farad_ledger: CVLab 数据层（SQLite 元数据+结果, parquet 曲线）。

单一写者：本包只在电脑 B 上打开数据库；A 侧数据经 B 的 API 提交。
CVLabtest 不 import 本包的任何东西——是 GUI 在启动时 optional-import 这里的 bridge。
"""
from .api import Ledger, connect          # noqa: F401
from .reader import read, metadata        # noqa: F401
