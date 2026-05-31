#!/usr/bin/env python3
"""
Keel Approvals Matrix Bot
 
A bot that polls Keel's approval endpoint and sends notifications to Matrix rooms.
Polls every 30 seconds for pending approvals and sends formatted messages only when approvals exist.
In listener mode, also runs the equivalent of "keel get approvals" every night at 11:30 PM Pacific.
Also listens for incoming messages and responds to "keel get approvals" command.
Uses Matrix username/password to obtain fresh access tokens on startup.
"""

import os
import sys
import json
import asyncio
import argparse
from datetime import datetime, timedelta
from typing import Optional, Tuple
from dataclasses import dataclass, field
from dataclasses import asdict
import base64
from zoneinfo import ZoneInfo
import re
import threading
import html
from urllib.parse import quote, unquote, urlparse, parse_qs

import httpx
from nio import AsyncClient, RoomMessageText


# Build version — set via KEEL_MATRIX_BOT_VERSION env var (injected by Dockerfile/Ansible)
BUILD_VERSION = os.environ.get("KEEL_MATRIX_BOT_VERSION", "0.0.0-dev")

# Unicode box drawing character for message separators
# NOTE: When this is used inside f-string tuple concatenation (like f"{SEPARATOR}"),
# Python bytecode causes the entire result to be repeated N times!
# ALWAYS pre-compute this outside the tuple or use in regular string concat.
SEPARATOR = "─" * 40
DEFAULT_APPROVE_BASE_URL = os.environ.get("KEEL_MATRIX_BOT_PUBLIC_URL", "http://localhost:8080").rstrip("/")
STATE_DIR = "/mem"
DEFAULT_STATE_FILE = f"{STATE_DIR}/keel_matrix_bot_state.json"


@dataclass
class Approval:
    """Represents a Keel approval."""
    id: str
    provider: str
    identifier: str
    message: str
    current_version: str
    new_version: str
    created_at: str
    deadline: Optional[str] = None
    repository_name: str = ""
    repository_tag: str = ""
    repository_digest: str = ""


async def login_to_matrix(homeserver: str, username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
    """Login to Matrix homeserver using username/password and return (user_id, access_token)."""
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/login"
    
    payload = {
        "type": "m.login.password",
        "identifier": {
            "type": "m.id.user",
            "user": username
        },
        "password": password
    }
    
    print(f"[{datetime.now().isoformat()}] [LOGIN] Attempting login to {url} as user '{username}'")
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(url, json=payload)
            print(f"[{datetime.now().isoformat()}] [LOGIN] Response: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                user_id = data.get("user_id", "")
                access_token = data.get("access_token", "")
                device_id = data.get("device_id", "")
                print(f"[{datetime.now().isoformat()}] [LOGIN] Success! User: {user_id}, Device: {device_id}")
                return user_id, access_token
            else:
                print(f"[{datetime.now().isoformat()}] [LOGIN] Failed: HTTP {response.status_code}")
                return None, None
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] [LOGIN] Error: {type(e).__name__}")
            return None, None


def parse_approvals(data: list) -> list[Approval]:
    """Parse approval data from Keel API response."""
    approvals = []
    for item in data:
        event = item.get('event') or {}
        repository = event.get('repository') or {}
        approval = Approval(
            id=item.get('id', ''),
            provider=item.get('provider', 'unknown'),
            identifier=item.get('identifier', 'unknown'),
            message=item.get('message', 'No message'),
            current_version=item.get('currentVersion', 'unknown'),
            new_version=item.get('newVersion', 'unknown'),
            created_at=item.get('createdAt', ''),
            deadline=item.get('deadline'),
            repository_name=repository.get('name', ''),
            repository_tag=repository.get('tag', ''),
            repository_digest=repository.get('digest', '')
        )
        approvals.append(approval)
    return approvals


def get_action_label(action: str) -> str:
    """Return the user-facing label for an approval action."""
    return {
        "approve": "Approve",
        "reject": "Deny",
        "deny": "Deny",
    }.get(action.lower(), action.capitalize())


def get_action_gerund(action: str) -> str:
    """Return the correctly spelled gerund for an approval action."""
    return {
        "approve": "Approving",
        "reject": "Rejecting",
        "deny": "Denying",
    }.get(action.lower(), f"{action.capitalize()}ing")


def normalize_approval_action(action: str) -> str:
    """Normalize UI approval actions to the values expected by Keel."""
    normalized = action.lower().strip()
    return "reject" if normalized == "deny" else normalized


def format_approval_message(
    approval: Approval,
    approve_base_url: str = DEFAULT_APPROVE_BASE_URL,
) -> str:
    """Format an approval as a readable Matrix message."""
    # Get release notes URL for this identifier (returns N/A if not set)
    release_notes_url = get_release_notes_url(approval)
    release_notes_key = get_release_notes_key(approval)
    effective_image = get_effective_image_reference(approval)
    
    lines = [
        "📦 **Update Approval Required**",
        "",
        f"**Application:** `{approval.identifier}`",
    ]

    if effective_image:
        lines.append(f"**Image:** `{effective_image}`")

    lines.extend([
        f"**Update:** `{approval.current_version}` → `{approval.new_version}`",
        f"**Provider:** {approval.provider}",
        "",
        f"__{approval.message}__",
        "",
        f"🕐 Created: {approval.created_at[:19]}" if approval.created_at else "",
    ])
    
    if approval.deadline:
        try:
            deadline_clean = approval.deadline[:19]
            lines.append(f"⏰ Deadline: {deadline_clean}")
        except:
            pass
    
    # Add Release Notes URL
    lines.append(f"📝 Release Notes URL ({release_notes_key}): {release_notes_url}")
    
    approve_url = get_approval_action_url(approval.identifier, "approve", approve_base_url)
    deny_url = get_approval_action_url(approval.identifier, "deny", approve_base_url)
    image_name = get_image_name_from_identifier(approval.identifier)

    lines.extend([
        "",
        f"[Approve {image_name}]({approve_url}) | [Deny {image_name}]({deny_url})",
        f"Reply with `approve {approval.identifier}` or `reject {approval.identifier}` to take action.",
        "",
        "─" * 40
    ])
    
    return "\n".join(line for line in lines if line)


def get_approval_action_url(
    identifier: str,
    action: str,
    base_url: str = DEFAULT_APPROVE_BASE_URL,
) -> str:
    """Build the clickable approval action URL for a given approval identifier."""
    encoded_identifier = quote(identifier, safe="")
    normalized_action = normalize_approval_action(action)
    path = "approve" if normalized_action == "approve" else "deny"
    return f"{base_url.rstrip('/')}/{path}?identifier={encoded_identifier}"


def get_approve_action_url(identifier: str, base_url: str = DEFAULT_APPROVE_BASE_URL) -> str:
    """Build the clickable approval URL for a given approval identifier."""
    return get_approval_action_url(identifier, "approve", base_url)


def get_deny_action_url(identifier: str, base_url: str = DEFAULT_APPROVE_BASE_URL) -> str:
    """Build the clickable denial URL for a given approval identifier."""
    return get_approval_action_url(identifier, "deny", base_url)


def render_async_approval_action_page(identifier: str, action: str) -> str:
    """Render a page that asynchronously calls the approval API without redirecting."""
    encoded_identifier = quote(identifier, safe="")
    safe_identifier = html.escape(identifier)
    normalized_action = normalize_approval_action(action)
    path = "approve" if normalized_action == "approve" else "deny"
    label = get_action_label(normalized_action)
    label_lower = label.lower()
    api_path = f"/api/{path}?identifier={encoded_identifier}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{label} Update</title>
  <style>
    body {{
      font-family: sans-serif;
      margin: 0;
      background: #f5f7fb;
      color: #1f2937;
    }}
    main {{
      max-width: 42rem;
      margin: 4rem auto;
      padding: 2rem;
      background: #ffffff;
      border: 1px solid #dbe3f0;
      border-radius: 12px;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}
    h1 {{
      margin-top: 0;
      font-size: 1.5rem;
    }}
    code {{
      word-break: break-word;
    }}
    #status {{
      margin-top: 1rem;
      padding: 0.875rem 1rem;
      border-radius: 8px;
      background: #eef2ff;
    }}
    .pending {{ background: #eef2ff; }}
    .success {{ background: #ecfdf5; }}
    .error {{ background: #fef2f2; }}
  </style>
</head>
<body>
  <main>
    <h1>Submitting {label_lower}</h1>
    <p>Approval target: <code>{safe_identifier}</code></p>
    <div id="status" class="pending">Sending request...</div>
  </main>
  <script>
    const statusEl = document.getElementById("status");
    async function submitApprovalAction() {{
      try {{
        const response = await fetch("{api_path}", {{
          method: "POST",
          headers: {{ "Accept": "application/json" }}
        }});
        const payload = await response.json();
        statusEl.textContent = payload.message || "{label} request completed.";
        statusEl.className = response.ok ? "success" : "error";
      }} catch (error) {{
        statusEl.textContent = "{label} request failed: " + error;
        statusEl.className = "error";
      }}
    }}
    void submitApprovalAction();
  </script>
</body>
</html>"""


def render_async_approve_page(identifier: str) -> str:
    """Render a page that asynchronously calls the approval API without redirecting."""
    return render_async_approval_action_page(identifier, "approve")


def get_image_name_from_identifier(identifier: str) -> str:
    """Extract a readable image name from a Keel approval identifier."""
    last_segment = identifier.split("/")[-1] if "/" in identifier else identifier
    image_name = last_segment.split(":")[0]
    return image_name or identifier


def get_resource_name_from_identifier(identifier: str) -> str:
    """Extract the Kubernetes resource name from a Keel approval identifier."""
    return get_image_name_from_identifier(identifier)


def normalize_repository_name(repository_name: str) -> str:
    """Return a readable repository name without Docker Hub registry noise."""
    repository_name = (repository_name or "").strip()
    for prefix in ("index.docker.io/", "docker.io/"):
        if repository_name.startswith(prefix):
            repository_name = repository_name[len(prefix):]
            break
    return repository_name


def get_repository_image_name(approval: Approval) -> str:
    """Return the image basename from the Keel event repository."""
    repository_name = normalize_repository_name(approval.repository_name)
    if not repository_name:
        return ""
    return repository_name.rstrip("/").split("/")[-1]


def get_effective_image_reference(approval: Approval) -> str:
    """Return the image that Keel says changed, including tag when available."""
    repository_name = normalize_repository_name(approval.repository_name)
    if not repository_name:
        return ""
    tag = approval.repository_tag or approval.new_version
    if tag and ":" not in repository_name.rsplit("/", 1)[-1]:
        return f"{repository_name}:{tag}"
    return repository_name


def get_release_notes_key(approval: Approval) -> str:
    """Return the preferred release-notes key for this approval.

    Keel identifies approvals by Kubernetes resource, so a multi-container
    deployment can look like deployment/firefox/firefox:latest even when the
    changed image is linuxserver/wireguard:latest. For that case, use
    resource/image (firefox/wireguard) so users can configure per-image notes.
    """
    resource_name = get_resource_name_from_identifier(approval.identifier)
    repository_image = get_repository_image_name(approval)
    if resource_name and repository_image and repository_image.lower() != resource_name.lower():
        return f"{resource_name}/{repository_image}"
    return resource_name or approval.identifier


def get_release_notes_lookup_keys(target) -> list[str]:
    """Return release-note lookup keys for an Approval or raw identifier."""
    if isinstance(target, Approval):
        keys = [
            target.identifier,
            get_release_notes_key(target),
            get_resource_name_from_identifier(target.identifier),
            get_repository_image_name(target),
            normalize_repository_name(target.repository_name),
            get_effective_image_reference(target),
        ]
    else:
        identifier = str(target)
        keys = [
            identifier,
            get_resource_name_from_identifier(identifier),
        ]

    seen = set()
    normalized_keys = []
    for key in keys:
        if not key:
            continue
        key = str(key).strip()
        if not key:
            continue
        lower_key = key.lower()
        if lower_key in seen:
            continue
        seen.add(lower_key)
        normalized_keys.append(key)
    return normalized_keys


def format_approvals_list(
    approvals: list[Approval],
    approval_timestamps: dict = None,
    approve_base_url: str = DEFAULT_APPROVE_BASE_URL
) -> str:
    """Format a list of approvals for display.
    
    Args:
        approvals: List of Approval objects to format
        approval_timestamps: Optional dict mapping approval identifier to first_seen timestamp
    """
    if not approvals:
        return "✅ No pending approvals at this time."
    
    lines = [
        f"📋 **Pending Approvals ({len(approvals)})**",
        "",
    ]
    
    for i, approval in enumerate(approvals, 1):
        # Calculate days pending using created_at from Keel (not bot's tracking timestamp)
        # This ensures accurate pending time even if the bot was just restarted
        days_pending = ""
        if approval.created_at:
            try:
                # Parse the created_at timestamp (format: "2026-04-14T16:23:31Z")
                # Replace Z with +00:00 to indicate UTC timezone
                created_at_clean = approval.created_at.replace('Z', '+00:00')
                created_at_dt = datetime.fromisoformat(created_at_clean)
                # Ensure timezone-aware datetime in UTC
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=ZoneInfo("UTC"))
                # Get current time in UTC for accurate comparison
                now_utc = datetime.now(ZoneInfo("UTC"))
                delta = now_utc - created_at_dt
                total_days = delta.total_seconds() / 86400  # Convert to decimal days
                days_pending = f"⏳ Pending: {total_days:.2f} days"
            except (ValueError, TypeError, Exception):
                pass
        
        lines.extend([
            f"**{i}. {approval.identifier}**",
            f"   Update: `{approval.current_version}` → `{approval.new_version}`",
            f"   Provider: {approval.provider}",
            f"   Created: {approval.created_at[:19] if approval.created_at else 'Unknown'}",
        ])

        effective_image = get_effective_image_reference(approval)
        if effective_image:
            lines.append(f"   Image: `{effective_image}`")

        approve_url = get_approve_action_url(approval.identifier, approve_base_url)
        deny_url = get_deny_action_url(approval.identifier, approve_base_url)
        image_name = get_image_name_from_identifier(approval.identifier)
        lines.append(f"   [Approve {image_name}]({approve_url}) | [Deny {image_name}]({deny_url})")
        
        if days_pending:
            lines.append(f"   {days_pending}")
        
        # Always show a release notes row so nightly reports have the same
        # fields for every approval, even before a URL has been configured.
        release_notes_key = get_release_notes_key(approval)
        release_notes_url = get_release_notes_url(approval)
        lines.append(f"   📝 Release Notes ({release_notes_key}): {release_notes_url}")
        
        lines.append("")
    
    lines.append("─" * 40)
    return "\n".join(lines)


async def fetch_pending_approvals(keel_url: str, username: str = "", password: str = "", timeout: int = 30) -> tuple[list[Approval], dict]:
    """Fetch pending approvals from Keel API without retaining authenticated response data."""
    url = f"{keel_url.rstrip('/')}/v1/approvals"
    headers = {}
    
    # Add Basic Auth if credentials provided
    if username and password:
        auth_string = f"{username}:{password}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()
        headers["Authorization"] = f"Basic {auth_bytes}"
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(url, headers=headers)
            print(f"[{datetime.now().isoformat()}] Approval API response: HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            
            if not isinstance(data, list):
                print(f"[{datetime.now().isoformat()}] Unexpected response format: {type(data)}")
                return [], {"status": response.status_code, "error": "unexpected_response_format"}
            
            approvals = parse_approvals(data)
            print(f"[{datetime.now().isoformat()}] Parsed {len(approvals)} approvals")
            return approvals, {"status": response.status_code}
        except httpx.HTTPStatusError as e:
            print(f"[{datetime.now().isoformat()}] HTTP error fetching approvals: {e.response.status_code}")
            return [], {"status": e.response.status_code, "error": "http_error"}
        except httpx.RequestError as e:
            print(f"[{datetime.now().isoformat()}] Request error fetching approvals: {type(e).__name__}")
            return [], {"error": "request_error"}
        except json.JSONDecodeError:
            print(f"[{datetime.now().isoformat()}] JSON decode error fetching approvals")
            return [], {"error": "invalid_json"}


async def send_matrix_message(
    homeserver: str,
    user_id: str,
    access_token: str,
    room_id: str,
    message: str
) -> bool:
    """Send a message to a Matrix room using the Matrix REST API directly."""
    print(f"[{datetime.now().isoformat()}] [DEBUG] Connecting to Matrix homeserver: {homeserver}")
    
    import uuid
    txn_id = uuid.uuid4().hex
    
    # Keep authentication in a header so it cannot leak through URLs or exceptions.
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"

    def markdown_links_to_html(text: str) -> str:
        """Render Markdown-style links to Matrix formatted HTML."""
        pattern = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
        output = []
        last_index = 0
        for match in pattern.finditer(text):
            output.append(html.escape(text[last_index:match.start()]))
            label = html.escape(match.group(1))
            href = html.escape(match.group(2), quote=True)
            output.append(f'<a href="{href}">{label}</a>')
            last_index = match.end()
        output.append(html.escape(text[last_index:]))
        return "".join(output).replace("\n", "<br>")

    formatted_body = markdown_links_to_html(message)
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            print(f"[{datetime.now().isoformat()}] [DEBUG] Sending message to room: {room_id}")
            response = await client.put(
                url,
                json={
                    "msgtype": "m.text",
                    "body": message,
                    "format": "org.matrix.custom.html",
                    "formatted_body": formatted_body
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )
            
            print(f"[{datetime.now().isoformat()}] [DEBUG] Matrix API Response: HTTP {response.status_code}")
            
            if response.status_code in (200, 201):
                print(f"[{datetime.now().isoformat()}] [DEBUG] Message sent successfully to room {room_id}")
                return True
            else:
                print(f"[{datetime.now().isoformat()}] [DEBUG] Failed to send message: HTTP {response.status_code}")
                return False
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] [DEBUG] Error sending message: {type(e).__name__}")
            return False


async def resolve_matrix_room_reference(
    homeserver: str,
    access_token: str,
    room_reference: str
) -> str:
    """Resolve a Matrix room alias to a room ID and join it if needed."""
    room_reference = room_reference.strip()
    if not room_reference or not room_reference.startswith("#"):
        return room_reference

    join_url = (
        f"{homeserver.rstrip('/')}/_matrix/client/v3/join/"
        f"{quote(room_reference, safe='')}"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print(f"[{datetime.now().isoformat()}] Resolving Matrix room alias: {room_reference}")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(join_url, headers=headers, json={})
            if response.status_code == 200:
                response_data = response.json()
                resolved_room_id = response_data.get("room_id", room_reference)
                print(
                    f"[{datetime.now().isoformat()}] Joined Matrix room alias "
                    f"{room_reference} as {resolved_room_id}"
                )
                return resolved_room_id

            print(
                f"[{datetime.now().isoformat()}] Failed to join Matrix room alias "
                f"{room_reference}: HTTP {response.status_code}"
            )
        except Exception as e:
            print(
                f"[{datetime.now().isoformat()}] Exception while joining Matrix room alias "
                f"{room_reference}: {type(e).__name__}"
            )

    return room_reference


def resolve_state_file(state_file: Optional[str] = None, allow_non_mem_state_file: bool = False) -> str:
    """Resolve the bot state file path.

    Production memory must live under /mem so the NFS-backed state survives pod
    restarts. Tests can explicitly opt into temporary state files.
    """
    resolved = state_file or os.environ.get("KEEL_MATRIX_BOT_STATE_FILE", DEFAULT_STATE_FILE)
    resolved = os.path.abspath(resolved)
    mem_dir = os.path.abspath(STATE_DIR)

    if allow_non_mem_state_file:
        return resolved

    if resolved == mem_dir or resolved.startswith(f"{mem_dir}{os.sep}"):
        return resolved

    print(
        f"[{datetime.now().isoformat()}] Ignoring non-/mem approval memory path; "
        f"using {DEFAULT_STATE_FILE}"
    )
    return DEFAULT_STATE_FILE


class ApprovalMemory:
    """
    Persistent memory for tracking approval notifications.
    Saves state to a JSON file so it survives bot restarts.
    
    Tracks:
    - notified_approvals: Set of Keel approval IDs that have been notified
    - approval_timestamps: Map of approval ID to when it was first seen
    - approval_identifiers: Map of approval ID to approval identifier
    - release_notes_urls: Map of approval identifier to release notes URL
    - auto_approve_targets: Set of keywords to auto-approve when seen in pending approvals
    """
    
    def __init__(self, state_file: Optional[str] = None, allow_non_mem_state_file: bool = False):
        self.state_file = resolve_state_file(state_file, allow_non_mem_state_file)
        self._lock = threading.Lock()
        self.notified_approvals: set = set()
        self.approval_timestamps: dict = {}  # approval_id -> first_seen_timestamp
        self.approval_identifiers: dict = {}  # approval_id -> identifier
        self.release_notes_urls: dict = {}  # identifier -> release_notes_url
        self.auto_approve_targets: set = set()  # keywords/identifiers to auto-approve
        self._load_state()
    
    def _load_state(self):
        """Load state from the state file."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.notified_approvals = set(data.get('notified_approvals', []))
                    self.approval_timestamps = data.get('approval_timestamps', {})
                    self.approval_identifiers = data.get('approval_identifiers', {})
                    self.release_notes_urls = data.get('release_notes_urls', {})
                    self.auto_approve_targets = set(data.get('auto_approve_targets', []))
                    print(
                        f"[{datetime.now().isoformat()}] Loaded approval memory: "
                        f"{len(self.notified_approvals)} notified approvals, "
                        f"{len(self.release_notes_urls)} release notes URLs, "
                        f"{len(self.auto_approve_targets)} auto-approve target(s)"
                    )
        except (json.JSONDecodeError, IOError) as e:
            print(f"[{datetime.now().isoformat()}] Failed to load approval memory: {e}")
            self.notified_approvals = set()
            self.approval_timestamps = {}
            self.approval_identifiers = {}
            self.release_notes_urls = {}
            self.auto_approve_targets = set()
    
    def _save_state(self):
        """Save state to the state file."""
        try:
            state_dir = os.path.dirname(self.state_file)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            data = {
                'notified_approvals': list(self.notified_approvals),
                'approval_timestamps': self.approval_timestamps,
                'approval_identifiers': self.approval_identifiers,
                'release_notes_urls': self.release_notes_urls,
                'auto_approve_targets': sorted(self.auto_approve_targets)
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"[{datetime.now().isoformat()}] Failed to save approval memory: {e}")
    
    def is_approved(self, approval: Approval) -> bool:
        """Check if an approval has already been notified."""
        return approval.id in self.notified_approvals
    
    def get_new_approvals(self, approvals: list[Approval]) -> list[Approval]:
        """Return only approvals that haven't been notified yet."""
        with self._lock:
            new_approvals = []
            now = datetime.now().isoformat()
            for approval in approvals:
                self.approval_identifiers[approval.id] = approval.identifier
                if approval.id not in self.approval_timestamps:
                    self.approval_timestamps[approval.id] = now
                if approval.id not in self.notified_approvals:
                    new_approvals.append(approval)
            return new_approvals
    
    def mark_as_notified(self, approvals: list[Approval]):
        """Mark these approvals as notified to avoid duplicate messages."""
        with self._lock:
            for approval in approvals:
                self.notified_approvals.add(approval.id)
                self.approval_identifiers[approval.id] = approval.identifier
                if approval.id not in self.approval_timestamps:
                    self.approval_timestamps[approval.id] = datetime.now().isoformat()
            self._save_state()
    
    def clear(self):
        """Clear all tracked approvals (call at start of day for daily summary)."""
        with self._lock:
            self.notified_approvals.clear()
            self.approval_timestamps.clear()
            self.approval_identifiers.clear()
            self._save_state()
            print(f"[{datetime.now().isoformat()}] Cleared approval memory tracking")
    
    def refresh_from_approvals(self, approvals: list[Approval]):
        """Reconcile memory with the current Keel approval list.

        Removes stale approvals that no longer exist in Keel and records first-seen
        timestamps for current approvals. It intentionally does not mark current
        approvals as already notified; persisted state is what suppresses duplicates
        across restarts.
        """
        with self._lock:
            current_ids = {approval.id for approval in approvals}
            now = datetime.now().isoformat()
            for approval in approvals:
                self.approval_identifiers[approval.id] = approval.identifier
                if approval.id not in self.approval_timestamps:
                    self.approval_timestamps[approval.id] = now

            ids_to_remove = set(self.approval_identifiers.keys()) - current_ids
            ids_to_remove.update(set(self.notified_approvals) - current_ids)
            ids_to_remove.update(set(self.approval_timestamps.keys()) - current_ids)

            for approval_id in ids_to_remove:
                identifier = self.approval_identifiers.get(approval_id, approval_id)
                self.notified_approvals.discard(approval_id)
                self.approval_timestamps.pop(approval_id, None)
                self.approval_identifiers.pop(approval_id, None)
                print(f"[{datetime.now().isoformat()}] Removed stale approval from memory: {identifier} ({approval_id})")

            self._save_state()
            print(
                f"[{datetime.now().isoformat()}] Refreshed approval memory: "
                f"{len(current_ids)} current approvals, {len(self.notified_approvals)} notified approvals tracked, "
                f"{len(ids_to_remove)} stale approvals removed"
            )
    
    def remove_approval(self, identifier: str):
        """Remove an approval from tracking (e.g., when it's approved/rejected/archived)."""
        with self._lock:
            ids_to_remove = [
                approval_id for approval_id, stored_identifier in self.approval_identifiers.items()
                if stored_identifier == identifier
            ]
            for approval_id in ids_to_remove:
                self.notified_approvals.discard(approval_id)
                self.approval_timestamps.pop(approval_id, None)
                self.approval_identifiers.pop(approval_id, None)
            self._save_state()
            print(f"[{datetime.now().isoformat()}] Removed '{identifier}' from approval memory ({len(ids_to_remove)} approval id(s))")

    def add_auto_approve_target(self, target: str):
        """Add an auto-approve keyword/identifier target."""
        normalized = target.strip()
        if not normalized:
            return
        with self._lock:
            self.auto_approve_targets.add(normalized)
            self._save_state()
            print(f"[{datetime.now().isoformat()}] Added auto-approve target: {normalized}")

    def get_auto_approve_targets(self) -> list[str]:
        """Return auto-approve targets."""
        with self._lock:
            return sorted(self.auto_approve_targets)

    def get_matching_auto_targets(self, identifier: str) -> list[str]:
        """Return auto-approve targets matching this approval identifier."""
        identifier_lower = identifier.lower()
        with self._lock:
            return sorted(
                target for target in self.auto_approve_targets
                if target.lower() in identifier_lower or identifier_lower in target.lower()
            )


# Global approval memory instance
approval_memory = ApprovalMemory()


def get_new_approvals(approvals: list[Approval]) -> list[Approval]:
    """Return only approvals that haven't been notified yet."""
    return approval_memory.get_new_approvals(approvals)

def mark_as_notified(approvals: list[Approval]):
    """Mark these approvals as notified to avoid duplicate messages."""
    approval_memory.mark_as_notified(approvals)

def clear_notified_approvals():
    """Clear the set of notified approvals (call at start of day for daily summary)."""
    approval_memory.clear()

def remove_from_memory(identifier: str):
    """Remove an approval from memory (e.g., when it's been approved/rejected)."""
    approval_memory.remove_approval(identifier)

def set_release_notes_url(identifier: str, url: str):
    """Set the release notes URL for an approval identifier."""
    approval_memory.release_notes_urls[identifier] = url
    approval_memory._save_state()
    print(f"[{datetime.now().isoformat()}] Set release notes URL for '{identifier}': {url}")

def set_auto_approve_target(target: str):
    """Add an auto-approve target keyword/identifier."""
    approval_memory.add_auto_approve_target(target)

def get_auto_approve_targets() -> list[str]:
    """Return configured auto-approve targets."""
    return approval_memory.get_auto_approve_targets()

def get_matching_auto_targets(identifier: str) -> list[str]:
    """Return configured auto-approve targets matching an identifier."""
    return approval_memory.get_matching_auto_targets(identifier)

def get_release_notes_url(target) -> str:
    """Get the release notes URL for an approval or identifier. Returns 'N/A' if not set.
    
    Handles partial matching - if exact identifier not found, looks for keys
    that are contained within the identifier or vice versa. This allows storing
    'llama-cpp-amd' and matching 'deployment/llama-cpp-amd/llama-cpp-amd:latest'.
    When an Approval includes event.repository data, it also checks keys like
    'firefox/wireguard' for multi-image deployments.
    """
    lookup_keys = get_release_notes_lookup_keys(target)

    # Try exact match first against every derived key.
    for key in lookup_keys:
        if key in approval_memory.release_notes_urls:
            return approval_memory.release_notes_urls[key]
    
    # Try partial matching - check if any stored key is contained in the identifier
    # or if the identifier is contained in any stored key
    lookup_keys_lower = [key.lower() for key in lookup_keys]
    for key, url in approval_memory.release_notes_urls.items():
        key_lower = key.lower()
        if any(key_lower in lookup_key or lookup_key in key_lower for lookup_key in lookup_keys_lower):
            return url
    
    return "N/A"

async def poll_and_notify(
    keel_url: str,
    homeserver: str,
    user_id: str,
    access_token: str,
    room_id: str,
    keel_username: str = "",
    keel_password: str = "",
    dry_run: bool = False
) -> bool:
    """Poll Keel for approvals and send notifications to Matrix."""
    approvals, raw_response = await fetch_pending_approvals(keel_url, keel_username, keel_password)
    
    # Always log the approval listing result
    print(f"[{datetime.now().isoformat()}] Approval listing result: {raw_response}")
    
    # Always log the status
    if approvals:
        print(f"[{datetime.now().isoformat()}] Found {len(approvals)} pending approvals:")
        for approval in approvals:
            print(f"  - {approval.identifier}: {approval.current_version} -> {approval.new_version}")
    else:
        print(f"[{datetime.now().isoformat()}] No pending approvals found.")
    
    # Reconcile current approvals so stale entries do not suppress future alerts.
    approval_memory.refresh_from_approvals(approvals)

    # Only send notifications for NEW approvals (not already notified)
    new_approvals = get_new_approvals(approvals)
    
    if not new_approvals:
        print(f"[{datetime.now().isoformat()}] No new approvals to notify about.")
        return True
    
    print(f"[{datetime.now().isoformat()}] Found {len(new_approvals)} NEW approvals to notify:")
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    response_status = raw_response.get("status", "N/A")
    
    summary_lines = [
        f"📦 **Keel Approval Alert** ({now})",
        f"**HTTP Status:** {response_status}",
        f"**Pending approvals:** {len(new_approvals)}",
        "",
    ]
    
    for approval in new_approvals:
        summary_lines.append(format_approval_message(approval))
    
    message = "\n".join(summary_lines)
    
    if dry_run:
        print(f"[DRY RUN] Would send to Matrix:")
        print(message)
    else:
        success = await send_matrix_message(homeserver, user_id, access_token, room_id, message)
        if success:
            # Only mark as notified AFTER successful send
            mark_as_notified(new_approvals)
            print(f"[{datetime.now().isoformat()}] Successfully notified about {len(new_approvals)} approvals")
        else:
            # If sending failed, allow retry on next poll
            print(f"[{datetime.now().isoformat()}] Failed to send notification, will retry on next poll")
    
    return True


def filter_approvals_by_date(approvals: list[Approval], date: datetime) -> list[Approval]:
    """Filter approvals to only include those created on the specified date."""
    filtered = []
    for approval in approvals:
        if approval.created_at:
            try:
                created_date = datetime.fromisoformat(approval.created_at.replace('Z', '+00:00'))
                if created_date.date() == date.date():
                    filtered.append(approval)
            except (ValueError, AttributeError):
                continue
    return filtered


async def send_daily_summary(
    keel_url: str,
    homeserver: str,
    user_id: str,
    access_token: str,
    room_id: str,
    keel_username: str = "",
    keel_password: str = "",
    dry_run: bool = False,
    approve_base_url: str = DEFAULT_APPROVE_BASE_URL
) -> bool:
    """Send a daily summary of all approvals found today at 11:30 PM PST."""
    pst = ZoneInfo("America/Los_Angeles")
    now_pst = datetime.now(pst)
    today_start = now_pst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_pst.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Fetch all pending approvals
    approvals, _ = await fetch_pending_approvals(keel_url, keel_username, keel_password)
    
    # Filter approvals created today (in PST)
    today_approvals = []
    for approval in approvals:
        if approval.created_at:
            try:
                created_date = datetime.fromisoformat(approval.created_at.replace('Z', '+00:00'))
                created_date_pst = created_date.astimezone(pst)
                if today_start <= created_date_pst <= today_end:
                    today_approvals.append(approval)
            except (ValueError, AttributeError):
                continue
    
    # Format the daily summary message
    summary_date = now_pst.strftime("%Y-%m-%d")
    summary_time = now_pst.strftime("%H:%M:%S %Z")
    
    if not today_approvals:
        message = (
            f"📊 **Daily Approval Summary for {summary_date}**\n"
            f"⏰ Generated at: {summary_time}\n"
            f"\n"
            f"✅ No approvals were created today.\n"
            f"\n"
            f"{SEPARATOR}"
        )
    else:
        summary_lines = [
            f"📊 **Daily Approval Summary for {summary_date}**",
            f"⏰ Generated at: {summary_time}",
            f"📦 Total approvals created today: {len(today_approvals)}",
            "",
        ]

        approval_list = format_approvals_list(
            today_approvals,
            approval_memory.approval_timestamps,
            approve_base_url=approve_base_url
        )
        approval_lines = approval_list.split("\n")
        if approval_lines and approval_lines[0].startswith("📋 **Pending Approvals"):
            approval_lines = approval_lines[2:]
        summary_lines.extend(approval_lines)
        message = "\n".join(summary_lines)
    
    print(f"[{datetime.now().isoformat()}] Daily summary: {len(today_approvals)} approvals found today")
    
    if dry_run:
        print(f"[DRY RUN] Would send daily summary to Matrix:")
        print(message)
    else:
        await send_matrix_message(homeserver, user_id, access_token, room_id, message)
    
    return True


def get_time_until_next_summary() -> float:
    """Calculate seconds until the next 11:30 PM PST."""
    pst = ZoneInfo("America/Los_Angeles")
    now_pst = datetime.now(pst)
    
    # Calculate the next 11:30 PM PST
    next_summary = now_pst.replace(hour=23, minute=30, second=0, microsecond=0)
    
    # If it's already past 11:30 PM today, schedule for tomorrow
    if now_pst >= next_summary:
        next_summary = (now_pst + timedelta(days=1)).replace(hour=23, minute=30, second=0, microsecond=0)
    
    # Calculate seconds until next summary
    delta = next_summary - now_pst
    return delta.total_seconds()


class KeelMatrixBot:
    """Matrix bot that listens for commands and fetches approvals on demand."""
    
    def __init__(
        self,
        homeserver: str,
        matrix_username: str,
        matrix_password: str,
        room_id: str,
        keel_url: str,
        keel_username: str = "",
        keel_password: str = ""
    ):
        self.homeserver = homeserver
        self.matrix_username = matrix_username
        self.matrix_password = matrix_password
        self.user_id = None  # Will be obtained from login
        self.access_token = None  # Will be obtained from login
        self.room_reference = room_id
        self.room_id = room_id
        self.keel_url = keel_url
        self.keel_username = keel_username
        self.keel_password = keel_password
        self.client = None
        self.bot_user_id = None
        self.processed_events = set()  # Track processed events to avoid duplicates
        self.recently_processed_approvals = set()  # Track recent approvals to prevent duplicate responses
        self.public_base_url = os.environ.get("KEEL_MATRIX_BOT_PUBLIC_URL", DEFAULT_APPROVE_BASE_URL).rstrip("/")
        self.http_host = os.environ.get("KEEL_MATRIX_BOT_HTTP_HOST", "0.0.0.0")
        self.http_port = int(os.environ.get("KEEL_MATRIX_BOT_HTTP_PORT", "8080"))
        self.http_server = None
        self.pacific_tz = ZoneInfo("America/Los_Angeles")
        self.next_nightly_get_approvals_at = self._calculate_next_nightly_get_approvals()
        self.nightly_scheduler_task = None

    def _calculate_next_nightly_get_approvals(self, now_pacific: Optional[datetime] = None) -> datetime:
        """Return the next nightly schedule time (11:30 PM Pacific)."""
        now = now_pacific or datetime.now(self.pacific_tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.pacific_tz)
        else:
            now = now.astimezone(self.pacific_tz)

        next_run = now.replace(hour=23, minute=30, second=0, microsecond=0)
        if now >= next_run:
            next_run = (now + timedelta(days=1)).replace(hour=23, minute=30, second=0, microsecond=0)
        return next_run

    async def _nightly_get_approvals_scheduler(self):
        """Run `keel get approvals` every night at 11:30 PM Pacific."""
        while True:
            now_pacific = datetime.now(self.pacific_tz)
            if now_pacific.tzinfo is None:
                now_pacific = now_pacific.replace(tzinfo=self.pacific_tz)
            else:
                now_pacific = now_pacific.astimezone(self.pacific_tz)

            self.next_nightly_get_approvals_at = self._calculate_next_nightly_get_approvals(now_pacific)
            sleep_seconds = max(
                1,
                int((self.next_nightly_get_approvals_at - now_pacific).total_seconds())
            )
            print(
                f"[{datetime.now().isoformat()}] Nightly scheduler sleeping until "
                f"{self.next_nightly_get_approvals_at.isoformat()} "
                f"({sleep_seconds} seconds)"
            )
            await asyncio.sleep(sleep_seconds)

            try:
                print(
                    f"[{datetime.now().isoformat()}] Running scheduled nightly command "
                    f"'keel get approvals' (Pacific 11:30 PM)"
                )
                await self.handle_get_approvals(self.room_id)
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Nightly scheduler error: {e}")
    
    async def _cleanup_old_deduplication_entries(self):
        """Periodically clean up old deduplication entries to prevent memory growth."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            # Old entries are automatically filtered out by the timestamp in the key
            # Just limit the set size to prevent unbounded growth
            if len(self.recently_processed_approvals) > 100:
                # Keep only the most recent 50 entries
                self.recently_processed_approvals = set(list(self.recently_processed_approvals)[-50:])
                print(f"[{datetime.now().isoformat()}] Cleaned up old deduplication entries")

    def _should_send_message(self, room_id: str, message_type: str, action: str, identifier: str) -> bool:
        """Return False when the same result message was already sent recently."""
        dedup_key = f"{room_id}:{message_type}:{action}:{identifier}".lower()
        if dedup_key in self.recently_processed_approvals:
            print(f"[{datetime.now().isoformat()}] Skipping duplicate message: {dedup_key}")
            return False

        self.recently_processed_approvals.add(dedup_key)
        return True

    async def start_http_listener(self):
        """Start HTTP listener for click-to-approve links."""
        self.http_server = await asyncio.start_server(
            self._handle_http_connection,
            self.http_host,
            self.http_port
        )
        print(
            f"[{datetime.now().isoformat()}] HTTP listener started on "
            f"{self.http_host}:{self.http_port} (public base URL: {self.public_base_url})"
        )

    async def process_http_approval_action_request(self, identifier: str, action: str) -> tuple[int, str]:
        """Process an HTTP approval action request and return (status_code, message)."""
        clean_identifier = identifier.strip()
        if not clean_identifier:
            return 400, "Missing identifier query parameter."

        normalized_action = normalize_approval_action(action)
        if normalized_action not in {"approve", "reject"}:
            return 400, "Unsupported approval action."

        # Reuse existing approval flow so behavior stays consistent with Matrix commands.
        await self.handle_approve_reject(self.room_id, clean_identifier, normalized_action)
        return 200, f"{get_action_label(normalized_action)} request submitted for '{clean_identifier}'."

    async def process_http_approve_request(self, identifier: str) -> tuple[int, str]:
        """Process an HTTP approval request and return (status_code, message)."""
        return await self.process_http_approval_action_request(identifier, "approve")

    def build_http_approval_action_page(self, identifier: str, action: str) -> tuple[int, str, str]:
        """Return the HTML page that triggers async approval action submission."""
        clean_identifier = identifier.strip()
        if not clean_identifier:
            return 400, "Missing identifier query parameter.", "text/plain"

        normalized_action = normalize_approval_action(action)
        if normalized_action not in {"approve", "reject"}:
            return 400, "Unsupported approval action.", "text/plain"

        return 200, render_async_approval_action_page(clean_identifier, normalized_action), "text/html"

    def build_http_approve_page(self, identifier: str) -> tuple[int, str, str]:
        """Return the HTML page that triggers async approval submission."""
        return self.build_http_approval_action_page(identifier, "approve")

    async def _write_http_response(self, writer, status_code: int, body: str, content_type: str):
        """Write a minimal HTTP/1.1 response to the socket."""
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }.get(status_code, "OK")
        body_bytes = body.encode("utf-8")
        headers = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            f"Content-Type: {content_type}; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(headers.encode("utf-8") + body_bytes)
        await writer.drain()

    async def _handle_http_connection(self, reader, writer):
        """Handle HTTP requests for health and click-to-approve endpoints."""
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return

            parts = request_line.decode("utf-8", errors="ignore").strip().split()
            if len(parts) < 2:
                await self._write_http_response(writer, 400, "Malformed request line.", "text/plain")
                writer.close()
                await writer.wait_closed()
                return

            method, raw_target = parts[0], parts[1]

            # Read and ignore headers.
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break

            method = method.upper()
            if method not in {"GET", "POST"}:
                await self._write_http_response(writer, 405, "Only GET and POST are supported.", "text/plain")
                writer.close()
                await writer.wait_closed()
                return

            parsed = urlparse(raw_target)
            path = parsed.path or "/"
            query = parse_qs(parsed.query)

            if path == "/healthz":
                await self._write_http_response(writer, 200, "ok", "text/plain")
            elif path == "/approve" and method == "GET":
                identifier = query.get("identifier", [""])[0]
                identifier = unquote(identifier)
                status_code, body, content_type = self.build_http_approval_action_page(identifier, "approve")
                await self._write_http_response(writer, status_code, body, content_type)
            elif path in {"/deny", "/reject"} and method == "GET":
                identifier = query.get("identifier", [""])[0]
                identifier = unquote(identifier)
                status_code, body, content_type = self.build_http_approval_action_page(identifier, "reject")
                await self._write_http_response(writer, status_code, body, content_type)
            elif path == "/api/approve" and method == "POST":
                identifier = query.get("identifier", [""])[0]
                identifier = unquote(identifier)
                status_code, message = await self.process_http_approval_action_request(identifier, "approve")
                body = json.dumps({"message": message})
                await self._write_http_response(writer, status_code, body, "application/json")
            elif path in {"/api/deny", "/api/reject"} and method == "POST":
                identifier = query.get("identifier", [""])[0]
                identifier = unquote(identifier)
                status_code, message = await self.process_http_approval_action_request(identifier, "reject")
                body = json.dumps({"message": message})
                await self._write_http_response(writer, status_code, body, "application/json")
            else:
                await self._write_http_response(writer, 404, "Not found.", "text/plain")
        except Exception as e:
            err = f"Failed to process HTTP request: {e}"
            print(f"[{datetime.now().isoformat()}] {err}")
            try:
                await self._write_http_response(writer, 500, err, "text/plain")
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    
    async def start(self):
        """Start the Matrix client and listen for messages."""
        print(f"[{datetime.now().isoformat()}] Starting KeelMatrixBot listener mode...")
        
        # Login to Matrix to get access token
        self.user_id, self.access_token = await login_to_matrix(
            self.homeserver,
            self.matrix_username,
            self.matrix_password
        )
        
        if not self.user_id or not self.access_token:
            print(f"[{datetime.now().isoformat()}] Failed to login to Matrix, exiting")
            return

        self.room_id = await resolve_matrix_room_reference(
            self.homeserver,
            self.access_token,
            self.room_reference
        )
        
        self.client = AsyncClient(self.homeserver, self.user_id)
        self.client.access_token = self.access_token
        
        # Get bot's own user ID from the login response
        self.bot_user_id = self.user_id

        # Start a background task to clean up old deduplication entries
        asyncio.create_task(self._cleanup_old_deduplication_entries())

        # Start HTTP listener for click-to-approve links
        await self.start_http_listener()

        # Start dedicated scheduler for nightly "keel get approvals" (11:30 PM Pacific)
        self.nightly_scheduler_task = asyncio.create_task(self._nightly_get_approvals_scheduler())

        # Perform initial sync BEFORE registering callbacks so old room history
        # commands are not processed after a restart.
        print(f"[{datetime.now().isoformat()}] Syncing with Matrix homeserver (startup catch-up)...")
        
        # Initial sync with retry
        next_batch = None
        for attempt in range(5):
            sync_response = await self.client.sync(timeout=30000)
            
            # Check if it's a SyncError
            if hasattr(sync_response, 'next_batch'):
                next_batch = sync_response.next_batch
                print(f"[{datetime.now().isoformat()}] Synced, next_batch: {next_batch}")
                break
            else:
                print(f"[{datetime.now().isoformat()}] Sync error: {sync_response}, attempt {attempt + 1}/5")
                await asyncio.sleep(5)
        
        if next_batch is None:
            print(f"[{datetime.now().isoformat()}] Failed to sync after 5 attempts, exiting")
            return

        # Register callback only after we have the startup next_batch token.
        # From now on we sync with `since=next_batch`, so only NEW commands are processed.
        self.client.add_event_callback(self.on_message, RoomMessageText)
        
        print(
            f"[{datetime.now().isoformat()}] Bot listening for messages in room "
            f"{self.room_id} (configured as {self.room_reference})"
        )
        print(f"[{datetime.now().isoformat()}] Also polling for approvals every 30 seconds")
        print(
            f"[{datetime.now().isoformat()}] Nightly scheduler set for 11:30 PM Pacific "
            f"(next run: {self.next_nightly_get_approvals_at.isoformat()})"
        )
        
        # Initialize polling timer
        poll_interval = 30
        last_poll = datetime.now()
        
        # Main sync loop
        try:
            while True:
                try:
                    # Calculate how long until next poll
                    now = datetime.now()
                    time_since_poll = (now - last_poll).total_seconds()
                    
                    # Poll for approvals if interval has passed
                    if time_since_poll >= poll_interval:
                        print(f"[{datetime.now().isoformat()}] Polling for approvals...")
                        await self.poll_with_auto_approve()
                        last_poll = datetime.now()
                    
                    # Sync for new events (use shorter timeout to check polling more frequently)
                    sync_timeout = min(5000, int((poll_interval - time_since_poll) * 1000)) if time_since_poll < poll_interval else 5000
                    sync_response = await self.client.sync(timeout=sync_timeout, since=next_batch)
                    
                    if hasattr(sync_response, 'next_batch'):
                        next_batch = sync_response.next_batch
                    else:
                        print(f"[{datetime.now().isoformat()}] Sync error: {sync_response}")
                    
                    # Small sleep to prevent tight loop
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"[{datetime.now().isoformat()}] Sync exception: {e}")
                    await asyncio.sleep(5)
        finally:
            if self.nightly_scheduler_task:
                self.nightly_scheduler_task.cancel()
    
    async def on_message(self, room, event):
        """Handle incoming Matrix messages."""
        # Skip if this is our own message
        if event.sender == self.user_id:
            return
        
        # Skip already processed events
        if event.event_id in self.processed_events:
            return
        self.processed_events.add(event.event_id)
        
        # Limit the size of processed events set to prevent memory growth
        if len(self.processed_events) > 1000:
            # Keep only the last 500
            self.processed_events = set(list(self.processed_events)[-500:])
        
        # Parse the message body
        if not hasattr(event, 'body') or not event.body:
            return
        
        message_text = event.body.strip()
        print(f"[{datetime.now().isoformat()}] Received message from {event.sender}: {message_text}")
        
        # Check for "keel help" first (fast path, no API calls needed)
        if re.match(r'^keel\s+help\s*$', message_text, re.IGNORECASE):
            help_text = (
                "📖 **Keel Bot Commands**\n"
                "\n"
                "**📋 Query**\n"
                "`keel get approvals` — List all pending approvals\n"
                "`keel get auto` — List configured auto-approve targets\n"
                "`keel get version` — Show bot build version\n"
                "\n"
                "**✅ Approve / ❌ Reject**\n"
                "`keel approve <keyword>` — Approve matching approval (e.g., `keel approve kured`)\n"
                "`keel reject <keyword>` — Reject matching approval (e.g., `keel reject kured`)\n"
                "`approve <identifier>` — Approve by exact identifier\n"
                "`reject <identifier>` — Reject by exact identifier\n"
                "\n"
                "**⚙️ Configuration**\n"
                "`keel set-auto <keyword>` — Auto-approve future matching approvals (e.g., `keel set-auto kured`)\n"
                "`keel set-url <identifier> <url>` — Attach release notes URL to an approval\n"
                "`keel set-url firefox/wireguard <url>` — Attach notes to the WireGuard image inside Firefox\n"
                "\n"
                "**💡 Tips**\n"
                "• Keyword search is case-insensitive and matches partial identifiers\n"
                "• Multi-image deployments can use `<deployment>/<image>`, for example `firefox/wireguard`\n"
                "• If multiple matches are found, the bot will list them for disambiguation\n"
                "• Auto-approve runs every 30 seconds on pending approvals\n"
                "• Click the [Approve] links in approval messages for one-click approval\n"
                "\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room.room_id, help_text
            )
        elif re.match(r'^keel\s+get\s+approvals\s*$', message_text, re.IGNORECASE):
            print(f"[{datetime.now().isoformat()}] Detected 'keel get approvals' command, fetching approvals...")
            await self.handle_get_approvals(room.room_id)
        elif re.match(r'^keel\s+get\s+version\s*$', message_text, re.IGNORECASE):
            print(f"[{datetime.now().isoformat()}] Detected 'keel get version' command")
            await self.handle_get_version(room.room_id)
        elif re.match(r'^keel\s+get\s+auto\s*$', message_text, re.IGNORECASE):
            print(f"[{datetime.now().isoformat()}] Detected 'keel get auto' command, listing auto-approve targets...")
            await self.handle_get_auto(room.room_id)
        elif re.match(r'^keel\s+approve\s+', message_text, re.IGNORECASE):
            # Extract the search term from "keel approve <search_term>"
            search_term = re.sub(r'^keel\s+approve\s+', '', message_text, flags=re.IGNORECASE).strip()
            print(f"[{datetime.now().isoformat()}] Detected 'keel approve' command for search term: {search_term}")
            await self.handle_keel_approve_reject(room.room_id, search_term, "approve")
        elif re.match(r'^keel\s+reject\s+', message_text, re.IGNORECASE):
            # Extract the search term from "keel reject <search_term>"
            search_term = re.sub(r'^keel\s+reject\s+', '', message_text, flags=re.IGNORECASE).strip()
            print(f"[{datetime.now().isoformat()}] Detected 'keel reject' command for search term: {search_term}")
            await self.handle_keel_approve_reject(room.room_id, search_term, "reject")
        elif re.match(r'^approve\s+', message_text, re.IGNORECASE):
            # Legacy support: "approve <identifier>"
            identifier = re.sub(r'^approve\s+', '', message_text, flags=re.IGNORECASE).strip()
            print(f"[{datetime.now().isoformat()}] Detected 'approve' command for: {identifier}")
            await self.handle_approve_reject(room.room_id, identifier, "approve")
        elif re.match(r'^reject\s+', message_text, re.IGNORECASE):
            # Legacy support: "reject <identifier>"
            identifier = re.sub(r'^reject\s+', '', message_text, flags=re.IGNORECASE).strip()
            print(f"[{datetime.now().isoformat()}] Detected 'reject' command for: {identifier}")
            await self.handle_approve_reject(room.room_id, identifier, "reject")
        elif re.match(r'^keel\s+set-url\s+', message_text, re.IGNORECASE):
            # Extract the identifier and URL from "keel set-url <identifier> <url>"
            parts = re.sub(r'^keel\s+set-url\s+', '', message_text, flags=re.IGNORECASE).strip().split(None, 1)
            if len(parts) < 2:
                response_msg = (
                    f"❌ **Error: Missing identifier or URL**\n"
                    f"\n"
                    f"Usage: `keel set-url <identifier> <url>`\n"
                    f"\n"
                    f"Example: `keel set-url immich https://github.com/imagegenius/docker-immich/releases`\n"
                    f"Example: `keel set-url firefox/wireguard https://github.com/linuxserver/docker-wireguard/releases`\n"
                    f"\n"
                    f"{SEPARATOR}"
                )
                await send_matrix_message(
                    self.homeserver, self.user_id, self.access_token,
                    room.room_id, response_msg
                )
                return
            
            identifier = parts[0].strip()
            url = parts[1].strip()
            
            print(f"[{datetime.now().isoformat()}] Detected 'keel set-url' command for: {identifier} -> {url}")
            await self.handle_set_url(room.room_id, identifier, url)
        elif re.match(r'^keel\s+set-auto(\s+.*)?$', message_text, re.IGNORECASE):
            # Extract the target from "keel set-auto <target>"
            target = re.sub(r'^keel\s+set-auto\s*', '', message_text, flags=re.IGNORECASE).strip()
            print(f"[{datetime.now().isoformat()}] Detected 'keel set-auto' command for: {target}")
            await self.handle_set_auto(room.room_id, target)
    
    async def handle_get_approvals(self, room_id: str):
        """Handle the 'keel get approvals' command by fetching and displaying approvals."""
        sending_msg = "🔄 Fetching approvals from Keel..."
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, sending_msg
        )
        
        approvals, raw_response = await fetch_pending_approvals(
            self.keel_url, self.keel_username, self.keel_password
        )
        
        response_status = raw_response.get("status", "N/A")
        
        # Create response message with status info
        if raw_response.get("error"):
            response_msg = (
                f"❌ **Error fetching approvals**\n"
                f"\n"
                f"Status: {response_status}\n"
                f"Error: {raw_response['error']}\n"
                f"\n"
                f"{SEPARATOR}"
            )
        else:
            # Pass approval_timestamps to show days since first seen
            response_msg = format_approvals_list(
                approvals,
                approval_memory.approval_timestamps,
                approve_base_url=self.public_base_url
            )
            # Add HTTP status to the top
            response_lines = response_msg.split("\n")
            # Insert status after the first line
            for i, line in enumerate(response_lines):
                if line.startswith("📋 **Pending Approvals"):
                    response_lines.insert(1, f"**HTTP Status:** {response_status}")
                    response_lines.insert(2, "")
                    break
            response_msg = "\n".join(response_lines)
        
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )

    async def handle_get_auto(self, room_id: str):
        """Handle the 'keel get auto' command by listing auto-approve image targets."""
        targets = get_auto_approve_targets()
        if not targets:
            response_msg = (
                "ℹ️ **Auto-Approve Images**\n"
                "\n"
                "No auto-approve images are configured yet.\n"
                "Use `keel set-auto <identifier-or-keyword>` to add one.\n"
                "\n"
                f"{SEPARATOR}"
            )
        else:
            lines = "\n".join(f"**{i}.** `{target}`" for i, target in enumerate(targets, 1))
            response_msg = (
                f"🤖 **Auto-Approve Images ({len(targets)})**\n"
                "\n"
                f"{lines}\n"
                "\n"
                f"{SEPARATOR}"
            )

        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )
    
    async def handle_get_version(self, room_id: str):
        """Handle the 'keel get version' command by returning the bot build version."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        response_msg = (
            f"🏷️ **Keel Matrix Bot Version**\n"
            f"\n"
            f"**Build Version:** `{BUILD_VERSION}`\n"
            f"**Checked at:** {now}\n"
            f"\n"
            f"{SEPARATOR}"
        )
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )

    async def handle_set_url(self, room_id: str, identifier: str, url: str):
        """Handle the 'keel set-url <identifier> <url>' command to set a release notes URL."""
        # Store the URL in memory
        set_release_notes_url(identifier, url)
        
        response_msg = (
            f"✅ **Release notes URL set!**\n"
            f"\n"
            f"Identifier: `{identifier}`\n"
            f"Release Notes URL: {url}\n"
            f"\n"
            f"Future approval notifications for this identifier will include this URL.\n"
            f"\n"
            f"{SEPARATOR}"
        )
        
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )

    async def handle_set_auto(self, room_id: str, target: str):
        """Handle the 'keel set-auto <target>' command to auto-approve matching approvals."""
        if not target:
            response_msg = (
                f"❌ **Error: Missing target**\n"
                f"\n"
                f"Usage: `keel set-auto <identifier-or-keyword>`\n"
                f"Example: `keel set-auto llama-cpp-amd`\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return

        set_auto_approve_target(target)
        targets = get_auto_approve_targets()
        target_lines = "\n".join(f"- `{item}`" for item in targets)

        # Send immediate confirmation so the room always gets feedback.
        ack_msg = (
            f"✅ **Auto-approve target saved**\n"
            f"\n"
            f"Target: `{target}`\n"
            f"\n"
            f"Running immediate scan for matching pending approvals...\n"
            f"\n"
            f"{SEPARATOR}"
        )
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, ack_msg
        )
        
        # Immediately apply auto-approval to current pending approvals so users
        # don't have to wait for the next poll cycle.
        approvals, raw_response = await fetch_pending_approvals(
            self.keel_url, self.keel_username, self.keel_password
        )
        if raw_response.get("error"):
            response_msg = (
                f"✅ **Auto-approve target set!**\n"
                f"\n"
                f"New target: `{target}`\n"
                f"\n"
                f"Could not run immediate auto-approve due to fetch error:\n"
                f"`{raw_response.get('error')}`\n"
                f"\n"
                f"Target is saved and will still be applied on the next poll.\n"
                f"\n"
                f"**Configured auto-approve targets ({len(targets)}):**\n"
                f"{target_lines}\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return

        auto_approved_identifiers = await self.auto_approve_matching_approvals(room_id, approvals)

        response_msg = (
            f"✅ **Auto-approve target set!**\n"
            f"\n"
            f"New target: `{target}`\n"
            f"\n"
            f"Any pending approval whose identifier matches this target will be auto-approved when seen.\n"
            f"Immediate auto-approved now: **{len(auto_approved_identifiers)}**\n"
            f"\n"
            f"**Configured auto-approve targets ({len(targets)}):**\n"
            f"{target_lines}\n"
            f"\n"
            f"{SEPARATOR}"
        )
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )

    async def poll_with_auto_approve(self):
        """Poll Keel approvals, auto-approve matches, then notify for remaining new approvals."""
        approvals, raw_response = await fetch_pending_approvals(
            self.keel_url, self.keel_username, self.keel_password
        )

        print(f"[{datetime.now().isoformat()}] Approval listing result: {raw_response}")
        if approvals:
            print(f"[{datetime.now().isoformat()}] Found {len(approvals)} pending approvals:")
            for approval in approvals:
                print(f"  - {approval.identifier}: {approval.current_version} -> {approval.new_version}")
        else:
            print(f"[{datetime.now().isoformat()}] No pending approvals found.")

        approval_memory.refresh_from_approvals(approvals)
        auto_approved_identifiers = await self.auto_approve_matching_approvals(self.room_id, approvals)
        approvals_for_notify = [a for a in approvals if a.identifier not in auto_approved_identifiers]

        new_approvals = get_new_approvals(approvals_for_notify)
        if not new_approvals:
            print(f"[{datetime.now().isoformat()}] No new approvals to notify about.")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        response_status = raw_response.get("status", "N/A")
        summary_lines = [
            f"📦 **Keel Approval Alert** ({now})",
            f"**HTTP Status:** {response_status}",
            f"**Pending approvals:** {len(new_approvals)}",
            "",
        ]
        for approval in new_approvals:
            summary_lines.append(format_approval_message(approval))

        message = "\n".join(summary_lines)
        success = await send_matrix_message(
            self.homeserver, self.user_id, self.access_token, self.room_id, message
        )
        if success:
            mark_as_notified(new_approvals)
            print(f"[{datetime.now().isoformat()}] Successfully notified about {len(new_approvals)} approvals")
        else:
            print(f"[{datetime.now().isoformat()}] Failed to send notification, will retry on next poll")

    async def auto_approve_matching_approvals(self, room_id: str, approvals: list[Approval]) -> set[str]:
        """Auto-approve approvals whose identifier matches configured auto targets."""
        if not approvals:
            return set()

        auto_approved_identifiers = set()
        for approval in approvals:
            matches = get_matching_auto_targets(approval.identifier)
            if not matches:
                continue

            matched_targets = ", ".join(f"`{m}`" for m in matches)
            status_msg = (
                f"🤖 **Auto-approve matched**\n"
                f"\n"
                f"Identifier: `{approval.identifier}`\n"
                f"Matched target(s): {matched_targets}\n"
                f"\n"
                f"Approving automatically...\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, status_msg
            )

            url = f"{self.keel_url.rstrip('/')}/v1/approvals"
            headers = {"Content-Type": "application/json"}
            if self.keel_username and self.keel_password:
                auth_string = f"{self.keel_username}:{self.keel_password}"
                auth_bytes = base64.b64encode(auth_string.encode()).decode()
                headers["Authorization"] = f"Basic {auth_bytes}"

            payload = {
                "action": "approve",
                "identifier": approval.identifier,
                "voter": self.user_id
            }

            async with httpx.AsyncClient(timeout=30) as client:
                try:
                    response = await client.post(url, headers=headers, json=payload)
                    print(f"[{datetime.now().isoformat()}] Auto-approve response: HTTP {response.status_code}")

                    if response.status_code in (200, 201):
                        delete_payload = {
                            "id": approval.id,
                            "identifier": approval.identifier,
                            "action": "delete",
                            "voter": self.user_id
                        }
                        delete_response = await client.post(url, headers=headers, json=delete_payload)
                        print(f"[{datetime.now().isoformat()}] Auto-delete response: HTTP {delete_response.status_code}")

                        if delete_response.status_code in (200, 201):
                            auto_approved_identifiers.add(approval.identifier)
                            remove_from_memory(approval.identifier)
                            response_msg = (
                                f"✅ **Auto-approve successful**\n"
                                f"\n"
                                f"Identifier: `{approval.identifier}`\n"
                                f"Approval ID: `{approval.id}`\n"
                                f"Update: `{approval.current_version}` → `{approval.new_version}`\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                        else:
                            response_msg = (
                                f"⚠️ **Auto-approve succeeded but delete failed**\n"
                                f"\n"
                                f"Identifier: `{approval.identifier}`\n"
                                f"Delete status: {delete_response.status_code}\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                    else:
                        response_msg = (
                            f"❌ **Auto-approve failed**\n"
                            f"\n"
                            f"Identifier: `{approval.identifier}`\n"
                            f"Status: {response.status_code}\n"
                            f"\n"
                            f"{SEPARATOR}"
                        )
                except httpx.RequestError as e:
                    response_msg = (
                        f"❌ **Auto-approve request error**\n"
                        f"\n"
                        f"Identifier: `{approval.identifier}`\n"
                        f"Error: {type(e).__name__}\n"
                        f"\n"
                        f"{SEPARATOR}"
                    )

            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )

        return auto_approved_identifiers

    async def _delete_stale_approval(
        self,
        client,
        url: str,
        headers: dict,
        identifier: str,
        approval_id: Optional[str] = None,
    ) -> tuple[bool, Optional[str], str]:
        """Delete a stale approval entry from Keel after approve/reject reports 404."""
        resolved_approval_id = approval_id

        if not resolved_approval_id:
            approvals, raw_response = await fetch_pending_approvals(
                self.keel_url, self.keel_username, self.keel_password
            )
            if raw_response.get("error"):
                return False, None, f"lookup failed: {raw_response['error']}"

            for approval in approvals:
                if approval.identifier == identifier:
                    resolved_approval_id = approval.id
                    break

        if not resolved_approval_id:
            return False, None, "no matching approval ID found in Keel"

        delete_payload = {
            "id": resolved_approval_id,
            "identifier": identifier,
            "action": "delete",
            "voter": self.user_id
        }
        delete_response = await client.post(url, headers=headers, json=delete_payload)
        print(
            f"[{datetime.now().isoformat()}] Stale delete response: "
            f"HTTP {delete_response.status_code}"
        )

        if delete_response.status_code in (200, 201):
            return True, resolved_approval_id, "deleted"

        return (
            False,
            resolved_approval_id,
            f"delete failed with status {delete_response.status_code}"
        )
    
    async def handle_approve_reject(self, room_id: str, identifier: str, action: str):
        """Handle approve/reject commands by sending the action to Keel's API."""
        if not identifier:
            response_msg = (
                f"❌ **Error: Missing identifier**\n"
                f"\n"
                f"Please provide an approval identifier.\n"
                f"Usage: `{action} <identifier>`\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return
        
        sending_msg = f"🔄 {get_action_gerund(action)} `{identifier}`..."
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, sending_msg
        )
        
        # Call Keel's API to approve/reject
        url = f"{self.keel_url.rstrip('/')}/v1/approvals"
        headers = {"Content-Type": "application/json"}
        
        if self.keel_username and self.keel_password:
            auth_string = f"{self.keel_username}:{self.keel_password}"
            auth_bytes = base64.b64encode(auth_string.encode()).decode()
            headers["Authorization"] = f"Basic {auth_bytes}"
        
        payload = {
            "action": action,
            "identifier": identifier,
            "voter": self.user_id
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                print(f"[{datetime.now().isoformat()}] {action.capitalize()} response: HTTP {response.status_code}")
                
                if response.status_code in (200, 201):
                    # Success - fetch approval ID and delete it
                    approvals, _ = await fetch_pending_approvals(
                        self.keel_url, self.keel_username, self.keel_password
                    )
                    
                    approval_id = None
                    for approval in approvals:
                        if approval.identifier == identifier:
                            approval_id = approval.id
                            break
                    
                    # Now delete the approval
                    if approval_id:
                        delete_payload = {
                            "id": approval_id,
                            "identifier": identifier,
                            "action": "delete",
                            "voter": self.user_id
                        }
                        delete_response = await client.post(url, headers=headers, json=delete_payload)
                        print(f"[{datetime.now().isoformat()}] Delete response: HTTP {delete_response.status_code}")
                        
                        if delete_response.status_code in (200, 201):
                            response_msg = (
                                f"✅ **{action.capitalize()} and delete successful!**\n"
                                f"\n"
                                f"Identifier: `{identifier}`\n"
                                f"Approval ID: `{approval_id}`\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                        else:
                            response_msg = (
                                f"⚠️ **{action.capitalize()} successful but delete failed!**\n"
                                f"\n"
                                f"Identifier: `{identifier}`\n"
                                f"Approval ID: `{approval_id}`\n"
                                f"Delete status: {delete_response.status_code}\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                    else:
                        response_msg = (
                            f"✅ **{action.capitalize()} successful!**\n"
                            f"\n"
                            f"Identifier: `{identifier}`\n"
                            f"Action: {action}\n"
                            f"\n"
                            f"{SEPARATOR}"
                        )
                    
                    # Remove from memory
                    remove_from_memory(identifier)
                elif response.status_code == 404:
                    stale_deleted, stale_approval_id, stale_delete_detail = await self._delete_stale_approval(
                        client, url, headers, identifier
                    )
                    stale_delete_msg = (
                        f"Stale approval removed from Keel approvals response.\n"
                        f"Approval ID: `{stale_approval_id}`\n"
                        if stale_deleted else
                        f"Stale approval removal status: {stale_delete_detail}\n"
                    )
                    response_msg = (
                        f"⚠️ **Approval not found**\n"
                        f"\n"
                        f"Identifier: `{identifier}`\n"
                        f"\n"
                        f"The approval may have already been processed or does not exist.\n"
                        f"{stale_delete_msg}"
                        f"\n"
                        f"{SEPARATOR}"
                    )
                    # Remove from memory if not found (stale entry)
                    remove_from_memory(identifier)
                else:
                    response_msg = (
                        f"❌ **Error {get_action_gerund(action)} approval**\n"
                        f"\n"
                        f"Identifier: `{identifier}`\n"
                        f"Status: {response.status_code}\n"
                        f"\n"
                        f"{SEPARATOR}"
                    )
            except httpx.RequestError as e:
                response_msg = (
                    f"❌ **Request error**\n"
                    f"\n"
                    f"Identifier: `{identifier}`\n"
                    f"Error: {type(e).__name__}\n"
                    f"\n"
                    f"{SEPARATOR}"
                )
        
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )
    
    async def handle_keel_approve_reject(self, room_id: str, search_term: str, action: str):
        """Handle 'keel approve <search_term>' or 'keel reject <search_term>' commands.
        Searches for approvals containing the search term in their identifier and performs the action.
        """
        if not search_term:
            response_msg = (
                f"❌ **Error: Missing search term**\n"
                f"\n"
                f"Please provide a search term.\n"
                f"Usage: `keel {action} <keyword>`\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return
        
        # Create key for tracking processed searches (not for blocking, just for spam prevention)
        action_key = f"{action}:{search_term}"
        
        sending_msg = f"🔍 Searching for approval containing `{search_term}`..."
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, sending_msg
        )
        
        # Fetch all pending approvals
        approvals, raw_response = await fetch_pending_approvals(
            self.keel_url, self.keel_username, self.keel_password
        )
        
        if raw_response.get("error"):
            response_msg = (
                f"❌ **Error fetching approvals**\n"
                f"\n"
                f"Error: {raw_response['error']}\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return
        
        # Find approvals matching the search term (case-insensitive)
        matching_approvals = []
        search_lower = search_term.lower()
        for approval in approvals:
            if search_lower in approval.identifier.lower():
                matching_approvals.append(approval)
        
        if len(matching_approvals) == 0:
            # Mark this search as processed to prevent duplicate "no matching" messages
            self.recently_processed_approvals.add(action_key)
            response_msg = (
                f"⚠️ **No matching approvals found**\n"
                f"\n"
                f"Search term: `{search_term}`\n"
                f"\n"
                f"No pending approvals contain this keyword.\n"
                f"Use `keel get approvals` to see all pending approvals.\n"
                f"\n"
                f"{SEPARATOR}"
            )
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return
        
        if len(matching_approvals) > 1:
            # Mark this search as processed to prevent duplicate "multiple matches" messages
            self.recently_processed_approvals.add(action_key)
            # Multiple matches - list them for user to choose
            lines = [
                f"⚠️ **Multiple matching approvals found**\n",
                f"\n",
                f"Search term: `{search_term}`\n",
                f"\n",
                f"Please be more specific. Matching approvals:\n",
                "",
            ]
            for i, approval in enumerate(matching_approvals, 1):
                lines.append(f"**{i}. {approval.identifier}**")
                lines.append(f"   Update: `{approval.current_version}` → `{approval.new_version}`")
                lines.append("")
            lines.append("─" * 40)
            response_msg = "\n".join(lines)
            await send_matrix_message(
                self.homeserver, self.user_id, self.access_token,
                room_id, response_msg
            )
            return
        
        # Exactly one match - proceed with the action
        approval = matching_approvals[0]
        identifier = approval.identifier
        approval_id = approval.id
        
        # Create a unique key based on approval ID for more precise deduplication
        # Also mark the early dedup key as processed
        final_dedup_key = f"{action}:{approval_id}"
        
        # Double-check with approval_id (more precise than search_term)
        if final_dedup_key in self.recently_processed_approvals:
            print(f"[{datetime.now().isoformat()}] Final duplicate check: skipping {final_dedup_key}")
            # Also mark the early key to prevent further retries
            self.recently_processed_approvals.add(action_key)
            return
        
        # Mark both keys as processed
        self.recently_processed_approvals.add(action_key)
        self.recently_processed_approvals.add(final_dedup_key)
        
        confirming_msg = (
            f"✅ Found matching approval!\n"
            f"\n"
            f"**{approval.identifier}**\n"
            f"Update: `{approval.current_version}` → `{approval.new_version}`\n"
            f"\n"
            f"Proceeding with {action}...\n"
            f"\n"
            f"{SEPARATOR}"
        )
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, confirming_msg
        )
        
        # Call Keel's API to approve/reject
        url = f"{self.keel_url.rstrip('/')}/v1/approvals"
        headers = {"Content-Type": "application/json"}
        
        if self.keel_username and self.keel_password:
            auth_string = f"{self.keel_username}:{self.keel_password}"
            auth_bytes = base64.b64encode(auth_string.encode()).decode()
            headers["Authorization"] = f"Basic {auth_bytes}"
        
        payload = {
            "action": action,
            "identifier": identifier,
            "voter": self.user_id
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                print(f"[{datetime.now().isoformat()}] {action.capitalize()} response: HTTP {response.status_code}")
                
                if response.status_code in (200, 201):
                    # Success - now delete the approval
                    if approval_id:
                        delete_payload = {
                            "id": approval_id,
                            "identifier": identifier,
                            "action": "delete",
                            "voter": self.user_id
                        }
                        delete_response = await client.post(url, headers=headers, json=delete_payload)
                        print(f"[{datetime.now().isoformat()}] Delete response: HTTP {delete_response.status_code}")
                        
                        if delete_response.status_code in (200, 201):
                            response_msg = (
                                f"✅ **{action.capitalize()} and delete successful!**\n"
                                f"\n"
                                f"Identifier: `{identifier}`\n"
                                f"Approval ID: `{approval_id}`\n"
                                f"Update: `{approval.current_version}` → `{approval.new_version}`\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                        else:
                            response_msg = (
                                f"⚠️ **{action.capitalize()} successful but delete failed!**\n"
                                f"\n"
                                f"Identifier: `{identifier}`\n"
                                f"Approval ID: `{approval_id}`\n"
                                f"Delete status: {delete_response.status_code}\n"
                                f"\n"
                                f"{SEPARATOR}"
                            )
                    else:
                        response_msg = (
                            f"✅ **{action.capitalize()} successful!**\n"
                            f"\n"
                            f"Identifier: `{identifier}`\n"
                            f"Update: `{approval.current_version}` → `{approval.new_version}`\n"
                            f"\n"
                            f"{SEPARATOR}"
                        )
                    
                    # Remove from memory
                    remove_from_memory(identifier)
                elif response.status_code == 404:
                    stale_deleted, stale_approval_id, stale_delete_detail = await self._delete_stale_approval(
                        client, url, headers, identifier, approval_id
                    )
                    stale_delete_msg = (
                        f"Stale approval removed from Keel approvals response.\n"
                        f"Approval ID: `{stale_approval_id}`\n"
                        if stale_deleted else
                        f"Stale approval removal status: {stale_delete_detail}\n"
                    )
                    response_msg = (
                        f"⚠️ **Approval not found**\n"
                        f"\n"
                        f"Identifier: `{identifier}`\n"
                        f"\n"
                        f"The approval may have already been processed or does not exist.\n"
                        f"{stale_delete_msg}"
                        f"\n"
                        f"{SEPARATOR}"
                    )
                    # Remove from memory if not found (stale entry)
                    remove_from_memory(identifier)
                else:
                    response_msg = (
                        f"❌ **Error {get_action_gerund(action)} approval**\n"
                        f"\n"
                        f"Identifier: `{identifier}`\n"
                        f"Status: {response.status_code}\n"
                        f"\n"
                        f"{SEPARATOR}"
                    )
            except httpx.RequestError as e:
                response_msg = (
                    f"❌ **Request error**\n"
                    f"\n"
                    f"Identifier: `{identifier}`\n"
                    f"Error: {type(e).__name__}\n"
                    f"\n"
                    f"{SEPARATOR}"
                )
        
        await send_matrix_message(
            self.homeserver, self.user_id, self.access_token,
            room_id, response_msg
        )


async def main():
    parser = argparse.ArgumentParser(description="Keel Approvals Matrix Bot")
    parser.add_argument(
        "--keel-url",
        default=os.environ.get("KEEL_URL", "http://keel.example.svc.cluster.local:9300"),
        help="Keel service URL"
    )
    parser.add_argument(
        "--homeserver",
        default=os.environ.get("MATRIX_HOMESERVER", "https://matrix.org"),
        help="Matrix homeserver URL"
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("MATRIX_USER_ID", ""),
        help="Matrix user ID (e.g. @bot:matrix.org)"
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("MATRIX_ACCESS_TOKEN", ""),
        help="Matrix access token (if not provided, will login with username/password)"
    )
    parser.add_argument(
        "--matrix-username",
        default=os.environ.get("MATRIX_USERNAME", ""),
        help="Matrix username for login"
    )
    parser.add_argument(
        "--matrix-password",
        default=os.environ.get("MATRIX_PASSWORD", ""),
        help="Matrix password for login"
    )
    parser.add_argument(
        "--room-id",
        default=os.environ.get("KEEL_MATRIX_ROOM_ID", ""),
        help="Matrix room ID or alias to send notifications to"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't actually send messages, just print what would be sent"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (for CronJob)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)"
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Run in listener mode to respond to commands"
    )
    
    args = parser.parse_args()
    
    # Get Keel credentials from environment
    keel_username = os.environ.get("KEEL_USERNAME", "")
    keel_password = os.environ.get("KEEL_PASSWORD", "")
    
    # Get Matrix credentials
    matrix_username = args.matrix_username or os.environ.get("MATRIX_USERNAME", "")
    matrix_password = args.matrix_password or os.environ.get("MATRIX_PASSWORD", "")
    
    # Determine effective user_id and access_token
    effective_user_id = args.user_id
    effective_access_token = args.access_token
    
    # If we have username/password but no token, login to get a fresh token
    if matrix_username and matrix_password and not effective_access_token:
        print(f"[{datetime.now().isoformat()}] No access token provided, logging in with username '{matrix_username}'...")
        user_id, access_token = await login_to_matrix(args.homeserver, matrix_username, matrix_password)
        if user_id and access_token:
            effective_user_id = user_id
            effective_access_token = access_token
        else:
            print("ERROR: Failed to obtain Matrix access token via login")
            sys.exit(1)
    elif not effective_user_id or not effective_access_token:
        print("ERROR: Either MATRIX_ACCESS_TOKEN or (MATRIX_USERNAME + MATRIX_PASSWORD) is required")
        sys.exit(1)
    
    if not args.room_id:
        print("ERROR: KEEL_MATRIX_ROOM_ID environment variable is required")
        sys.exit(1)

    resolved_room_id = args.room_id
    if not args.dry_run:
        resolved_room_id = await resolve_matrix_room_reference(
            args.homeserver,
            effective_access_token,
            args.room_id
        )
    
    print(f"Starting Keel Matrix Bot v{BUILD_VERSION}...")
    print(f"  State File: {approval_memory.state_file}")
    print(f"  Keel URL: {args.keel_url}")
    print(f"  Homeserver: {args.homeserver}")
    print(f"  User: {args.user_id}")
    print(f"  Room: {resolved_room_id} (configured as {args.room_id})")
    print(f"  Keel Username: '{keel_username}'")
    print(f"  Keel Password: {'[REDACTED]' if keel_password else '(not set)'}")
    print(f"  Using Keel Auth: {'Yes' if keel_username and keel_password else 'No (basicauth disabled)'}")
    
    # Reconcile approval memory from Keel API on startup without muting current approvals.
    print(f"[{datetime.now().isoformat()}] Reconciling approval memory from Keel API on startup...")
    startup_approvals, _ = await fetch_pending_approvals(args.keel_url, keel_username, keel_password)
    approval_memory.refresh_from_approvals(startup_approvals)
    print(f"[{datetime.now().isoformat()}] Startup reconciliation complete")
    
    if args.listen:
        # Run in listener mode
        print("Running in LISTENER mode - will respond to commands")
        bot = KeelMatrixBot(
            homeserver=args.homeserver,
            matrix_username=matrix_username,
            matrix_password=matrix_password,
            room_id=resolved_room_id,
            keel_url=args.keel_url,
            keel_username=keel_username,
            keel_password=keel_password
        )
        await bot.start()
    elif args.once:
        await poll_and_notify(
            args.keel_url,
            args.homeserver,
            effective_user_id,
            effective_access_token,
            resolved_room_id,
            keel_username,
            keel_password,
            args.dry_run
        )
    else:
        print(f"Polling every {args.interval} seconds...")
        print(f"Daily summary scheduled for 11:30 PM PST")
        
        # Initialize next summary time
        time_until_summary = get_time_until_next_summary()
        print(f"Next daily summary in {time_until_summary:.0f} seconds ({time_until_summary/3600:.1f} hours)")
        
        while True:
            try:
                # Check if it's time for daily summary
                if time_until_summary <= 0:
                    print(f"[{datetime.now().isoformat()}] Running daily summary...")
                    await send_daily_summary(
                        args.keel_url,
                        args.homeserver,
                        effective_user_id,
                        effective_access_token,
                        resolved_room_id,
                        keel_username,
                        keel_password,
                        args.dry_run
                    )
                    # Reset timer for next day
                    time_until_summary = get_time_until_next_summary()
                    print(f"Next daily summary in {time_until_summary:.0f} seconds ({time_until_summary/3600:.1f} hours)")
                
                # Poll for approvals
                await poll_and_notify(
                    args.keel_url,
                    args.homeserver,
                    effective_user_id,
                    effective_access_token,
                    resolved_room_id,
                    keel_username,
                    keel_password,
                    args.dry_run
                )
            except Exception as e:
                print(f"Error during poll: {e}")
            
            # Sleep for the polling interval, but account for summary time
            sleep_time = min(args.interval, time_until_summary) if time_until_summary > 0 else args.interval
            await asyncio.sleep(sleep_time)
            
            # Decrement summary timer
            if time_until_summary > 0:
                time_until_summary -= sleep_time


if __name__ == "__main__":
    asyncio.run(main())
