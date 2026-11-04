#!/usr/bin/env python3
"""Monta o candidato de integração a partir do core versionado, sem o alterar."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parents[1] / "bin" / "zcode-worker"
TARGET = ROOT / "out" / "zcode-worker-candidate"
MAIN_MARKER = b'\n\nif __name__ == "__main__":\n'
LEGACY_FOOTER = MAIN_MARKER + b"    sys.exit(main())\n"
HOOK_MARKERS = (b"_INTEGRATION_SRC", b"_integration_main(core=sys.modules[__name__])")


def _split_body(source_bytes: bytes) -> bytes:
    """Separa o corpo do core do rodapé legado ou do hook de integração atual."""
    if source_bytes.endswith(LEGACY_FOOTER):
        return source_bytes[: -len(LEGACY_FOOTER)]
    index = source_bytes.rfind(MAIN_MARKER)
    if index >= 0 and all(marker in source_bytes[index:] for marker in HOOK_MARKERS):
        return source_bytes[:index]
    raise SystemExit("footer do core instalado não confere")


def _footer(src: Path) -> bytes:
    fixed_src = str(src.resolve())
    return (
        "\n\n"
        "if \"--_background-child\" in sys.argv[1:] or (\"--list-models\" in sys.argv[1:] and any(a == \"--saida\" or a.startswith(\"--saida=\") for a in sys.argv[1:])):\n"
        "    raise SystemExit(main())\n"
        f"_INTEGRATION_SRC = {fixed_src!r}\n"
        "sys.path.insert(0, _INTEGRATION_SRC)\n"
        "from zcode_worker.cli import main as _integration_main\n"
        "raise SystemExit(_integration_main(core=sys.modules[__name__]))\n"
    ).encode("utf-8")


def main() -> int:
    source_stat = SOURCE.lstat()
    if not stat.S_ISREG(source_stat.st_mode) or SOURCE.is_symlink():
        raise SystemExit("core instalado inválido")
    source_bytes = SOURCE.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    body = _split_body(source_bytes)
    if b"def main(" not in body:
        raise SystemExit("corpo do core instalado não confere")
    TARGET.parent.mkdir(mode=0o700, exist_ok=True)
    hook = b"\n".join(b"    " + line for line in _footer(ROOT / "src").splitlines())
    TARGET.write_bytes(body + b"\n\nif __name__ == \"__main__\":\n" + hook + b"\n")
    os.chmod(TARGET, 0o755)
    print(
        json.dumps(
            {
                "source": str(SOURCE),
                "source_sha256": source_sha256,
                "source_bytes": len(source_bytes),
                "target": str(TARGET),
                "target_bytes": TARGET.stat().st_size,
                "target_mode": oct(TARGET.stat().st_mode & 0o777),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
