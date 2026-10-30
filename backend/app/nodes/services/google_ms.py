"""Google Workspace, Microsoft 365 and social integrations (OAuth2 credentials)."""

import base64
import json
from email.message import EmailMessage
from urllib.parse import quote

from ..declarative import Cursor, F, NextUrl, op, service

GOOGLE = dict(group="productivity", color="#4285f4")
MS = dict(group="productivity", color="#0078d4")
SOCIAL = dict(group="marketing")

# ---- Google Sheets ---------------------------------------------------------------------
SHEET = F("spreadsheet_id", "Spreadsheet id", req=True, desc="From the URL: /spreadsheets/d/<id>/")
TAB = F("sheet", "Sheet (tab) name", req=True, default="Sheet1")


def _rows_to_objects(values: list[list]) -> list[dict]:
    if not values:
        return []
    head = [str(h) for h in values[0]]
    return [{**{head[k]: (row[k] if k < len(row) else "") for k in range(len(head))}, "row_number": n + 2}
            for n, row in enumerate(values[1:])]


async def _sheets_get(call, v, item, ctx, p):
    data = await call("GET", f"/spreadsheets/{v['spreadsheet_id']}/values/{quote(v['sheet'] + '!' + (v.get('range') or 'A:ZZ'), safe='')}")
    return _rows_to_objects(data.get("values") or [])


async def _sheets_append(call, v, item, ctx, p):
    """Map the item's fields onto the header row (n8n's 'map automatically'); new keys are ignored."""
    sheet, sid = v["sheet"], v["spreadsheet_id"]
    head = ((await call("GET", f"/spreadsheets/{sid}/values/{quote(sheet + '!1:1', safe='')}")).get("values") or [[]])[0]
    data = v.get("row") or {k: x for k, x in item.items() if k != "binary"}
    if not head:  # empty sheet: write a header row from the item first
        head = list(data)
        await call("PUT", f"/spreadsheets/{sid}/values/{quote(sheet + '!1:1', safe='')}", params={"valueInputOption": "RAW"},
                   json={"values": [head]})
    row = [data.get(h, "") if not isinstance(data.get(h), (dict, list)) else json.dumps(data.get(h)) for h in head]
    res = await call("POST", f"/spreadsheets/{sid}/values/{quote(sheet + '!A1', safe='')}:append",
                     params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, json={"values": [row]})
    return [{**data, "updatedRange": (res.get("updates") or {}).get("updatedRange")}]


async def _sheets_update(call, v, item, ctx, p):
    sheet, sid, n = v["sheet"], v["spreadsheet_id"], int(v["row_number"])
    head = ((await call("GET", f"/spreadsheets/{sid}/values/{quote(sheet + '!1:1', safe='')}")).get("values") or [[]])[0]
    data = v.get("row") or {k: x for k, x in item.items() if k not in ("binary", "row_number")}
    current = ((await call("GET", f"/spreadsheets/{sid}/values/{quote(f'{sheet}!{n}:{n}', safe='')}")).get("values") or [[]])[0]
    row = [data[h] if h in data else (current[k] if k < len(current) else "") for k, h in enumerate(head)]
    await call("PUT", f"/spreadsheets/{sid}/values/{quote(f'{sheet}!A{n}', safe='')}", params={"valueInputOption": "USER_ENTERED"},
               json={"values": [row]})
    return [{**data, "row_number": n}]


service("google_sheets", "Google Sheets", ["google_oauth2"], "https://sheets.googleapis.com/v4", icon="sheet", **GOOGLE, ops=[
    op("row", "get", "Get rows", "GET", "", SHEET, TAB, F("range", "Range (empty = all)", desc="e.g. A1:F100"), custom=_sheets_get),
    op("row", "append", "Append row (match columns)", "POST", "", SHEET, TAB,
       F("row", "Row (JSON, empty = the input item)", type="json", desc="Keys are matched to the header row"), custom=_sheets_append),
    op("row", "update", "Update row", "PUT", "", SHEET, TAB, F("row_number", type="number", req=True, default="{{ $json.row_number }}"),
       F("row", "Values (JSON, empty = the input item)", type="json"), custom=_sheets_update),
    op("range", "clear", "Clear range", "POST", "/spreadsheets/{spreadsheet_id}/values/{a1}:clear", SHEET, F("a1", "Range (A1)", req=True, default="Sheet1!A2:Z")),
    op("spreadsheet", "create", "Create spreadsheet", "POST", "/spreadsheets", F("title", req=True),
       body=lambda v: {"properties": {"title": v["title"]}}),
])

# ---- Google Drive -------------------------------------------------------------------------


async def _drive_upload(call, v, item, ctx, p):
    data, ref = ctx.file(item, p.get("binary_property") or "data")
    meta = {"name": v.get("name") or ref.get("fileName"), **({"parents": [v["folder_id"]]} if v.get("folder_id") else {})}
    boundary = "debbuilder_boundary_7d9"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {ref.get('mimeType') or 'application/octet-stream'}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
    res = await call("POST", "https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
                     content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    return [res]


async def _drive_download(call, v, item, ctx, p):
    meta = await call("GET", f"/files/{v['file_id']}", params={"fields": "id,name,mimeType"})
    if meta.get("mimeType", "").startswith("application/vnd.google-apps"):  # Docs/Sheets/Slides: export
        fmt = v.get("export_as") or "application/pdf"
        r = await call("GET", f"/files/{v['file_id']}/export", params={"mimeType": fmt})
        ext = {"application/pdf": "pdf", "text/csv": "csv", "text/plain": "txt",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"}.get(fmt, "bin")
        name, mime = f"{meta['name']}.{ext}", fmt
    else:
        r = await call("GET", f"/files/{v['file_id']}", params={"alt": "media"})
        name, mime = meta["name"], meta.get("mimeType")
    ref = ctx.put_file(r.content, name, mime)
    return [{**{k: x for k, x in item.items() if k != "binary"}, **meta, "binary": {**(item.get("binary") or {}), p.get("binary_property") or "data": ref}}]


service("google_drive", "Google Drive", ["google_oauth2"], "https://www.googleapis.com/drive/v3", icon="hard-drive", group="storage", color="#1fa463", ops=[
    op("file", "list", "Search / list files", "GET", "/files", F("q", "Query", desc="e.g. name contains 'report' and trashed = false"),
       F("fields", default="nextPageToken, files(id,name,mimeType,modifiedTime,webViewLink,parents)"), items="files",
       paginate=Cursor("pageToken", "nextPageToken", size_param="pageSize", size=100)),
    op("file", "upload", "Upload", "POST", "", F("name", "File name (empty = original)"), F("folder_id", "Parent folder id"), upload=True,
       custom=_drive_upload, extra=False),
    op("file", "download", "Download (Docs/Sheets are exported)", "GET", "", F("file_id", req=True),
       F("export_as", "Export Google files as", type="options", default="application/pdf", options=[
           "application/pdf", "text/csv", "text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]), download=True, custom=_drive_download),
    op("file", "delete", "Delete", "DELETE", "/files/{file_id}", F("file_id", req=True)),
    op("file", "copy", "Copy", "POST", "/files/{file_id}/copy", F("file_id", req=True), F("name")),
    op("folder", "create", "Create folder", "POST", "/files", F("name", req=True), F("parent_id", "Parent folder id"),
       body=lambda v: {"name": v["name"], "mimeType": "application/vnd.google-apps.folder", **({"parents": [v["parent_id"]]} if v.get("parent_id") else {})}),
    op("permission", "share", "Share", "POST", "/files/{file_id}/permissions", F("file_id", req=True),
       F("role", type="options", options=["reader", "commenter", "writer"], default="reader"), F("type", type="options", options=["user", "anyone", "domain"],
       default="user"), F("emailAddress", "Email (for type user)")),
])

# ---- Google Calendar / Gmail ------------------------------------------------------------------
CAL = F("calendar_id", default="primary", req=True)


def _event(v: dict) -> dict:
    def when(x):
        return {"date": x} if len(str(x)) == 10 else {"dateTime": x, **({"timeZone": v["time_zone"]} if v.get("time_zone") else {})}
    out = {k: v[k] for k in ("summary", "description", "location") if v.get(k)}
    if v.get("start"):
        out["start"] = when(v["start"])
    if v.get("end"):
        out["end"] = when(v["end"])
    if v.get("attendees"):
        out["attendees"] = [{"email": e} for e in v["attendees"]]
    return out


EVENT_FIELDS = [F("summary", "Title"), F("start", desc="ISO date-time, or YYYY-MM-DD for all-day"), F("end"), F("description", type="text"),
                F("location"), F("attendees", type="list"), F("time_zone", desc="e.g. Europe/Berlin")]
service("google_calendar", "Google Calendar", ["google_oauth2"], "https://www.googleapis.com/calendar/v3", icon="calendar", **GOOGLE, ops=[
    op("event", "create", "Create", "POST", "/calendars/{calendar_id}/events", CAL, *EVENT_FIELDS, body=_event),
    op("event", "get", "Get", "GET", "/calendars/{calendar_id}/events/{event_id}", CAL, F("event_id", req=True)),
    op("event", "update", "Update", "PATCH", "/calendars/{calendar_id}/events/{event_id}", CAL, F("event_id", req=True), *EVENT_FIELDS, body=_event),
    op("event", "delete", "Delete", "DELETE", "/calendars/{calendar_id}/events/{event_id}", CAL, F("event_id", req=True)),
    op("event", "list", "Get many", "GET", "/calendars/{calendar_id}/events", CAL, F("timeMin", desc="ISO, e.g. {{ $now.isoformat() }}"), F("timeMax"),
       F("q", "Search text"), F("singleEvents", type="boolean", default=True), F("orderBy", type="options", options=["", "startTime", "updated"]),
       items="items", paginate=Cursor("pageToken", "nextPageToken", size_param="maxResults", size=250)),
])


def _gmail_raw(v: dict) -> dict:
    m = EmailMessage()
    m["To"] = ", ".join(v["to"])
    m["Subject"] = v["subject"]
    for h in ("cc", "bcc"):
        if v.get(h):
            m[h.capitalize()] = ", ".join(v[h])
    if v.get("html"):
        m.set_content(v.get("text") or "")
        m.add_alternative(v["html"], subtype="html")
    else:
        m.set_content(v.get("text") or "")
    return {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode(), **({"threadId": v["thread_id"]} if v.get("thread_id") else {})}


service("gmail", "Gmail", ["google_oauth2"], "https://gmail.googleapis.com/gmail/v1/users/me", icon="mail", color="#ea4335", group="communication", ops=[
    op("message", "send", "Send", "POST", "/messages/send", F("to", type="list", req=True), F("subject", req=True), F("text", "Text body", type="text"),
       F("html", "HTML body", type="text"), F("cc", type="list"), F("bcc", type="list"), F("thread_id", "Reply in thread id"), body=_gmail_raw),
    op("message", "list", "Get many", "GET", "/messages", F("q", "Search", desc="Gmail search, e.g. is:unread from:boss"), items="messages",
       paginate=Cursor("pageToken", "nextPageToken", size_param="maxResults", size=100)),
    op("message", "get", "Get", "GET", "/messages/{id}", F("id", "Message id", req=True),
       F("format", type="options", options=["full", "metadata", "minimal"], default="full")),
    op("message", "modify", "Add / remove labels", "POST", "/messages/{id}/modify", F("id", "Message id", req=True),
       F("addLabelIds", "Add labels", type="list"), F("removeLabelIds", "Remove labels", type="list", desc="e.g. UNREAD to mark read")),
    op("message", "trash", "Move to trash", "POST", "/messages/{id}/trash", F("id", "Message id", req=True), extra=False),
    op("label", "list", "Get labels", "GET", "/labels", items="labels"),
])

# ---- Microsoft 365 (Graph) -----------------------------------------------------------------
GRAPH = "https://graph.microsoft.com/v1.0"
service("outlook", "Microsoft Outlook", ["microsoft_oauth2"], GRAPH, icon="mail", **MS, ops=[
    op("message", "send", "Send email", "POST", "/me/sendMail", F("to", type="list", req=True), F("subject", req=True), F("body", type="text", req=True),
       F("html", "Body is HTML", type="boolean", default=True), F("cc", type="list"),
       body=lambda v: {"message": {"subject": v["subject"], "body": {"contentType": "HTML" if v.get("html", True) else "Text", "content": v["body"]},
                                   "toRecipients": [{"emailAddress": {"address": e}} for e in v["to"]],
                                   **({"ccRecipients": [{"emailAddress": {"address": e}} for e in v["cc"]]} if v.get("cc") else {})},
                       "saveToSentItems": True}),
    op("message", "list", "Get many", "GET", "/me/messages", F("$filter", "Filter (OData)"), F("$search", "Search"), items="value",
       paginate=NextUrl("@odata.nextLink")),
    op("message", "get", "Get", "GET", "/me/messages/{id}", F("id", "Message id", req=True)),
    op("message", "reply", "Reply", "POST", "/me/messages/{id}/reply", F("id", "Message id", req=True), F("comment", type="text", req=True)),
    op("message", "move", "Move to folder", "POST", "/me/messages/{id}/move", F("id", "Message id", req=True), F("destinationId", "Folder id or well-known name",
       req=True, default="archive")),
    op("event", "create", "Create calendar event", "POST", "/me/events", F("subject", req=True), F("start", req=True, desc="ISO date-time"), F("end", req=True),
       F("time_zone", default="UTC"), F("body", "Description", type="text"), F("attendees", type="list"), F("location"),
       body=lambda v: {"subject": v["subject"], "start": {"dateTime": v["start"], "timeZone": v.get("time_zone") or "UTC"},
                       "end": {"dateTime": v["end"], "timeZone": v.get("time_zone") or "UTC"},
                       **({"body": {"contentType": "HTML", "content": v["body"]}} if v.get("body") else {}),
                       **({"location": {"displayName": v["location"]}} if v.get("location") else {}),
                       **({"attendees": [{"emailAddress": {"address": e}, "type": "required"} for e in v["attendees"]]} if v.get("attendees") else {})}),
    op("event", "list", "Get calendar events", "GET", "/me/events", F("$filter", "Filter (OData)"), items="value", paginate=NextUrl("@odata.nextLink")),
])
service("onedrive", "Microsoft OneDrive", ["microsoft_oauth2"], GRAPH, icon="cloud", group="storage", color="#0078d4", ops=[
    op("file", "list", "List folder", "GET", "/me/drive/root:{path}:/children", F("path", "Folder path", req=True, default="/", desc="Start with /"),
       items="value", paginate=NextUrl("@odata.nextLink")),
    op("file", "upload", "Upload (up to 250 MB)", "PUT", "/me/drive/root:{path}:/content", F("path", "Destination path", req=True, desc="/Documents/report.pdf"),
       upload=True, extra=False),
    op("file", "download", "Download", "GET", "/me/drive/root:{path}:/content", F("path", "File path", req=True), download=True),
    op("file", "delete", "Delete", "DELETE", "/me/drive/items/{item_id}", F("item_id", req=True)),
    op("folder", "create", "Create folder", "POST", "/me/drive/root:{parent}:/children", F("parent", "Parent path", default="/", req=True), F("name", req=True),
       body=lambda v: {"name": v["name"], "folder": {}, "@microsoft.graph.conflictBehavior": "rename"}),
    op("file", "share", "Create sharing link", "POST", "/me/drive/items/{item_id}/createLink", F("item_id", req=True),
       F("type", type="options", options=["view", "edit"], default="view"), F("scope", type="options", options=["anonymous", "organization"], default="organization")),
])
service("microsoft_todo", "Microsoft To Do", ["microsoft_oauth2"], GRAPH, icon="list-todo", **MS, ops=[
    op("list", "list", "Get task lists", "GET", "/me/todo/lists", items="value"),
    op("task", "create", "Create task", "POST", "/me/todo/lists/{list_id}/tasks", F("list_id", req=True), F("title", req=True),
       F("importance", type="options", options=["normal", "high", "low"], default="normal"), F("body_text", "Notes", type="text"),
       body=lambda v: {"title": v["title"], "importance": v.get("importance") or "normal",
                       **({"body": {"content": v["body_text"], "contentType": "text"}} if v.get("body_text") else {})}),
    op("task", "list", "Get tasks", "GET", "/me/todo/lists/{list_id}/tasks", F("list_id", req=True), items="value", paginate=NextUrl("@odata.nextLink")),
    op("task", "complete", "Complete task", "PATCH", "/me/todo/lists/{list_id}/tasks/{task_id}", F("list_id", req=True), F("task_id", req=True),
       body=lambda v: {"status": "completed"}),
])
service("microsoft_teams", "Microsoft Teams", ["microsoft_oauth2"], GRAPH, icon="users", group="communication", color="#6264a7",
        description="Microsoft Teams via Graph (needs ChannelMessage.Send / Chat scopes on the credential).", ops=[
    op("message", "channel", "Send channel message", "POST", "/teams/{team_id}/channels/{channel_id}/messages", F("team_id", req=True), F("channel_id", req=True),
       F("content", type="text", req=True), F("html", "Content is HTML", type="boolean"),
       body=lambda v: {"body": {"contentType": "html" if v.get("html") else "text", "content": v["content"]}}),
    op("message", "chat", "Send chat message", "POST", "/chats/{chat_id}/messages", F("chat_id", req=True), F("content", type="text", req=True),
       body=lambda v: {"body": {"content": v["content"]}}),
    op("team", "list", "Get my teams", "GET", "/me/joinedTeams", items="value"),
    op("channel", "list", "Get channels", "GET", "/teams/{team_id}/channels", F("team_id", req=True), items="value"),
])

# ---- Zoom / social --------------------------------------------------------------------------
service("zoom", "Zoom", ["zoom_oauth2"], "https://api.zoom.us/v2", color="#2d8cff", icon="video", group="communication", ops=[
    op("meeting", "create", "Create", "POST", "/users/me/meetings", F("topic", req=True), F("start_time", desc="ISO, e.g. 2026-11-02T10:00:00Z"),
       F("duration", "Duration (minutes)", type="number", default=30), F("agenda", type="text"), F("timezone"), F("password"),
       body=lambda v: {"type": 2, **{k: v[k] for k in ("topic", "start_time", "duration", "agenda", "timezone", "password") if v.get(k)}}),
    op("meeting", "get", "Get", "GET", "/meetings/{meeting_id}", F("meeting_id", req=True)),
    op("meeting", "list", "Get many", "GET", "/users/me/meetings", F("type", type="options", options=["scheduled", "live", "upcoming"], default="upcoming"),
       items="meetings", paginate=Cursor("next_page_token", "next_page_token", size_param="page_size", size=300)),
    op("meeting", "delete", "Delete", "DELETE", "/meetings/{meeting_id}", F("meeting_id", req=True)),
])
service("x", "X (Twitter)", ["x_oauth2"], "https://api.twitter.com/2", color="#000000", icon="twitter", **SOCIAL, ops=[
    op("tweet", "create", "Post", "POST", "/tweets", F("text", type="text", req=True), F("reply_to", "Reply to tweet id"),
       body=lambda v: {"text": v["text"], **({"reply": {"in_reply_to_tweet_id": v["reply_to"]}} if v.get("reply_to") else {})}, out="data"),
    op("tweet", "delete", "Delete", "DELETE", "/tweets/{id}", F("id", "Tweet id", req=True), out="data"),
    op("tweet", "search", "Search recent", "GET", "/tweets/search/recent", F("query", req=True), items="data",
       paginate=Cursor("next_token", "meta.next_token", size_param="max_results", size=100)),
    op("user", "me", "Get me", "GET", "/users/me", out="data"),
])
service("linkedin", "LinkedIn", ["linkedin_oauth2"], "https://api.linkedin.com", color="#0a66c2", icon="linkedin", **SOCIAL, ops=[
    op("user", "me", "Get me (sub = person id)", "GET", "/v2/userinfo"),
    op("post", "create", "Create text post", "POST", "/rest/posts", F("person_id", "Person id (from Get me → sub)", req=True), F("text", type="text", req=True),
       F("visibility", type="options", options=["PUBLIC", "CONNECTIONS"], default="PUBLIC"),
       headers=lambda v: {"LinkedIn-Version": "202501", "X-Restli-Protocol-Version": "2.0.0"},
       body=lambda v: {"author": f"urn:li:person:{v['person_id']}", "commentary": v["text"], "visibility": v.get("visibility") or "PUBLIC",
                       "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                       "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}),
])
service("spotify", "Spotify", ["spotify_oauth2"], "https://api.spotify.com/v1", color="#1db954", icon="music", **SOCIAL, ops=[
    op("search", "search", "Search", "GET", "/search", F("q", "Query", req=True), F("type", type="options", options=["track", "artist", "album", "playlist"],
       default="track"), F("limit", type="number", default=20)),
    op("playlist", "create", "Create playlist", "POST", "/users/{user_id}/playlists", F("user_id", req=True), F("name", req=True), F("description"),
       F("public", type="boolean")),
    op("playlist", "add", "Add tracks", "POST", "/playlists/{playlist_id}/tracks", F("playlist_id", req=True), F("uris", "Track URIs", type="list", req=True)),
    op("player", "current", "Currently playing", "GET", "/me/player/currently-playing"),
    op("user", "me", "Get me", "GET", "/me"),
])
