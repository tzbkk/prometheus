"""SQLCipher decryption for NTQQ databases.

NTQQ wraps each SQLite database in a custom SQLCipher envelope:
- The first 1024 bytes are an NTQQ-specific header (starts with ``SQLite header 3``).
- The real SQLCipher data follows: page 1 begins with a 16-byte salt.
- Pages are 4096 bytes. The crypto reserve is 48 bytes (16-byte IV + 20-byte HMAC-SHA1
  + 12 bytes padding), so the SQLite reserved-bytes-per-page header field is 80.
- The 32-byte key from memory is used directly as the AES-256-CBC key (no PBKDF2).
"""

from __future__ import annotations

import struct
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
EXT_HEADER = 1024
SALT_SIZE = 16
RESERVE = 48
IV_SIZE = 16
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGIC = (0x377F0682, 0x377F0683)


def _aes_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(ct) + dec.finalize()


def _decrypt_page(key: bytes, page_data: bytes, page_no: int) -> bytes:
    skip = SALT_SIZE if page_no == 1 else 0
    data = page_data[skip:]
    enc_len = len(data) - RESERVE
    ciphertext = data[:enc_len]
    iv = data[enc_len : enc_len + IV_SIZE]
    plaintext = _aes_cbc_decrypt(key, iv, ciphertext)
    full = bytearray(PAGE_SIZE)
    if page_no == 1:
        full[0:16] = SQLITE_MAGIC
        copy_len = min(len(plaintext), PAGE_SIZE - 16)
        full[16 : 16 + copy_len] = plaintext[:copy_len]
        full[16] = PAGE_SIZE >> 8
        full[17] = PAGE_SIZE & 0xFF
    else:
        copy_len = min(len(plaintext), PAGE_SIZE)
        full[:copy_len] = plaintext[:copy_len]
    return bytes(full)


def _apply_wal(key: bytes, wal_path: Path, pages: dict[int, bytes]) -> int:
    if not wal_path.exists():
        return 0
    wal = wal_path.read_bytes()
    if len(wal) < 32:
        return 0
    magic = struct.unpack(">I", wal[:4])[0]
    if magic not in WAL_MAGIC:
        return 0

    frame_size = 24 + PAGE_SIZE
    offset = 32
    committed: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    applied = 0

    while offset + frame_size <= len(wal):
        pgno, commit_val = struct.unpack(">II", wal[offset : offset + 8])
        page_data = wal[offset + 24 : offset + 24 + PAGE_SIZE]
        pending[pgno] = _decrypt_page(key, page_data, pgno)
        if commit_val != 0:
            committed.update(pending)
            pending.clear()
        offset += frame_size

    for pgno, page in committed.items():
        pages[pgno] = page
        applied += 1
    return applied


def decrypt_file(src: Path, dst: Path, key_hex: str) -> int:
    """Decrypt an NTQQ SQLCipher database (with WAL) to a plain SQLite file.

    Returns the number of pages in the output.
    """
    key = bytes.fromhex(key_hex)
    with open(src, "rb") as f:
        f.seek(EXT_HEADER)
        raw = f.read()

    n_pages = len(raw) // PAGE_SIZE
    pages: dict[int, bytes] = {}
    for p in range(n_pages):
        pages[p + 1] = _decrypt_page(key, raw[p * PAGE_SIZE : (p + 1) * PAGE_SIZE], p + 1)

    wal_applied = _apply_wal(key, src.with_suffix(src.suffix + "-wal"), pages)

    max_pno = max(pages) if pages else 0
    out = bytearray()
    for pno in range(1, max_pno + 1):
        out += pages.get(pno, bytes(PAGE_SIZE))

    dst.write_bytes(out)
    return max_pno


def open_decrypted(path: Path) -> sqlite3.Connection:
    """Open a decrypted database, raising if it's still encrypted."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        con.close()
        raise RuntimeError(f"{path.name} could not be opened (still encrypted?): {exc}") from exc
    return con
