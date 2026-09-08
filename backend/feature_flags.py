"""
功能回滚开关 (Feature Flags)
============================
运行时控制新功能的开启/关闭，便于线上紧急回退。

设计决策 (2026-05-22, P0-7):
- 原 YAML 计划路径: backend/config/feature_flags.py
- 实际改为:        backend/feature_flags.py
  原因: 当前 backend/config.py 已是模块文件,
        若新建 backend/config/ 目录会与之冲突(包名与模块名同名,
        Python 解析时会报 ImportError 或加载错误的对象)。
        平级文件方案最简单,无破坏性。

5 个开关 (全部默认 True = 开启新逻辑):
  FEATURE_NEW_PARSER  -> P0-1 黑名单文件过滤
  AIGC_OCR            -> P1-1 OCR 解析分支
  FEWSHOT             -> P1-2 Few-shot 提示词注入
  ANCHOR_V2           -> P1-4 置信度算法 v2 + P1-6 校准函数
  MONITOR_GUARD       -> P0-6 评分监控守卫

回退方式 (无需改代码):
  Windows PowerShell:  $env:FEATURE_NEW_PARSER="false"
  Linux/Mac bash:      export FEATURE_NEW_PARSER=false
  .env 文件:           FEATURE_NEW_PARSER=false

CLI 用法 (在 scoring-system/ 目录下执行):
  python -m backend.feature_flags list      # 查看当前所有开关状态
  python -m backend.feature_flags get NAME  # 查询单个开关
  python -m backend.feature_flags set NAME true|false  # 设置 (写入 .env)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

# ---- .env 加载 (轻量实现, 不引入 python-dotenv 依赖) ----
_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent  # scoring-system/
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_env_file() -> None:
    """启动时把 .env 中未在环境里设置过的变量加载进 os.environ。
    决策依据: 已存在的环境变量优先级更高,允许临时 shell 覆盖 .env 文件值。"""
    if not _ENV_FILE.exists():
        return
    try:
        for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # .env 读取失败不应阻断应用启动,静默忽略
        pass


_load_env_file()


# ---- 开关定义 ----
# 决策依据 (P0-7): 全部默认 True,即默认启用新逻辑。
# 紧急回退时改 .env 即可,无需重新部署代码。
DEFAULTS: Dict[str, bool] = {
    "FEATURE_NEW_PARSER": True,   # P0-1: 文件过滤黑名单逻辑
    "AIGC_OCR":           True,   # P1-1: AIGC OCR 分支
    "FEWSHOT":            True,   # P1-2: Few-shot 示例注入
    "ANCHOR_V2":          True,   # P1-4/P1-6: 置信度 v2 + 校准
    "MONITOR_GUARD":      True,   # P0-6: 评分监控守卫
    "DETERMINISTIC_MODE": False,  # P2-2: 评分结果可复现性（seed=42, temperature=0）
    "ENABLE_BATCH_SCORING": True, # Phase 3: 纯AI批量评分模式
}

_TRUE_SET = {"1", "true", "yes", "on", "y", "t"}
_FALSE_SET = {"0", "false", "no", "off", "n", "f"}


def _parse_bool(raw: str, default: bool) -> bool:
    """宽容解析布尔: 大小写无关,支持多种写法。无效值用 default。"""
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE_SET:
        return True
    if v in _FALSE_SET:
        return False
    return default


def is_enabled(name: str) -> bool:
    """查询单个开关。
    决策依据: 未知开关返回 False (保守策略,避免误触发未声明的功能)。"""
    if name not in DEFAULTS:
        return False
    return _parse_bool(os.environ.get(name, ""), DEFAULTS[name])


class _Flags:
    """属性式访问: flags.FEATURE_NEW_PARSER
    决策依据: 拼错开关名会立刻抛 AttributeError,比字符串 key 安全。"""

    def __getattr__(self, name: str) -> bool:
        if name not in DEFAULTS:
            raise AttributeError(f"Unknown feature flag: {name}")
        return is_enabled(name)

    def all(self) -> Dict[str, bool]:
        return {name: is_enabled(name) for name in DEFAULTS}


flags = _Flags()


# ---- CLI ----
def _cli_list() -> int:
    print(f"{'FLAG':<22} {'CURRENT':<8} {'DEFAULT':<8} SOURCE")
    print("-" * 60)
    for name, default in DEFAULTS.items():
        raw = os.environ.get(name)
        current = is_enabled(name)
        source = "env/file" if raw is not None else "default"
        print(f"{name:<22} {str(current):<8} {str(default):<8} {source}")
    return 0


def _cli_get(name: str) -> int:
    if name not in DEFAULTS:
        print(f"ERROR: unknown flag '{name}'", file=sys.stderr)
        return 2
    print("true" if is_enabled(name) else "false")
    return 0


def _cli_set(name: str, value: str) -> int:
    """把开关写入 .env (覆盖同名行,新增到末尾)。"""
    if name not in DEFAULTS:
        print(f"ERROR: unknown flag '{name}'", file=sys.stderr)
        return 2
    parsed = _parse_bool(value, default=DEFAULTS[name])
    new_line = f"{name}={'true' if parsed else 'false'}"

    lines: list[str] = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{name}=") or stripped.startswith(f"{name} ="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 同步更新 os.environ,使后续调用立即生效(无需重启)
    os.environ[name] = "true" if parsed else "false"
    print(f"OK: {new_line} (written to {_ENV_FILE})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python -m backend.feature_flags {list|get NAME|set NAME VALUE}")
        return 1
    cmd = argv[1]
    if cmd == "list":
        return _cli_list()
    if cmd == "get" and len(argv) == 3:
        return _cli_get(argv[2])
    if cmd == "set" and len(argv) == 4:
        return _cli_set(argv[2], argv[3])
    print("Usage: python -m backend.feature_flags {list|get NAME|set NAME VALUE}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
