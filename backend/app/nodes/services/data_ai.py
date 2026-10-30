"""AI services, scraping, search and data integrations."""

from ..declarative import F, Offset, op, service

AIS = dict(group="ai_services")


async def _tavily_auth(cred: dict):
    return {"Authorization": f"Bearer {cred['api_key']}"}, {}

service("firecrawl", "Firecrawl", ["firecrawl"], "https://api.firecrawl.dev/v1", color="#ff6b00", icon="flame", **AIS, ops=[
    op("page", "scrape", "Scrape a page", "POST", "/scrape", F("url", req=True), F("formats", type="list", default="markdown"),
       F("onlyMainContent", type="boolean", default=True), out="data"),
    op("site", "crawl", "Start a crawl", "POST", "/crawl", F("url", req=True), F("limit", type="number", default=10),
       F("formats", type="list", default="markdown", to="scrapeOptions.formats")),
    op("site", "status", "Get crawl status / results", "GET", "/crawl/{id}", F("id", "Crawl id", req=True)),
    op("site", "map", "Map a site's URLs", "POST", "/map", F("url", req=True), F("search"), F("limit", type="number", default=100), items="links"),
    op("page", "extract", "Extract structured data", "POST", "/extract", F("urls", type="list", req=True), F("prompt", type="text"), F("schema", type="json")),
])
service("apify", "Apify", ["apify"], "https://api.apify.com/v2", color="#97d700", icon="bug-play", **AIS, ops=[
    op("actor", "run_sync", "Run actor and get results", "POST", "/acts/{actor_id}/run-sync-get-dataset-items",
       F("actor_id", req=True, desc="e.g. apify~web-scraper"), F("input", type="json", default="{}"), F("timeout", "Timeout (s)", type="number", where="query"),
       body=lambda v: v.get("input") or {}),
    op("actor", "run", "Start actor run", "POST", "/acts/{actor_id}/runs", F("actor_id", req=True), F("input", type="json", default="{}"),
       body=lambda v: v.get("input") or {}, out="data"),
    op("run", "get", "Get run", "GET", "/actor-runs/{run_id}", F("run_id", req=True), out="data"),
    op("dataset", "items", "Get dataset items", "GET", "/datasets/{dataset_id}/items", F("dataset_id", req=True), F("clean", type="boolean", default=True),
       paginate=Offset(size_param="limit", size=1000)),
])
service("tavily", "Tavily", ["tavily"], "https://api.tavily.com", color="#5468ff", icon="search", **AIS, ops=[
    op("search", "search", "Search the web", "POST", "/search", F("query", req=True), F("max_results", type="number", default=5),
       F("search_depth", type="options", options=["basic", "advanced"], default="basic"), F("include_answer", type="boolean"),
       F("topic", type="options", options=["general", "news"], default="general"), items="results"),
    op("page", "extract", "Extract pages", "POST", "/extract", F("urls", type="list", req=True), items="results"),
], auth=_tavily_auth)
service("serpapi", "SerpAPI", ["serpapi"], "https://serpapi.com", color="#2b6cb0", icon="search", **AIS, ops=[
    op("search", "google", "Google search", "GET", "/search.json", F("q", "Query", req=True), F("engine", default="google"), F("location"), F("gl", "Country"),
       F("hl", "Language"), F("num", type="number", default=10)),
])
service("brave_search", "Brave Search", ["brave_search"], "https://api.search.brave.com/res/v1", color="#fb542b", icon="search", **AIS, ops=[
    op("search", "web", "Web search", "GET", "/web/search", F("q", "Query", req=True), F("count", type="number", default=10), F("country"),
       items="web.results"),
    op("search", "news", "News search", "GET", "/news/search", F("q", "Query", req=True), F("count", type="number", default=10), items="results"),
])
service("openweathermap", "OpenWeatherMap", ["openweathermap"], "https://api.openweathermap.org/data/2.5", color="#eb6e4b", icon="cloud-sun", **AIS, ops=[
    op("weather", "current", "Current weather", "GET", "/weather", F("q", "City", desc="e.g. Berlin,de"), F("lat"), F("lon"),
       F("units", type="options", options=["metric", "imperial", "standard"], default="metric")),
    op("weather", "forecast", "5-day forecast", "GET", "/forecast", F("q", "City"), F("lat"), F("lon"),
       F("units", type="options", options=["metric", "imperial", "standard"], default="metric"), items="list"),
])

# ---- Speech / media ---------------------------------------------------------------
service("elevenlabs", "ElevenLabs", ["elevenlabs"], "https://api.elevenlabs.io/v1", color="#000000", icon="audio-lines", **AIS, ops=[
    op("speech", "tts", "Text to speech (audio file)", "POST", "/text-to-speech/{voice_id}", F("voice_id", req=True, default="21m00Tcm4TlvDq8ikWAM"),
       F("text", type="text", req=True), F("model_id", default="eleven_multilingual_v2"), F("output_format", default="mp3_44100_128", where="query"),
       download=True),
    op("voice", "list", "Get voices", "GET", "/voices", items="voices"),
])
service("deepgram", "Deepgram", ["deepgram"], "https://api.deepgram.com/v1", color="#13ef93", icon="mic", **AIS, ops=[
    op("audio", "transcribe_url", "Transcribe audio URL", "POST", "/listen", F("url", req=True), F("model", default="nova-3", where="query"),
       F("smart_format", type="boolean", default=True, where="query"), F("language", where="query"), out="results.channels.0.alternatives.0"),
    op("audio", "transcribe_file", "Transcribe audio file", "POST", "/listen", F("model", default="nova-3", where="query"),
       F("smart_format", type="boolean", default=True, where="query"), upload=True, out="results.channels.0.alternatives.0"),
])
service("assemblyai", "AssemblyAI", ["assemblyai"], "https://api.assemblyai.com/v2", color="#2545d3", icon="mic", **AIS, ops=[
    op("transcript", "create", "Transcribe audio URL", "POST", "/transcript", F("audio_url", req=True), F("speaker_labels", type="boolean"),
       F("language_code")),
    op("transcript", "get", "Get transcript", "GET", "/transcript/{id}", F("id", "Transcript id", req=True)),
])
service("replicate", "Replicate", ["replicate"], "https://api.replicate.com/v1", color="#000000", icon="cpu", **AIS, ops=[
    op("prediction", "create", "Run a model", "POST", "/models/{owner}/{model}/predictions", F("owner", req=True), F("model", req=True),
       F("input", type="json", req=True), F("wait", "Wait up to 60 s for the result", type="boolean", default=True, where="none"),
       headers=lambda v: {"Prefer": "wait"} if v.get("wait", True) else {}, body=lambda v: {"input": v["input"]}),
    op("prediction", "get", "Get prediction", "GET", "/predictions/{id}", F("id", "Prediction id", req=True)),
])
service("perplexity", "Perplexity", ["perplexity"], "https://api.perplexity.ai", color="#20808d", icon="sparkles", **AIS, ops=[
    op("chat", "ask", "Ask (web-grounded answer)", "POST", "/chat/completions", F("model", default="sonar", req=True), F("question", type="text", req=True),
       F("system", "System prompt", type="text"),
       body=lambda v: {"model": v["model"], "messages": ([{"role": "system", "content": v["system"]}] if v.get("system") else [])
                       + [{"role": "user", "content": v["question"]}]}),
])
service("stability", "Stability AI", ["stability"], "https://api.stability.ai/v2beta", color="#8b5cf6", icon="image", **AIS, ops=[
    op("image", "generate", "Generate image (core)", "POST", "/stable-image/generate/core", F("prompt", type="text", req=True), F("negative_prompt"),
       F("aspect_ratio", default="1:1"), F("output_format", type="options", options=["png", "jpeg", "webp"], default="png"),
       send="multipart", headers=lambda v: {"Accept": "image/*"}, download=True),
])
