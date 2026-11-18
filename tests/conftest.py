import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BUNDLES = ROOT / "fixtures" / "bundles"


@pytest.fixture
def bundle_copy(tmp_path):
    """Copy a fixture bundle into a temp dir so a test can mutate it."""
    def _copy(name):
        dst = tmp_path / name
        shutil.copytree(BUNDLES / name, dst)
        return dst
    return _copy


def edit_json(path, fn):
    data = json.loads(Path(path).read_text())
    fn(data)
    Path(path).write_text(json.dumps(data, indent=2))
