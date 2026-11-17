"""Verification of a legacy recorder session (frames.bin) before Retry Analysis may use it (plan section 5).

frames.bin is a sequence of [uint32 little-endian length][CDR-serialized ScanFrame] records (recorder_node,
read_session.py). A recording is replay-safe only if every record is complete (the length prefixes walk the
file exactly to its end) and their number equals the frames the scan actually published.
"""

from __future__ import annotations

import struct
from pathlib import Path


def count_records(frames_bin: str | Path) -> tuple[int, str | None]:
    """(records, error). error is None only if the file is a complete sequence of records."""
    path = Path(frames_bin)
    try:
        size = path.stat().st_size
        n, pos = 0, 0
        with path.open("rb") as f:
            while pos < size:
                head = f.read(4)
                if len(head) < 4:
                    return n, f"truncated length prefix at byte {pos}"
                (length,) = struct.unpack("<I", head)
                if length == 0 or pos + 4 + length > size:
                    return n, f"record {n} ({length} bytes) runs past the end of the file at byte {pos}"
                f.seek(length, 1)
                pos += 4 + length
                n += 1
        return n, None
    except OSError as e:
        return 0, f"cannot read {path}: {e}"


def verify(raw_dir: str | None, recorded: int | None, published: int | None) -> tuple[bool, str]:
    """(replay_eligible, note) for a closed raw session of a scan whose stream published `published` frames."""
    if not raw_dir:
        return False, "no raw recording linked to this scan"
    if published is None:
        return False, "the scan's frame stream was not closed"
    if recorded is None:
        return False, "the raw recording was not closed"
    n, error = count_records(Path(raw_dir) / "frames.bin")
    if error:
        return False, f"frames.bin damaged: {error}"
    if n != recorded:
        return False, f"frames.bin holds {n} frames, the recorder reported {recorded}"
    if n != published:
        return False, f"frames.bin holds {n} frames, the scan published {published}"
    return True, f"frames.bin verified: {n} frames"
