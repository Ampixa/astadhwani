"""Record packaged model identity without private source paths."""

import hashlib
import json
import sys
from pathlib import Path

app = Path(sys.argv[1])
root = app / "Contents/Resources"
files = {}
for p in sorted((root / "models/wave1").iterdir()):
    with p.open("rb") as f:
        files[p.name] = hashlib.file_digest(f, "sha256").hexdigest()
(root / "model-checksums.json").write_text(
    json.dumps(
        {
            "profile": json.loads((root / "studio/backend/model-profile.json").read_text()),
            "sha256": files,
        },
        indent=2,
    )
    + "\n"
)
