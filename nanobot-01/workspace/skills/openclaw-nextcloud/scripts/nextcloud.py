#!/usr/bin/env python3
"""Nextcloud CalDAV + WebDAV operations for nanobot-01 python3_exec dispatch.

Commands (calendar):
  calendar_list    -- PROPFIND calendars, list names + URLs
  calendar_create  -- PUT VEVENT to a calendar
  calendar_delete  -- DELETE event by UID
  calendar_update  -- Fetch + patch + re-PUT VEVENT

Commands (tasks):
  tasks_list       -- PROPFIND VTODO items from tasks calendar
  tasks_create     -- PUT VTODO to tasks calendar
  tasks_complete   -- PATCH VTODO status to COMPLETED
  tasks_delete     -- DELETE VTODO by UID

Commands (files):
  files_list       -- PROPFIND WebDAV directory
  files_search     -- Nextcloud full-text search (SEARCH endpoint)
  files_read       -- GET file content
  files_write      -- PUT file content (--path, --content)
  files_delete     -- DELETE file
  files_mkdir      -- MKCOL create directory

Env vars (from nextcloud.env static mount):
  NEXTCLOUD_ADMIN_USER     (e.g. digiant)
  NEXTCLOUD_ADMIN_PASSWORD
  NEXTCLOUD_URL            (default: http://nextcloud)

Output: JSON to stdout. Errors: {"status":"error","error":"..."} + exit 1.
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_NC_URL = os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/")
_NC_USER = os.environ.get("NEXTCLOUD_ADMIN_USER", "digiant")
_NC_PASS = os.environ.get("NEXTCLOUD_ADMIN_PASSWORD", "")

_DAV_BASE    = f"{_NC_URL}/remote.php/dav"
_CALDAV_BASE = f"{_DAV_BASE}/calendars/{_NC_USER}"
_WEBDAV_BASE = f"{_DAV_BASE}/files/{_NC_USER}"
_NOTES_API   = f"{_NC_URL}/apps/notes/api/v1"


def _auth():
    return HTTPBasicAuth(_NC_USER, _NC_PASS)


def _headers(extra=None):
    h = {"Content-Type": "application/xml; charset=utf-8"}
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------------------
# CalDAV helpers
# ---------------------------------------------------------------------------

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <cs:getctag/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

_REPORT_EVENTS = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag/>
    <c:calendar-data/>
  </d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="{component}"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

_REPORT_VTODO = _REPORT_EVENTS.replace("{component}", "VTODO")
_REPORT_VEVENT = _REPORT_EVENTS.replace("{component}", "VEVENT")


def _caldav_discover_calendar(calendar_hint):
    """PROPFIND to discover calendar URL by partial name match."""
    r = requests.request(
        "PROPFIND", f"{_CALDAV_BASE}/",
        headers={**_headers(), "Depth": "1"},
        data=_PROPFIND_CALENDARS,
        auth=_auth(), timeout=15
    )
    if r.status_code not in (207,):
        return None, {"error": f"PROPFIND calendars failed: HTTP {r.status_code}",
                      "body": r.text[:300]}

    # Find calendar URL by displayname match (case-insensitive partial match)
    hint_lower = calendar_hint.lower()
    # Extract <d:href> + <d:displayname> pairs from multistatus
    hrefs = re.findall(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", r.text)
    names = re.findall(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", r.text)

    # Zip them — PROPFIND returns one <response> per calendar
    # Parse responses properly
    responses = re.findall(
        r"<d:response[^>]*>(.*?)</d:response>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    if not responses:
        # Try alternate namespace prefix
        responses = re.findall(
            r"<[a-z]+:response[^>]*>(.*?)</[a-z]+:response>",
            r.text, re.DOTALL | re.IGNORECASE
        )

    calendars = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        name_m = re.search(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", resp)
        if href_m:
            href = href_m.group(1).strip()
            name = name_m.group(1).strip() if name_m else ""
            # Skip the principal URL itself
            if not href.endswith(f"/{_NC_USER}/") and href.count("/") > 5:
                calendars.append({"href": href, "name": name})

    if not calendars:
        return None, {"error": "No calendars found in PROPFIND response", "body": r.text[:500]}

    # Find best match by name
    for cal in calendars:
        if hint_lower in cal["name"].lower() or hint_lower in cal["href"].lower():
            origin = re.match(r"https?://[^/]+", _NC_URL)
            prefix = origin.group(0) if origin else _NC_URL
            # If href is a path, build full URL
            if cal["href"].startswith("/"):
                return prefix + cal["href"], None
            return cal["href"], None

    # No match — use first calendar
    cal = calendars[0]
    if cal["href"].startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        prefix = origin.group(0) if origin else _NC_URL
        return prefix + cal["href"], None
    return cal["href"], None


def _make_vevent(uid, title, start, end=None, description=None):
    """Generate a VCALENDAR/VEVENT ICS string."""
    if not end:
        end = start

    # Ensure basic ISO format
    start_str = start.replace("-", "").replace(":", "").replace(" ", "T")
    if "T" not in start_str:
        start_str += "T000000"
    end_str = end.replace("-", "").replace(":", "").replace(" ", "T")
    if "T" not in end_str:
        end_str += "T010000"

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    desc_line = f"DESCRIPTION:{description}" if description else ""

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Sovereign//nanobot-01//EN
BEGIN:VEVENT
UID:{uid}
DTSTART:{start_str}
DTEND:{end_str}
SUMMARY:{title}
{desc_line}
DTSTAMP:{now}
CREATED:{now}
END:VEVENT
END:VCALENDAR""".strip()


def _make_vtodo(uid, summary, due=None, description=None, status="NEEDS-ACTION"):
    """Generate a VCALENDAR/VTODO ICS string."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    due_line = ""
    if due:
        due_str = due.replace("-", "").replace(":", "").replace(" ", "T")
        if "T" not in due_str:
            due_str += "T000000"
        due_line = f"DUE:{due_str}"
    desc_line = f"DESCRIPTION:{description}" if description else ""

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Sovereign//nanobot-01//EN
BEGIN:VTODO
UID:{uid}
SUMMARY:{summary}
STATUS:{status}
{due_line}
{desc_line}
DTSTAMP:{now}
CREATED:{now}
END:VTODO
END:VCALENDAR""".strip()


# ---------------------------------------------------------------------------
# Calendar commands
# ---------------------------------------------------------------------------

def cmd_calendar_list():
    """List all calendars."""
    r = requests.request(
        "PROPFIND", f"{_CALDAV_BASE}/",
        headers={**_headers(), "Depth": "1"},
        data=_PROPFIND_CALENDARS,
        auth=_auth(), timeout=15
    )
    if r.status_code not in (207,):
        return {"status": "error", "error": f"PROPFIND failed: HTTP {r.status_code}",
                "body": r.text[:300]}

    responses = re.findall(
        r"<d:response[^>]*>(.*?)</d:response>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    if not responses:
        responses = re.findall(
            r"<[a-z]+:response[^>]*>(.*?)</[a-z]+:response>",
            r.text, re.DOTALL | re.IGNORECASE
        )

    calendars = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        name_m = re.search(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", resp)
        comp_m = re.search(r"comp name=['\"]([^'\"]+)['\"]", resp, re.IGNORECASE)
        if href_m:
            href = href_m.group(1).strip()
            if href.count("/") > 5:  # skip principal URL
                calendars.append({
                    "name": name_m.group(1).strip() if name_m else "",
                    "url": href,
                    "component": comp_m.group(1).upper() if comp_m else "VEVENT",
                })

    return {"status": "ok", "calendars": calendars, "count": len(calendars)}


def cmd_calendar_create(title, start, end=None, description=None, calendar="personal"):
    """Create a calendar event."""
    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    uid = str(uuid.uuid4())
    ics = _make_vevent(uid, title, start, end, description)

    put_url = f"{cal_url.rstrip('/')}/{uid}.ics"
    # put_url may be relative — build absolute
    if put_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        put_url = (origin.group(0) if origin else _NC_URL) + put_url

    r = requests.put(
        put_url,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        data=ics.encode("utf-8"),
        auth=_auth(), timeout=15
    )
    if r.status_code not in (201, 204):
        return {"status": "error", "error": f"PUT event failed: HTTP {r.status_code}",
                "url": put_url, "body": r.text[:300]}

    return {"status": "ok", "uid": uid, "http_status": r.status_code, "url": put_url}


def cmd_calendar_delete(uid, calendar="personal"):
    """Delete a calendar event by UID."""
    if not uid or not uid.strip():
        return {"status": "error", "step": "uid_guard", "error": "uid is required"}

    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    del_url = f"{cal_url.rstrip('/')}/{uid.strip()}.ics"
    if del_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        del_url = (origin.group(0) if origin else _NC_URL) + del_url

    r = requests.delete(del_url, auth=_auth(), timeout=15)
    if r.status_code not in (200, 204):
        return {"status": "error", "error": f"DELETE failed: HTTP {r.status_code}",
                "url": del_url, "body": r.text[:200]}

    return {"status": "ok", "uid": uid, "http_status": r.status_code}


# ---------------------------------------------------------------------------
# Tasks commands
# ---------------------------------------------------------------------------

def cmd_tasks_list(calendar="tasks"):
    """List VTODO tasks from a calendar."""
    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    r = requests.request(
        "REPORT", cal_url.rstrip("/") + "/",
        headers={**_headers(), "Depth": "1"},
        data=_REPORT_VTODO,
        auth=_auth(), timeout=20
    )
    if r.status_code not in (207,):
        return {"status": "error", "error": f"REPORT VTODO failed: HTTP {r.status_code}",
                "body": r.text[:300]}

    # Parse VTODO blocks from calendar-data
    tasks = []
    for cal_data in re.findall(r"BEGIN:VCALENDAR.*?END:VCALENDAR", r.text, re.DOTALL):
        uid_m = re.search(r"^UID:(.+)$", cal_data, re.MULTILINE)
        sum_m = re.search(r"^SUMMARY:(.+)$", cal_data, re.MULTILINE)
        sta_m = re.search(r"^STATUS:(.+)$", cal_data, re.MULTILINE)
        due_m = re.search(r"^DUE:(.+)$", cal_data, re.MULTILINE)
        if uid_m:
            tasks.append({
                "uid": uid_m.group(1).strip(),
                "summary": sum_m.group(1).strip() if sum_m else "",
                "status": sta_m.group(1).strip() if sta_m else "NEEDS-ACTION",
                "due": due_m.group(1).strip() if due_m else "",
            })

    return {"status": "ok", "tasks": tasks, "count": len(tasks)}


def cmd_tasks_create(summary, due=None, description=None, calendar="tasks"):
    """Create a VTODO task."""
    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    uid = str(uuid.uuid4())
    ics = _make_vtodo(uid, summary, due, description)

    put_url = f"{cal_url.rstrip('/')}/{uid}.ics"
    if put_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        put_url = (origin.group(0) if origin else _NC_URL) + put_url

    r = requests.put(
        put_url,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        data=ics.encode("utf-8"),
        auth=_auth(), timeout=15
    )
    if r.status_code not in (201, 204):
        return {"status": "error", "error": f"PUT task failed: HTTP {r.status_code}",
                "url": put_url, "body": r.text[:300]}

    return {"status": "ok", "uid": uid, "http_status": r.status_code}


def cmd_tasks_complete(uid, calendar="tasks"):
    """Mark a task as COMPLETED by fetching, modifying, and re-PUTting the ICS."""
    if not uid or not uid.strip():
        return {"status": "error", "step": "uid_guard", "error": "uid is required"}

    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    task_url = f"{cal_url.rstrip('/')}/{uid.strip()}.ics"
    if task_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        task_url = (origin.group(0) if origin else _NC_URL) + task_url

    # Fetch existing ICS
    r_get = requests.get(task_url, auth=_auth(), timeout=15)
    if r_get.status_code != 200:
        return {"status": "error", "error": f"GET task failed: HTTP {r_get.status_code}"}

    ics = r_get.text
    # Update STATUS field
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ics = re.sub(r"^STATUS:.*$", "STATUS:COMPLETED", ics, flags=re.MULTILINE)
    ics = re.sub(r"^PERCENT-COMPLETE:.*$", "PERCENT-COMPLETE:100", ics, flags=re.MULTILINE)
    # Add COMPLETED timestamp if not present
    if "COMPLETED:" not in ics:
        ics = ics.replace("END:VTODO", f"COMPLETED:{now}\nEND:VTODO")

    r_put = requests.put(
        task_url,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        data=ics.encode("utf-8"),
        auth=_auth(), timeout=15
    )
    if r_put.status_code not in (200, 201, 204):
        return {"status": "error", "error": f"PUT task update failed: HTTP {r_put.status_code}",
                "body": r_put.text[:200]}

    return {"status": "ok", "uid": uid, "http_status": r_put.status_code}


# ---------------------------------------------------------------------------
# Files commands (WebDAV)
# ---------------------------------------------------------------------------

_PROPFIND_FILES = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:resourcetype/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>"""


def cmd_files_list(path="/"):
    """List files in a WebDAV path."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"

    r = requests.request(
        "PROPFIND", url,
        headers={**_headers(), "Depth": "1"},
        data=_PROPFIND_FILES,
        auth=_auth(), timeout=15
    )
    if r.status_code not in (207,):
        return {"status": "error", "error": f"PROPFIND files failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:300]}

    responses = re.findall(
        r"<d:response[^>]*>(.*?)</d:response>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    if not responses:
        responses = re.findall(
            r"<[a-z]+:response[^>]*>(.*?)</[a-z]+:response>",
            r.text, re.DOTALL | re.IGNORECASE
        )

    files = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        name_m = re.search(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", resp)
        size_m = re.search(r"<[^:>]*:getcontentlength[^>]*>([^<]*)</[^:>]*:getcontentlength>", resp)
        type_m = re.search(r"<[^:>]*:getcontenttype[^>]*>([^<]*)</[^:>]*:getcontenttype>", resp)
        is_dir = "<d:collection" in resp or "<collection" in resp

        if href_m:
            href = href_m.group(1).strip()
            # Skip the directory itself
            webdav_path = re.sub(r"^https?://[^/]+", "", href)
            base_path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
            if webdav_path.rstrip("/") == base_path:
                continue
            name = unquote(name_m.group(1).strip() if name_m else href.rstrip("/").rsplit("/", 1)[-1])
            files.append({
                "name": name,
                "path": unquote(webdav_path),
                "type": "dir" if is_dir else "file",
                "size": int(size_m.group(1)) if size_m and size_m.group(1) else 0,
                "content_type": type_m.group(1).strip() if type_m else "",
            })

    return {"status": "ok", "files": files, "count": len(files)}


def cmd_files_search(query, path="/"):
    """Search files by name in Nextcloud (SEARCH DAV method)."""
    url = f"{_NC_URL}/remote.php/dav/files/{_NC_USER}"

    search_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:f="http://nextcloud.com/ns/dav/search/files"
                  xmlns:oc="http://owncloud.org/ns">
  <d:basicsearch>
    <d:select><d:prop><d:displayname/><oc:fileid/><d:getcontentlength/>
      <d:getcontenttype/><d:getetag/>
    </d:prop></d:select>
    <d:from><d:scope><d:href>{url}{path}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
    <d:where>
      <d:like><d:prop><d:displayname/></d:prop><d:literal>%{query}%</d:literal></d:like>
    </d:where>
    <d:limit><d:nresults>50</d:nresults></d:limit>
  </d:basicsearch>
</d:searchrequest>"""

    r = requests.request(
        "SEARCH", url,
        headers={"Content-Type": "application/xml; charset=utf-8"},
        data=search_xml,
        auth=_auth(), timeout=20
    )
    if r.status_code not in (207,):
        # Fallback: PROPFIND + filter by name
        return _files_search_propfind_fallback(query, path)

    responses = re.findall(
        r"<d:response[^>]*>(.*?)</d:response>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    files = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        name_m = re.search(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", resp)
        size_m = re.search(r"<[^:>]*:getcontentlength[^>]*>([^<]*)</[^:>]*:getcontentlength>", resp)
        if href_m:
            href = href_m.group(1).strip()
            name = name_m.group(1).strip() if name_m else href.rsplit("/", 1)[-1]
            files.append({
                "name": name,
                "path": re.sub(r"^https?://[^/]+", "", href),
                "size": int(size_m.group(1)) if size_m and size_m.group(1) else 0,
            })

    return {"status": "ok", "files": files, "count": len(files)}


def _files_search_propfind_fallback(query, path):
    """Fallback: PROPFIND with depth infinity and filter by displayname."""
    url = f"{_WEBDAV_BASE}{path}"
    r = requests.request(
        "PROPFIND", url,
        headers={**_headers(), "Depth": "infinity"},
        data=_PROPFIND_FILES,
        auth=_auth(), timeout=30
    )
    if r.status_code not in (207,):
        return {"status": "error", "error": f"PROPFIND search fallback failed: HTTP {r.status_code}"}

    responses = re.findall(
        r"<d:response[^>]*>(.*?)</d:response>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    query_lower = query.lower()
    files = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        name_m = re.search(r"<[^:>]*:displayname[^>]*>([^<]*)</[^:>]*:displayname>", resp)
        if href_m:
            href = href_m.group(1).strip()
            name = name_m.group(1).strip() if name_m else href.rsplit("/", 1)[-1]
            if query_lower in name.lower():
                files.append({"name": name,
                               "path": re.sub(r"^https?://[^/]+", "", href)})

    return {"status": "ok", "files": files[:50], "count": len(files)}


def cmd_files_read(path):
    """Read (GET) file content from WebDAV."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"
    r = requests.get(url, auth=_auth(), timeout=30)
    if r.status_code != 200:
        return {"status": "error", "error": f"GET file failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:300]}
    return {"status": "ok", "path": path, "content": r.text, "size": len(r.content)}


def cmd_files_write(path, content):
    """Write (PUT) content to a file via WebDAV."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"
    r = requests.put(
        url,
        headers={"Content-Type": "application/octet-stream"},
        data=content.encode("utf-8") if isinstance(content, str) else content,
        auth=_auth(), timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        return {"status": "error", "error": f"PUT file failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:300]}
    return {"status": "ok", "path": path, "http_status": r.status_code}


def cmd_files_delete(path):
    """Delete a file via WebDAV."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"
    r = requests.delete(url, auth=_auth(), timeout=15)
    if r.status_code not in (200, 204):
        return {"status": "error", "error": f"DELETE file failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:200]}
    return {"status": "ok", "path": path, "http_status": r.status_code}


def cmd_files_mkdir(path):
    """Create a directory via WebDAV MKCOL."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"
    r = requests.request("MKCOL", url, auth=_auth(), timeout=15)
    if r.status_code not in (200, 201):
        return {"status": "error", "error": f"MKCOL failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:200]}
    return {"status": "ok", "path": path, "http_status": r.status_code}


def cmd_files_list_recursive(path="/"):
    """List all files and folders under a WebDAV path (Depth: infinity)."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{_WEBDAV_BASE}{path}"

    r = requests.request(
        "PROPFIND", url,
        headers={**_headers(), "Depth": "infinity"},
        data=_PROPFIND_FILES,
        auth=_auth(), timeout=30,
    )
    if r.status_code not in (207,):
        return {"status": "error", "error": f"PROPFIND recursive failed: HTTP {r.status_code}",
                "url": url, "body": r.text[:300]}

    responses = re.findall(r"<d:response[^>]*>(.*?)</d:response>", r.text, re.DOTALL | re.IGNORECASE)
    if not responses:
        responses = re.findall(r"<[a-z]+:response[^>]*>(.*?)</[a-z]+:response>", r.text, re.DOTALL | re.IGNORECASE)

    files = []
    for resp in responses:
        href_m = re.search(r"<[^:>]*:href[^>]*>([^<]+)</[^:>]*:href>", resp)
        size_m = re.search(r"<[^:>]*:getcontentlength[^>]*>([^<]*)</[^:>]*:getcontentlength>", resp)
        type_m = re.search(r"<[^:>]*:getcontenttype[^>]*>([^<]*)</[^:>]*:getcontenttype>", resp)
        is_dir = "<d:collection" in resp or "<collection" in resp
        if not href_m:
            continue
        href = href_m.group(1).strip()
        webdav_path = re.sub(r"^https?://[^/]+", "", href)
        base_path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
        if webdav_path.rstrip("/") == base_path:
            continue
        # Strip WebDAV prefix to get logical path, then URL-decode
        dav_prefix = f"/remote.php/dav/files/{_NC_USER}"
        logical = webdav_path[len(dav_prefix):] if webdav_path.startswith(dav_prefix) else webdav_path
        logical = unquote(logical)
        files.append({
            "path": logical,
            "type": "dir" if is_dir else "file",
            "size": int(size_m.group(1)) if size_m and size_m.group(1) else 0,
            "content_type": type_m.group(1).strip() if type_m else "",
        })

    return {"status": "ok", "path": path, "files": files, "count": len(files)}


_READ_RECURSIVE_MAX_BYTES = 512 * 1024   # 512 KB total content cap
_READ_RECURSIVE_MAX_FILE  = 64  * 1024   # 64 KB per file cap


def cmd_files_read_recursive(path="/"):
    """List all files under a WebDAV path and return the content of each text file.

    Skips binary files (non text/* content-type). Hard caps: 512 KB total, 64 KB per file.
    Returns {status, path, files: [{path, content, size}], total_bytes, skipped}.
    """
    listing = cmd_files_list_recursive(path)
    if listing.get("status") == "error":
        return listing

    results = []
    total_bytes = 0
    skipped = []

    for entry in listing["files"]:
        if entry["type"] == "dir":
            continue
        ct = entry.get("content_type", "")
        # Skip obvious binaries
        if ct and not ct.startswith("text/") and ct not in (
            "application/json", "application/xml", "application/javascript",
            "application/x-yaml", "application/yaml", "",
        ):
            skipped.append({"path": entry["path"], "reason": f"binary ({ct})"})
            continue
        if total_bytes >= _READ_RECURSIVE_MAX_BYTES:
            skipped.append({"path": entry["path"], "reason": "total cap reached"})
            continue

        url = f"{_WEBDAV_BASE}{entry['path']}"
        try:
            r = requests.get(url, auth=_auth(), timeout=30)
        except Exception as e:
            skipped.append({"path": entry["path"], "reason": str(e)})
            continue
        if r.status_code != 200:
            skipped.append({"path": entry["path"], "reason": f"HTTP {r.status_code}"})
            continue

        content = r.text[:_READ_RECURSIVE_MAX_FILE]
        total_bytes += len(content.encode("utf-8"))
        results.append({"path": entry["path"], "content": content, "size": len(r.content)})

    return {
        "status": "ok",
        "path": path,
        "files": results,
        "count": len(results),
        "total_bytes": total_bytes,
        "skipped": skipped,
    }


def cmd_calendar_update(uid, calendar="personal", title=None, start=None, end=None, description=None):
    """Update a calendar event: fetch existing ICS, patch fields, re-PUT."""
    if not uid or not uid.strip():
        return {"status": "error", "step": "uid_guard", "error": "uid is required"}

    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    event_url = f"{cal_url.rstrip('/')}/{uid.strip()}.ics"
    if event_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        event_url = (origin.group(0) if origin else _NC_URL) + event_url

    r_get = requests.get(event_url, auth=_auth(), timeout=15)
    if r_get.status_code != 200:
        return {"status": "error", "error": f"GET event failed: HTTP {r_get.status_code}",
                "url": event_url}

    ics = r_get.text
    if title:
        ics = re.sub(r"^SUMMARY:.*$", f"SUMMARY:{title}", ics, flags=re.MULTILINE)
    if start:
        start_str = start.replace("-", "").replace(":", "").replace(" ", "T")
        if "T" not in start_str:
            start_str += "T000000"
        ics = re.sub(r"^DTSTART[^:]*:.*$", f"DTSTART:{start_str}", ics, flags=re.MULTILINE)
    if end:
        end_str = end.replace("-", "").replace(":", "").replace(" ", "T")
        if "T" not in end_str:
            end_str += "T010000"
        ics = re.sub(r"^DTEND[^:]*:.*$", f"DTEND:{end_str}", ics, flags=re.MULTILINE)
    if description is not None:
        if re.search(r"^DESCRIPTION:", ics, re.MULTILINE):
            ics = re.sub(r"^DESCRIPTION:.*$", f"DESCRIPTION:{description}", ics, flags=re.MULTILINE)
        else:
            ics = ics.replace("END:VEVENT", f"DESCRIPTION:{description}\nEND:VEVENT")

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ics = re.sub(r"^LAST-MODIFIED:.*$", f"LAST-MODIFIED:{now}", ics, flags=re.MULTILINE)
    if "LAST-MODIFIED:" not in ics:
        ics = ics.replace("END:VEVENT", f"LAST-MODIFIED:{now}\nEND:VEVENT")

    r_put = requests.put(
        event_url,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        data=ics.encode("utf-8"),
        auth=_auth(), timeout=15,
    )
    if r_put.status_code not in (200, 201, 204):
        return {"status": "error", "error": f"PUT event update failed: HTTP {r_put.status_code}",
                "body": r_put.text[:200]}
    return {"status": "ok", "uid": uid, "http_status": r_put.status_code}


def cmd_tasks_delete(uid, calendar="tasks"):
    """Delete a VTODO task by UID."""
    if not uid or not uid.strip():
        return {"status": "error", "step": "uid_guard", "error": "uid is required"}

    cal_url, err = _caldav_discover_calendar(calendar)
    if err:
        return {"status": "error", **err}

    task_url = f"{cal_url.rstrip('/')}/{uid.strip()}.ics"
    if task_url.startswith("/"):
        origin = re.match(r"https?://[^/]+", _NC_URL)
        task_url = (origin.group(0) if origin else _NC_URL) + task_url

    r = requests.delete(task_url, auth=_auth(), timeout=15)
    if r.status_code not in (200, 204):
        return {"status": "error", "error": f"DELETE task failed: HTTP {r.status_code}",
                "url": task_url, "body": r.text[:200]}
    return {"status": "ok", "uid": uid, "http_status": r.status_code}


# ---------------------------------------------------------------------------
# Notes commands (Nextcloud Notes REST API)
# ---------------------------------------------------------------------------

def cmd_notes_list():
    """List all notes."""
    r = requests.get(f"{_NOTES_API}/notes", auth=_auth(), timeout=15)
    if r.status_code != 200:
        return {"status": "error", "error": f"GET notes failed: HTTP {r.status_code}",
                "body": r.text[:300]}
    notes = r.json()
    return {
        "status": "ok",
        "notes": [
            {"id": n.get("id"), "title": n.get("title"), "category": n.get("category", ""),
             "favorite": n.get("favorite", False), "modified": n.get("modified")}
            for n in notes
        ],
        "count": len(notes),
    }


def cmd_notes_read(note_id):
    """Read a single note by ID."""
    if not note_id:
        return {"status": "error", "step": "id_guard", "error": "note-id is required"}
    r = requests.get(f"{_NOTES_API}/notes/{note_id}", auth=_auth(), timeout=15)
    if r.status_code != 200:
        return {"status": "error", "error": f"GET note failed: HTTP {r.status_code}",
                "body": r.text[:300]}
    n = r.json()
    return {
        "status": "ok",
        "id": n.get("id"),
        "title": n.get("title"),
        "content": n.get("content", ""),
        "category": n.get("category", ""),
        "favorite": n.get("favorite", False),
        "modified": n.get("modified"),
    }


def cmd_notes_create(title, content="", category=""):
    """Create a new note."""
    payload = {"title": title, "content": content, "category": category}
    r = requests.post(
        f"{_NOTES_API}/notes",
        json=payload,
        headers={"Content-Type": "application/json"},
        auth=_auth(), timeout=15,
    )
    if r.status_code not in (200, 201):
        return {"status": "error", "error": f"POST note failed: HTTP {r.status_code}",
                "body": r.text[:300]}
    n = r.json()
    return {"status": "ok", "id": n.get("id"), "title": n.get("title"), "http_status": r.status_code}


def cmd_notes_update(note_id, title=None, content=None, category=None, favorite=None):
    """Update an existing note (partial update via PUT)."""
    if not note_id:
        return {"status": "error", "step": "id_guard", "error": "note-id is required"}
    # Fetch current state first
    r_get = requests.get(f"{_NOTES_API}/notes/{note_id}", auth=_auth(), timeout=15)
    if r_get.status_code != 200:
        return {"status": "error", "error": f"GET note failed: HTTP {r_get.status_code}"}
    current = r_get.json()
    payload = {
        "title":    title    if title    is not None else current.get("title", ""),
        "content":  content  if content  is not None else current.get("content", ""),
        "category": category if category is not None else current.get("category", ""),
        "favorite": favorite if favorite is not None else current.get("favorite", False),
    }
    r = requests.put(
        f"{_NOTES_API}/notes/{note_id}",
        json=payload,
        headers={"Content-Type": "application/json"},
        auth=_auth(), timeout=15,
    )
    if r.status_code != 200:
        return {"status": "error", "error": f"PUT note failed: HTTP {r.status_code}",
                "body": r.text[:300]}
    n = r.json()
    return {"status": "ok", "id": n.get("id"), "title": n.get("title"), "http_status": r.status_code}


def cmd_notes_delete(note_id):
    """Delete a note by ID."""
    if not note_id:
        return {"status": "error", "step": "id_guard", "error": "note-id is required"}
    r = requests.delete(f"{_NOTES_API}/notes/{note_id}", auth=_auth(), timeout=15)
    if r.status_code not in (200, 204):
        return {"status": "error", "error": f"DELETE note failed: HTTP {r.status_code}",
                "body": r.text[:200]}
    return {"status": "ok", "id": note_id, "http_status": r.status_code}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Nextcloud CalDAV + WebDAV operations")
    parser.add_argument("--command", required=True,
                        choices=["calendar_list", "calendar_create", "calendar_delete",
                                 "calendar_update",
                                 "tasks_list", "tasks_create", "tasks_complete", "tasks_delete",
                                 "files_list", "files_search", "files_read", "files_write",
                                 "files_delete", "files_mkdir",
                                 "files_list_recursive", "files_read_recursive",
                                 "notes_list", "notes_read", "notes_create",
                                 "notes_update", "notes_delete"])
    # Calendar / event params
    parser.add_argument("--title", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--calendar", default="personal")
    parser.add_argument("--uid", default="")
    # Task params
    parser.add_argument("--summary", default="")
    parser.add_argument("--due", default="")
    # File params
    parser.add_argument("--path", default="/")
    parser.add_argument("--query", default="")
    parser.add_argument("--content", default="")
    # Notes params
    parser.add_argument("--note-id", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--favorite", type=str, default="false")
    args = parser.parse_args()

    if not _NC_PASS:
        print(json.dumps({"status": "error",
                          "error": "NEXTCLOUD_ADMIN_PASSWORD not set"}))
        sys.exit(1)

    try:
        if args.command == "calendar_list":
            result = cmd_calendar_list()
        elif args.command == "calendar_create":
            result = cmd_calendar_create(args.title, args.start, args.end or None,
                                         args.description or None, args.calendar)
        elif args.command == "calendar_delete":
            result = cmd_calendar_delete(args.uid, args.calendar)
        elif args.command == "tasks_list":
            result = cmd_tasks_list(args.calendar or "tasks")
        elif args.command == "tasks_create":
            result = cmd_tasks_create(args.summary, args.due or None,
                                      args.description or None, args.calendar or "tasks")
        elif args.command == "tasks_complete":
            result = cmd_tasks_complete(args.uid, args.calendar or "tasks")
        elif args.command == "tasks_delete":
            result = cmd_tasks_delete(args.uid, args.calendar or "tasks")
        elif args.command == "files_list":
            result = cmd_files_list(args.path or "/")
        elif args.command == "files_search":
            result = cmd_files_search(args.query, args.path or "/")
        elif args.command == "files_read":
            result = cmd_files_read(args.path)
        elif args.command == "files_write":
            result = cmd_files_write(args.path, args.content)
        elif args.command == "files_delete":
            result = cmd_files_delete(args.path)
        elif args.command == "files_mkdir":
            result = cmd_files_mkdir(args.path)
        elif args.command == "files_list_recursive":
            result = cmd_files_list_recursive(args.path or "/")
        elif args.command == "files_read_recursive":
            result = cmd_files_read_recursive(args.path or "/")
        elif args.command == "calendar_update":
            result = cmd_calendar_update(
                args.uid, args.calendar,
                title=args.title or None,
                start=args.start or None,
                end=args.end or None,
                description=args.description or None,
            )
        elif args.command == "notes_list":
            result = cmd_notes_list()
        elif args.command == "notes_read":
            result = cmd_notes_read(args.note_id)
        elif args.command == "notes_create":
            result = cmd_notes_create(args.title, args.content or "", args.category or "")
        elif args.command == "notes_update":
            result = cmd_notes_update(
                args.note_id,
                title=args.title or None,
                content=args.content or None,
                category=args.category or None,
                favorite=True if str(args.favorite).lower() in ("true", "1", "yes") else None,
            )
        elif args.command == "notes_delete":
            result = cmd_notes_delete(args.note_id)
        else:
            result = {"status": "error", "error": f"Unknown command: {args.command}"}
    except Exception as e:
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    print(json.dumps(result))
    sys.exit(1 if result.get("status") == "error" else 0)


if __name__ == "__main__":
    main()
