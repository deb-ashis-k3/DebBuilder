"""Developer & DevOps integrations."""

import time

from ..declarative import AfterId, Cursor, F, Link, NextUrl, Offset, Page, op, service  # noqa: F401

DEV = dict(group="devops")

# ---- GitHub ----------------------------------------------------------------
OWNER, REPO = F("owner", req=True), F("repo", "Repository", req=True)
ISSUE_NO = F("issue_number", type="number", req=True)
service("github", "GitHub", ["github", "github_oauth2"],
        lambda c: (c.get("server") or "https://api.github.com"), color="#24292f", icon="github", **DEV, ops=[
    op("issue", "create", "Create", "POST", "/repos/{owner}/{repo}/issues", OWNER, REPO, F("title", req=True), F("body", type="text"),
       F("labels", type="list"), F("assignees", type="list")),
    op("issue", "get", "Get", "GET", "/repos/{owner}/{repo}/issues/{issue_number}", OWNER, REPO, ISSUE_NO),
    op("issue", "update", "Update", "PATCH", "/repos/{owner}/{repo}/issues/{issue_number}", OWNER, REPO, ISSUE_NO, F("title"),
       F("body", type="text"), F("state", type="options", options=["", "open", "closed"]), F("labels", type="list")),
    op("issue", "list", "Get many", "GET", "/repos/{owner}/{repo}/issues", OWNER, REPO,
       F("state", type="options", options=["open", "closed", "all"], default="open"), F("labels", type="list"), paginate=Link()),
    op("issue", "comment", "Create comment", "POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments", OWNER, REPO, ISSUE_NO,
       F("body", type="text", req=True)),
    op("pull_request", "list", "Get many", "GET", "/repos/{owner}/{repo}/pulls", OWNER, REPO,
       F("state", type="options", options=["open", "closed", "all"], default="open"), paginate=Link()),
    op("pull_request", "get", "Get", "GET", "/repos/{owner}/{repo}/pulls/{pull_number}", OWNER, REPO, F("pull_number", type="number", req=True)),
    op("pull_request", "create", "Create", "POST", "/repos/{owner}/{repo}/pulls", OWNER, REPO, F("title", req=True),
       F("head", req=True, desc="Branch with the changes"), F("base", req=True, default="main"), F("body", type="text"),
       F("draft", type="boolean")),
    op("repository", "get", "Get", "GET", "/repos/{owner}/{repo}", OWNER, REPO),
    op("repository", "list", "Get many (mine)", "GET", "/user/repos", F("visibility", type="options", options=["all", "public", "private"], default="all"),
       paginate=Link()),
    op("file", "get", "Get", "GET", "/repos/{owner}/{repo}/contents/{path}", OWNER, REPO, F("path", req=True), F("ref", desc="Branch / tag / SHA")),
    op("file", "upsert", "Create or update", "PUT", "/repos/{owner}/{repo}/contents/{path}", OWNER, REPO, F("path", req=True),
       F("content", type="text", req=True, transform="base64"), F("message", "Commit message", req=True),
       F("sha", desc="Required when updating: the current file's SHA"), F("branch")),
    op("release", "create", "Create", "POST", "/repos/{owner}/{repo}/releases", OWNER, REPO, F("tag_name", req=True), F("name"),
       F("body", type="text"), F("draft", type="boolean"), F("prerelease", type="boolean")),
    op("release", "list", "Get many", "GET", "/repos/{owner}/{repo}/releases", OWNER, REPO, paginate=Link()),
    op("workflow", "dispatch", "Trigger a workflow run", "POST", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches", OWNER, REPO,
       F("workflow_id", desc="Workflow file name or id", req=True), F("ref", default="main", req=True), F("inputs", type="json")),
    op("user", "me", "Get me", "GET", "/user"),
])

# ---- GitLab ----------------------------------------------------------------
PROJ = F("project_id", "Project (id or path)", req=True, desc="e.g. 123 or group/project")
service("gitlab", "GitLab", ["gitlab"], "{server}/api/v4", color="#fc6d26", icon="gitlab", **DEV, ops=[
    op("issue", "create", "Create", "POST", "/projects/{project_id}/issues", PROJ, F("title", req=True), F("description", type="text"),
       F("labels"), F("assignee_ids", type="list")),
    op("issue", "get", "Get", "GET", "/projects/{project_id}/issues/{issue_iid}", PROJ, F("issue_iid", type="number", req=True)),
    op("issue", "update", "Update", "PUT", "/projects/{project_id}/issues/{issue_iid}", PROJ, F("issue_iid", type="number", req=True),
       F("title"), F("description", type="text"), F("state_event", type="options", options=["", "close", "reopen"])),
    op("issue", "list", "Get many", "GET", "/projects/{project_id}/issues", PROJ,
       F("state", type="options", options=["opened", "closed", "all"], default="opened"), paginate=Link()),
    op("issue", "comment", "Create comment", "POST", "/projects/{project_id}/issues/{issue_iid}/notes", PROJ,
       F("issue_iid", type="number", req=True), F("body", type="text", req=True)),
    op("merge_request", "list", "Get many", "GET", "/projects/{project_id}/merge_requests", PROJ,
       F("state", type="options", options=["opened", "closed", "merged", "all"], default="opened"), paginate=Link()),
    op("merge_request", "create", "Create", "POST", "/projects/{project_id}/merge_requests", PROJ, F("source_branch", req=True),
       F("target_branch", req=True, default="main"), F("title", req=True), F("description", type="text")),
    op("project", "get", "Get", "GET", "/projects/{project_id}", PROJ),
    op("project", "list", "Get many (member of)", "GET", "/projects", F("membership", type="boolean", default=True), paginate=Link()),
    op("file", "get", "Get", "GET", "/projects/{project_id}/repository/files/{file_path}", PROJ, F("file_path", req=True), F("ref", default="main", req=True)),
    op("release", "create", "Create", "POST", "/projects/{project_id}/releases", PROJ, F("tag_name", req=True), F("name"), F("description", type="text"),
       F("ref", desc="Branch/SHA to tag if the tag doesn't exist")),
    op("pipeline", "create", "Trigger pipeline", "POST", "/projects/{project_id}/pipeline", PROJ, F("ref", req=True, default="main")),
])

# ---- Bitbucket -------------------------------------------------------------
WS, SLUG = F("workspace", req=True), F("repo_slug", "Repository", req=True)
service("bitbucket", "Bitbucket", ["bitbucket"], "https://api.bitbucket.org/2.0", color="#0052cc", icon="git-branch", **DEV, ops=[
    op("repository", "list", "Get many", "GET", "/repositories/{workspace}", WS, items="values", paginate=NextUrl("next")),
    op("repository", "get", "Get", "GET", "/repositories/{workspace}/{repo_slug}", WS, SLUG),
    op("pull_request", "list", "Get many", "GET", "/repositories/{workspace}/{repo_slug}/pullrequests", WS, SLUG,
       F("state", type="options", options=["OPEN", "MERGED", "DECLINED", "SUPERSEDED"], default="OPEN"), items="values", paginate=NextUrl("next")),
    op("pull_request", "create", "Create", "POST", "/repositories/{workspace}/{repo_slug}/pullrequests", WS, SLUG, F("title", req=True),
       F("source", "Source branch", req=True, to="source.branch.name"), F("destination", "Destination branch", default="main", to="destination.branch.name"),
       F("description", type="text")),
    op("pipeline", "run", "Run pipeline", "POST", "/repositories/{workspace}/{repo_slug}/pipelines/", WS, SLUG,
       F("branch", req=True, default="main"), body=lambda v: {"target": {"type": "pipeline_ref_target", "ref_type": "branch", "ref_name": v["branch"]}}),
])

# ---- Vercel / Netlify / Cloudflare / DigitalOcean ---------------------------
service("vercel", "Vercel", ["vercel"], "https://api.vercel.com", color="#000000", icon="triangle", **DEV, ops=[
    op("deployment", "list", "Get many", "GET", "/v6/deployments", F("projectId", "Project id"), F("target", type="options", options=["", "production", "preview"]),
       items="deployments", paginate=Cursor("until", "pagination.next", size_param="limit", size=100)),
    op("deployment", "get", "Get", "GET", "/v13/deployments/{id}", F("id", "Deployment id or URL", req=True)),
    op("project", "list", "Get many", "GET", "/v9/projects", items="projects", paginate=Cursor("until", "pagination.next", size_param="limit")),
    op("env", "create", "Create environment variable", "POST", "/v10/projects/{project}/env", F("project", "Project id or name", req=True),
       F("key", req=True), F("value", req=True), F("target", type="list", default="production,preview,development"),
       F("type", type="options", options=["encrypted", "plain", "sensitive"], default="encrypted")),
])
service("netlify", "Netlify", ["netlify"], "https://api.netlify.com/api/v1", color="#00c7b7", icon="globe", **DEV, ops=[
    op("site", "list", "Get many", "GET", "/sites", paginate=Page(size_param="per_page")),
    op("site", "get", "Get", "GET", "/sites/{site_id}", F("site_id", req=True)),
    op("deploy", "list", "Get many", "GET", "/sites/{site_id}/deploys", F("site_id", req=True), paginate=Page(size_param="per_page")),
    op("build", "trigger", "Trigger a build", "POST", "/sites/{site_id}/builds", F("site_id", req=True), F("clear_cache", type="boolean")),
])
ZONE = F("zone_id", req=True)
service("cloudflare", "Cloudflare", ["cloudflare"], "https://api.cloudflare.com/client/v4", color="#f38020", icon="cloud", ok_field="success",
        error_path="errors.0.message", **DEV, ops=[
    op("zone", "list", "Get many", "GET", "/zones", F("name", desc="Filter by domain"), items="result", paginate=Page(size_param="per_page", size=50)),
    op("dns", "list", "Get DNS records", "GET", "/zones/{zone_id}/dns_records", ZONE, F("type", desc="A, CNAME, TXT…"), F("name"),
       items="result", paginate=Page(size_param="per_page")),
    op("dns", "create", "Create DNS record", "POST", "/zones/{zone_id}/dns_records", ZONE,
       F("type", type="options", options=["A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA"], default="A", req=True),
       F("name", req=True), F("content", req=True), F("ttl", type="number", default=1), F("proxied", type="boolean"), out="result"),
    op("dns", "update", "Update DNS record", "PATCH", "/zones/{zone_id}/dns_records/{record_id}", ZONE, F("record_id", req=True),
       F("content"), F("ttl", type="number"), F("proxied", type="boolean"), out="result"),
    op("dns", "delete", "Delete DNS record", "DELETE", "/zones/{zone_id}/dns_records/{record_id}", ZONE, F("record_id", req=True), out="result"),
    op("cache", "purge", "Purge cache", "POST", "/zones/{zone_id}/purge_cache", ZONE, F("files", "URLs (empty = everything)", type="list"),
       body=lambda v: {"files": v["files"]} if v.get("files") else {"purge_everything": True}, out="result"),
])
service("digitalocean", "DigitalOcean", ["digitalocean"], "https://api.digitalocean.com/v2", color="#0080ff", icon="server", **DEV, ops=[
    op("droplet", "list", "Get many", "GET", "/droplets", F("tag_name"), items="droplets", paginate=Page(size_param="per_page")),
    op("droplet", "get", "Get", "GET", "/droplets/{droplet_id}", F("droplet_id", type="number", req=True), out="droplet"),
    op("droplet", "create", "Create", "POST", "/droplets", F("name", req=True), F("region", req=True, default="nyc3"),
       F("size", req=True, default="s-1vcpu-1gb"), F("image", req=True, default="ubuntu-24-04-x64"), F("ssh_keys", type="list"), F("tags", type="list"),
       out="droplet"),
    op("droplet", "delete", "Delete", "DELETE", "/droplets/{droplet_id}", F("droplet_id", type="number", req=True)),
    op("droplet", "action", "Run action", "POST", "/droplets/{droplet_id}/actions", F("droplet_id", type="number", req=True),
       F("type", type="options", options=["reboot", "power_cycle", "shutdown", "power_off", "power_on", "snapshot"], req=True, default="reboot"),
       out="action"),
    op("domain_record", "list", "Get domain records", "GET", "/domains/{domain}/records", F("domain", req=True), items="domain_records",
       paginate=Page(size_param="per_page")),
    op("domain_record", "create", "Create domain record", "POST", "/domains/{domain}/records", F("domain", req=True),
       F("type", req=True, default="A"), F("name", req=True), F("data", req=True), F("ttl", type="number", default=1800), out="domain_record"),
])

# ---- Sentry / PagerDuty / Datadog / Jenkins ---------------------------------
ORG = F("organization_slug", "Organization", req=True)
service("sentry", "Sentry", ["sentry"], "https://sentry.io/api/0", color="#362d59", icon="bug", **DEV, ops=[
    op("issue", "list", "Get many", "GET", "/projects/{organization_slug}/{project_slug}/issues/", ORG, F("project_slug", "Project", req=True),
       F("query", default="is:unresolved"), paginate=Link()),
    op("issue", "get", "Get", "GET", "/organizations/{organization_slug}/issues/{issue_id}/", ORG, F("issue_id", req=True)),
    op("issue", "update", "Update", "PUT", "/organizations/{organization_slug}/issues/{issue_id}/", ORG, F("issue_id", req=True),
       F("status", type="options", options=["resolved", "unresolved", "ignored"], default="resolved"), F("assignedTo")),
    op("project", "list", "Get many", "GET", "/organizations/{organization_slug}/projects/", ORG, paginate=Link()),
    op("event", "list", "Get issue events", "GET", "/organizations/{organization_slug}/issues/{issue_id}/events/", ORG, F("issue_id", req=True),
       paginate=Link()),
])
service("pagerduty", "PagerDuty", ["pagerduty"], "https://api.pagerduty.com", color="#06ac38", icon="siren", **DEV, ops=[
    op("incident", "create", "Create", "POST", "/incidents", F("from_email", "From (user email)", req=True, where="header", to="From"),
       F("title", req=True), F("service_id", req=True), F("urgency", type="options", options=["high", "low"], default="high"), F("details", type="text"),
       body=lambda v: {"incident": {"type": "incident", "title": v["title"], "urgency": v.get("urgency") or "high",
                                    "service": {"id": v["service_id"], "type": "service_reference"},
                                    **({"body": {"type": "incident_body", "details": v["details"]}} if v.get("details") else {})}},
       out="incident"),
    op("incident", "get", "Get", "GET", "/incidents/{id}", F("id", req=True), out="incident"),
    op("incident", "list", "Get many", "GET", "/incidents", F("statuses[]", "Statuses", type="list", sep=None), items="incidents",
       paginate=Offset(size_param="limit", size=100)),
    op("incident", "update", "Update status", "PUT", "/incidents/{id}", F("id", req=True), F("from_email", "From (user email)", req=True, where="header", to="From"),
       F("status", type="options", options=["acknowledged", "resolved"], default="acknowledged"),
       body=lambda v: {"incident": {"type": "incident_reference", "status": v["status"]}}, out="incident"),
    op("note", "create", "Add note", "POST", "/incidents/{id}/notes", F("id", req=True), F("from_email", "From (user email)", req=True, where="header", to="From"),
       F("content", type="text", req=True), body=lambda v: {"note": {"content": v["content"]}}, out="note"),
    op("service", "list", "Get many services", "GET", "/services", items="services", paginate=Offset(size_param="limit")),
])
service("datadog", "Datadog", ["datadog"], "https://api.{site}", color="#632ca6", icon="activity", **DEV,
        description="Datadog: post events and submit metrics (read APIs also need an application key: use Custom API call).", ops=[
    op("event", "create", "Post an event", "POST", "/api/v1/events", F("title", req=True), F("text", type="text", req=True),
       F("alert_type", type="options", options=["info", "warning", "error", "success"], default="info"), F("tags", type="list")),
    op("metric", "submit", "Submit a metric point", "POST", "/api/v2/series", F("metric", req=True), F("value", type="number", req=True),
       F("tags", type="list"), body=lambda v: {"series": [{"metric": v["metric"], "type": 0,
                                                        "points": [{"timestamp": int(time.time()), "value": v["value"]}],
                                                        "tags": v.get("tags") or []}]}),
])
service("jenkins", "Jenkins", ["jenkins"], "{url}", color="#d24939", icon="hammer", **DEV, ops=[
    op("job", "build", "Trigger build", "POST", "/job/{job}/build", F("job", req=True, desc="Job name (folder/job for folders: use folder/job/name)")),
    op("job", "build_params", "Trigger build with parameters", "POST", "/job/{job}/buildWithParameters", F("job", req=True),
       send="query", extra=True),  # build parameters go in "Additional fields (JSON)" → query string
    op("job", "get", "Get", "GET", "/job/{job}/api/json", F("job", req=True)),
    op("build", "last", "Get last build", "GET", "/job/{job}/lastBuild/api/json", F("job", req=True)),
])
