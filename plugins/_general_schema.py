"""Shared schema for Phase 3.5 general tool plugins."""

GENERAL_PARAMETERS = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING"}, "url": {"type": "STRING"},
        "selector": {"type": "STRING"}, "value": {"type": "STRING"},
        "path": {"type": "STRING"}, "output": {"type": "STRING"}, "workspace": {"type": "STRING"},
        "content": {"type": "STRING"}, "query": {"type": "STRING"},
        "code": {"type": "STRING"}, "target": {"type": "STRING"},
        "goal": {"type": "STRING"}, "instruction": {"type": "STRING"},
        "timeout": {"type": "INTEGER"}, "timeout_ms": {"type": "INTEGER"},
        "max_steps": {"type": "INTEGER"}, "format": {"type": "STRING"},
    },
    "required": [],
}