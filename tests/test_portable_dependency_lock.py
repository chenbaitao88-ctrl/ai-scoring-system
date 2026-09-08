from pathlib import Path
import tomllib


ROOT_DIR = Path(__file__).resolve().parents[1]
INTEL_MAC_MARKER = "sys_platform == 'darwin' and platform_machine == 'x86_64'"


def test_intel_mac_onnxruntime_is_pinned_to_available_wheel():
    pyproject = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert f"onnxruntime==1.23.2; {INTEL_MAC_MARKER}" in dependencies
    assert INTEL_MAC_MARKER in pyproject["tool"]["uv"]["required-environments"]

    lock_text = (ROOT_DIR / "uv.lock").read_text(encoding="utf-8")
    assert "onnxruntime-1.23.2-cp311-cp311-macosx_13_0_x86_64.whl" in lock_text
    assert "platform_machine == 'x86_64' and sys_platform == 'darwin'" in lock_text


def test_non_intel_platforms_keep_current_onnxruntime_version():
    pyproject = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert (
        "onnxruntime==1.29.0; "
        "sys_platform != 'darwin' or platform_machine != 'x86_64'"
    ) in dependencies
