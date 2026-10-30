"""Declarative catalog of credential types (n8n-style).

Each type has:
  fields  – form definition (secret fields are masked + encrypted)
  auth    – how to authenticate an HTTP request with it, as templates over
            the fields: {"headers": {...}, "query": {...}, "basic": [user, pass]}
            or {"oauth2": True}. Lets the HTTP Request node use ANY credential.
  test    – request that proves the credential works:
            {"url", "method"?, "json"?, "headers"?, "ok_field"?}
            or {"custom": "<name>"} for non-HTTP checks (DBs, SMTP, AWS…).
Templates use {field} placeholders, filled from the credential (with defaults).
"""


def F(name, label, type="string", *, secret=False, default=None, placeholder="", required=True,
      options=None, help="", show_if=None):
    return {"name": name, "label": label, "type": type, "secret": secret, "default": default,
            "placeholder": placeholder, "required": required, "options": options, "help": help,
            "show_if": show_if}


KEY = lambda label="API Key", name="api_key", **kw: F(name, label, secret=True, **kw)  # noqa: E731
BEARER = {"headers": {"Authorization": "Bearer {api_key}"}}

TYPES: dict[str, dict] = {}


def T(key, label, category, *, fields, auth=None, test=None, color="#6366f1", docs="", description="", icon=None):
    TYPES[key] = {"label": label, "category": category, "fields": fields, "auth": auth or {}, "test": test,
                  "color": color, "docs": docs, "description": description, "icon": icon}


def bearer_api(key, label, category, test_url, *, color, docs="", description="", extra=(), test_extra=None):
    T(key, label, category, fields=[KEY(), *extra], auth=BEARER,
      test={"url": test_url, **(test_extra or {})}, color=color, docs=docs, description=description)


# ---------------------------------------------------------------------------
# AI & LLM
# ---------------------------------------------------------------------------
AI = "AI & LLM"
T("openai", "OpenAI / OpenAI-compatible", AI, color="#10a37f", docs="https://platform.openai.com/api-keys",
  description="OpenAI, or any OpenAI-compatible server (vLLM, LM Studio, LocalAI, Groq…)",
  fields=[KEY(required=False, help="Required for OpenAI; optional for local servers started without --api-key"),
          F("base_url", "Base URL", default="https://api.openai.com/v1", required=False,
                   help="Change for vLLM / LM Studio / proxies, e.g. http://host:8000/v1"),
          F("organization", "Organization ID", required=False)],
  auth={"headers": {"Authorization": "Bearer {api_key}", "OpenAI-Organization": "{organization}"}},
  test={"url": "{base_url}/models"})
T("anthropic", "Anthropic", AI, color="#d97757", docs="https://console.anthropic.com/settings/keys",
  fields=[KEY()], auth={"headers": {"x-api-key": "{api_key}", "anthropic-version": "2023-06-01"}},
  test={"url": "https://api.anthropic.com/v1/models"})
T("google_genai", "Google Gemini (AI Studio)", AI, color="#4285f4", docs="https://aistudio.google.com/apikey",
  fields=[KEY()], auth={"query": {"key": "{api_key}"}},
  test={"url": "https://generativelanguage.googleapis.com/v1beta/models"})
T("azure_openai", "Azure OpenAI", AI, color="#0078d4",
  fields=[KEY(), F("endpoint", "Endpoint", placeholder="https://<resource>.openai.azure.com"),
          F("api_version", "API version", default="2024-10-21")],
  auth={"headers": {"api-key": "{api_key}"}}, test={"url": "{endpoint}/openai/models?api-version={api_version}"})
T("ollama", "Ollama", AI, color="#111827", fields=[F("base_url", "Base URL", default="http://localhost:11434")],
  test={"url": "{base_url}/api/tags"})
bearer_api("mistral", "Mistral AI", AI, "https://api.mistral.ai/v1/models", color="#fa520f")
bearer_api("groq", "Groq", AI, "https://api.groq.com/openai/v1/models", color="#f55036")
bearer_api("openrouter", "OpenRouter", AI, "https://openrouter.ai/api/v1/key", color="#6467f2")
bearer_api("deepseek", "DeepSeek", AI, "https://api.deepseek.com/models", color="#4d6bfe")
bearer_api("xai", "xAI (Grok)", AI, "https://api.x.ai/v1/models", color="#000000")
bearer_api("cohere", "Cohere", AI, "https://api.cohere.com/v1/models", color="#39594d")
bearer_api("together", "Together AI", AI, "https://api.together.xyz/v1/models", color="#0f6fff")
bearer_api("fireworks", "Fireworks AI", AI, "https://api.fireworks.ai/inference/v1/models", color="#6720ff")
bearer_api("perplexity", "Perplexity", AI, "https://api.perplexity.ai/chat/completions", color="#20808d",
           test_extra={"method": "POST", "json": {"model": "sonar", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}})
bearer_api("huggingface", "Hugging Face", AI, "https://huggingface.co/api/whoami-v2", color="#ffb000")
bearer_api("replicate", "Replicate", AI, "https://api.replicate.com/v1/account", color="#000000")
bearer_api("stability", "Stability AI", AI, "https://api.stability.ai/v1/user/account", color="#7c3aed")
T("pinecone", "Pinecone", AI, color="#000000", fields=[KEY()], auth={"headers": {"Api-Key": "{api_key}"}},
  test={"url": "https://api.pinecone.io/indexes"})
T("qdrant", "Qdrant", AI, color="#dc244c", fields=[F("url", "URL", placeholder="https://xyz.cloud.qdrant.io:6333"), KEY(required=False)],
  auth={"headers": {"api-key": "{api_key}"}}, test={"url": "{url}/collections"})

# ---------------------------------------------------------------------------
# Voice & speech
# ---------------------------------------------------------------------------
VOICE = "Voice & Speech"
T("livekit", "LiveKit", VOICE, color="#ff4f2c", docs="https://cloud.livekit.io",
  fields=[F("url", "Server URL", placeholder="wss://your-project.livekit.cloud"), F("api_key", "API Key"),
          KEY("API Secret", "api_secret"), F("sip_trunk_id", "Outbound SIP Trunk ID", required=False),
          F("sip_uri", "SIP URI (for inbound calls)", required=False, placeholder="xxxxxxxx.sip.livekit.cloud",
            help="Project settings → SIP URI in LiveKit Cloud. Carriers (Plivo, Twilio…) send inbound calls here.")],
  test={"custom": "livekit"})
T("plivo", "Plivo", VOICE, color="#43b049", docs="https://console.plivo.com/dashboard/",
  fields=[F("auth_id", "Auth ID"), KEY("Auth Token", "auth_token")],
  auth={"basic": ["{auth_id}", "{auth_token}"]}, test={"url": "https://api.plivo.com/v1/Account/{auth_id}/"})
T("deepgram", "Deepgram (STT/TTS)", VOICE, color="#13ef93", fields=[KEY()],
  auth={"headers": {"Authorization": "Token {api_key}"}}, test={"url": "https://api.deepgram.com/v1/projects"})
T("elevenlabs", "ElevenLabs (TTS)", VOICE, color="#000000", fields=[KEY()],
  auth={"headers": {"xi-api-key": "{api_key}"}}, test={"url": "https://api.elevenlabs.io/v1/user"})
T("cartesia", "Cartesia (TTS)", VOICE, color="#111111", fields=[KEY()],
  auth={"headers": {"X-API-Key": "{api_key}", "Cartesia-Version": "2024-11-13"}}, test={"url": "https://api.cartesia.ai/voices"})
T("assemblyai", "AssemblyAI", VOICE, color="#2545d3", fields=[KEY()],
  auth={"headers": {"Authorization": "{api_key}"}}, test={"url": "https://api.assemblyai.com/v2/transcript?limit=1"})
T("twilio", "Twilio", VOICE, color="#f22f46",
  fields=[F("account_sid", "Account SID"), KEY("Auth Token", "auth_token")],
  auth={"basic": ["{account_sid}", "{auth_token}"]}, test={"url": "https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json"})
T("vonage", "Vonage", VOICE, color="#000000", fields=[F("api_key", "API Key"), KEY("API Secret", "api_secret")],
  auth={"query": {"api_key": "{api_key}", "api_secret": "{api_secret}"}}, test={"url": "https://rest.nexmo.com/account/get-balance"})

# ---------------------------------------------------------------------------
# Messaging & chat
# ---------------------------------------------------------------------------
MSG = "Messaging & Chat"
T("whatsapp", "WhatsApp Business Cloud API", MSG, color="#25d366", docs="https://developers.facebook.com/docs/whatsapp/cloud-api",
  fields=[KEY("Access Token", "access_token"), F("phone_number_id", "Phone Number ID"),
          F("business_account_id", "WhatsApp Business Account ID", required=False),
          KEY("Webhook Verify Token", "verify_token", help="Any string; paste the same value in Meta's webhook settings"),
          KEY("App Secret (signature check)", "app_secret", required=False), F("api_version", "Graph API version", default="v21.0")],
  auth={"headers": {"Authorization": "Bearer {access_token}"}},
  test={"url": "https://graph.facebook.com/{api_version}/{phone_number_id}"})
T("slack", "Slack Bot", MSG, color="#4a154b", fields=[KEY("Bot Token (xoxb-…)", "bot_token")],
  auth={"headers": {"Authorization": "Bearer {bot_token}"}}, test={"url": "https://slack.com/api/auth.test", "ok_field": "ok"})
T("telegram", "Telegram Bot", MSG, color="#229ed9", fields=[KEY("Bot Token", "bot_token")],
  test={"url": "https://api.telegram.org/bot{bot_token}/getMe", "ok_field": "ok"})
T("discord_bot", "Discord Bot", MSG, color="#5865f2", fields=[KEY("Bot Token", "bot_token")],
  auth={"headers": {"Authorization": "Bot {bot_token}"}}, test={"url": "https://discord.com/api/v10/users/@me"})
T("discord_webhook", "Discord Webhook", MSG, color="#5865f2", fields=[KEY("Webhook URL", "webhook_url")],
  test={"url": "{webhook_url}"})
T("teams_webhook", "Microsoft Teams Webhook", MSG, color="#4b53bc", fields=[KEY("Incoming webhook URL", "webhook_url")])
T("mattermost", "Mattermost", MSG, color="#0058cc", fields=[F("url", "Server URL"), KEY("Access Token", "access_token")],
  auth={"headers": {"Authorization": "Bearer {access_token}"}}, test={"url": "{url}/api/v4/users/me"})
T("matrix", "Matrix", MSG, color="#000000", fields=[F("homeserver", "Homeserver URL", default="https://matrix.org"), KEY("Access Token", "access_token")],
  auth={"headers": {"Authorization": "Bearer {access_token}"}}, test={"url": "{homeserver}/_matrix/client/v3/account/whoami"})
T("line", "LINE Messaging", MSG, color="#06c755", fields=[KEY("Channel access token", "access_token")],
  auth={"headers": {"Authorization": "Bearer {access_token}"}}, test={"url": "https://api.line.me/v2/bot/info"})

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL = "Email"
T("smtp", "SMTP", EMAIL, color="#64748b",
  fields=[F("host", "Host"), F("port", "Port", "number", default=587), F("username", "Username", required=False),
          KEY("Password", "password", required=False),
          F("security", "Security", "options", default="starttls", options=["starttls", "ssl", "none"]),
          F("from_email", "Default From", required=False)],
  test={"custom": "smtp"})
T("imap", "IMAP", EMAIL, color="#64748b",
  fields=[F("host", "Host"), F("port", "Port", "number", default=993), F("username", "Username"), KEY("Password", "password"),
          F("ssl", "Use SSL", "boolean", default=True)], test={"custom": "imap"})
bearer_api("sendgrid", "SendGrid", EMAIL, "https://api.sendgrid.com/v3/scopes", color="#1a82e2")
T("mailgun", "Mailgun", EMAIL, color="#f06b66",
  fields=[KEY(), F("base_url", "API base", "options", default="https://api.mailgun.net", options=["https://api.mailgun.net", "https://api.eu.mailgun.net"])],
  auth={"basic": ["api", "{api_key}"]}, test={"url": "{base_url}/v3/domains"})
T("mailchimp", "Mailchimp", EMAIL, color="#ffe01b",
  fields=[KEY(help="Ends with -usXX"), F("server", "Server prefix", placeholder="us21")],
  auth={"basic": ["anystring", "{api_key}"]}, test={"url": "https://{server}.api.mailchimp.com/3.0/ping"})
T("postmark", "Postmark", EMAIL, color="#ffde00", fields=[KEY("Server Token", "api_key")],
  auth={"headers": {"X-Postmark-Server-Token": "{api_key}"}}, test={"url": "https://api.postmarkapp.com/server"})
bearer_api("resend", "Resend", EMAIL, "https://api.resend.com/domains", color="#000000")
T("brevo", "Brevo (Sendinblue)", EMAIL, color="#0b996e", fields=[KEY()],
  auth={"headers": {"api-key": "{api_key}"}}, test={"url": "https://api.brevo.com/v3/account"})

# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------
DB = "Databases"
DB_FIELDS = lambda port, user="": [F("host", "Host", default="localhost"), F("port", "Port", "number", default=port),  # noqa: E731
                                   F("database", "Database"), F("user", "User", default=user), KEY("Password", "password", required=False),
                                   F("ssl", "Use SSL", "boolean", default=False)]
T("postgres", "Postgres", DB, color="#336791", fields=DB_FIELDS(5432, "postgres"), test={"custom": "postgres"})
T("mysql", "MySQL / MariaDB", DB, color="#00758f", fields=DB_FIELDS(3306, "root"), test={"custom": "mysql"})
T("redis", "Redis", DB, color="#dc382d",
  fields=[F("host", "Host", default="localhost"), F("port", "Port", "number", default=6379), F("db", "Database index", "number", default=0),
          F("username", "Username", required=False), KEY("Password", "password", required=False), F("ssl", "Use TLS", "boolean", default=False)],
  test={"custom": "redis"})
T("mongodb", "MongoDB", DB, color="#47a248",
  fields=[KEY("Connection string", "uri", placeholder="mongodb+srv://user:pass@cluster/db"), F("database", "Default database", required=False)],
  test={"custom": "mongodb"})
T("supabase", "Supabase", DB, color="#3ecf8e", fields=[F("url", "Project URL", placeholder="https://xyz.supabase.co"), KEY("Service role key", "api_key")],
  auth={"headers": {"apikey": "{api_key}", "Authorization": "Bearer {api_key}"}}, test={"url": "{url}/rest/v1/"})
T("airtable", "Airtable", DB, color="#18bfff", fields=[KEY("Personal access token", "api_key")], auth=BEARER,
  test={"url": "https://api.airtable.com/v0/meta/whoami"})

# ---------------------------------------------------------------------------
# Queues & streaming (used by the listener triggers)
# ---------------------------------------------------------------------------
STREAM = "Queues & Streaming"
T("kafka", "Kafka", STREAM, color="#231f20", description="Apache Kafka, Confluent Cloud, Redpanda, MSK",
  fields=[F("brokers", "Brokers", default="localhost:9092", help="Comma separated host:port list"),
          F("client_id", "Client ID", default="debbuilder", required=False),
          F("ssl", "Use SSL/TLS", "boolean", default=False),
          F("sasl_mechanism", "SASL mechanism", "options", default="none", options=["none", "PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]),
          F("username", "Username", show_if={"sasl_mechanism": ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]}),
          KEY("Password", "password", show_if={"sasl_mechanism": ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]})],
  test={"custom": "kafka"})
T("rabbitmq", "RabbitMQ", STREAM, color="#ff6600",
  fields=[F("host", "Host", default="localhost"), F("port", "Port", "number", default=5672),
          F("vhost", "Virtual host", default="/"), F("username", "Username", default="guest"),
          KEY("Password", "password", default="guest"), F("ssl", "Use TLS (amqps)", "boolean", default=False)],
  test={"custom": "rabbitmq"})
T("mqtt", "MQTT", STREAM, color="#660066", description="Mosquitto, EMQX, HiveMQ, AWS IoT…",
  fields=[F("host", "Host", default="localhost"), F("port", "Port", "number", default=1883),
          F("username", "Username", required=False), KEY("Password", "password", required=False),
          F("client_id", "Client ID", required=False, help="Leave empty for a random one"),
          F("ssl", "Use TLS", "boolean", default=False)],
  test={"custom": "mqtt"})
T("nats", "NATS", STREAM, color="#27aae1",
  fields=[F("servers", "Servers", default="nats://localhost:4222", help="Comma separated URLs"),
          F("username", "Username", required=False), KEY("Password", "password", required=False),
          KEY("Token", "token", required=False)],
  test={"custom": "nats"})

# ---------------------------------------------------------------------------
# Productivity & project management
# ---------------------------------------------------------------------------
PROD = "Productivity"
T("notion", "Notion", PROD, color="#000000", fields=[KEY("Internal integration secret", "api_key")],
  auth={"headers": {"Authorization": "Bearer {api_key}", "Notion-Version": "2022-06-28"}}, test={"url": "https://api.notion.com/v1/users/me"})
bearer_api("asana", "Asana", PROD, "https://app.asana.com/api/1.0/users/me", color="#f06a6a")
T("trello", "Trello", PROD, color="#0079bf", fields=[F("api_key", "API Key"), KEY("Token", "token")],
  auth={"query": {"key": "{api_key}", "token": "{token}"}}, test={"url": "https://api.trello.com/1/members/me"})
T("clickup", "ClickUp", PROD, color="#7b68ee", fields=[KEY()], auth={"headers": {"Authorization": "{api_key}"}},
  test={"url": "https://api.clickup.com/api/v2/user"})
T("monday", "monday.com", PROD, color="#ff3d57", fields=[KEY()], auth={"headers": {"Authorization": "{api_key}"}},
  test={"url": "https://api.monday.com/v2", "method": "POST", "json": {"query": "{ me { id } }"}})
T("linear", "Linear", PROD, color="#5e6ad2", fields=[KEY()], auth={"headers": {"Authorization": "{api_key}"}},
  test={"url": "https://api.linear.app/graphql", "method": "POST", "json": {"query": "{ viewer { id } }"}})
T("jira", "Jira Cloud", PROD, color="#0052cc",
  fields=[F("domain", "Site URL", placeholder="https://your-team.atlassian.net"), F("email", "Email"), KEY("API Token", "api_token")],
  auth={"basic": ["{email}", "{api_token}"]}, test={"url": "{domain}/rest/api/3/myself"})
T("confluence", "Confluence Cloud", PROD, color="#0052cc",
  fields=[F("domain", "Site URL", placeholder="https://your-team.atlassian.net"), F("email", "Email"), KEY("API Token", "api_token")],
  auth={"basic": ["{email}", "{api_token}"]}, test={"url": "{domain}/wiki/rest/api/user/current"})
bearer_api("todoist", "Todoist", PROD, "https://api.todoist.com/rest/v2/projects", color="#e44332")
bearer_api("calendly", "Calendly", PROD, "https://api.calendly.com/users/me", color="#006bff")
bearer_api("typeform", "Typeform", PROD, "https://api.typeform.com/me", color="#262627")
T("jotform", "Jotform", PROD, color="#ff6100", fields=[KEY()], auth={"query": {"apiKey": "{api_key}"}}, test={"url": "https://api.jotform.com/user"})
T("figma", "Figma", PROD, color="#a259ff", fields=[KEY("Personal access token", "api_key")], auth={"headers": {"X-Figma-Token": "{api_key}"}},
  test={"url": "https://api.figma.com/v1/me"})
bearer_api("webflow", "Webflow", PROD, "https://api.webflow.com/v2/token/authorized_by", color="#4353ff")
T("wordpress", "WordPress", PROD, color="#21759b",
  fields=[F("url", "Site URL"), F("username", "Username"), KEY("Application password", "password")],
  auth={"basic": ["{username}", "{password}"]}, test={"url": "{url}/wp-json/wp/v2/users/me"})
bearer_api("dropbox", "Dropbox (access token)", PROD, "https://api.dropboxapi.com/2/users/get_current_account", color="#0061ff",
           test_extra={"method": "POST"})

# ---------------------------------------------------------------------------
# CRM, sales & support
# ---------------------------------------------------------------------------
CRM = "CRM & Support"
bearer_api("hubspot", "HubSpot (private app)", CRM, "https://api.hubapi.com/crm/v3/objects/contacts?limit=1", color="#ff7a59")
T("pipedrive", "Pipedrive", CRM, color="#017737", fields=[KEY("API Token", "api_key")], auth={"query": {"api_token": "{api_key}"}},
  test={"url": "https://api.pipedrive.com/v1/users/me"})
T("zendesk", "Zendesk", CRM, color="#03363d",
  fields=[F("subdomain", "Subdomain", placeholder="acme"), F("email", "Email"), KEY("API Token", "api_token")],
  auth={"basic": ["{email}/token", "{api_token}"]}, test={"url": "https://{subdomain}.zendesk.com/api/v2/users/me.json"})
T("freshdesk", "Freshdesk", CRM, color="#25c16f", fields=[F("domain", "Domain", placeholder="acme"), KEY()],
  auth={"basic": ["{api_key}", "X"]}, test={"url": "https://{domain}.freshdesk.com/api/v2/agents/me"})
bearer_api("intercom", "Intercom", CRM, "https://api.intercom.io/me", color="#1f8ded")
T("zoho_crm", "Zoho CRM", CRM, color="#e42527", fields=[KEY("Access token", "api_key"), F("dc", "Data center", default="com")],
  auth={"headers": {"Authorization": "Zoho-oauthtoken {api_key}"}}, test={"url": "https://www.zohoapis.{dc}/crm/v2/users?type=CurrentUser"})

# ---------------------------------------------------------------------------
# Payments & e-commerce
# ---------------------------------------------------------------------------
PAY = "Payments & E-commerce"
bearer_api("stripe", "Stripe", PAY, "https://api.stripe.com/v1/balance", color="#635bff")
T("paypal", "PayPal", PAY, color="#003087",
  fields=[F("client_id", "Client ID"), KEY("Client Secret", "client_secret"),
          F("environment", "Environment", "options", default="live", options=["live", "sandbox"])],
  test={"custom": "paypal"})
T("shopify", "Shopify", PAY, color="#96bf48", fields=[F("shop", "Shop subdomain", placeholder="my-store"), KEY("Admin API access token", "access_token")],
  auth={"headers": {"X-Shopify-Access-Token": "{access_token}"}}, test={"url": "https://{shop}.myshopify.com/admin/api/2024-10/shop.json"})
T("woocommerce", "WooCommerce", PAY, color="#96588a",
  fields=[F("url", "Store URL"), F("consumer_key", "Consumer key"), KEY("Consumer secret", "consumer_secret")],
  auth={"basic": ["{consumer_key}", "{consumer_secret}"]}, test={"url": "{url}/wp-json/wc/v3/system_status"})
T("razorpay", "Razorpay", PAY, color="#0c2651", fields=[F("key_id", "Key ID"), KEY("Key Secret", "key_secret")],
  auth={"basic": ["{key_id}", "{key_secret}"]}, test={"url": "https://api.razorpay.com/v1/payments?count=1"})

# ---------------------------------------------------------------------------
# Developer & DevOps
# ---------------------------------------------------------------------------
DEV = "Developer & DevOps"
T("github", "GitHub (token)", DEV, color="#181717", fields=[KEY("Personal access token", "api_key"), F("server", "API URL", default="https://api.github.com")],
  auth={"headers": {"Authorization": "Bearer {api_key}", "Accept": "application/vnd.github+json"}}, test={"url": "{server}/user"})
T("gitlab", "GitLab", DEV, color="#fc6d26", fields=[KEY("Access token", "api_key"), F("server", "Server", default="https://gitlab.com")],
  auth={"headers": {"PRIVATE-TOKEN": "{api_key}"}}, test={"url": "{server}/api/v4/user"})
T("bitbucket", "Bitbucket", DEV, color="#0052cc", fields=[F("username", "Username"), KEY("App password", "password")],
  auth={"basic": ["{username}", "{password}"]}, test={"url": "https://api.bitbucket.org/2.0/user"})
bearer_api("vercel", "Vercel", DEV, "https://api.vercel.com/v2/user", color="#000000")
bearer_api("netlify", "Netlify", DEV, "https://api.netlify.com/api/v1/user", color="#00c7b7")
bearer_api("cloudflare", "Cloudflare", DEV, "https://api.cloudflare.com/client/v4/user/tokens/verify", color="#f38020")
bearer_api("digitalocean", "DigitalOcean", DEV, "https://api.digitalocean.com/v2/account", color="#0080ff")
bearer_api("sentry", "Sentry", DEV, "https://sentry.io/api/0/organizations/", color="#362d59")
T("datadog", "Datadog", DEV, color="#632ca6", fields=[KEY(), F("site", "Site", default="datadoghq.com")],
  auth={"headers": {"DD-API-KEY": "{api_key}"}}, test={"url": "https://api.{site}/api/v1/validate"})
T("pagerduty", "PagerDuty", DEV, color="#06ac38", fields=[KEY()], auth={"headers": {"Authorization": "Token token={api_key}"}},
  test={"url": "https://api.pagerduty.com/abilities"})
T("jenkins", "Jenkins", DEV, color="#d24939", fields=[F("url", "Jenkins URL"), F("username", "Username"), KEY("API token", "api_token")],
  auth={"basic": ["{username}", "{api_token}"]}, test={"url": "{url}/api/json"})

# ---------------------------------------------------------------------------
# Cloud & storage
# ---------------------------------------------------------------------------
CLOUD = "Cloud & Storage"
T("aws", "AWS (access keys)", CLOUD, color="#ff9900",
  fields=[F("access_key_id", "Access Key ID"), KEY("Secret Access Key", "secret_access_key"),
          F("region", "Region", default="us-east-1"), KEY("Session token", "session_token", required=False)],
  test={"custom": "aws"})
T("google_service_account", "Google Service Account", CLOUD, color="#4285f4",
  fields=[F("service_account_json", "Service account key (JSON)", "textarea", secret=True),
          F("scopes", "Scopes", default="https://www.googleapis.com/auth/cloud-platform", help="Space separated")],
  test={"custom": "google_service_account"})
T("ftp", "FTP", CLOUD, color="#64748b",
  fields=[F("host", "Host"), F("port", "Port", "number", default=21), F("username", "Username", default="anonymous"),
          KEY("Password", "password", required=False), F("tls", "FTPS (TLS)", "boolean", default=False)],
  test={"custom": "ftp"})
T("ssh", "SSH / SFTP", CLOUD, color="#334155",
  fields=[F("host", "Host"), F("port", "Port", "number", default=22), F("username", "Username"),
          KEY("Password", "password", required=False),
          F("private_key", "Private key (PEM / OpenSSH)", "textarea", secret=True, required=False,
            help="Use a password, a private key, or both"),
          KEY("Key passphrase", "passphrase", required=False)],
  test={"custom": "ssh"})

# ---------------------------------------------------------------------------
# Search & data
# ---------------------------------------------------------------------------
SEARCH = "Search & Data"
T("tavily", "Tavily Search", SEARCH, color="#5468ff", fields=[KEY()],
  test={"url": "https://api.tavily.com/search", "method": "POST", "json": {"api_key": "{api_key}", "query": "ping", "max_results": 1}})
T("serpapi", "SerpAPI", SEARCH, color="#2b6cb0", fields=[KEY()], auth={"query": {"api_key": "{api_key}"}},
  test={"url": "https://serpapi.com/account.json"})
T("brave_search", "Brave Search", SEARCH, color="#fb542b", fields=[KEY()], auth={"headers": {"X-Subscription-Token": "{api_key}"}},
  test={"url": "https://api.search.brave.com/res/v1/web/search?q=ping&count=1"})
T("openweathermap", "OpenWeatherMap", SEARCH, color="#eb6e4b", fields=[KEY()], auth={"query": {"appid": "{api_key}"}},
  test={"url": "https://api.openweathermap.org/data/2.5/weather?q=London"})
bearer_api("apify", "Apify", SEARCH, "https://api.apify.com/v2/users/me", color="#97d700")
bearer_api("firecrawl", "Firecrawl", SEARCH, "https://api.firecrawl.dev/v1/team/credit-usage", color="#ff6b00")

# ---------------------------------------------------------------------------
# OAuth2 (Connect my account)
# ---------------------------------------------------------------------------
OAUTH = "OAuth2 apps"
OAUTH_FIELDS = lambda auth_url, token_url, scope, extra="": [  # noqa: E731
    F("grant_type", "Grant type", "options", default="authorization_code", options=["authorization_code", "client_credentials"]),
    F("auth_url", "Authorization URL", default=auth_url, show_if={"grant_type": ["authorization_code"]}),
    F("token_url", "Access token URL", default=token_url),
    F("client_id", "Client ID"), KEY("Client Secret", "client_secret"),
    F("scope", "Scope", default=scope, required=False),
    F("auth_query", "Extra auth URL parameters", default=extra, required=False, placeholder="access_type=offline&prompt=consent"),
    F("client_auth", "Send client credentials", "options", default="body", options=["body", "header"]),
]


def oauth(key, label, auth_url, token_url, scope, test_url=None, *, color, extra="", category=OAUTH, description=""):
    T(key, label, category, fields=OAUTH_FIELDS(auth_url, token_url, scope, extra), auth={"oauth2": True},
      test={"custom": "oauth2", "url": test_url}, color=color, description=description)


oauth("oauth2", "OAuth2 API (generic)", "", "", "", None, color="#7c3aed", category="Generic / HTTP",
      description="Any OAuth2 provider: authorization code or client credentials")
oauth("google_oauth2", "Google (Gmail, Sheets, Drive, Calendar…)", "https://accounts.google.com/o/oauth2/v2/auth",
      "https://oauth2.googleapis.com/token",
      "openid email https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/spreadsheets "
      "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/calendar",
      "https://www.googleapis.com/oauth2/v3/userinfo", color="#4285f4", extra="access_type=offline&prompt=consent")
oauth("microsoft_oauth2", "Microsoft 365 (Outlook, Teams, OneDrive, Excel)",
      "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "https://login.microsoftonline.com/common/oauth2/v2.0/token",
      "openid offline_access User.Read Mail.ReadWrite Mail.Send Files.ReadWrite Calendars.ReadWrite",
      "https://graph.microsoft.com/v1.0/me", color="#0078d4")
oauth("github_oauth2", "GitHub (OAuth app)", "https://github.com/login/oauth/authorize", "https://github.com/login/oauth/access_token",
      "repo read:user", "https://api.github.com/user", color="#181717")
oauth("slack_oauth2", "Slack (OAuth)", "https://slack.com/oauth/v2/authorize", "https://slack.com/api/oauth.v2.access",
      "chat:write channels:read users:read", "https://slack.com/api/auth.test", color="#4a154b")
oauth("salesforce_oauth2", "Salesforce", "https://login.salesforce.com/services/oauth2/authorize",
      "https://login.salesforce.com/services/oauth2/token", "api refresh_token", None, color="#00a1e0")
oauth("hubspot_oauth2", "HubSpot (OAuth)", "https://app.hubspot.com/oauth/authorize", "https://api.hubapi.com/oauth/v1/token",
      "crm.objects.contacts.read crm.objects.contacts.write", None, color="#ff7a59")
oauth("zoom_oauth2", "Zoom", "https://zoom.us/oauth/authorize", "https://zoom.us/oauth/token", "", "https://api.zoom.us/v2/users/me", color="#2d8cff")
oauth("dropbox_oauth2", "Dropbox (OAuth)", "https://www.dropbox.com/oauth2/authorize", "https://api.dropboxapi.com/oauth2/token",
      "", None, color="#0061ff", extra="token_access_type=offline")
oauth("linkedin_oauth2", "LinkedIn", "https://www.linkedin.com/oauth/v2/authorization", "https://www.linkedin.com/oauth/v2/accessToken",
      "openid profile email w_member_social", "https://api.linkedin.com/v2/userinfo", color="#0a66c2")
oauth("spotify_oauth2", "Spotify", "https://accounts.spotify.com/authorize", "https://accounts.spotify.com/api/token",
      "user-read-email playlist-modify-private", "https://api.spotify.com/v1/me", color="#1db954")
oauth("x_oauth2", "X (Twitter)", "https://twitter.com/i/oauth2/authorize", "https://api.twitter.com/2/oauth2/token",
      "tweet.read tweet.write users.read offline.access", "https://api.twitter.com/2/users/me", color="#000000",
      extra="code_challenge=challenge&code_challenge_method=plain")
oauth("notion_oauth2", "Notion (public integration)", "https://api.notion.com/v1/oauth/authorize", "https://api.notion.com/v1/oauth/token",
      "", None, color="#000000", extra="owner=user")

# ---------------------------------------------------------------------------
# Generic / HTTP
# ---------------------------------------------------------------------------
GEN = "Generic / HTTP"
T("http_bearer", "Bearer Token", GEN, color="#64748b", fields=[KEY("Token", "token"), F("test_url", "Test URL (optional)", required=False)],
  auth={"headers": {"Authorization": "Bearer {token}"}}, test={"url": "{test_url}"})
T("http_basic", "Basic Auth", GEN, color="#64748b",
  fields=[F("username", "Username"), KEY("Password", "password"), F("test_url", "Test URL (optional)", required=False)],
  auth={"basic": ["{username}", "{password}"]}, test={"url": "{test_url}"})
T("http_header", "Header Auth", GEN, color="#64748b",
  fields=[F("name", "Header name", default="X-API-Key"), KEY("Header value", "value"), F("test_url", "Test URL (optional)", required=False)],
  auth={"headers": {"{name}": "{value}"}}, test={"url": "{test_url}"})
T("http_query", "Query Auth", GEN, color="#64748b",
  fields=[F("name", "Query parameter", default="api_key"), KEY("Value", "value"), F("test_url", "Test URL (optional)", required=False)],
  auth={"query": {"{name}": "{value}"}}, test={"url": "{test_url}"})
T("http_custom", "Custom Auth (JSON)", GEN, color="#64748b",
  fields=[F("headers", "Headers (JSON object)", "textarea", secret=True, default="{}"),
          F("query", "Query params (JSON object)", "textarea", required=False, default="{}"),
          F("test_url", "Test URL (optional)", required=False)],
  auth={"custom_json": True}, test={"url": "{test_url}"})

CATEGORY_ORDER = [AI, VOICE, MSG, EMAIL, DB, STREAM, PROD, CRM, PAY, DEV, CLOUD, SEARCH, OAUTH, GEN]
