"""Build a source-only archive using an explicit file allowlist."""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = json.loads((ROOT / "PUBLIC_FILES.json").read_text(encoding="utf-8"))
assert len(FILES) == len(set(FILES)), "Repeated source entry"
for name in FILES:
    assert (ROOT / name).resolve().is_relative_to(ROOT), "Source entry outside project"


target = ROOT.parent / (ROOT.name + "-source.zip")
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for name in FILES:
        p = ROOT / name
        raw = p.read_bytes()
        if name.endswith(".bat"):
            raw.decode("ascii")
            assert b"\n" not in raw.replace(b"\r\n", b""), name + " requires Windows CRLF"
        else:
            raw = raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
        info = zipfile.ZipInfo.from_file(p, ROOT.name + "/" + name)
        info.create_system = 3
        info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
        z.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED)
with zipfile.ZipFile(target) as z:
    assert z.testzip() is None
    assert len(z.namelist()) == len(FILES)
print(str(target))
