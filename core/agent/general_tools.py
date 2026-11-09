"""General-purpose safe tools registered through the existing plugin registry."""

from __future__ import annotations

import ast
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from core import gemini
from core.confirm import request as request_confirmation
from plugins.ide_agent.policy import ProjectPolicy

_BROWSER_LOCK = threading.Lock()
_SENSITIVE_DOMAINS = {
    "bank",
    "paypal.com",
    "stripe.com",
    "login",
    "accounts.google.com",
}


def _workspace(root: str | None = None) -> ProjectPolicy:
    """Return the configured safe workspace policy."""
    return ProjectPolicy(root or os.environ.get("AURA_AGENT_WORKSPACE", Path.cwd()))


def browser_tool(parameters: dict, player=None) -> str:
    """Perform one bounded Playwright browser operation."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright is not installed. Run: pip install playwright and playwright install chromium."
    action = str(parameters.get("action") or "").casefold()
    url = str(parameters.get("url") or "")
    hostname = urlparse(url).hostname or ""
    if action == "navigate" and any(
        marker in hostname.casefold() for marker in _SENSITIVE_DOMAINS
    ):
        return request_confirmation(
            "Open sensitive website",
            hostname,
            lambda: browser_tool({**parameters, "confirmed": True}, player),
        )
    timeout = min(60_000, max(1_000, int(parameters.get("timeout_ms", 15_000))))
    with _BROWSER_LOCK, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(timeout)
        try:
            if action == "navigate":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return page.title()
            if action == "click":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                page.locator(str(parameters["selector"])).click()
                return "clicked"
            if action == "fill":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                page.locator(str(parameters["selector"])).fill(
                    str(parameters.get("value", ""))
                )
                return "filled"
            if action == "extract_text":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return page.locator("body").inner_text()[:20_000]
            if action == "download":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                with page.expect_download(timeout=timeout) as download_info:
                    page.locator(str(parameters["selector"])).click()
                download = download_info.value
                target = _workspace(parameters.get("workspace")).require(parameters["path"])
                download.save_as(str(target))
                return f"download saved: {target}"
            if action == "screenshot":
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                target = _workspace(parameters.get("workspace")).require(
                    parameters["path"]
                )
                page.screenshot(
                    path=str(target), full_page=bool(parameters.get("full_page"))
                )
                return f"screenshot saved: {target}"
            return f"unsupported browser action: {action}"
        finally:
            browser.close()


def _capture_screen() -> bytes:
    """Capture the primary screen as PNG bytes."""
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("mss and Pillow are required for screen capture") from exc
    with mss.mss() as capture:
        monitor = capture.monitors[1]
        image = Image.frombytes(
            "RGB", (monitor["width"], monitor["height"]), capture.grab(monitor).rgb
        )
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def screen_tool(parameters: dict, player=None) -> str:
    """Capture, ask Gemini for a bounded action, execute it, and recapture."""
    try:
        import pyautogui
        from google.genai import types
    except ImportError as exc:
        return f"Screen automation dependencies are unavailable: {exc}"
    goal = str(parameters.get("goal") or parameters.get("instruction") or "")
    if not goal:
        return "A screen goal is required."
    maximum = min(5, max(1, int(parameters.get("max_steps", 3))))
    evidence = []
    for step in range(maximum):
        image = _capture_screen()
        response = gemini.text(
            [
                types.Part.from_bytes(data=image, mime_type="image/png"),
                ("Return only JSON: {action: click|type|hotkey|scroll|done, x, y, text, key, amount}. "
                 f"Goal: {goal}\nPrevious evidence: {evidence[-1:]}"),
            ],
            tier=gemini.SMART,
            timeout_ms=30_000,
        )
        try:
            action = json.loads(
                str(response or "")
                .strip()
                .removeprefix("```json")
                .removesuffix("```")
                .strip()
            )
        except json.JSONDecodeError as exc:
            return f"Gemini could not provide a readable screen action: {exc}"
        try:
            confidence = float(action.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.6:
            return "Gemini could not read the screen with sufficient confidence; no action was executed."
        if action.get("action") == "done":
            return f"screen goal verified after {step} step(s): {evidence[-1:] or ['done']}"
        command = str(action.get("action") or "")
        if command == "click":
            pyautogui.click(int(action["x"]), int(action["y"]))
        elif command == "type":
            pyautogui.write(str(action.get("text", "")))
        elif command == "hotkey":
            pyautogui.hotkey(*str(action.get("key", "")).split("+"))
        elif command == "scroll":
            pyautogui.scroll(int(action.get("amount", 0)))
        else:
            return f"unsupported or unsafe screen action: {command}"
        time.sleep(0.2)
        evidence.append(action)
    return f"screen step cap reached without verified completion: {evidence[-1:]}"


def files_tool(parameters: dict, player=None) -> str:
    """Read, write, or extract common document formats inside the safe workspace."""
    policy = _workspace(parameters.get("workspace"))
    action = str(parameters.get("action") or "read").casefold()
    target = policy.require(parameters["path"])
    suffix = target.suffix.casefold()
    if action == "write":
        content = str(parameters.get("content", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
        return f"wrote {target} ({target.stat().st_size} bytes)"
    if action == "convert":
        output = policy.require(parameters["output"])
        target_format = str(parameters.get("format") or output.suffix.lstrip(".")).casefold()
        if suffix == ".json" and target_format == "csv":
            records = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                return "JSON-to-CSV conversion requires an array of objects."
            with output.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=sorted({key for item in records for key in item}))
                writer.writeheader()
                writer.writerows(records)
            return f"converted to {output}"
        if suffix in {".txt", ".md"} and target_format in {"txt", "md"}:
            output.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            return f"converted to {output}"
        return f"unsupported conversion: {suffix} to {target_format}"
    if action != "read":
        return f"unsupported files action: {action}"
    if suffix in {".txt", ".md", ".log", ".py", ".js", ".yaml", ".yml"}:
        return target.read_text(encoding="utf-8", errors="replace")[:30_000]
    if suffix == ".json":
        return json.dumps(
            json.loads(target.read_text(encoding="utf-8")), indent=2, ensure_ascii=False
        )[:30_000]
    if suffix == ".csv":
        with target.open(newline="", encoding="utf-8", errors="replace") as stream:
            return "\n".join(",".join(row) for row in list(csv.reader(stream))[:500])
    if suffix == ".docx":
        from docx import Document

        return "\n".join(paragraph.text for paragraph in Document(target).paragraphs)[
            :30_000
        ]
    if suffix == ".pdf":
        from PyPDF2 import PdfReader

        return "\n".join(
            page.extract_text() or "" for page in PdfReader(str(target)).pages
        )[:30_000]
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(target, read_only=True, data_only=True)
        return "\n".join(
            ",".join(str(value or "") for value in row)
            for row in next(iter(workbook.worksheets)).iter_rows(values_only=True)
        )[:30_000]
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        from PIL import Image

        image = Image.open(target)
        return f"image {image.width}x{image.height}, format={image.format}, bytes={target.stat().st_size}"
    return f"unsupported file format: {suffix}"


def web_research_tool(parameters: dict, player=None) -> str:
    """Search, fetch, and summarize web sources with links."""
    query = str(parameters.get("query") or "").strip()
    if not query:
        return "A research query is required."
    try:
        from ddgs import DDGS

        results = list(
            DDGS().text(
                query, max_results=min(8, int(parameters.get("max_results", 5)))
            )
        )
    except (ImportError, requests.RequestException, ValueError, TypeError) as exc:
        return f"Web search failed: {exc}"
    sources = []
    for item in results:
        url = str(item.get("href") or item.get("url") or "")
        if not url:
            continue
        try:
            page = requests.get(url, timeout=15, headers={"User-Agent": "AURA/3.5"})
            text = re.sub(r"\s+", " ", page.text)[:6000]
        except requests.RequestException:
            text = str(item.get("body") or "")
        sources.append({"title": item.get("title", ""), "url": url, "text": text})
    if not sources:
        return "No sources found."
    response = gemini.text(
        "Summarize these sources factually. Cite each claim with its numbered Markdown link.\n"
        + "\n".join(
            f"[{i}] {item['title']} {item['url']}\n{item['text']}"
            for i, item in enumerate(sources, 1)
        ),
        tier=gemini.SMART,
        timeout_ms=60_000,
    )
    return (
        str(response or "")
        + "\n\nSources:\n"
        + "\n".join(f"- [{item['title']}]({item['url']})" for item in sources)
    )


def python_sandbox_tool(parameters: dict, player=None) -> str:
    """Run short Python with a restricted import set and no network."""
    code = str(parameters.get("code") or "")
    if not code:
        return "Python code is required."
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"sandbox syntax error: {exc}"
    allowed = {"json", "math", "statistics", "re", "datetime", "pathlib"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".")[0] for alias in node.names]
            if any(name not in allowed for name in names):
                return "sandbox blocked a non-approved import"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "open", "eval", "exec", "__import__", "compile",
        }:
            return f"sandbox blocked builtin: {node.func.id}"
    with tempfile.TemporaryDirectory(prefix="aura-sandbox-") as folder:
        script = Path(folder) / "main.py"
        script.write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(script)],
                check=False,
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=min(30, max(1, int(parameters.get("timeout", 10)))),
            )
        except subprocess.TimeoutExpired:
            return "sandbox timed out"
        return f"exit_code={result.returncode}\n{result.stdout[:8000]}{result.stderr[:4000]}"


def app_launcher_tool(parameters: dict, player=None) -> str:
    """Open a user-approved application or file without shell interpolation."""
    target = str(parameters.get("target") or "").strip()
    if not target:
        return "An application or file target is required."
    path = Path(target).expanduser()
    if not path.exists() and not re.fullmatch(r"[A-Za-z0-9_. -]+", target):
        return "target is not a safe application name or existing file"
    try:
        if path.exists() and hasattr(os, "startfile"):
            os.startfile(str(path))
        elif path.exists():
            subprocess.Popen(["xdg-open", str(path)])
        else:
            subprocess.Popen([target])
        return f"opened {target}"
    except (OSError, FileNotFoundError) as exc:
        return f"could not open {target}: {exc}"
