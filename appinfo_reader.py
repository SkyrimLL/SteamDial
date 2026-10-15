"""Read-only parser for Steam's binary `appcache/appinfo.vdf`.

Supports format versions 27, 28 and 29 (29 stores keys in a trailing string table).
Only `common.name` and `common.type` are extracted, which is all the library needs.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

log = logging.getLogger(__name__)

_MAGIC_V27 = 0x07564427
_MAGIC_V28 = 0x07564428
_MAGIC_V29 = 0x07564429

# Binary KeyValues type tags.
_T_MAP, _T_STR, _T_INT32, _T_FLOAT, _T_PTR, _T_WSTR, _T_COLOR, _T_UINT64, _T_END, _T_INT64, _T_END_ALT = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0A, 0x0B,
)


@dataclass(frozen=True)
class AppInfo:
    appid: int
    name: str
    app_type: str


class _BinaryKV:
    """Minimal binary KeyValues decoder over an in-memory buffer."""

    def __init__(self, buf: bytes, string_table: Optional[List[str]]):
        self._buf = buf
        self._pos = 0
        self._strings = string_table

    def _cstring(self) -> str:
        end = self._buf.index(b"\x00", self._pos)
        s = self._buf[self._pos:end].decode("utf-8", errors="replace")
        self._pos = end + 1
        return s

    def _wstring(self) -> str:
        end = self._pos
        while self._buf[end:end + 2] != b"\x00\x00":
            end += 2
        s = self._buf[self._pos:end].decode("utf-16-le", errors="replace")
        self._pos = end + 2
        return s

    def _unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        (value,) = struct.unpack_from(fmt, self._buf, self._pos)
        self._pos += size
        return value

    def _key(self) -> str:
        if self._strings is None:
            return self._cstring()
        return self._strings[self._unpack("<I")]

    def parse_map(self) -> Dict[str, object]:
        result: Dict[str, object] = {}
        while self._pos < len(self._buf):
            tag = self._buf[self._pos]
            self._pos += 1
            if tag in (_T_END, _T_END_ALT):
                break
            key = self._key()
            if tag == _T_MAP:
                result[key] = self.parse_map()
            elif tag == _T_STR:
                result[key] = self._cstring()
            elif tag in (_T_INT32, _T_PTR, _T_COLOR):
                result[key] = self._unpack("<i")
            elif tag == _T_FLOAT:
                result[key] = self._unpack("<f")
            elif tag == _T_UINT64:
                result[key] = self._unpack("<Q")
            elif tag == _T_INT64:
                result[key] = self._unpack("<q")
            elif tag == _T_WSTR:
                result[key] = self._wstring()
            else:
                raise ValueError(f"Unknown binary KV tag 0x{tag:02X} at {self._pos - 1}")
        return result


class AppInfoReader:
    """Iterates `AppInfo` records from appinfo.vdf. The file is opened read-only."""

    def __init__(self, path: Path):
        self._path = path

    def read_all(self) -> Dict[int, AppInfo]:
        try:
            return {info.appid: info for info in self._iter()}
        except FileNotFoundError:
            log.warning("appinfo.vdf not found at %s", self._path)
        except (OSError, ValueError, struct.error, IndexError) as exc:
            log.warning("Failed to parse %s: %s", self._path, exc)
        return {}

    def _iter(self) -> Iterator[AppInfo]:
        with open(self._path, "rb") as fh:
            data = fh.read()

        magic, _universe = struct.unpack_from("<II", data, 0)
        pos = 8
        string_table: Optional[List[str]] = None

        if magic == _MAGIC_V29:
            (table_offset,) = struct.unpack_from("<q", data, pos)
            pos += 8
            string_table = self._read_string_table(data, table_offset)
            header_skip = 60
        elif magic == _MAGIC_V28:
            header_skip = 60
        elif magic == _MAGIC_V27:
            header_skip = 40
        else:
            raise ValueError(f"Unsupported appinfo.vdf magic 0x{magic:08X}")

        while pos + 8 <= len(data):
            appid, size = struct.unpack_from("<II", data, pos)
            pos += 8
            if appid == 0:
                break
            record = data[pos + header_skip:pos + size]
            pos += size
            try:
                root = _BinaryKV(record, string_table).parse_map()
            except (ValueError, struct.error, IndexError) as exc:
                log.debug("Skipping app %s: %s", appid, exc)
                continue
            common = root.get("appinfo", {}).get("common", {}) if isinstance(root.get("appinfo"), dict) else {}
            name = common.get("name") if isinstance(common, dict) else None
            if name:
                yield AppInfo(appid, str(name), str(common.get("type", "")).lower())

    @staticmethod
    def _read_string_table(data: bytes, offset: int) -> List[str]:
        (count,) = struct.unpack_from("<I", data, offset)
        pos = offset + 4
        strings: List[str] = []
        for _ in range(count):
            end = data.index(b"\x00", pos)
            strings.append(data[pos:end].decode("utf-8", errors="replace"))
            pos = end + 1
        return strings
