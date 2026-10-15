"""Read-only hosting hints; exact source/marker proof still owns readiness."""
import base64
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from urllib.parse import quote
from django.conf import settings
from . import website_goal as goals
from .website_connections import authority_guard, require_unlocked_remote_call
from .website_contract import WebsiteAuthorityError

CONFIGS = {"vercel": ("vercel.json", ".vercel/project.json"), "netlify": ("netlify.toml",),
    "cloudflare": ("wrangler.toml", "wrangler.json", "wrangler.jsonc", ".cloudflare/pages.json")}
INSTRUCTIONS = {
    "vercel": "Open your Vercel project, select the production branch and redeploy the merged commit.",
    "netlify": "Open your Netlify site, select Deploys and deploy the merged production branch.",
    "cloudflare": "Open Cloudflare Workers & Pages and deploy the merged commit to the site's production project.",
    "github_pages": "Open the repository's Actions tab and run its Pages deployment workflow for the merged default branch.",
    "github_actions": "Open the repository's Actions tab and run its deployment workflow for the merged branch.",
    "unknown": "Deploy the merged branch using your hosting provider's production deployment process.",
}


def enabled():
    """Provider observation is an independent default-off rollout."""
    return getattr(settings, "ARTICLES_DEPLOYMENT_DETECTION_ENABLED", False)


def workflow_signal(source, branch, default_branch):
    """Recognise deployment actions and explicit default-branch push triggers only."""
    import yaml
    try:
        value = yaml.load(source, Loader=yaml.BaseLoader)
    except (ValueError, yaml.YAMLError):
        return None
    if not isinstance(value, dict):return None
    jobs = goals.mapping(value.get("jobs"))
    uses = [str(step.get("uses", "")).split("@", 1)[0].lower() for job in list(jobs.values())[:32]
        if isinstance(job, dict) for step in (job.get("steps") if isinstance(job.get("steps"), list) else [])[:100] if isinstance(step, dict)]
    provider = "github_pages" if "actions/deploy-pages" in uses else "cloudflare" if "cloudflare/wrangler-action" in uses else "netlify" if any(name.startswith("netlify/actions/") or name == "nwtgck/actions-netlify" for name in uses) else "vercel" if "amondnet/vercel-action" in uses else None
    if provider is None:
        for name,job in list(jobs.items())[:32]:
            if not isinstance(job,dict):continue
            environment = job.get("environment") if isinstance(job.get("environment"),str) else goals.mapping(job.get("environment")).get("name")
            if "deploy" in str(name).lower() and isinstance(environment,str) and environment in {"production","live","github-pages"}:
                provider = "github_actions";break
    if provider is None:return None
    trigger = value.get("on")
    events = trigger if isinstance(trigger, dict) else {key: None for key in trigger} if isinstance(trigger, list) else {trigger: None} if isinstance(trigger, str) else {}
    push = events.get("push")
    automatic = False
    if "push" in events and branch == default_branch:
        if push in (None, ""):automatic = True
        elif isinstance(push, dict) and not any(key in push for key in ("paths", "paths-ignore")):
            include = push.get("branches")
            exclude = push.get("branches-ignore") or []
            if isinstance(include, str):include = [include]
            if isinstance(exclude, str):exclude = [exclude]
            automatic = (include is None or isinstance(include, list) and any(isinstance(pattern, str) and not pattern.startswith("!") and fnmatchcase(branch, pattern) for pattern in include)) and isinstance(exclude, list) and not any(isinstance(pattern, str) and fnmatchcase(branch, pattern) for pattern in exclude)
            if isinstance(include, list) and any(isinstance(pattern, str) and pattern.startswith("!") and fnmatchcase(branch, pattern[1:]) for pattern in include):automatic = False
    return {"provider": provider, "automation": "automatic" if automatic else "manual" if set(events) == {"workflow_dispatch"} else "unknown"}


def fingerprint(files, *, app_root, branch, default_branch, headers=None):
    """Project bounded source/header hints without retaining source or header values."""
    providers, signals, automation = set(), [], "unknown"
    root = app_root.strip("/") if isinstance(app_root, str) else ""
    for provider, paths in CONFIGS.items():
        if any((root + "/" if root else "") + path in files for path in paths):
            providers.add(provider);signals.append("config:" + provider)
    for path, source in list(files.items())[:64]:
        if not path.startswith(".github/workflows/") or not isinstance(source, str) or len(source) > 131072:continue
        signal = workflow_signal(source, branch, default_branch)
        if signal:
            providers.add(signal["provider"]);signals.append("workflow:" + signal["provider"])
            if signal["automation"] == "automatic":automation = "automatic"
            elif signal["automation"] == "manual" and automation != "automatic":automation = "manual"
    observed = goals.mapping(headers)
    header_provider = "vercel" if str(observed.get("server", "")).lower() == "vercel" else "netlify" if "x-nf-request-id" in observed else "cloudflare" if "cf-ray" in observed else None
    if header_provider:
        signals.append("header:" + header_provider)
        if not providers:providers.add(header_provider)
    provider = next(iter(providers)) if len(providers) == 1 else "github_actions" if providers and all(signal.startswith("workflow:") for signal in signals) else "unknown"
    return {"version": 1, "provider": provider, "automation": automation if provider != "unknown" else "unknown", "confidence": 0.9 if providers and provider != "unknown" and any(signal.startswith(("config:", "workflow:")) for signal in signals) else 0.6 if header_provider and provider != "unknown" else 0,
        "signals": sorted(set(signals))[:12]}


def read_files(connection, source):
    """Read fixed config paths and at most 16 workflows at one GitHub commit."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    require_unlocked_remote_call()
    try:
        credential = create_installation_access_token(installation_id=connection.installation_id, repository=connection.github_repo, permission_mode="read", repository_id=connection.repository_id, use_cache=False)
    except (GitHubAppTokenError, http_client.RequestException) as exc:
        from .website_github_failure import github_failure
        raise github_failure(exc,legacy_code="github_temporarily_unavailable",legacy_message="Deployment configuration access is temporarily unavailable.",legacy_retryable=True,source=True) from exc
    headers = {"Authorization": "Bearer " + credential.token, "Accept": "application/vnd.github+json"}
    base = "https://api.github.com/repos/" + connection.github_repo
    def get(path):
        response = http_client.get(base + path, headers=headers, timeout=(3, 10))
        if response.status_code == 404 and path:return None
        if response.status_code != 200:
            from .website_github_failure import github_failure
            raise github_failure(ValueError("Unproved deployment config access"),legacy_code="github_temporarily_unavailable",legacy_message="Deployment configuration could not be read.",legacy_retryable=True,response=response,source=True)
        return response.json()
    def content(path):
        value = get("/contents/" + quote(path, safe="/") + "?ref=" + source)
        if not isinstance(value, dict) or value.get("encoding") != "base64" or type(value.get("size")) is not int or not 0 <= value["size"] <= 131072:return None
        encoded = value.get("content")
        if not isinstance(encoded, str) or len(encoded) > 180000:return None
        try:return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):return None
    try:
        metadata = get("")
        if not isinstance(metadata, dict) or type(metadata.get("id")) is not int or metadata["id"] != connection.repository_id or metadata.get("full_name", "").lower() != connection.github_repo.lower():
            raise ValueError("Repository identity changed")
        files = {}
        prefix = connection.app_root.rstrip("/") + "/" if connection.app_root not in {"", "."} else ""
        for paths in CONFIGS.values():
            for path in paths:
                source_text = content(prefix + path)
                if source_text is not None:files[prefix + path] = source_text
        listing = get("/contents/.github/workflows?ref=" + source)
        if isinstance(listing, list):
            for row in listing[:16]:
                path = row.get("path") if isinstance(row, dict) else None
                if not isinstance(path, str) or PurePosixPath(path).parent.as_posix() != ".github/workflows" or not path.endswith((".yml", ".yaml")):continue
                source_text = content(path)
                if source_text is not None:files[path] = source_text
        return files, metadata.get("default_branch")
    except (http_client.RequestException, ValueError, TypeError) as exc:
        raise WebsiteAuthorityError("github_temporarily_unavailable", "Deployment configuration could not be read. It will retry.", retryable=True) from exc
    finally:
        try:http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except http_client.RequestException:pass


def ensure_fingerprint(connection, binding):
    """Reuse only a same-generation/source/root observation and fence new reads."""
    if not enabled():return None
    from .website_site_fingerprint import enabled as site_enabled, for_connection, hosting_hint
    source = binding["source_sha"]
    with authority_guard(binding, action="read") as current:
        saved = goals.mapping(current.capabilities).get("deploymentFingerprint")
        site = for_connection(current, source)
        site_digest = site['digest'] if site else None
        if valid(saved, current, source) and (not site_enabled() or saved.get('siteFingerprintDigest') == site_digest):return saved
    files, default_branch = read_files(connection, source)
    value = {**fingerprint(files, app_root=connection.app_root, branch=connection.branch, default_branch=default_branch),
        "sourceSha": source, "connectionGeneration": connection.generation, "appRoot": connection.app_root}
    hint = hosting_hint(site) if site else None
    if site:
        value['siteFingerprintDigest'] = site_digest
    if hint:
        value['signals'] = sorted(set(value.get('signals', [])) | {'site-fingerprint:' + site['digest'][:12]})[:12]
        if value['provider'] == 'unknown':
            value.update(provider=hint, confidence=0.6, automation='unknown')
        elif value['provider'] not in {hint, 'github_actions'}:
            value.update(provider='unknown', confidence=0, automation='unknown')
    with authority_guard(binding, action="read") as current:
        current.capabilities = {**goals.mapping(current.capabilities), "deploymentFingerprint": value}
        current.save(update_fields=["capabilities", "updated_at"])
    connection.capabilities = current.capabilities
    return value


def valid(value, connection, source):
    """Provider hints must have the current source and selected application scope."""
    return (isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
        and isinstance(value.get("provider"),str) and value["provider"] in INSTRUCTIONS
        and isinstance(value.get("automation"),str) and value["automation"] in {"automatic", "manual", "unknown"}
        and isinstance(source,str) and goals.SHA.fullmatch(source)
        and type(value.get("connectionGeneration")) is int and value["connectionGeneration"] == connection.generation
        and value.get("sourceSha") == source and value.get("appRoot") == connection.app_root)


def manual_action(connection, source):
    """Fixed provider instructions; no owner/provider dictionary is copied into UI."""
    value = goals.mapping(connection.capabilities).get("deploymentFingerprint")
    provider = value["provider"] if valid(value, connection, source) else "unknown"
    return {"id": "deploy_site", "label": "Deploy your site", "hosting_provider": provider,
        "instructions": INSTRUCTIONS[provider]}


def observe_headers(connection, binding, headers):
    """Keep header names as hints only, fenced to the same exact source."""
    if not enabled():return
    hint = fingerprint({}, app_root=connection.app_root, branch=connection.branch, default_branch=connection.branch, headers=headers)
    if not hint["signals"]:return
    with authority_guard(binding, action="read") as current:
        saved = goals.mapping(current.capabilities).get("deploymentFingerprint")
        if not valid(saved, current, binding["source_sha"]):return
        value = {**saved, "signals": sorted(set(saved.get("signals", [])) | set(hint["signals"]))[:12]}
        if not saved.get("signals"):
            value.update(provider=hint["provider"],confidence=hint["confidence"])
        if value == saved:return
        current.capabilities = {**goals.mapping(current.capabilities), "deploymentFingerprint": value}
        current.save(update_fields=["capabilities", "updated_at"])
    connection.capabilities = current.capabilities
