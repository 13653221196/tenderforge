"""启动前自检：解释器、依赖、数据目录、端口、OCR、模型。

    python -m backend.app.preflight

只读检查，不改任何数据、不起服务。退出码：

    0 = 可以启动（可能带警告，功能会降级，界面里也看得到）
    1 = 有阻断项，必须先按提示修好

设计原则：每条检查都给「怎么办」，不给「错误码」。用户不该为了启动去读日志。
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 依赖：import 名 -> pip 包名（找不到时按 pip 名提示安装）
DEPS = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "pydantic": "pydantic",
    "multipart": "python-multipart",
    "docx": "python-docx",
    "fitz": "pymupdf",
    "openpyxl": "openpyxl",
    "httpx": "httpx",
    "PIL": "pillow",
}
OPTIONAL_DEPS = {"langchain_core": "langchain-core", "langchain_openai": "langchain-openai"}

# R08：自检、启动脚本与真实服务读同一份 host/port（含 .env）
try:
    from . import runtime
    HOST, PORT = runtime.HOST, runtime.PORT
except Exception:  # 自检自己不能因为配置模块坏了就崩
    HOST = os.environ.get("BIDAGENT_HOST") or "127.0.0.1"
    try:
        PORT = int(os.environ.get("BIDAGENT_PORT") or "5210")
    except ValueError:
        PORT = 5210

OK, WARN, FAIL = "OK", "注意", "阻断"
_rows: list[tuple[str, str, str]] = []
# 这些检查自身抛错也要阻断：它们回答的是「能不能安全启动」，不是「功能会不会降级」
CRITICAL = {"check_python", "check_deps", "check_data", "check_port"}


def _add(level: str, title: str, detail: str = "", fix: str = "") -> None:
    _rows.append((level, title, detail))
    print(f"[{level}] {title}" + (f"：{detail}" if detail else ""))
    if fix and level != OK:
        print(f"       怎么办：{fix}")


def check_python() -> None:
    v = sys.version_info
    if v < (3, 10):
        _add(FAIL, "Python 版本", f"当前 {v.major}.{v.minor}，需要 3.10 及以上",
             "装 Python 3.11/3.12，然后用它重新建虚拟环境：python -m venv .venv")
    else:
        _add(OK, "Python 版本", f"{v.major}.{v.minor}.{v.micro} · {sys.executable}")


def check_deps() -> None:
    missing = [pip for mod, pip in DEPS.items() if importlib.util.find_spec(mod) is None]
    if missing:
        _add(FAIL, "运行依赖", "缺少 " + "、".join(missing),
             "在项目根目录执行：pip install -r backend/requirements.txt")
    else:
        _add(OK, "运行依赖", f"{len(DEPS)} 个必需包都在")
    opt = [pip for mod, pip in OPTIONAL_DEPS.items() if importlib.util.find_spec(mod) is None]
    if opt:
        _add(WARN, "模型调用依赖", "缺少 " + "、".join(opt),
             "只用演示模式可以不装；要接真实模型：pip install -r backend/requirements.txt")


def check_data() -> None:
    from . import config
    d = config.DATA_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        _add(FAIL, "数据目录不可写", f"{d}（{type(exc).__name__}: {exc}）",
             "换一个有写权限的目录：设置环境变量 BIDAGENT_DATA 指向它，重启即可")
        return
    free_mb = 0
    try:
        import shutil
        free_mb = int(shutil.disk_usage(d).free / (1024 * 1024))
    except Exception:
        pass
    _add(OK, "数据目录", f"{d}" + (f"，剩余 {free_mb} MB" if free_mb else ""))
    if free_mb and free_mb < 500:
        _add(WARN, "磁盘空间", f"只剩 {free_mb} MB",
             "导出 Word、留快照都要占空间，建议清理到 1 GB 以上")
    db = config.DB_PATH
    _add(OK if db.exists() else WARN, "数据库",
         f"{db.name} 已存在，会沿用现有标书" if db.exists() else "还没有数据库，首次启动会自动建")


def check_port() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        busy = s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()
    if busy:
        _add(FAIL, "端口被占用", f"{HOST}:{PORT} 已经有程序在监听，本实例无法在这个地址启动",
             f"先打开 http://{HOST}:{PORT} 看是不是已经在跑；确实要另起一个，"
             f"就在 .env 里改 BIDAGENT_HOST / BIDAGENT_PORT（自检、启动.bat 和服务会一起改），"
             f"或命令行覆盖：set BIDAGENT_PORT=新端口")
    else:
        _add(OK, "端口", f"{HOST}:{PORT} 空闲（与启动脚本、真实服务一致）")


def check_ocr() -> None:
    try:
        from . import parsers
        name, _ = parsers.ocr_engine()
    except Exception as exc:
        name = ""
        _add(WARN, "OCR 探测失败", f"{type(exc).__name__}: {exc}", "")
    if name:
        _add(OK, "OCR", f"已启用 {name}，扫描件可识别")
    else:
        _add(WARN, "OCR 未安装", "扫描件（图片型 PDF）读不出文字，会被拒绝入库",
             "需要识别扫描件时装一个：pip install rapidocr_onnxruntime（首次会下模型包）")


def check_models() -> None:
    from . import config
    f = config.MODELS_FILE
    if not f.exists():
        _add(WARN, "模型", "还没接入模型，界面会是「演示模式」（规则先行，不生成文字）",
             "打开界面右上角「接入模型」，填一个 OpenAI 兼容地址与 Key 即可")
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8") or "{}")
    except Exception:
        _add(WARN, "模型配置", f"{f.name} 读不出来，会退回演示模式",
             "删掉 data/models.json 重新接入，或检查文件是否被改坏")
        return
    models = data.get("models") if isinstance(data, dict) else None
    models = models or (data if isinstance(data, list) else [])
    usable = [m for m in models if isinstance(m, dict) and (m.get("api_key") or "").strip()]
    if usable:
        m = usable[0]
        base = str(m.get("base_url") or "").strip()
        key = str(m.get("api_key") or "").strip()
        verified, why = _probe_model(base, key)
        if verified:
            _add(OK, "模型", f"{len(usable)} 个密钥配置齐全 · 已验证可连通（{base}）")
        else:
            _add(OK, "模型", f"{len(usable)} 个密钥配置齐全 · 未验证连通（{why}）",
                 "配置齐全不代表能调通；首次生成前请在界面「接入模型 → 测试连接」确认，"
                 "离线环境也能先启动用演示模式")
    else:
        _add(WARN, "模型", "配置文件里没有可用密钥，界面会是演示模式",
             "在界面「接入模型」里补一个 Key；密钥只存本机数据目录")


def _probe_model(base: str, key: str) -> tuple[bool, str]:
    """只做一次轻量 /models 探测；失败不算阻断，只如实区分「配置齐全」与「已连通」。"""
    if not base:
        return False, "没填接口地址"
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(base.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 500, f"探测返回 {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__} / 超时或不可达"


def main() -> int:
    print("=" * 60)
    print("标书智能体 · 启动前自检")
    print("=" * 60)
    print(f"解释器：{sys.executable}")
    print("依赖安装：python -m pip install -r backend/requirements.txt（务必用上面这个解释器）")
    for fn in (check_python, check_deps, check_data, check_port, check_ocr, check_models):
        try:
            fn()
        except Exception as exc:
            critical = fn.__name__ in CRITICAL
            _add(FAIL if critical else WARN,
                 fn.__name__ + ("（自检自身失败）" if critical else "（检查本身出错）"),
                 f"{type(exc).__name__}: {exc}",
                 "这是关键检查，不能当作「功能降级」继续启动；按提示修好再重试"
                 if critical else "可先继续启动，功能会降级")
    fails = [r for r in _rows if r[0] == FAIL]
    warns = [r for r in _rows if r[0] == WARN]
    print("-" * 60)
    if fails:
        print(f"有 {len(fails)} 项必须先处理，处理完再启动。")
        return 1
    print(f"可以启动。{('有 %d 项提醒，按上面的说明确认一下即可。' % len(warns)) if warns else '各项都正常。'}")
    print(f"启动后浏览器打开 http://{HOST}:{PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
