from __future__ import annotations

import sys
from pathlib import Path

from converge_orchestrator import shell

_CHILD_SCRIPT = """
import sys
sys.stdout.buffer.write('caf\\u00e9 \\u2713 \\u4e2d\\u6587'.encode('utf-8'))
sys.stdout.buffer.write(b'\\x88')
sys.stdout.buffer.write('\\u00e1\\u00e9\\u00ed\\u00f3\\u00fa'.encode('utf-8'))
sys.stdout.buffer.write(b'\\n')
sys.stdout.buffer.flush()
sys.exit(0)
"""


def _child(tmp_path: Path) -> list[str]:
    script = tmp_path / "child_utf8.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    return [sys.executable, str(script)]


def test_run_decodes_utf8_output_with_replacement_instead_of_locale_crash(
    tmp_path: Path,
) -> None:
    """Test E: non-ASCII subprocess output must never reproduce the V20 cp1250 crash.

    The child writes exact UTF-8 bytes plus one byte (0x88) that is undefined in both UTF-8
    (standalone continuation byte) and the cp1250 locale. Before the explicit-encoding fix,
    subprocess.run decoded with the Windows locale codec and raised UnicodeDecodeError inside
    the reader thread; now decoding is deterministic UTF-8 with replacement code points.
    Decoding "café" correctly also proves the UTF-8 codec was used (cp1250 would yield "cafÃ©").
    """
    result = shell.run(_child(tmp_path), cwd=tmp_path)

    assert result.returncode == 0
    assert "café ✓ 中文" in result.stdout
    assert "áéíóú" in result.stdout
    assert "\ufffd" in result.stdout


def test_run_configured_decodes_utf8_output_with_replacement_instead_of_locale_crash(
    tmp_path: Path,
) -> None:
    result = shell.run_configured(_child(tmp_path), cwd=tmp_path)

    assert result.returncode == 0
    assert "café ✓ 中文" in result.stdout
    assert "áéíóú" in result.stdout
    assert "\ufffd" in result.stdout