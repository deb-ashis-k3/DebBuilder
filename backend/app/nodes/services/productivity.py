"""Productivity, project management, docs, forms and storage integrations."""

import json

from ..declarative import Cursor, F, Link, NextUrl, Offset, Page, adf, op, service  # noqa: F401

PROD = dict(group="productivity")

# ---- Notion ----------------------------------------------------------------
service("notion", "Notion", ["notion", "notion_oauth2"], "https://api.notion.com/v1", color="#000000", icon="notebook", **PROD, ops=[
    op("page", "create", "Create in database", "POST", "/pages", F("database_id", req=True),
       F("properties", type="json", req=True, desc='Notion properties, e.g. {"Name": {"title": [{"text": {"content": "Hello"}}]}}'),
       F("content", "Text content (paragraphs)", type="text"),
       body=lambda v: {"parent": {"database_id": v["database_id"]}, "properties": v["properties"],
                       **({"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": p}}]}}
                                        for p in str(v["content"]).split("\n\n") if p.strip()]} if v.get("content") else {})}),
    op("page", "get", "Get", "GET", "/pages/{page_id}", F("page_id", req=True)),
    op("page", "update", "Update properties", "PATCH", "/pages/{page_id}", F("page_id", req=True), F("properties", type="json"),
       F("archived", type="boolean")),
    op("database", "query", "Query (get many pages)", "POST", "/databases/{database_id}/query", F("database_id", req=True),
       F("filter", type="json", desc="Notion filter object"), F("sorts", type="json"), items="results",
       paginate=Cursor("start_cursor", "next_cursor", location="body", size_param="page_size", size=100)),
    op("database", "get", "Get", "GET", "/databases/{database_id}", F("database_id", req=True)),
    op("block", "append", "Append text to page", "PATCH", "/blocks/{block_id}/children", F("block_id", "Page or block id", req=True),
       F("text", type="text", req=True),
       body=lambda v: {"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": p}}]}}
                                    for p in str(v["text"]).split("\n\n") if p.strip()]}),
    op("block", "children", "Get page content", "GET", "/blocks/{block_id}/children", F("block_id", "Page or block id", req=True), items="results",
       paginate=Cursor("start_cursor", "next_cursor", size_param="page_size")),
    op("search", "search", "Search", "POST", "/search", F("query"), F("filter_type", "Only", type="options", options=["", "page", "database"]),
       body=lambda v: {**({"query": v["query"]} if v.get("query") else {}),
                       **({"filter": {"property": "object", "value": v["filter_type"]}} if v.get("filter_type") else {})},
       items="results", paginate=Cursor("start_cursor", "next_cursor", location="body", size_param="page_size")),
    op("user", "list", "Get many", "GET", "/users", items="results", paginate=Cursor("start_cursor", "next_cursor", size_param="page_size")),
])

# ---- Airtable --------------------------------------------------------------
BASE, TABLE = F("base_id", "Base id", req=True, desc="appXXXXXXXX"), F("table", "Table (name or id)", req=True)
service("airtable", "Airtable", ["airtable"], "https://api.airtable.com/v0", color="#18bfff", icon="table", **PROD, ops=[
    op("record", "list", "Get many / search", "GET", "/{base_id}/{table}", BASE, TABLE, F("filterByFormula", "Filter formula"),
       F("view"), items="records", paginate=Cursor("offset", "offset", size_param="pageSize", size=100)),
    op("record", "get", "Get", "GET", "/{base_id}/{table}/{record_id}", BASE, TABLE, F("record_id", req=True)),
    op("record", "create", "Create", "POST", "/{base_id}/{table}", BASE, TABLE, F("fields", type="json", req=True, desc='{"Name": "Ann"}'),
       F("typecast", type="boolean")),
    op("record", "update", "Update", "PATCH", "/{base_id}/{table}/{record_id}", BASE, TABLE, F("record_id", req=True),
       F("fields", type="json", req=True), F("typecast", type="boolean")),
    op("record", "upsert", "Create or update (by field)", "PATCH", "/{base_id}/{table}", BASE, TABLE, F("fields", type="json", req=True),
       F("merge_on", "Match on fields", type="list", req=True),
       body=lambda v: {"performUpsert": {"fieldsToMergeOn": v["merge_on"]}, "records": [{"fields": v["fields"]}], "typecast": bool(v.get("typecast"))},
       items="records"),
    op("record", "delete", "Delete", "DELETE", "/{base_id}/{table}/{record_id}", BASE, TABLE, F("record_id", req=True)),
    op("base", "list", "Get many bases", "GET", "/meta/bases", items="bases", paginate=Cursor("offset", "offset")),
])

# ---- Asana -----------------------------------------------------------------
service("asana", "Asana", ["asana"], "https://app.asana.com/api/1.0", color="#f06a6a", icon="list-checks", **PROD, ops=[
    op("task", "create", "Create", "POST", "/tasks", F("name", req=True), F("workspace", "Workspace id"), F("projects", "Project ids", type="list"),
       F("notes", type="text"), F("assignee", desc="User id or email or 'me'"), F("due_on", desc="YYYY-MM-DD"), wrap="data", out="data"),
    op("task", "get", "Get", "GET", "/tasks/{task_gid}", F("task_gid", "Task id", req=True), out="data"),
    op("task", "update", "Update", "PUT", "/tasks/{task_gid}", F("task_gid", "Task id", req=True), F("name"), F("notes", type="text"),
       F("completed", type="boolean"), F("due_on"), wrap="data", out="data"),
    op("task", "delete", "Delete", "DELETE", "/tasks/{task_gid}", F("task_gid", "Task id", req=True)),
    op("task", "list", "Get many (in project)", "GET", "/projects/{project_gid}/tasks", F("project_gid", "Project id", req=True),
       F("opt_fields", default="name,completed,assignee,due_on"), items="data", paginate=Cursor("offset", "next_page.offset", size_param="limit")),
    op("task", "comment", "Add comment", "POST", "/tasks/{task_gid}/stories", F("task_gid", "Task id", req=True), F("text", type="text", req=True),
       wrap="data", out="data"),
    op("project", "list", "Get many", "GET", "/projects", F("workspace", "Workspace id", req=True), items="data",
       paginate=Cursor("offset", "next_page.offset", size_param="limit")),
])

# ---- Trello (key + token in the query; writes use query parameters too) -----
service("trello", "Trello", ["trello"], "https://api.trello.com/1", color="#0079bf", icon="kanban", **PROD, ops=[
    op("card", "create", "Create", "POST", "/cards", F("idList", "List id", req=True), F("name", req=True), F("desc", "Description", type="text"),
       F("due"), F("idLabels", "Label ids", type="list"), send="query"),
    op("card", "get", "Get", "GET", "/cards/{id}", F("id", "Card id", req=True)),
    op("card", "update", "Update", "PUT", "/cards/{id}", F("id", "Card id", req=True), F("name"), F("desc", "Description", type="text"),
       F("idList", "Move to list id"), F("closed", "Archived", type="boolean"), send="query"),
    op("card", "delete", "Delete", "DELETE", "/cards/{id}", F("id", "Card id", req=True)),
    op("card", "list", "Get many in list", "GET", "/lists/{list_id}/cards", F("list_id", req=True)),
    op("card", "comment", "Add comment", "POST", "/cards/{id}/actions/comments", F("id", "Card id", req=True), F("text", type="text", req=True), send="query"),
    op("list", "create", "Create list", "POST", "/lists", F("idBoard", "Board id", req=True), F("name", req=True), send="query"),
    op("board", "get", "Get board", "GET", "/boards/{board_id}", F("board_id", req=True)),
    op("board", "lists", "Get lists of board", "GET", "/boards/{board_id}/lists", F("board_id", req=True)),
])

# ---- ClickUp ---------------------------------------------------------------
service("clickup", "ClickUp", ["clickup"], "https://api.clickup.com/api/v2", color="#7b68ee", icon="check-square", **PROD, ops=[
    op("task", "create", "Create", "POST", "/list/{list_id}/task", F("list_id", req=True), F("name", req=True), F("description", type="text"),
       F("status"), F("priority", type="number", desc="1 urgent … 4 low"), F("due_date", type="number", desc="Unix ms"), F("assignees", type="list")),
    op("task", "get", "Get", "GET", "/task/{task_id}", F("task_id", req=True)),
    op("task", "update", "Update", "PUT", "/task/{task_id}", F("task_id", req=True), F("name"), F("description", type="text"), F("status"),
       F("priority", type="number")),
    op("task", "delete", "Delete", "DELETE", "/task/{task_id}", F("task_id", req=True)),
    op("task", "list", "Get many", "GET", "/list/{list_id}/task", F("list_id", req=True), F("archived", type="boolean"), items="tasks",
       paginate=Page(param="page", size_param=None, start=0)),
    op("comment", "create", "Create comment", "POST", "/task/{task_id}/comment", F("task_id", req=True), F("comment_text", type="text", req=True)),
    op("space", "list", "Get many spaces", "GET", "/team/{team_id}/space", F("team_id", "Workspace (team) id", req=True), items="spaces"),
])

# ---- Todoist ---------------------------------------------------------------
service("todoist", "Todoist", ["todoist"], "https://api.todoist.com/rest/v2", color="#e44332", icon="circle-check", **PROD, ops=[
    op("task", "create", "Create", "POST", "/tasks", F("content", req=True), F("description", type="text"), F("project_id"), F("due_string", desc="e.g. tomorrow at 9"),
       F("priority", type="number", desc="1 (normal) … 4 (urgent)"), F("labels", type="list")),
    op("task", "get", "Get", "GET", "/tasks/{id}", F("id", "Task id", req=True)),
    op("task", "update", "Update", "POST", "/tasks/{id}", F("id", "Task id", req=True), F("content"), F("description", type="text"), F("due_string"),
       F("priority", type="number")),
    op("task", "close", "Close (complete)", "POST", "/tasks/{id}/close", F("id", "Task id", req=True), extra=False),
    op("task", "reopen", "Reopen", "POST", "/tasks/{id}/reopen", F("id", "Task id", req=True), extra=False),
    op("task", "delete", "Delete", "DELETE", "/tasks/{id}", F("id", "Task id", req=True)),
    op("task", "list", "Get many", "GET", "/tasks", F("project_id"), F("filter", desc="Todoist filter, e.g. today")),
    op("project", "list", "Get many projects", "GET", "/projects"),
])

# ---- Jira / Confluence (Atlassian Cloud) ------------------------------------
KEY_ = F("issue_key", "Issue key", req=True, desc="e.g. PROJ-123")
service("jira", "Jira Software", ["jira"], "{domain}/rest/api/3", color="#0052cc", icon="square-kanban", **PROD, ops=[
    op("issue", "create", "Create", "POST", "/issue", F("project", "Project key", req=True), F("summary", req=True),
       F("issuetype", "Issue type", default="Task", req=True), F("description", type="text"), F("labels", type="list"), F("assignee_id", "Assignee account id"),
       F("priority", "Priority name"),
       body=lambda v: {"fields": {"project": {"key": v["project"]}, "summary": v["summary"], "issuetype": {"name": v["issuetype"]},
                                  **({"description": adf(v["description"])} if v.get("description") else {}),
                                  **({"labels": v["labels"]} if v.get("labels") else {}),
                                  **({"assignee": {"accountId": v["assignee_id"]}} if v.get("assignee_id") else {}),
                                  **({"priority": {"name": v["priority"]}} if v.get("priority") else {}),
                                  **{k: x for k, x in v.items() if k not in ("project", "summary", "issuetype", "description", "labels",
                                                                             "assignee_id", "priority")}}}),
    op("issue", "get", "Get", "GET", "/issue/{issue_key}", KEY_, F("fields", desc="Comma separated, empty = all")),
    op("issue", "update", "Update", "PUT", "/issue/{issue_key}", KEY_, F("summary"), F("description", type="text"), F("labels", type="list"),
       body=lambda v: {"fields": {**({"summary": v["summary"]} if v.get("summary") else {}),
                                  **({"description": adf(v["description"])} if v.get("description") else {}),
                                  **({"labels": v["labels"]} if v.get("labels") else {}),
                                  **{k: x for k, x in v.items() if k not in ("issue_key", "summary", "description", "labels")}}}),
    op("issue", "delete", "Delete", "DELETE", "/issue/{issue_key}", KEY_),
    op("issue", "search", "Search (JQL)", "GET", "/search/jql", F("jql", "JQL", req=True, default="project = PROJ ORDER BY created DESC"),
       F("fields", default="summary,status,assignee,created"), items="issues", paginate=Cursor("nextPageToken", "nextPageToken", size_param="maxResults")),
    op("issue", "transition", "Change status (transition)", "POST", "/issue/{issue_key}/transitions", KEY_, F("transition_id", req=True),
       body=lambda v: {"transition": {"id": str(v["transition_id"])}}),
    op("issue", "transitions", "Get available transitions", "GET", "/issue/{issue_key}/transitions", KEY_, items="transitions"),
    op("comment", "create", "Add comment", "POST", "/issue/{issue_key}/comment", KEY_, F("text", type="text", req=True),
       body=lambda v: {"body": adf(v["text"])}),
    op("comment", "list", "Get comments", "GET", "/issue/{issue_key}/comment", KEY_, items="comments"),
    op("project", "list", "Get many projects", "GET", "/project/search", items="values", paginate=Offset(param="startAt", size_param="maxResults", size=50)),
])
service("confluence", "Confluence", ["confluence"], "{domain}/wiki/rest/api", color="#1868db", icon="book-open-text", **PROD, ops=[
    op("page", "create", "Create", "POST", "/content", F("space", "Space key", req=True), F("title", req=True),
       F("body", "Content (storage format / HTML)", type="text", req=True), F("parent_id", "Parent page id"),
       body=lambda v: {"type": "page", "title": v["title"], "space": {"key": v["space"]},
                       "body": {"storage": {"value": v["body"], "representation": "storage"}},
                       **({"ancestors": [{"id": str(v["parent_id"])}]} if v.get("parent_id") else {})}),
    op("page", "get", "Get", "GET", "/content/{id}", F("id", "Page id", req=True), F("expand", default="body.storage,version")),
    op("page", "update", "Update", "PUT", "/content/{id}", F("id", "Page id", req=True), F("title", req=True), F("body", "Content (storage format)", type="text", req=True),
       F("version", "New version number (current + 1)", type="number", req=True),
       body=lambda v: {"type": "page", "title": v["title"], "version": {"number": v["version"]},
                       "body": {"storage": {"value": v["body"], "representation": "storage"}}}),
    op("page", "delete", "Delete", "DELETE", "/content/{id}", F("id", "Page id", req=True)),
    op("search", "cql", "Search (CQL)", "GET", "/content/search", F("cql", "CQL", req=True, default='type=page AND text ~ "release"'),
       items="results", paginate=Offset(param="start", size_param="limit", size=50)),
])

# ---- Linear / Monday (GraphQL) ----------------------------------------------
service("linear", "Linear", ["linear"], "https://api.linear.app/graphql", color="#5e6ad2", icon="square-chart-gantt", **PROD, ops=[
    op("issue", "create", "Create", "POST", "", F("teamId", "Team id", req=True), F("title", req=True), F("description", type="text"),
       F("priority", type="number", desc="0 none, 1 urgent … 4 low"), F("assigneeId"),
       graphql="mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier title url } } }",
       body=lambda v: {"input": v}, out="data.issueCreate.issue"),
    op("issue", "get", "Get", "POST", "", F("id", "Issue id or key", req=True),
       graphql="query($id: String!) { issue(id: $id) { id identifier title description url priority state { name } assignee { name } } }",
       out="data.issue"),
    op("issue", "update", "Update", "POST", "", F("id", "Issue id", req=True), F("title"), F("description", type="text"), F("stateId"),
       F("priority", type="number"),
       graphql="mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { id identifier title url } } }",
       body=lambda v: {"id": v["id"], "input": {k: x for k, x in v.items() if k != "id"}}, out="data.issueUpdate.issue"),
    op("issue", "list", "Get many", "POST", "", F("first", "How many", type="number", default=50),
       graphql="query($first: Int) { issues(first: $first, orderBy: updatedAt) { nodes { id identifier title url priority state { name } } } }",
       items="data.issues.nodes"),
    op("comment", "create", "Add comment", "POST", "", F("issueId", "Issue id", req=True), F("body", type="text", req=True),
       graphql="mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success comment { id body url } } }",
       body=lambda v: {"input": v}, out="data.commentCreate.comment"),
])
service("monday", "monday.com", ["monday"], "https://api.monday.com/v2", color="#ff3d57", icon="layout-grid", **PROD, ops=[
    op("item", "create", "Create", "POST", "", F("board_id", req=True), F("item_name", req=True), F("group_id"),
       F("column_values", type="json", desc='{"status": {"label": "Done"}}'),
       graphql="mutation($board_id: ID!, $item_name: String!, $group_id: String, $column_values: JSON) "
               "{ create_item(board_id: $board_id, item_name: $item_name, group_id: $group_id, column_values: $column_values) { id name } }",
       body=lambda v: {**v, "column_values": json.dumps(v["column_values"]) if v.get("column_values") else None}, out="data.create_item"),
    op("item", "list", "Get many in board", "POST", "", F("board_id", req=True), F("limit", type="number", default=50),
       graphql="query($board_id: [ID!], $limit: Int) { boards(ids: $board_id) { items_page(limit: $limit) "
               "{ items { id name group { title } column_values { id text } } } } }",
       items="data.boards.0.items_page.items"),
    op("item", "update_column", "Update column values", "POST", "", F("board_id", req=True), F("item_id", req=True), F("column_values", type="json", req=True),
       graphql="mutation($board_id: ID!, $item_id: ID!, $column_values: JSON!) "
               "{ change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $column_values) { id name } }",
       body=lambda v: {**v, "column_values": json.dumps(v["column_values"])}, out="data.change_multiple_column_values"),
    op("update", "create", "Add update (comment)", "POST", "", F("item_id", req=True), F("body", type="text", req=True),
       graphql="mutation($item_id: ID!, $body: String!) { create_update(item_id: $item_id, body: $body) { id } }", out="data.create_update"),
])

# ---- Scheduling, forms, design ----------------------------------------------
service("calendly", "Calendly", ["calendly"], "https://api.calendly.com", color="#006bff", icon="calendar-days", **PROD, ops=[
    op("user", "me", "Get me", "GET", "/users/me", out="resource"),
    op("event", "list", "Get scheduled events", "GET", "/scheduled_events", F("user", "User URI", req=True, desc="From 'Get me' → uri"),
       F("status", type="options", options=["", "active", "canceled"]), F("min_start_time"), items="collection",
       paginate=Cursor("page_token", "pagination.next_page_token", size_param="count")),
    op("event", "cancel", "Cancel event", "POST", "/scheduled_events/{uuid}/cancellation", F("uuid", "Event UUID", req=True), F("reason")),
    op("event_type", "list", "Get event types", "GET", "/event_types", F("user", "User URI", req=True), items="collection",
       paginate=Cursor("page_token", "pagination.next_page_token", size_param="count")),
])
service("typeform", "Typeform", ["typeform"], "https://api.typeform.com", color="#262627", icon="clipboard-list", **PROD, ops=[
    op("form", "list", "Get many forms", "GET", "/forms", items="items", paginate=Page(size_param="page_size", size=200)),
    op("response", "list", "Get responses", "GET", "/forms/{form_id}/responses", F("form_id", req=True), F("since"), items="items",
       paginate=Cursor("before", "items.-1.token", size_param="page_size")),
])
service("jotform", "JotForm", ["jotform"], "https://api.jotform.com", color="#ff6100", icon="clipboard-list", **PROD, ops=[
    op("form", "list", "Get many forms", "GET", "/user/forms", items="content", paginate=Offset(size_param="limit")),
    op("submission", "list", "Get submissions", "GET", "/form/{form_id}/submissions", F("form_id", req=True), items="content",
       paginate=Offset(size_param="limit")),
])
service("figma", "Figma", ["figma"], "https://api.figma.com/v1", color="#f24e1e", icon="figma", **PROD, ops=[
    op("file", "get", "Get file", "GET", "/files/{file_key}", F("file_key", req=True), F("depth", type="number", default=1)),
    op("comment", "list", "Get comments", "GET", "/files/{file_key}/comments", F("file_key", req=True), items="comments"),
    op("comment", "create", "Post comment", "POST", "/files/{file_key}/comments", F("file_key", req=True), F("message", type="text", req=True)),
    op("image", "export", "Export images", "GET", "/images/{file_key}", F("file_key", req=True), F("ids", "Node ids", type="list", req=True),
       F("format", type="options", options=["png", "jpg", "svg", "pdf"], default="png")),
])

# ---- CMS -------------------------------------------------------------------
service("webflow", "Webflow", ["webflow"], "https://api.webflow.com/v2", color="#146ef5", icon="layout-template", **PROD, ops=[
    op("site", "list", "Get many sites", "GET", "/sites", items="sites"),
    op("collection", "list", "Get collections", "GET", "/sites/{site_id}/collections", F("site_id", req=True), items="collections"),
    op("item", "list", "Get many items", "GET", "/collections/{collection_id}/items", F("collection_id", req=True), items="items",
       paginate=Offset(size_param="limit", size=100)),
    op("item", "create", "Create item", "POST", "/collections/{collection_id}/items", F("collection_id", req=True),
       F("fieldData", "Field data", type="json", req=True, desc='{"name": "Hello", "slug": "hello"}'), F("isDraft", type="boolean")),
    op("item", "update", "Update item", "PATCH", "/collections/{collection_id}/items/{item_id}", F("collection_id", req=True), F("item_id", req=True),
       F("fieldData", "Field data", type="json", req=True)),
    op("item", "delete", "Delete item", "DELETE", "/collections/{collection_id}/items/{item_id}", F("collection_id", req=True), F("item_id", req=True)),
    op("item", "publish", "Publish items", "POST", "/collections/{collection_id}/items/publish", F("collection_id", req=True),
       F("itemIds", "Item ids", type="list", req=True)),
])
POST_FIELDS = [F("title"), F("content", type="text"), F("status", type="options", options=["", "publish", "draft", "pending", "private"]),
               F("excerpt", type="text"), F("categories", "Category ids", type="list"), F("tags", "Tag ids", type="list")]
service("wordpress", "WordPress", ["wordpress"], "{url}/wp-json/wp/v2", color="#21759b", icon="newspaper", **PROD, ops=[
    op("post", "create", "Create", "POST", "/posts", *POST_FIELDS),
    op("post", "get", "Get", "GET", "/posts/{id}", F("id", "Post id", req=True)),
    op("post", "update", "Update", "POST", "/posts/{id}", F("id", "Post id", req=True), *POST_FIELDS),
    op("post", "delete", "Delete", "DELETE", "/posts/{id}", F("id", "Post id", req=True), F("force", "Skip trash", type="boolean")),
    op("post", "list", "Get many", "GET", "/posts", F("search"), F("status", type="options", options=["", "publish", "draft"]),
       paginate=Page(size_param="per_page", size=100)),
    op("page", "create", "Create page", "POST", "/pages", F("title"), F("content", type="text"), F("status", type="options", options=["", "publish", "draft"])),
    op("user", "list", "Get many users", "GET", "/users", paginate=Page(size_param="per_page", size=100)),
])

# ---- Dropbox (files) ---------------------------------------------------------
_dbx_arg = lambda v: {"Dropbox-API-Arg": json.dumps(v, ensure_ascii=True)}  # noqa: E731
service("dropbox", "Dropbox", ["dropbox", "dropbox_oauth2"], "https://api.dropboxapi.com/2", color="#0061ff", icon="box", group="storage", ops=[
    op("file", "upload", "Upload", "POST", "/files/upload", F("path", "Destination path", req=True, desc="/folder/file.pdf", where="none"),
       F("overwrite", type="boolean", where="none"),
       headers=lambda v: _dbx_arg({"path": v["path"], "mode": "overwrite" if v.get("overwrite") else "add", "autorename": not v.get("overwrite")}),
       upload=True, base="https://content.dropboxapi.com/2", extra=False),
    op("file", "download", "Download", "POST", "/files/download", F("path", req=True, where="none"),
       headers=lambda v: _dbx_arg({"path": v["path"]}), download=True, base="https://content.dropboxapi.com/2", extra=False),
    op("folder", "list", "List folder", "POST", "/files/list_folder", F("path", default="", desc="'' = root"), F("recursive", type="boolean"),
       items="entries", paginate=None),
    op("folder", "create", "Create folder", "POST", "/files/create_folder_v2", F("path", req=True), out="metadata"),
    op("file", "delete", "Delete", "POST", "/files/delete_v2", F("path", req=True), out="metadata"),
    op("file", "move", "Move / rename", "POST", "/files/move_v2", F("from_path", req=True), F("to_path", req=True), out="metadata"),
    op("file", "search", "Search", "POST", "/files/search_v2", F("query", req=True), items="matches"),
    op("file", "link", "Get temporary link", "POST", "/files/get_temporary_link", F("path", req=True)),
])
