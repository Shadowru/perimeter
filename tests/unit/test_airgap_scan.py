import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN = REPO / "tools" / "ci" / "airgap_scan.py"


def test_repo_is_clean():
    proc = subprocess.run([sys.executable, str(SCAN)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_scanner_catches_external_url(tmp_path, monkeypatch):
    # Проверяем сам сканер: подсунем нарушение во временную копию core/.
    import importlib.util
    spec = importlib.util.spec_from_file_location("airgap_scan", SCAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    bad = tmp_path / "core" / "evil.py"
    bad.parent.mkdir(parents=True)
    bad.write_text('URL = "https://telemetry.example.com/ping"\n', encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    findings = mod.scan_file(bad, [])
    assert findings and "telemetry.example.com" in findings[0]


def test_scanner_ignores_comments_and_loopback(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("airgap_scan", SCAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ok = tmp_path / "ok.py"
    ok.write_text(
        "# see https://example.com/docs\n"
        'BASE = "http://127.0.0.1:8090"\n'
        'MASK = "255.255.255.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "dummy").mkdir()

    class FakeRepo:
        pass

    mod.REPO = tmp_path
    assert mod.scan_file(ok, []) == []


def test_audit_log_is_not_tracked_by_git():
    """Журнал аудита — данные заказчика, в репозитории ему не место.

    31.07 он попал в коммит через `git add -A`. Содержимое оказалось
    безобидным, но сам факт — утечка по построению.
    """
    import subprocess
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
    tracked = [l for l in out.splitlines() if l.startswith("var/") or l.endswith("audit.log")]
    assert not tracked, f"в репозитории лежат журналы: {tracked}"
