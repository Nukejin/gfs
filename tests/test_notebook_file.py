"""Colab 用ノートブック (colab/trade_sim.ipynb) のテスト。"""

import ast
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK = ROOT / "colab" / "trade_sim.ipynb"

_spec = importlib.util.spec_from_file_location("build_notebook", ROOT / "tools" / "build_notebook.py")
build_notebook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_notebook)


def _cell_code(cell_id: str) -> str:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    (cell,) = [c for c in notebook["cells"] if c["metadata"].get("id") == cell_id]
    return "".join(cell["source"])


def _yfinance_is_recent() -> bool:
    try:
        version = importlib.metadata.version("yfinance")
    except importlib.metadata.PackageNotFoundError:
        return False
    return tuple(int(x) for x in re.findall(r"\d+", version)[:3]) >= (1, 7, 0)


class NotebookFileTest(unittest.TestCase):
    def test_notebook_is_up_to_date(self):
        self.assertEqual(
            NOTEBOOK.read_text(encoding="utf-8"),
            build_notebook.build(),
            "パッケージを変更したら python tools/build_notebook.py でノートブックを作り直してください",
        )

    def test_embedded_sources_match_package(self):
        tree = ast.parse(_cell_code("setup"))
        (sources,) = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_SOURCES"
        ]
        package_files = {f"trade_sim/{p.name}" for p in (ROOT / "trade_sim").glob("*.py")}
        self.assertEqual(set(sources), package_files)
        for name, text in sources.items():
            self.assertEqual(text, (ROOT / name).read_text(encoding="utf-8"), name)

    def test_cells_compile(self):
        for cell_id in ("setup", "app", "reset"):
            with self.subTest(cell=cell_id):
                compile(_cell_code(cell_id), cell_id, "exec")

    @unittest.skipUnless(_yfinance_is_recent(), "yfinance 1.7.0 以上が必要 (無いとセルが pip install を実行するため)")
    def test_setup_cell_runs_outside_colab(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _cell_code("setup") + (
                "\nimport trade_sim, trade_sim.notebook\n"
                "print('PACKAGE', trade_sim.__file__)\n"
                "print('DATA', DATA_FILE)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=tmp, capture_output=True, text=True, timeout=120
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            tmp_dir = Path(tmp).resolve()
            self.assertIn(f"PACKAGE {tmp_dir / 'trade_sim_app' / 'trade_sim' / '__init__.py'}", result.stdout)
            self.assertIn(f"DATA {tmp_dir / 'portfolio.json'}", result.stdout)


if __name__ == "__main__":
    unittest.main()
