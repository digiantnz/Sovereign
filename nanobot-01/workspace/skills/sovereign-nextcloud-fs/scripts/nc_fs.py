#!/usr/bin/env python3
"""Nextcloud full filesystem operations for nanobot-01 python3_exec dispatch.

Commands:
  telegram_upload  -- read binary from tmp_path, MKCOL /downloads/, PUT /downloads/{filename}
  fs_list          -- PROPFIND Depth:1 on any path; items with full paths
  fs_list_recursive -- PROPFIND Depth:infinity; full tree
  fs_read          -- GET file content (binary → base64)
  fs_move          -- WebDAV MOVE src → dest
  fs_copy          -- WebDAV COPY src → dest
  fs_mkdir         -- MKCOL path (idempotent: 405=already exists)
  fs_delete        -- DELETE file or folder
  fs_tag           -- OCS create-or-find tag, get fileId, assign tag to file
  fs_untag         -- OCS remove tag from file
  fs_search        -- PROPFIND Depth:infinity + client-side name filter

Env vars (from nextcloud.env static mount):
  NEXTCLOUD_ADMIN_USER
  NEXTCLOUD_ADMIN_PASSWORD
  NEXTCLOUD_URL  (default: http://nextcloud)

Output: JSON to stdout. Errors: {"status":"error","error":"..."} + exit 1.
"""

import argparse
import base64
import json
import os
import re
import sys
from urllib.parse import unquote, quote

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_NC_URL  = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "digiant")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")

_DAV_BASE    = f"{_NC_URL}/remote.php/dav"
_WEBDAV_BASE = f"{_DAV_BASE}/files/{_NC_USER}"
_OCS_TAGS    = f"{_NC_URL}/ocs/v2.php/apps/systemtags/api/v1"


def _auth():
    return HTTPBasicAuth(_NC_USER, _NC_PASS)


def _dav_headers(extra=None):
    h = {"Content-Type": "application/xml; charset=utf-8"}
    if extra:
        h.update(extra)
    return h


def _ocs_headers():
    return {"OCS-APIRequest": "true", "Accept": "application/json"}


def _dav_url(path):
    return f"{_WEBDAV_BASE}/{quote(path.lstrip('/'), safe='/')}"


def _out(obj):
    print(json.dumps(obj))


def _err(msg, **kwargs):
    _out({"status": "error", "error": msg, **kwargs})
    sys.exit(1)


# ---------------------------------------------------------------------------
# PROPFIND parser
# ---------------------------------------------------------------------------

_PROPFIND_XML = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    "<d:prop>"
    "<d:displayname/><d:getcontentlength/><d:getlastmodified/>"
    "<d:resourcetype/><d:getcontenttype/>"
    "<oc:fileid/>"
    "</d:prop>"
    "</d:propfind>"
)

_BASE_PATH_STRIP = f"/remote.php/dav/files/{_NC_USER}"


def _parse_propfind(xml, listing_path, include_self=False):
    prefix = (_BASE_PATH_STRIP + "/" + listing_path.strip("/")).rstrip("/")
    items = []
    for resp in re.findall(r"<d:response>(.*?)</d:response>", xml, re.DOTALL):
        href_m = re.search(r"<d:href>(.*?)</d:href>", resp)
        if not href_m:
            continue
        href = href_m.group(1).rstrip("/")
        if not include_self and (href == prefix or href == prefix + "/"):
            continue
        name = unquote(href.split("/")[-1])
        if not name:
            continue
        is_dir = "<d:collection" in resp
        size_m = re.search(r"<d:getcontentlength>(.*?)</d:getcontentlength>", resp)
        mod_m  = re.search(r"<d:getlastmodified>(.*?)</d:getlastmodified>", resp)
        ct_m   = re.search(r"<d:getcontenttype>(.*?)</d:getcontenttype>", resp)
        fid_m  = re.search(r"<oc:fileid>(.*?)</oc:fileid>", resp)
        # Reconstruct path relative to user root
        item_path = unquote(href[len(_BASE_PATH_STRIP):]) if href.startswith(_BASE_PATH_STRIP) else href
        items.append({
            "name":         name,
            "type":         "folder" if is_dir else "file",
            "size":         int(size_m.group(1)) if size_m else 0,
            "modified":     mod_m.group(1) if mod_m else "",
            "content_type": ct_m.group(1) if ct_m else "",
            "path":         item_path,
            "file_id":      fid_m.group(1).strip() if fid_m else "",
        })
    return sorted(items, key=lambda x: (x["type"] != "folder", x["name"].lower()))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_telegram_upload(args):
    filename  = args.filename
    tmp_path  = args.tmp_path
    mime_type = args.mime_type or "application/octet-stream"
    size      = int(args.size or 0)

    if not os.path.isfile(tmp_path):
        _err(f"tmp_path not found: {tmp_path}")

    with open(tmp_path, "rb") as f:
        content = f.read()

    # Ensure /downloads/ exists — MKCOL; 405 = already exists, both OK
    folder_url = _dav_url("downloads")
    r = requests.request("MKCOL", folder_url, auth=_auth())
    if r.status_code not in (201, 405):
        _err(f"MKCOL /downloads/ failed: {r.status_code}", response_body=r.text[:200])

    dest_path = f"/downloads/{filename}"
    upload_url = _dav_url(dest_path.lstrip("/"))
    r = requests.put(upload_url, data=content, auth=_auth(),
                     headers={"Content-Type": mime_type})
    if r.status_code not in (200, 201, 204):
        _err(f"PUT {dest_path} failed: {r.status_code}", response_body=r.text[:200])

    _out({
        "status":      "ok",
        "path":        dest_path,
        "filename":    filename,
        "size":        len(content),
        "http_status": r.status_code,
    })


def cmd_fs_list(args):
    path = args.path or "/"
    url  = _dav_url(path)
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers=_dav_headers({"Depth": "1"}),
                         data=_PROPFIND_XML.encode())
    if r.status_code not in (207, 200):
        _err(f"PROPFIND {path} failed: {r.status_code}", http_status=r.status_code)
    items = _parse_propfind(r.text, path)
    _out({"status": "ok", "path": path, "items": items, "count": len(items),
          "http_status": r.status_code})


def cmd_fs_list_recursive(args):
    path = args.path or "/"
    url  = _dav_url(path)
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers=_dav_headers({"Depth": "infinity"}),
                         data=_PROPFIND_XML.encode())
    if r.status_code not in (207, 200):
        _err(f"PROPFIND {path} failed: {r.status_code}", http_status=r.status_code)
    items = _parse_propfind(r.text, path)
    _out({"status": "ok", "path": path, "items": items, "count": len(items),
          "http_status": r.status_code})


def cmd_fs_read(args):
    path = args.path
    url  = _dav_url(path)
    r = requests.get(url, auth=_auth(), timeout=30)
    if r.status_code != 200:
        _err(f"GET {path} failed: {r.status_code}", http_status=r.status_code)
    ct = r.headers.get("content-type", "")
    is_text = ct.startswith("text/") or ct in (
        "application/json", "application/xml", "application/javascript",
        "application/x-yaml", "application/yaml",
    )
    if is_text:
        _out({"status": "ok", "path": path, "content": r.text,
              "content_type": ct, "size": len(r.content), "binary": False,
              "http_status": r.status_code})
    else:
        _out({"status": "ok", "path": path,
              "content": base64.b64encode(r.content).decode(),
              "content_type": ct, "size": len(r.content), "binary": True,
              "http_status": r.status_code})


def cmd_fs_move(args):
    src  = args.src
    dest = args.dest
    src_url = _dav_url(src)
    dest_url = _dav_url(dest)
    r = requests.request("MOVE", src_url, auth=_auth(),
                         headers={"Destination": dest_url, "Overwrite": "F"})
    if r.status_code not in (201, 204):
        _err(f"MOVE {src} → {dest} failed: {r.status_code}", http_status=r.status_code,
             response_body=r.text[:200])
    _out({"status": "ok", "src": src, "dest": dest, "http_status": r.status_code})


def cmd_fs_copy(args):
    src  = args.src
    dest = args.dest
    src_url  = _dav_url(src)
    dest_url = _dav_url(dest)
    r = requests.request("COPY", src_url, auth=_auth(),
                         headers={"Destination": dest_url, "Overwrite": "F"})
    if r.status_code not in (201, 204):
        _err(f"COPY {src} → {dest} failed: {r.status_code}", http_status=r.status_code,
             response_body=r.text[:200])
    _out({"status": "ok", "src": src, "dest": dest, "http_status": r.status_code})


def cmd_fs_mkdir(args):
    path = args.path
    url  = _dav_url(path)
    r = requests.request("MKCOL", url, auth=_auth())
    # 405 = already exists — treat as success
    if r.status_code not in (201, 405):
        _err(f"MKCOL {path} failed: {r.status_code}", http_status=r.status_code,
             response_body=r.text[:200])
    _out({"status": "ok", "path": path, "http_status": r.status_code,
          "already_existed": r.status_code == 405})


def cmd_fs_delete(args):
    path = args.path
    url  = _dav_url(path)
    r = requests.delete(url, auth=_auth())
    if r.status_code not in (200, 204):
        _err(f"DELETE {path} failed: {r.status_code}", http_status=r.status_code,
             response_body=r.text[:200])
    _out({"status": "ok", "path": path, "http_status": r.status_code})


def _get_file_id(path):
    """PROPFIND to get the owncloud fileId for a path."""
    url = _dav_url(path)
    propfind = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:prop><oc:fileid/></d:prop>"
        "</d:propfind>"
    )
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers=_dav_headers({"Depth": "0"}),
                         data=propfind.encode())
    if r.status_code not in (207, 200):
        return None, r.status_code
    m = re.search(r"<oc:fileid>(.*?)</oc:fileid>", r.text)
    return (m.group(1).strip() if m else None), r.status_code


def _find_tag_id(tag_name):
    """Look up an existing system tag by name. Returns tag_id string or None."""
    _dav_tags = f"{_NC_URL}/remote.php/dav/systemtags"
    _propfind = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:prop><oc:display-name/><oc:id/></d:prop>"
        "</d:propfind>"
    )
    r = requests.request("PROPFIND", f"{_dav_tags}/",
                         auth=_auth(),
                         headers={"Depth": "1", "Content-Type": "application/xml"},
                         data=_propfind.encode())
    if r.status_code != 207:
        return None
    for name_m, id_m in zip(
        re.finditer(r"<oc:display-name>(.*?)</oc:display-name>", r.text),
        re.finditer(r"<oc:id>(\d+)</oc:id>", r.text),
    ):
        if name_m.group(1).strip() == tag_name:
            return id_m.group(1).strip()
    return None


def _find_or_create_tag(tag_name):
    """Find an existing system tag by name, or create it via WebDAV. Returns (tag_id, created)."""
    existing = _find_tag_id(tag_name)
    if existing:
        return existing, False
    # Create tag via DAV POST
    r2 = requests.post(f"{_NC_URL}/remote.php/dav/systemtags/", auth=_auth(),
                       headers={"Content-Type": "application/json"},
                       json={"name": tag_name, "userVisible": True, "userAssignable": False})
    if r2.status_code in (200, 201):
        loc = r2.headers.get("Content-Location", "") or r2.headers.get("Location", "")
        if loc:
            return loc.rstrip("/").split("/")[-1], True
    return None, False


def cmd_fs_tag(args):
    path     = args.path
    tag_name = args.tag

    file_id, propfind_status = _get_file_id(path)
    if not file_id:
        _err(f"Could not get fileId for {path}", propfind_http_status=propfind_status)

    tag_id, created = _find_or_create_tag(tag_name)
    if not tag_id:
        _err(f"Could not find or create tag '{tag_name}'")

    # Assign tag via WebDAV systemtags-relations
    assign_url = f"{_NC_URL}/remote.php/dav/systemtags-relations/files/{file_id}/{tag_id}"
    r = requests.put(assign_url, auth=_auth(), headers={"Content-Length": "0"})
    # 201 = created relation, 204 = assigned, 409 = already assigned — all success
    if r.status_code not in (200, 201, 204, 409):
        _err(f"Tag assign failed: {r.status_code}", http_status=r.status_code,
             response_body=r.text[:200])

    _out({
        "status":          "ok",
        "path":            path,
        "tag":             tag_name,
        "tag_id":          tag_id,
        "file_id":         file_id,
        "created_tag":     created,
        "already_tagged":  r.status_code == 409,
        "http_status":     r.status_code,
        "http_calls_made": ["PROPFIND fileid", "GET/POST tag", "PUT assign"],
    })


def cmd_fs_untag(args):
    path     = args.path
    tag_name = args.tag

    file_id, _ = _get_file_id(path)
    if not file_id:
        _err(f"Could not get fileId for {path}")

    # Find tag_id via DAV PROPFIND (lookup only, no creation)
    tag_id = _find_tag_id(tag_name)
    if not tag_id:
        _err(f"Tag '{tag_name}' not found")

    del_url = f"{_NC_URL}/remote.php/dav/systemtags-relations/files/{file_id}/{tag_id}"
    r2 = requests.delete(del_url, auth=_auth())
    if r2.status_code not in (200, 204, 404):
        _err(f"Untag failed: {r2.status_code}", http_status=r2.status_code)

    _out({"status": "ok", "path": path, "tag": tag_name, "http_status": r2.status_code})


def cmd_fs_search(args):
    query = args.query
    path  = args.path or "/"
    url   = _dav_url(path)
    r = requests.request("PROPFIND", url, auth=_auth(),
                         headers=_dav_headers({"Depth": "infinity"}),
                         data=_PROPFIND_XML.encode(), timeout=30)
    if r.status_code not in (207, 200):
        _err(f"PROPFIND {path} failed: {r.status_code}", http_status=r.status_code)

    q = query.lower()
    all_items = _parse_propfind(r.text, path)
    matches = [i for i in all_items if q in i["name"].lower()]
    _out({"status": "ok", "query": query, "path": path,
          "items": matches, "count": len(matches), "http_status": r.status_code})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMANDS = {
    "telegram_upload":    cmd_telegram_upload,
    "fs_list":            cmd_fs_list,
    "fs_list_recursive":  cmd_fs_list_recursive,
    "fs_read":            cmd_fs_read,
    "fs_move":            cmd_fs_move,
    "fs_copy":            cmd_fs_copy,
    "fs_mkdir":           cmd_fs_mkdir,
    "fs_delete":          cmd_fs_delete,
    "fs_tag":             cmd_fs_tag,
    "fs_untag":           cmd_fs_untag,
    "fs_search":          cmd_fs_search,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--command",   required=True, choices=list(_COMMANDS))
    p.add_argument("--path",      default="")
    p.add_argument("--src",       default="")
    p.add_argument("--dest",      default="")
    p.add_argument("--query",     default="")
    p.add_argument("--tag",       default="")
    p.add_argument("--filename",  default="")
    p.add_argument("--tmp_path",  dest="tmp_path", default="")
    p.add_argument("--size",      default="0")
    p.add_argument("--mime_type", dest="mime_type", default="application/octet-stream")
    args = p.parse_args()

    fn = _COMMANDS[args.command]
    try:
        fn(args)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"{args.command} raised: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
