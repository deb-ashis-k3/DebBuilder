"""Messaging, SMS and email/marketing integrations."""

import hashlib

from ..declarative import Cursor, F, NextUrl, Offset, Page, op, service  # noqa: F401

COMMS = dict(group="communication")
MKT = dict(group="marketing")

# ---- Slack (Web API: 200 + {"ok": false} on errors) ------------------------------
CH = F("channel", req=True, desc="Channel id (C…), or #name for posting")
service("slack_api", "Slack", ["slack", "slack_oauth2"], "https://slack.com/api", color="#4a154b", icon="slack", ok_field="ok", error_path="error", **COMMS,
        ops=[
    op("message", "post", "Send message", "POST", "/chat.postMessage", CH, F("text", type="text", req=True), F("thread_ts", "Reply in thread (ts)"),
       F("blocks", type="json", desc="Block Kit blocks"), F("unfurl_links", type="boolean"), out="message"),
    op("message", "update", "Update message", "POST", "/chat.update", CH, F("ts", "Message ts", req=True), F("text", type="text", req=True)),
    op("message", "delete", "Delete message", "POST", "/chat.delete", CH, F("ts", "Message ts", req=True)),
    op("message", "permalink", "Get permalink", "GET", "/chat.getPermalink", CH, F("message_ts", req=True)),
    op("channel", "list", "Get many channels", "GET", "/conversations.list", F("types", default="public_channel,private_channel"),
       F("exclude_archived", type="boolean", default=True), items="channels", paginate=Cursor("cursor", "response_metadata.next_cursor", size_param="limit", size=200)),
    op("channel", "history", "Get messages", "GET", "/conversations.history", CH, F("oldest"), items="messages",
       paginate=Cursor("cursor", "response_metadata.next_cursor", size_param="limit", size=200)),
    op("channel", "replies", "Get thread replies", "GET", "/conversations.replies", CH, F("ts", "Thread ts", req=True), items="messages",
       paginate=Cursor("cursor", "response_metadata.next_cursor", size_param="limit")),
    op("channel", "create", "Create channel", "POST", "/conversations.create", F("name", req=True), F("is_private", type="boolean"), out="channel"),
    op("channel", "invite", "Invite users", "POST", "/conversations.invite", CH, F("users", "User ids", req=True), out="channel"),
    op("channel", "topic", "Set topic", "POST", "/conversations.setTopic", CH, F("topic", req=True), out="channel"),
    op("reaction", "add", "Add reaction", "POST", "/reactions.add", CH, F("timestamp", "Message ts", req=True), F("name", "Emoji name", req=True, default="thumbsup")),
    op("user", "list", "Get many users", "GET", "/users.list", items="members", paginate=Cursor("cursor", "response_metadata.next_cursor", size_param="limit", size=200)),
    op("user", "info", "Get user", "GET", "/users.info", F("user", "User id", req=True), out="user"),
    op("user", "by_email", "Find user by email", "GET", "/users.lookupByEmail", F("email", req=True), out="user"),
])

# ---- Discord / Teams / Mattermost / Matrix / LINE -----------------------------------
service("discord", "Discord", ["discord_bot"], "https://discord.com/api/v10", color="#5865f2", icon="message-square", **COMMS, ops=[
    op("message", "send", "Send message", "POST", "/channels/{channel_id}/messages", F("channel_id", req=True), F("content", type="text", req=True),
       F("embeds", type="json"), F("tts", type="boolean")),
    op("message", "list", "Get messages", "GET", "/channels/{channel_id}/messages", F("channel_id", req=True), F("limit", type="number", default=50)),
    op("message", "delete", "Delete message", "DELETE", "/channels/{channel_id}/messages/{message_id}", F("channel_id", req=True), F("message_id", req=True)),
    op("message", "react", "Add reaction", "PUT", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me", F("channel_id", req=True),
       F("message_id", req=True), F("emoji", req=True, default="👍"), extra=False),
    op("channel", "get", "Get channel", "GET", "/channels/{channel_id}", F("channel_id", req=True)),
    op("member", "list", "Get guild members", "GET", "/guilds/{guild_id}/members", F("guild_id", req=True), F("limit", type="number", default=100)),
])
service("discord_webhook", "Discord Webhook", ["discord_webhook"], "{webhook_url}", color="#5865f2", icon="webhook", custom_call=False, **COMMS, ops=[
    op("message", "send", "Send message", "POST", "", F("content", type="text", req=True), F("username"), F("avatar_url"), F("embeds", type="json")),
])
service("teams_webhook", "Microsoft Teams Webhook", ["teams_webhook"], "{webhook_url}", color="#6264a7", icon="webhook", custom_call=False, **COMMS, ops=[
    op("message", "send", "Send message", "POST", "", F("text", type="text", req=True), F("title"),
       body=lambda v: {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
           "type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
           "body": ([{"type": "TextBlock", "text": v["title"], "weight": "Bolder", "size": "Medium", "wrap": True}] if v.get("title") else [])
           + [{"type": "TextBlock", "text": v["text"], "wrap": True}]}}]}),
])
service("mattermost", "Mattermost", ["mattermost"], "{url}/api/v4", color="#0058cc", icon="message-square", **COMMS, ops=[
    op("message", "post", "Send message", "POST", "/posts", F("channel_id", req=True), F("message", type="text", req=True), F("root_id", "Reply to post id")),
    op("message", "delete", "Delete message", "DELETE", "/posts/{post_id}", F("post_id", req=True)),
    op("channel", "list", "Get team channels", "GET", "/teams/{team_id}/channels", F("team_id", req=True), paginate=Page(size_param="per_page", size=100, start=0)),
    op("user", "me", "Get me", "GET", "/users/me"),
])
service("matrix", "Matrix", ["matrix"], "{homeserver}/_matrix/client/v3", color="#000000", icon="message-square", **COMMS, ops=[
    op("message", "send", "Send message", "PUT", "/rooms/{room_id}/send/m.room.message/{txn_id}", F("room_id", req=True),
       F("txn_id", "Transaction id", default="{{ uuid() }}", desc="Unique per message"), F("body", "Text", type="text", req=True),
       F("msgtype", type="options", options=["m.text", "m.notice"], default="m.text")),
    op("room", "joined", "Get joined rooms", "GET", "/joined_rooms"),
])
service("line", "LINE", ["line"], "https://api.line.me/v2/bot", color="#06c755", icon="message-circle", **COMMS, ops=[
    op("message", "push", "Push message", "POST", "/message/push", F("to", "User / group id", req=True), F("text", type="text", req=True),
       body=lambda v: {"to": v["to"], "messages": [{"type": "text", "text": v["text"]}]}),
    op("message", "broadcast", "Broadcast to all followers", "POST", "/message/broadcast", F("text", type="text", req=True),
       body=lambda v: {"messages": [{"type": "text", "text": v["text"]}]}),
    op("profile", "get", "Get user profile", "GET", "/profile/{user_id}", F("user_id", req=True)),
])

# ---- SMS / voice -------------------------------------------------------------------
service("twilio", "Twilio", ["twilio"], "https://api.twilio.com/2010-04-01/Accounts/{account_sid}", color="#f22f46", icon="phone", **COMMS, ops=[
    op("sms", "send", "Send SMS / MMS", "POST", "/Messages.json", F("From", "From number", req=True), F("To", "To number", req=True),
       F("Body", "Message", type="text", req=True), F("MediaUrl", "Media URL"), send="form"),
    op("whatsapp", "send", "Send WhatsApp message", "POST", "/Messages.json", F("From", "From number", req=True), F("To", "To number", req=True),
       F("Body", "Message", type="text", req=True), send="form",
       body=lambda v: {"From": v["From"] if str(v["From"]).startswith("whatsapp:") else f"whatsapp:{v['From']}",
                       "To": v["To"] if str(v["To"]).startswith("whatsapp:") else f"whatsapp:{v['To']}", "Body": v["Body"]}),
    op("call", "create", "Make a call", "POST", "/Calls.json", F("From", "From number", req=True), F("To", "To number", req=True),
       F("Twiml", "TwiML", type="text", default="<Response><Say>Hello from DebBuilder</Say></Response>"), F("Url", "Or TwiML URL"), send="form"),
    op("message", "list", "Get messages", "GET", "/Messages.json", F("To"), F("From"), items="messages", paginate=NextUrl("next_page_uri")),
])
service("vonage", "Vonage", ["vonage"], "https://rest.nexmo.com", color="#000000", icon="phone", **COMMS, ops=[
    op("sms", "send", "Send SMS", "POST", "/sms/json", F("from", "From", req=True), F("to", "To", req=True), F("text", type="text", req=True), send="form",
       items="messages"),
])

# ---- Email / marketing ----------------------------------------------------------------
service("sendgrid", "SendGrid", ["sendgrid"], "https://api.sendgrid.com/v3", color="#1a82e2", icon="mail", **MKT, ops=[
    op("mail", "send", "Send email", "POST", "/mail/send", F("from_email", "From", req=True), F("to", "To", type="list", req=True), F("subject", req=True),
       F("text", type="text"), F("html", "HTML", type="text"), F("template_id"), F("dynamic_template_data", type="json"),
       body=lambda v: {"from": {"email": v["from_email"]},
                       "personalizations": [{"to": [{"email": e} for e in v["to"]],
                                             **({"dynamic_template_data": v["dynamic_template_data"]} if v.get("dynamic_template_data") else {})}],
                       "subject": v["subject"], **({"template_id": v["template_id"]} if v.get("template_id") else {}),
                       "content": [c for c in ([{"type": "text/plain", "value": v["text"]}] if v.get("text") else [])
                                   + ([{"type": "text/html", "value": v["html"]}] if v.get("html") else [])] or None}),
    op("contact", "upsert", "Add or update contact", "PUT", "/marketing/contacts", F("email", req=True), F("first_name"), F("last_name"),
       F("list_ids", type="list"),
       body=lambda v: {**({"list_ids": v["list_ids"]} if v.get("list_ids") else {}),
                       "contacts": [{k: v[k] for k in ("email", "first_name", "last_name") if v.get(k)}]}),
    op("list", "list", "Get many lists", "GET", "/marketing/lists", items="result"),
])
service("mailgun", "Mailgun", ["mailgun"], "{base_url}/v3", color="#f06b66", icon="mail", **MKT, ops=[
    op("mail", "send", "Send email", "POST", "/{domain}/messages", F("domain", req=True), F("from", req=True), F("to", req=True), F("subject", req=True),
       F("text", type="text"), F("html", "HTML", type="text"), F("cc"), F("bcc"), send="form"),
])
LIST = F("list_id", "Audience (list) id", req=True)
EMAIL_ = F("email_address", "Email", req=True)
_HASH = {"subscriber_hash": lambda v: hashlib.md5(str(v["email_address"]).strip().lower().encode()).hexdigest()}  # noqa: S324 - Mailchimp's member id
service("mailchimp", "Mailchimp", ["mailchimp"], "https://{server}.api.mailchimp.com/3.0", color="#ffe01b", icon="mail", **MKT, ops=[
    op("member", "upsert", "Add or update member", "PUT", "/lists/{list_id}/members/{subscriber_hash}", LIST, EMAIL_,
       F("status_if_new", type="options", options=["subscribed", "pending", "unsubscribed"], default="subscribed"),
       F("merge_fields", type="json", desc='{"FNAME": "Ann"}'), F("tags", type="list"), derive=_HASH),
    op("member", "get", "Get member", "GET", "/lists/{list_id}/members/{subscriber_hash}", LIST, F("email_address", "Email", req=True, where="none"),
       derive=_HASH),
    op("member", "delete", "Archive member", "DELETE", "/lists/{list_id}/members/{subscriber_hash}", LIST,
       F("email_address", "Email", req=True, where="none"), derive=_HASH),
    op("member", "list", "Get many members", "GET", "/lists/{list_id}/members", LIST, F("status"), items="members",
       paginate=Offset(size_param="count", size=1000)),
    op("list", "list", "Get many audiences", "GET", "/lists", items="lists", paginate=Offset(size_param="count")),
    op("campaign", "list", "Get many campaigns", "GET", "/campaigns", F("status"), items="campaigns", paginate=Offset(size_param="count")),
    op("campaign", "send", "Send campaign", "POST", "/campaigns/{campaign_id}/actions/send", F("campaign_id", req=True), extra=False),
])
service("postmark", "Postmark", ["postmark"], "https://api.postmarkapp.com", color="#ffde00", icon="mail", **MKT, ops=[
    op("mail", "send", "Send email", "POST", "/email", F("From", req=True), F("To", req=True), F("Subject", req=True), F("TextBody", type="text"),
       F("HtmlBody", type="text"), F("MessageStream", default="outbound"), F("Tag")),
    op("mail", "send_template", "Send with template", "POST", "/email/withTemplate", F("From", req=True), F("To", req=True),
       F("TemplateAlias", req=True), F("TemplateModel", type="json", default="{}")),
])
service("resend", "Resend", ["resend"], "https://api.resend.com", color="#000000", icon="mail", **MKT, ops=[
    op("mail", "send", "Send email", "POST", "/emails", F("from", req=True), F("to", type="list", req=True), F("subject", req=True),
       F("html", "HTML", type="text"), F("text", type="text"), F("cc", type="list"), F("bcc", type="list"), F("reply_to")),
    op("mail", "get", "Get email", "GET", "/emails/{id}", F("id", "Email id", req=True)),
])
service("brevo", "Brevo", ["brevo"], "https://api.brevo.com/v3", color="#0b996e", icon="mail", **MKT, ops=[
    op("mail", "send", "Send transactional email", "POST", "/smtp/email", F("sender_email", "From", req=True), F("sender_name", "From name"),
       F("to", type="list", req=True), F("subject", req=True), F("htmlContent", "HTML", type="text"), F("textContent", "Text", type="text"),
       F("templateId", type="number"), F("params", type="json"),
       body=lambda v: {"sender": {"email": v["sender_email"], **({"name": v["sender_name"]} if v.get("sender_name") else {})},
                       "to": [{"email": e} for e in v["to"]], "subject": v["subject"],
                       **{k: v[k] for k in ("htmlContent", "textContent", "templateId", "params") if v.get(k)}}),
    op("contact", "create", "Create contact", "POST", "/contacts", F("email", req=True), F("attributes", type="json"), F("listIds", type="json"),
       F("updateEnabled", "Update if exists", type="boolean", default=True)),
    op("contact", "get", "Get contact", "GET", "/contacts/{identifier}", F("identifier", "Email or id", req=True)),
    op("contact", "update", "Update contact", "PUT", "/contacts/{identifier}", F("identifier", "Email or id", req=True), F("attributes", type="json"),
       F("listIds", type="json")),
    op("contact", "delete", "Delete contact", "DELETE", "/contacts/{identifier}", F("identifier", "Email or id", req=True)),
])
