"""Plugin-loader shim for the Phase 2 IDE agent package."""

from __future__ import annotations

from plugins.ide_agent import IDEAgentService

PLUGIN = {
    "name": "ide_agent",
    "description": "Inspect, edit, test, and review code inside the active project through safe filesystem tools.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list_tree, read_file, search_code, apply_patch, create_file, run_command, run_tests, git_status, git_diff, code_task, open_file",
            },
            "project_root": {"type": "STRING"},
            "path": {"type": "STRING"},
            "query": {"type": "STRING"},
            "patch": {"type": "STRING"},
            "content": {"type": "STRING"},
            "command": {"type": "STRING"},
            "request": {"type": "STRING"},
            "message": {"type": "STRING"},
            "paths": {"type": "ARRAY", "items": {"type": "STRING"}},
            "line": {"type": "INTEGER"},
            "confirmed": {"type": "BOOLEAN"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch a safe IDE-agent operation."""
    params = dict(parameters or {})
    service = IDEAgentService(params.get("project_root") or None)
    action = str(params.get("action") or "").casefold()

    def request_confirmation(title: str, detail: str, operation) -> str:
        """Use the shared human confirmation gate for destructive edits."""
        from core import confirm
        return confirm.request("ide-agent", title, detail, operation)

    def apply_patch_with_gate(patch: str) -> str:
        preview = service.apply_patch(patch, confirmed=False)
        if player and hasattr(player, "show_content"):
            player.show_content("IDE AGENT DIFF", service.preview_patch(patch))
        if not preview.startswith("CONFIRMATION_REQUIRED"):
            return preview
        return request_confirmation(
            "Apply IDE agent code changes",
            service.preview_patch(patch),
            lambda: service.apply_patch(patch, confirmed=True),
        )

    def create_file_with_gate(path: str, content: str) -> str:
        preview = service.create_file(path, content, confirmed=False)
        if not preview.startswith("CONFIRMATION_REQUIRED"):
            return preview
        return request_confirmation(
            "Create IDE agent file",
            f"{path}\n\n{content[:12000]}",
            lambda: service.create_file(path, content, confirmed=True),
        )

    if action == "list_tree":
        return service.list_tree(params.get("path", "."))
    if action == "read_file":
        return service.read_file(params["path"], int(params.get("start", 1)), params.get("end"))
    if action == "search_code":
        return service.search_code(params["query"])
    if action == "apply_patch":
        return apply_patch_with_gate(params["patch"])
    if action == "create_file":
        return create_file_with_gate(params["path"], params.get("content", ""))
    if action == "run_command":
        return service.run_command(params["command"], int(params.get("timeout", 60)), bool(params.get("allow_install", False)))
    if action == "run_tests":
        return service.run_tests(params.get("command", "pytest -q"), int(params.get("timeout", 300)))
    if action in {"git_status", "git_diff"}:
        return service.git("status" if action == "git_status" else "diff")
    if action == "git_commit":
        paths = [str(item) for item in params.get("paths", [])]
        return request_confirmation(
            "Create local git commit",
            f"{params.get('message', '')}\nPaths: {', '.join(paths) or 'project changes'}",
            lambda: service.git_commit(params["message"], paths),
        )
    if action == "code_task":
        return service.code_task(
            params["request"],
            confirmed=False,
            confirm=request_confirmation,
        )
    if action == "open_file":
        return service.open_file(params["path"], params.get("line"))
    return f"Unsupported IDE-agent action: {action}"