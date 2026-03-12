"""WebDAV adapter — Nextcloud file operations.

Reference: openclaw-nextcloud community skill (keithvassallomt)
All methods return explicit structured dicts with http_status and http_calls_made.
Never uses raise_for_status() — every response code is checked and returned verbatim.

Model B note: operations candidates for future DSL frontmatter:
  list(path), read(path), write(path, content), delete(path), mkdir(path), search(query, path)
"""

import os
import re
import httpx
from urllib.parse import unquote

# Base URL: {NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}
# Always constructed without trailing slash; _url() appends path correctly.
_NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NEXTCLOUD_USER = os.environ.get("WEBDAV_USER", "digiant")
WEBDAV_BASE = os.environ.get(
    "WEBDAV_BASE",
    f"{_NEXTCLOUD_URL}/remote.php/dav/files/{_NEXTCLOUD_USER}",
).rstrip("/")

WEBDAV_USER = os.environ.get("WEBDAV_USER", "digiant")
WEBDAV_PASS = os.environ.get("WEBDAV_PASS", "")

PROPFIND_XML = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop>"
    "<d:displayname/><d:getcontentlength/><d:getlastmodified/>"
    "<d:resourcetype/><d:getcontenttype/>"
    "</d:prop>"
    "</d:propfind>"
)

# Path prefix stripped from PROPFIND hrefs — e.g. /remote.php/dav/files/digiant
_BASE_PATH = re.sub(r"^https?://[^/]+", "", WEBDAV_BASE)


def _url(path: str) -> str:
    """Construct a full WebDAV URL rooted at WEBDAV_BASE.

    Always produces: {WEBDAV_BASE}/{path} with exactly one slash between them
    and no leading double-slashes.  path="/" → WEBDAV_BASE + "/"
    path="/Notes/Request/" → WEBDAV_BASE + "/Notes/Request/"
    """
    clean = path.lstrip("/")
    return f"{WEBDAV_BASE}/{clean}"


def _parse_propfind(xml: str, listing_path: str) -> list[dict]:
    """Parse PROPFIND XML into a clean list of {name, type, size, modified, content_type}.

    listing_path is the _BASE_PATH + the requested path, used to strip the prefix from hrefs.
    """
    prefix = (_BASE_PATH + "/" + listing_path.strip("/")).rstrip("/")
    items = []
    for response in re.findall(r"<d:response>(.*?)</d:response>", xml, re.DOTALL):
        href_m = re.search(r"<d:href>(.*?)</d:href>", response)
        if not href_m:
            continue
        href = href_m.group(1).rstrip("/")
        # Skip the directory entry itself
        if href == prefix or href == prefix + "/":
            continue
        name = unquote(href.split("/")[-1])
        if not name:
            continue
        is_dir = "<d:collection" in response
        size_m = re.search(r"<d:getcontentlength>(.*?)</d:getcontentlength>", response)
        mod_m  = re.search(r"<d:getlastmodified>(.*?)</d:getlastmodified>", response)
        ct_m   = re.search(r"<d:getcontenttype>(.*?)</d:getcontenttype>", response)
        items.append({
            "name":         name,
            "type":         "folder" if is_dir else "file",
            "size":         int(size_m.group(1)) if size_m else 0,
            "modified":     mod_m.group(1) if mod_m else "",
            "content_type": ct_m.group(1) if ct_m else "",
        })
    return sorted(items, key=lambda x: (x["type"] != "folder", x["name"].lower()))


class WebDAVAdapter:
    def _auth(self):
        return (WEBDAV_USER, WEBDAV_PASS)

    async def list(self, path: str = "/") -> dict:
        """PROPFIND Depth:1 at path relative to user root.

        Returns http_status and http_calls_made so callers always have real server state.
        Never uses raise_for_status().
        """
        url = _url(path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.request(
                "PROPFIND", url,
                auth=self._auth(),
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=PROPFIND_XML.encode(),
            )
        http_status = r.status_code
        if http_status not in (207, 200):
            return {
                "status": "error",
                "path": path,
                "http_status": http_status,
                "http_calls_made": [f"PROPFIND {url}"],
                "error": f"PROPFIND returned {http_status}",
                "response_body": r.text[:500],
            }
        items = _parse_propfind(r.text, path)
        return {
            "status": "ok",
            "path": path,
            "http_status": http_status,
            "http_calls_made": [f"PROPFIND {url}"],
            "items": items,
            "count": len(items),
        }

    async def list_directory(self, path: str = "/") -> dict:
        """Alias for list() — path always appended to WEBDAV_BASE."""
        return await self.list(path)

    async def navigate(self, path: str) -> dict:
        """PROPFIND Depth:1 — same as list() but each item also carries its full path."""
        result = await self.list(path)
        if result.get("status") == "ok":
            base = path.rstrip("/")
            for item in result.get("items", []):
                item["path"] = f"{base}/{item['name']}"
        return result

    async def read(self, path: str) -> dict:
        """GET file content. Returns http_status explicitly — never raise_for_status."""
        url = _url(path)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, auth=self._auth())
        if r.status_code != 200:
            return {
                "status": "error",
                "path": path,
                "http_status": r.status_code,
                "http_calls_made": [f"GET {url}"],
                "error": f"GET returned {r.status_code}",
                "response_body": r.text[:500],
            }
        return {
            "status": "ok",
            "path": path,
            "http_status": r.status_code,
            "http_calls_made": [f"GET {url}"],
            "content": r.text,
            "size": len(r.content),
            "content_type": r.headers.get("content-type", ""),
        }

    async def write(self, path: str, content: str, content_type: str = "text/plain") -> dict:
        """PUT file content. Returns http_status explicitly — never raise_for_status.

        Nextcloud returns 201 on create, 204 on overwrite. Both are success.
        """
        url = _url(path)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.put(
                url, auth=self._auth(),
                content=content.encode() if isinstance(content, str) else content,
                headers={"Content-Type": content_type},
            )
        if r.status_code not in (200, 201, 204):
            return {
                "status": "error",
                "path": path,
                "http_status": r.status_code,
                "http_calls_made": [f"PUT {url}"],
                "error": f"PUT returned {r.status_code}",
                "response_body": r.text[:500],
            }
        return {
            "status": "ok",
            "path": path,
            "http_status": r.status_code,
            "http_calls_made": [f"PUT {url}"],
            "response_body": r.text[:200] if r.text else "",
        }

    async def delete(self, path: str) -> dict:
        """DELETE file or folder. Returns http_status explicitly — never raise_for_status."""
        url = _url(path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.delete(url, auth=self._auth())
        if r.status_code not in (200, 204):
            return {
                "status": "error",
                "path": path,
                "http_status": r.status_code,
                "http_calls_made": [f"DELETE {url}"],
                "error": f"DELETE returned {r.status_code}",
                "response_body": r.text[:500],
            }
        return {
            "status": "ok",
            "path": path,
            "http_status": r.status_code,
            "http_calls_made": [f"DELETE {url}"],
        }

    async def mkdir(self, path: str) -> dict:
        """MKCOL to create a folder. Returns http_status explicitly — never raise_for_status."""
        url = _url(path)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.request("MKCOL", url, auth=self._auth())
        if r.status_code not in (200, 201):
            return {
                "status": "error",
                "path": path,
                "http_status": r.status_code,
                "http_calls_made": [f"MKCOL {url}"],
                "error": f"MKCOL returned {r.status_code}",
                "response_body": r.text[:500],
            }
        return {
            "status": "ok",
            "path": path,
            "http_status": r.status_code,
            "http_calls_made": [f"MKCOL {url}"],
        }

    async def search(self, query: str, path: str = "/") -> dict:
        """Search for files by name under path using PROPFIND + client-side filter.

        Nextcloud does not support DASL SEARCH universally; we PROPFIND Depth:infinity
        (or Depth:1 per directory) and filter names client-side.
        Returns matching items with their full paths.

        Model B note: future operations DSL would declare this as:
          adapter: webdav, method: search, params: [query, path]
        """
        url = _url(path)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                "PROPFIND", url,
                auth=self._auth(),
                headers={"Depth": "infinity", "Content-Type": "application/xml"},
                content=PROPFIND_XML.encode(),
            )
        if r.status_code not in (207, 200):
            return {
                "status": "error",
                "query": query,
                "path": path,
                "http_status": r.status_code,
                "http_calls_made": [f"PROPFIND {url}"],
                "error": f"PROPFIND returned {r.status_code}",
                "response_body": r.text[:500],
            }

        # Parse all items from PROPFIND and filter by query substring (case-insensitive)
        q = query.lower()
        all_items: list[dict] = []
        prefix = (_BASE_PATH + "/" + path.strip("/")).rstrip("/")
        for response in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.DOTALL):
            href_m = re.search(r"<d:href>(.*?)</d:href>", response)
            if not href_m:
                continue
            href = href_m.group(1).rstrip("/")
            if href == prefix or href == prefix + "/":
                continue
            name = unquote(href.split("/")[-1])
            if not name or q not in name.lower():
                continue
            is_dir = "<d:collection" in response
            size_m = re.search(r"<d:getcontentlength>(.*?)</d:getcontentlength>", response)
            mod_m  = re.search(r"<d:getlastmodified>(.*?)</d:getlastmodified>", response)
            # Reconstruct path relative to user root
            item_path = unquote(href[len(_BASE_PATH):]) if href.startswith(_BASE_PATH) else href
            all_items.append({
                "name":     name,
                "path":     item_path,
                "type":     "folder" if is_dir else "file",
                "size":     int(size_m.group(1)) if size_m else 0,
                "modified": mod_m.group(1) if mod_m else "",
            })

        return {
            "status": "ok",
            "query": query,
            "path": path,
            "http_status": r.status_code,
            "http_calls_made": [f"PROPFIND {url}"],
            "count": len(all_items),
            "items": all_items,
        }
