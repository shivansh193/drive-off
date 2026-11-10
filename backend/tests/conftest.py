import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="driveoff-test-"))
os.environ["DATA_DIR"] = str(_tmp)
os.environ["PAYPAL_MODE"] = "mock"
os.environ["GEMINI_API_KEY"] = ""  # tests never call Gemini
os.environ["DB_PATH"] = str(_tmp / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from driveoff import config, db, demo  # noqa: E402

# keep clip rendering shared across tests (it's deterministic)
config.CLIPS_DIR = Path(__file__).resolve().parents[2] / "data" / "clips"
config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def fresh():
    db.reset()
    demo.seed("MOCKVAULT-test")
    demo.build_clips()
    from driveoff import paypal
    paypal.MOCK.__init__()
    yield
