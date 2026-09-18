"""
onedrive.py — file photos and documents James sends Rowan into his OneDrive.

Everything lands under ONEDRIVE_ROOT_FOLDER (default "Rowan"):
    Rowan/<Project>/Photos/2026-09-18 1432 pool shell poured.jpg
    Rowan/<Project>/Documents/2026-09-18 1440 CO-14.pdf
Anything Rowan can't match to a project goes to Rowan/_Unsorted/.

Graph creates missing folders on a path upload, and a name clash gets a
" 1" suffix rather than overwriting anything.

Needs the Files.ReadWrite delegated permission (re-run outlook_auth.py once
after adding it in Azure).
"""
import os
import re
from urllib.parse import quote

import requests

from outlook_mail import GRAPH, get_access_token

FILES_SCOPES = ["Files.ReadWrite"]
ROOT_FOLDER = os.getenv("ONEDRIVE_ROOT_FOLDER", "Rowan").strip().strip("/") or "Rowan"

SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024      # Graph's cap for a one-shot PUT
CHUNK = 5 * 320 * 1024                     # upload-session chunks: multiple of 320 KiB


def safe_name(text: str, limit: int = 80) -> str:
    """Strip characters OneDrive rejects and keep names short."""
    text = re.sub(r'[\\/:*?"<>|#%~&{}]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "file"


def _item_path(folder: str, filename: str) -> str:
    parts = [ROOT_FOLDER] + [p for p in folder.split("/") if p] + [filename]
    return "/".join(quote(p, safe="") for p in parts)


def upload_file(content: bytes, folder: str, filename: str,
                content_type: str = "application/octet-stream") -> dict:
    """
    Upload bytes to Rowan/<folder>/<filename>. Returns
    {"name", "web_url", "path", "id"}.
    """
    token = get_access_token(scopes=FILES_SCOPES)
    auth = {"Authorization": f"Bearer {token}"}
    path = _item_path(folder, filename)

    if len(content) <= SIMPLE_UPLOAD_LIMIT:
        resp = requests.put(
            f"{GRAPH}/me/drive/root:/{path}:/content",
            params={"@microsoft.graph.conflictBehavior": "rename"},
            headers={**auth, "Content-Type": content_type},
            data=content,
            timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OneDrive upload failed [{resp.status_code}]: {resp.text[:300]}")
        item = resp.json()
    else:
        resp = requests.post(
            f"{GRAPH}/me/drive/root:/{path}:/createUploadSession",
            headers={**auth, "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OneDrive upload session failed [{resp.status_code}]: {resp.text[:300]}")
        upload_url = resp.json()["uploadUrl"]
        total = len(content)
        item = None
        for start in range(0, total, CHUNK):
            end = min(start + CHUNK, total) - 1
            # The pre-authenticated upload URL must NOT get the bearer header.
            r = requests.put(
                upload_url,
                headers={
                    "Content-Length": str(end - start + 1),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
                data=content[start:end + 1],
                timeout=120,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"OneDrive chunk upload failed [{r.status_code}]: {r.text[:300]}")
            if r.status_code in (200, 201):
                item = r.json()
        if not item:
            raise RuntimeError("OneDrive upload finished without returning the file.")

    parent = (item.get("parentReference") or {}).get("path", "")
    return {
        "name": item.get("name", filename),
        "web_url": item.get("webUrl", ""),
        "path": f"{parent.split('root:', 1)[-1]}/{item.get('name', filename)}".lstrip("/"),
        "id": item.get("id", ""),
    }
