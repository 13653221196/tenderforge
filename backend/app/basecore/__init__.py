"""基座包：本地业务引擎（判定 / 排版 / 导出 / 可审计性）。

与 bidagent 的关系：
- 本包只提供算法与服务，不接管 HTTP 路由、不接管 store.py 的库；
- 持久化只用本项目自己的 `data/basecore.db`（技能启停、任务状态等少量数据）；
- 配置只读本项目自己的 `.env`（查找顺序见 `basecore/config.py`），
  不跨目录、不读任何其它项目的文件；
- 模型密钥由上层 `core_bridge._cfg()` 注入，本包不自行读取任何密钥。
"""
from __future__ import annotations
