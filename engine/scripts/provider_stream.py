"""Read request-scoped capacity metadata without exposing model reasoning.

CodeBuddy 2.148 converts provider ``length`` into ``end_turn`` in stream-json.
Its local diagnostic log retains the true reason. Only newly appended lines
whose request IDs were observed on this worker's stream may affect this worker.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


class CodeBuddyCapacityLog:
    def __init__(self, project: Path, logs: Path | None = None):
        home = Path(os.environ.get("CODEBUDDY_CONFIG_DIR") or Path.home() / ".codebuddy")
        self.logs = logs or home / "logs"
        self.prefix = project.name + "__"
        self.offsets = {}
        for path in self.paths():
            try:
                self.offsets[path] = path.stat().st_size
            except OSError:
                pass  # The CLI may rotate a log between listing and stat.
        self.exhausted = set()

    def paths(self):
        directory = self.logs / datetime.now().strftime("%Y-%m-%d")
        try:
            return [p for p in directory.iterdir() if p.name.startswith(self.prefix) and p.suffix == ".log"]
        except OSError:
            return []

    def check(self, request_ids):
        for path in self.paths():
            try:
                offset = self.offsets.get(path, 0)
                if path.stat().st_size < offset:
                    offset = 0  # Rotated log; still require this invocation's ID.
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break  # Revisit an unfinished write on the next event.
                        offset += len(line)
                        if b'finish_reason="length"' in line:
                            match = re.search(rb"requestId=([A-Za-z0-9_-]+)", line)
                            if match:
                                self.exhausted.add(match[1].decode("ascii"))
                self.offsets[path] = offset
            except OSError:
                continue  # Stream protocol remains the primary signal.
        return bool(self.exhausted.intersection(request_ids))
