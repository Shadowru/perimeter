#!/usr/bin/env python3
"""Статический скан правила №0 (воздушный зазор).

Ищет в production-коде (core/, inference/, bridge-1c/, skills/, ui/, vendor/):
  1) литералы http:// и https:// на не-loopback хосты;
  2) литералы внешних (не приватных/loopback) IPv4-адресов.

Комментарии (#, //, *) пропускаются. Осознанные исключения — в
tools/ci/airgap_allowlist.txt (формат: <путь-от-корня>|<подстрока>).
Выход 1 при любой находке — ломает CI.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["core", "inference", "bridge-1c", "skills", "ui", "vendor"]
CODE_SUFFIXES = {".py", ".c", ".h", ".cu", ".mm", ".js", ".ts", ".html", ".css", ".sh", ".yaml", ".yml", ".json", ".toml"}
SKIP_PARTS = {"__pycache__", "node_modules", ".git"}

URL_RE = re.compile(r"https?://([\w.\-]+)")
IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
COMMENT_RE = re.compile(r"^\s*(#|//|\*|/\*|--|;)")

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def load_allowlist() -> list[tuple[str, str]]:
    path = REPO / "tools" / "ci" / "airgap_allowlist.txt"
    entries = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                p, s = line.split("|", 1)
                entries.append((p.strip(), s.strip()))
    return entries


def allowed(relpath: str, line: str, allowlist: list[tuple[str, str]]) -> bool:
    return any(relpath.startswith(p) and s in line for p, s in allowlist)


def external_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_private or addr.is_unspecified
                or addr.is_link_local or addr.is_multicast or addr.is_reserved)


def scan_file(path: Path, allowlist: list[tuple[str, str]]) -> list[str]:
    rel = str(path.relative_to(REPO))
    findings = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings
    for lineno, line in enumerate(text.splitlines(), 1):
        if COMMENT_RE.match(line):
            continue
        if allowed(rel, line, allowlist):
            continue
        for m in URL_RE.finditer(line):
            host = m.group(1)
            if host not in LOOPBACK_HOSTS:
                findings.append(f"{rel}:{lineno}: внешний URL: {m.group(0)}")
        for m in IPV4_RE.finditer(line):
            if external_ip(m.group(1)):
                findings.append(f"{rel}:{lineno}: внешний IP-литерал: {m.group(1)}")
    return findings


def main() -> int:
    allowlist = load_allowlist()
    findings: list[str] = []
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in CODE_SUFFIXES and not (set(path.parts) & SKIP_PARTS):
                findings.extend(scan_file(path, allowlist))
    if findings:
        print("НАРУШЕНИЯ ПРАВИЛА №0 (воздушный зазор):")
        print("\n".join(findings))
        print(f"\nВсего: {len(findings)}. Осознанные исключения — tools/ci/airgap_allowlist.txt")
        return 1
    print("airgap_scan: чисто.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
