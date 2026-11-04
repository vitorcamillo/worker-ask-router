import os
from pathlib import Path
os.environ["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent / "adapters/zcode/src"), str(Path(__file__).parent / "adapters/zcode"), os.environ.get("PYTHONPATH", "")])
