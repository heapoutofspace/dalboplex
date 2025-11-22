#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml",
#     "jinja2",
#     "typer",
#     "rich",
#     "requests",
#     "pillow",
#     "truenas_api_client @ git+https://github.com/truenas/api_client.git",
# ]
# ///

"""
Docker Compose management tool with TrueNAS integration.

Render docker-compose templates with boilerplate and path replacements,
and manage custom apps on TrueNAS.
"""

import sys
import yaml
import typer
import ssl
import os
import hashlib
import shutil
import difflib
import re
import json
import requests
from pathlib import Path
from jinja2 import Template
from typing import Any, Optional
from collections import OrderedDict
from rich.console import Console
from rich.table import Table

# Monkey-patch SSL to accept self-signed certificates
import warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Patch ssl.SSLContext.wrap_socket to disable verification
_orig_SSLContext_wrap_socket = ssl.SSLContext.wrap_socket

def _patched_SSLContext_wrap_socket(self, *args, **kwargs):
    self.check_hostname = False
    self.verify_mode = ssl.CERT_NONE
    return _orig_SSLContext_wrap_socket(self, *args, **kwargs)

ssl.SSLContext.wrap_socket = _patched_SSLContext_wrap_socket

from truenas_api_client import Client

app = typer.Typer(help="Docker Compose management tool with TrueNAS integration")
console = Console()

# Configuration file location
CONFIG_DIR = Path.home() / ".config" / "dalboplex"
TRUENAS_CONFIG_FILE = CONFIG_DIR / "truenas.yml"


# Custom YAML representer to preserve order and formatting
def represent_none(self, _):
    return self.represent_scalar("tag:yaml.org,2002:null", "")


yaml.add_representer(type(None), represent_none)


# Custom YAML dumper that doesn't use aliases (anchors/references)
class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def load_config(config_path: Path) -> dict[str, Any]:
    """Load the render configuration file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_state(state_path: Path) -> dict[str, Any]:
    """Load the application state file."""
    if not state_path.exists():
        return {}

    with open(state_path) as f:
        return yaml.safe_load(f) or {}


def save_state(state_path: Path, state: dict[str, Any]):
    """Save the application state file."""
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with open(state_path, "w") as f:
        yaml.dump(state, f, default_flow_style=False, sort_keys=True)


def compute_config_hash(config_content: str) -> str:
    """Compute SHA256 hash of the configuration content."""
    return hashlib.sha256(config_content.encode('utf-8')).hexdigest()


def show_diff(old_content: str, new_content: str, old_label: str = "installed", new_label: str = "rendered"):
    """Display a unified diff between two content strings."""
    from rich.syntax import Syntax

    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=old_label,
        tofile=new_label,
        lineterm=''
    )

    diff_text = ''.join(diff)

    if diff_text:
        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
        console.print("\n[bold]Configuration changes:[/bold]")
        console.print(syntax)
        console.print()
    else:
        console.print("[dim]No differences found[/dim]")


def ensure_directories(client: Client, dirs: list[dict[str, Any]]):
    """
    Ensure directories exist with correct permissions via TrueNAS API.
    For files, only ownership is adjusted (not permissions).
    """
    for dir_info in dirs:
        path = dir_info["path"]
        uid = dir_info["uid"]
        gid = dir_info["gid"]
        mode = dir_info["mode"]

        # Check if path exists
        try:
            stat_result = client.call("filesystem.stat", path)
            exists = True
            is_file = stat_result.get("type") == "FILE"
        except Exception:
            exists = False
            is_file = False

        if not exists:
            # Determine if this should be a file or directory based on the path
            # If path has an extension (contains a dot after the last slash), treat as file
            path_obj = Path(path)
            looks_like_file = "." in path_obj.name

            if looks_like_file:
                console.print(f"[dim]Creating file parent directory: {path_obj.parent}[/dim]")
                try:
                    parent_path = str(path_obj.parent)

                    # Ensure parent directory exists
                    try:
                        client.call("filesystem.stat", parent_path)
                    except Exception:
                        # Parent doesn't exist, create it
                        client.call("filesystem.mkdir", {"path": parent_path})
                        # Set ownership on parent
                        client.call("filesystem.chown", {
                            "path": parent_path,
                            "uid": uid,
                            "gid": gid,
                            "options": {"recursive": False}
                        })
                        # Set permissions on parent
                        client.call("filesystem.setperm", {
                            "path": parent_path,
                            "mode": mode,
                            "options": {"recursive": False, "traverse": False}
                        })

                    console.print(f"[yellow]Note:[/yellow] File {path} doesn't exist - will be created by container")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to create parent for {path}: {e}")
                    raise
            else:
                # Create directory
                console.print(f"[dim]Creating directory: {path}[/dim]")
                try:
                    parent_path = str(path_obj.parent)

                    # Check if parent exists, create if needed
                    try:
                        client.call("filesystem.stat", parent_path)
                    except Exception:
                        # Parent doesn't exist, create it
                        client.call("filesystem.mkdir", {"path": parent_path})

                    # Now create the actual directory
                    client.call("filesystem.mkdir", {"path": path})

                    # Set ownership
                    client.call("filesystem.chown", {
                        "path": path,
                        "uid": uid,
                        "gid": gid,
                        "options": {"recursive": False}
                    })

                    # Set permissions
                    client.call("filesystem.setperm", {
                        "path": path,
                        "mode": mode,
                        "options": {"recursive": False, "traverse": False}
                    })
                    console.print(f"[green]✓[/green] Created {path} (uid={uid}, gid={gid}, mode={mode})")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to create {path}: {e}")
                    raise
        else:
            # Path exists - check and fix permissions/ownership if needed
            current_uid = stat_result.get("uid")
            current_gid = stat_result.get("gid")
            current_mode = oct(stat_result.get("mode", 0))[-3:]  # Get last 3 digits

            needs_chown = current_uid != uid or current_gid != gid
            # Only fix permissions on directories, not files
            needs_chmod = not is_file and current_mode != mode

            if needs_chown or needs_chmod:
                changes = []
                if needs_chown:
                    changes.append(f"ownership {current_uid}:{current_gid} → {uid}:{gid}")
                if needs_chmod:
                    changes.append(f"mode {current_mode} → {mode}")

                item_type = "file" if is_file else "directory"
                console.print(f"[yellow]⚠[/yellow] Fixing {item_type} {path}: {', '.join(changes)}")

                try:
                    if needs_chown:
                        client.call("filesystem.chown", {
                            "path": path,
                            "uid": uid,
                            "gid": gid,
                            "options": {"recursive": False}
                        })
                    if needs_chmod:
                        client.call("filesystem.setperm", {
                            "path": path,
                            "mode": mode,
                            "options": {"recursive": False, "traverse": False}
                        })
                    console.print(f"[green]✓[/green] Fixed {path}")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to fix {path}: {e}")
                    raise
            else:
                item_type = "file" if is_file else "directory"
                console.print(f"[green]✓[/green] {path} ({item_type})")


def parse_volume_placeholder(volume: str, service_name: str, config: dict[str, Any]) -> tuple[str, bool]:
    """
    Parse volume placeholder syntax like:
    - @config -> uses default template with folder=config, mount=/config
    - @config:/path -> uses default template with custom mount path
    - @config:ro -> uses default template with read-only option
    - @config:/path:ro -> custom mount path with read-only option
    - @docker -> uses specific docker mount if defined, otherwise uses default
    - @media -> uses specific media mount if defined, otherwise uses default

    Returns a tuple of (rendered_volume_string, uses_default_template).
    """
    if not volume.startswith("@"):
        return volume, False

    # Split the volume string into parts
    parts = volume.split(":")
    placeholder = parts[0]

    # Parse the placeholder @type
    mount_type = placeholder[1:]  # Remove @
    folder = mount_type  # Folder name is the same as mount type

    # Determine custom mount path and options
    # Possible formats:
    # - @type -> no custom mount, no options
    # - @type:ro -> no custom mount, with options
    # - @type:/custom/path -> custom mount, no options
    # - @type:/custom/path:ro -> custom mount, with options
    custom_mount_path = None
    mount_opts = None

    if len(parts) > 1:
        # Check if parts[1] is a mount option (like 'ro', 'rw') or a path
        if parts[1] in ['ro', 'rw', 'z', 'Z', 'shared', 'slave', 'private', 'rshared', 'rslave', 'rprivate']:
            # parts[1] is an option, not a path
            mount_opts = parts[1]
        else:
            # parts[1] is a custom mount path
            custom_mount_path = parts[1]
            # Check for options in parts[2]
            if len(parts) > 2:
                mount_opts = parts[2]

    # Get mount configuration
    if "volumes" not in config:
        raise ValueError("No 'volumes' section found in config")

    # Check if specific mount type exists, otherwise use default
    uses_default = False
    if mount_type in config["volumes"]:
        mount_config = config["volumes"][mount_type]
    elif "default" in config["volumes"]:
        mount_config = config["volumes"]["default"]
        uses_default = True
    else:
        raise ValueError(f"Mount type '{mount_type}' not found and no 'default' template in config")

    # Render host path using Jinja2
    # Support both 'host' and 'host_path' keys for backwards compatibility
    host_template_str = mount_config.get("host") or mount_config.get("host_path")
    if not host_template_str:
        raise ValueError(f"Mount config must have 'host' or 'host_path' key")

    host_path_template = Template(host_template_str)
    host_path = host_path_template.render(container=service_name, folder=folder, app=config.get("app"))

    # Determine mount path
    if custom_mount_path:
        mount_path = custom_mount_path
    else:
        # Support both 'mount' and 'mount_path' keys for backwards compatibility
        mount_path = mount_config.get("mount") or mount_config.get("mount_path")
        if not mount_path:
            raise ValueError(f"Mount config must have 'mount' or 'mount_path' key")

        # Render mount path template if it contains variables
        mount_path_template = Template(mount_path)
        mount_path = mount_path_template.render(container=service_name, folder=folder, app=config.get("app"))

    # Build final volume string
    result = f"{host_path}:{mount_path}"
    if mount_opts:
        result += f":{mount_opts}"

    return result, uses_default


def parse_label_template_call(label: str) -> tuple[str | None, list[str], dict[str, str]]:
    """
    Parse a label template call like @domain(example.com, port=8080, public=true) or @widget
    Returns: (template_name, positional_args, keyword_args)
    Returns (None, [], {}) if not a template call
    """
    if not label.startswith("@"):
        return None, [], {}

    # Extract template name and arguments
    import re
    # Match @template_name or @template_name(args)
    match = re.match(r'@(\w+)(?:\((.*)\))?$', label.strip())
    if not match:
        return None, [], {}

    template_name = match.group(1)
    args_str = match.group(2)

    # If no parentheses or empty parentheses, return empty args
    if not args_str:
        return template_name, [], {}

    # Parse arguments
    positional_args = []
    keyword_args = {}

    # Split by comma, but respect nesting
    args = []
    current_arg = ""
    paren_depth = 0

    for char in args_str:
        if char == ',' and paren_depth == 0:
            args.append(current_arg.strip())
            current_arg = ""
        else:
            if char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
            current_arg += char

    if current_arg.strip():
        args.append(current_arg.strip())

    # Process each argument
    for arg in args:
        if '=' in arg:
            key, value = arg.split('=', 1)
            keyword_args[key.strip()] = value.strip()
        else:
            positional_args.append(arg.strip())

    return template_name, positional_args, keyword_args


def process_x_features(
    x_features: list[dict[str, Any]] | None,
    container_name: str,
    config: dict[str, Any]
) -> list[str]:
    """
    Process x-features structure and convert to label template calls.
    Format: x-features is a list of dicts, each with a template name as key
    and arguments as a nested dict.

    Example (full syntax):
      x-features:
        - homepage:
            category: Download
            description: Archive extraction
            weight: 35

    Example (simple value syntax for single argument):
      x-features:
        - domain: files
        - homepage:
            category: Download
            description: Archive extraction

    For templates with a single positional argument, you can use the simple
    value syntax (e.g., "domain: files" instead of "domain: {subdomain: files}").
    All required arguments must be provided.
    Templates can access context from previously rendered features.
    """
    if not x_features:
        return []

    label_templates = config.get("labels", {})
    rendered_labels = []
    previous_contexts = {}
    template_counters = {}

    for feature_item in x_features:
        # Each item should be a dict with exactly one key (the template name)
        if not isinstance(feature_item, dict):
            console.print(f"[yellow]Warning:[/yellow] x-features item is not a dict: {feature_item}")
            continue

        if len(feature_item) != 1:
            console.print(f"[yellow]Warning:[/yellow] x-features item should have exactly one key: {feature_item}")
            continue

        template_name = list(feature_item.keys())[0]
        keyword_args_raw = feature_item[template_name]

        # Look up template definition
        if template_name not in label_templates:
            console.print(f"[red]Error:[/red] Unknown label template in x-features: {template_name}")
            raise typer.Exit(1)

        template_def = label_templates[template_name]
        template_args = template_def.get("args", [])
        template_defaults = template_def.get("defaults", {})

        # Handle simple value syntax for single positional argument
        # e.g., "domain: files" instead of "domain: {subdomain: files}"
        if keyword_args_raw is None:
            # Empty widget (e.g., "- widget:" with no args)
            keyword_args = {}
        elif not isinstance(keyword_args_raw, dict):
            # If there's exactly one required positional argument, use the simple value
            if len(template_args) > 0:
                first_arg = template_args[0]
                keyword_args = {first_arg: keyword_args_raw}
            else:
                console.print(f"[yellow]Warning:[/yellow] Template '{template_name}' has no positional arguments but received simple value: {keyword_args_raw}")
                continue
        else:
            keyword_args = keyword_args_raw

        # Increment counter for this template
        if template_name not in template_counters:
            template_counters[template_name] = 0
        template_counters[template_name] += 1

        # Build context with base values
        context = {
            "container": container_name,
            "index": template_counters[template_name],
            "app": config.get("app"),
        }

        # Add contexts from previous templates
        for prev_template_name, prev_context in previous_contexts.items():
            context[prev_template_name] = prev_context

        # Build the current template's argument context
        current_template_context = {}

        # Apply defaults
        for key, value in template_defaults.items():
            context[key] = value
            current_template_context[key] = value

        # Check that all required arguments are provided
        required_args = set(template_args) - set(template_defaults.keys())
        provided_args = set(keyword_args.keys())
        missing_args = required_args - provided_args

        if missing_args:
            console.print(f"[red]Error:[/red] Missing required arguments for '{template_name}' in container '{container_name}':")
            console.print(f"  Required: {', '.join(sorted(required_args))}")
            console.print(f"  Provided: {', '.join(sorted(provided_args))}")
            console.print(f"  Missing: {', '.join(sorted(missing_args))}")
            raise typer.Exit(1)

        # Apply provided keyword arguments
        for key, value in keyword_args.items():
            # Parse boolean values
            if isinstance(value, str):
                if value.lower() == 'true':
                    parsed_value = True
                elif value.lower() == 'false':
                    parsed_value = False
                else:
                    parsed_value = value
            else:
                parsed_value = value
            context[key] = parsed_value
            current_template_context[key] = parsed_value

        # Add kwargs dict for templates to iterate over
        context["kwargs"] = keyword_args

        # Store this template's context for future templates
        previous_contexts[template_name] = current_template_context

        # Render template
        from jinja2 import Environment

        def finalize_value(value):
            """Convert Python True/False to lowercase true/false for YAML/JSON compatibility."""
            if isinstance(value, bool):
                return 'true' if value else 'false'
            return value

        env = Environment(finalize=finalize_value, extensions=['jinja2.ext.do'])
        template_str = template_def.get("template", "")
        jinja_template = env.from_string(template_str)
        rendered = jinja_template.render(**context)

        # Split rendered template into individual labels
        for line in rendered.split('\n'):
            line = line.strip()
            if line and line.startswith('-'):
                line_content = line[2:].strip()

                # Check if this line contains a template call that needs recursive rendering
                if line_content.startswith('@'):
                    # Recursively render nested templates, passing along the contexts and counters
                    nested_rendered = render_label_templates(
                        [line_content],
                        container_name,
                        config,
                        previous_contexts=previous_contexts,
                        template_counters=template_counters
                    )
                    # Add all recursively rendered labels
                    rendered_labels.extend(nested_rendered)
                elif line_content:
                    rendered_labels.append(line_content)

    return rendered_labels


def render_label_templates(
    labels: list[str] | None,
    container_name: str,
    config: dict[str, Any],
    previous_contexts: dict[str, dict[str, Any]] | None = None,
    template_counters: dict[str, int] | None = None
) -> list[str]:
    """
    Process label templates in the format @template(args, key=value).
    Expands templates from config['labels'] section.
    Detects duplicate labels and raises an error if values conflict.
    Templates can access the context of previously rendered templates.
    Supports recursive template calls (templates can call other templates).
    """
    if not labels:
        return []

    # Initialize mutable defaults
    if previous_contexts is None:
        previous_contexts = {}
    if template_counters is None:
        template_counters = {}

    label_templates = config.get("labels", {})
    rendered_labels = []
    label_map = {}  # Track label keys and their values to detect conflicts

    for label in labels:
        template_name, positional_args, keyword_args = parse_label_template_call(label)

        if template_name is None:
            # Not a template call, keep as-is
            rendered_labels.append(label)
            continue

        # Look up template definition
        if template_name not in label_templates:
            console.print(f"[yellow]Warning:[/yellow] Unknown label template: @{template_name}")
            rendered_labels.append(label)
            continue

        template_def = label_templates[template_name]
        template_args = template_def.get("args", [])
        template_defaults = template_def.get("defaults", {})
        template_str = template_def.get("template", "")

        # Increment counter for this template
        if template_name not in template_counters:
            template_counters[template_name] = 0
        template_counters[template_name] += 1

        # Build template context starting with base values
        context = {
            "container": container_name,
            "index": template_counters[template_name],
            "app": config.get("app"),
        }

        # Add contexts from previous templates
        for prev_template_name, prev_context in previous_contexts.items():
            context[prev_template_name] = prev_context

        # Build the current template's argument context
        current_template_context = {}

        # Build kwargs dict with only user-provided arguments (no defaults)
        kwargs_dict = {}

        # Apply defaults first
        for key, value in template_defaults.items():
            context[key] = value
            current_template_context[key] = value

        # Add positional arguments (override defaults)
        for i, arg_name in enumerate(template_args):
            if i < len(positional_args):
                value = positional_args[i]
                context[arg_name] = value
                current_template_context[arg_name] = value
                kwargs_dict[arg_name] = value

        # Add keyword arguments (override defaults and positional args)
        for key, value in keyword_args.items():
            # Parse boolean values
            if value.lower() == 'true':
                parsed_value = True
            elif value.lower() == 'false':
                parsed_value = False
            else:
                parsed_value = value
            context[key] = parsed_value
            current_template_context[key] = parsed_value
            kwargs_dict[key] = parsed_value

        # Add kwargs dict to context for templates to iterate over user-provided args
        context["kwargs"] = kwargs_dict

        # Store this template's context for future templates
        previous_contexts[template_name] = current_template_context

        # Render template with custom Jinja2 environment
        from jinja2 import Environment

        # Create custom environment with finalize function to convert booleans
        def finalize_value(value):
            """Convert Python True/False to lowercase true/false for YAML/JSON compatibility."""
            if isinstance(value, bool):
                return 'true' if value else 'false'
            return value

        env = Environment(finalize=finalize_value, extensions=['jinja2.ext.do'])

        jinja_template = env.from_string(template_str)
        rendered = jinja_template.render(**context)

        # Split rendered template into individual labels (one per line, strip empty)
        for line in rendered.split('\n'):
            line = line.strip()
            if line and line.startswith('-'):
                # Remove leading "- " from YAML list format
                line_content = line[2:].strip()

                # Check if this line contains a template call that needs recursive rendering
                if line_content.startswith('@'):
                    # Recursively render nested templates, passing along the contexts and counters
                    nested_rendered = render_label_templates(
                        [line_content],
                        container_name,
                        config,
                        previous_contexts=previous_contexts,
                        template_counters=template_counters
                    )
                    # Add all recursively rendered labels
                    rendered_labels.extend(nested_rendered)
                else:
                    rendered_labels.append(line_content)

    # Check for duplicate labels and conflicts
    final_labels = []
    for label in rendered_labels:
        if '=' in label:
            key, value = label.split('=', 1)
        else:
            # Labels without '=' (just keys)
            key = label
            value = None

        if key in label_map:
            # Duplicate label found
            existing_value = label_map[key]
            if existing_value != value:
                # Conflict: same key, different values
                console.print(f"[red]Error:[/red] Duplicate label '{key}' with conflicting values in container '{container_name}':")
                console.print(f"  First value:  {existing_value if existing_value is not None else '(no value)'}")
                console.print(f"  Second value: {value if value is not None else '(no value)'}")
                raise typer.Exit(1)
            # Otherwise, it's a duplicate with the same value - just skip it
        else:
            # New label, add it
            label_map[key] = value
            final_labels.append(label)

    return final_labels


def merge_environment(base_env: list[str] | None, service_env: list[str] | None) -> list[str]:
    """
    Merge environment variables, allowing service_env to override base_env.
    Both inputs are lists in the format ['KEY=value', 'KEY2=value2'].
    Service environment variables override base environment variables.
    """
    if base_env is None and service_env is None:
        return []
    if base_env is None:
        return service_env or []
    if service_env is None:
        return base_env

    # Convert to dicts for merging
    base_dict = {}
    for item in base_env:
        if "=" in item:
            key, value = item.split("=", 1)
            base_dict[key] = value

    service_dict = {}
    for item in service_env:
        if "=" in item:
            key, value = item.split("=", 1)
            service_dict[key] = value

    # Merge: service overrides base
    merged = {**base_dict, **service_dict}

    # Convert back to list format
    return [f"{k}={v}" for k, v in merged.items()]


def merge_networks(base_networks: dict[str, Any] | None, service_networks: dict[str, Any] | None) -> dict[str, Any]:
    """
    Merge network configurations, allowing service_networks to override base_networks.
    Both inputs are dicts in the format {'network_name': config_or_null}.
    Service networks override base networks.
    """
    if base_networks is None and service_networks is None:
        return {}
    if base_networks is None:
        return service_networks or {}
    if service_networks is None:
        return base_networks

    # Merge: service overrides base
    return {**base_networks, **service_networks}


def apply_common_container_config(service_config: dict[str, Any], common_config: dict[str, Any]) -> dict[str, Any]:
    """
    Apply common container configuration to a service, merging where applicable.
    Service-specific values override common values.
    """
    if not common_config:
        return service_config

    # Process each property in common config
    for key, common_value in common_config.items():
        if common_value is None:
            continue

        service_value = service_config.get(key)

        # Special handling for specific properties
        if key == "environment":
            # Merge environment variables (lists with KEY=VALUE format)
            merged_env = merge_environment(common_value, service_value)
            if merged_env:
                service_config[key] = merged_env

        elif key == "networks":
            # Merge networks (only if service doesn't use network_mode: host)
            if service_config.get("network_mode") != "host":
                merged_networks = merge_networks(common_value, service_value)
                if merged_networks:
                    service_config[key] = merged_networks

        elif key == "x-features":
            # Merge x-features lists with intelligent merging
            if isinstance(common_value, list):
                if service_value is None:
                    service_config[key] = common_value
                elif isinstance(service_value, list):
                    # Build a map of common features by name
                    common_features = {}
                    for feature_item in common_value:
                        if isinstance(feature_item, dict) and len(feature_item) == 1:
                            feature_name = list(feature_item.keys())[0]
                            common_features[feature_name] = feature_item[feature_name]

                    # Process service features
                    merged_features = []
                    service_feature_names = set()
                    common_feature_usage = {}  # Track how many times each common feature is used

                    for feature_item in service_value:
                        if not isinstance(feature_item, dict) or len(feature_item) != 1:
                            merged_features.append(feature_item)
                            continue

                        feature_name = list(feature_item.keys())[0]
                        feature_value = feature_item[feature_name]

                        # Track if this is a common feature
                        if feature_name in common_features:
                            if feature_name in common_feature_usage:
                                # Error: common feature used multiple times in service
                                from rich.console import Console
                                console = Console()
                                console.print(f"[red]Error:[/red] Feature '{feature_name}' from common config is used multiple times in service '{service_config.get('container_name', 'unknown')}'")
                                console.print(f"  Common features can only be overridden once per service")
                                raise ValueError(f"Duplicate common feature '{feature_name}' in service")

                            common_feature_usage[feature_name] = True

                            # Merge: service properties override common ones
                            common_props = common_features[feature_name]
                            if isinstance(common_props, dict) and isinstance(feature_value, dict):
                                # Deep merge dicts
                                merged_props = {**common_props, **feature_value}
                                merged_features.append({feature_name: merged_props})
                            elif feature_value is None and common_props is not None:
                                # Service has empty feature, use common
                                merged_features.append({feature_name: common_props})
                            else:
                                # Service value takes precedence
                                merged_features.append(feature_item)
                        else:
                            # Not a common feature, keep as-is
                            merged_features.append(feature_item)

                        service_feature_names.add(feature_name)

                    # Add common features that weren't used in service
                    for feature_name, feature_value in common_features.items():
                        if feature_name not in service_feature_names:
                            merged_features.append({feature_name: feature_value})

                    service_config[key] = merged_features

        elif isinstance(common_value, dict) and isinstance(service_value, dict):
            # For nested dicts (like logging), do a deep merge
            service_config[key] = deep_merge(common_value, service_value)

        elif service_value is None:
            # If service doesn't have this property, add it from common
            service_config[key] = common_value

        # If service has the property and it's not a dict, service value takes precedence (do nothing)

    return service_config


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Deep merge two dictionaries. Override values take precedence.
    For nested dicts, merges recursively. For other types, override replaces base.
    """
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Override replaces base
            result[key] = value

    return result


def replace_secrets(yaml_content: str, secrets_path: Path, redacted: bool = False) -> str:
    """
    Replace $variable placeholders with values from secrets file.
    Only replaces variables that exist in the secrets file.
    Docker compose also uses $variable syntax for environment variables,
    so we only replace if the variable is found in secrets.

    Args:
        yaml_content: The YAML content to process
        secrets_path: Path to the .secrets.yml file
        redacted: If True, replace all secrets with "<REDACTED>" instead of actual values
    """
    if not secrets_path.exists():
        # No secrets file, return content as-is
        return yaml_content

    # Load secrets
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f) or {}

    # Replace each secret
    result = yaml_content
    for key, value in secrets.items():
        # Replace $key with the value or <REDACTED>
        # Use a pattern that matches $key as a whole word
        pattern = r'\$' + re.escape(key) + r'\b'
        replacement_value = "<REDACTED>" if redacted else str(value)
        result = re.sub(pattern, replacement_value, result)

    return result


def render_compose(template: dict[str, Any], config: dict[str, Any], app_name: str = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Render the docker-compose template with boilerplate.

    Returns a tuple of (rendered_config, default_volume_dirs).
    default_volume_dirs is a list of dicts with path, uid, gid, mode for directories to create.
    """
    if "services" not in template:
        return template, []

    # Add app name to config for use in templates
    if app_name:
        config = config.copy()
        config["app"] = app_name

    # Get common container configuration
    common_container = config.get("common", {}).get("container", {})

    # Get default volume config for directory creation
    default_volume_config = config.get("volumes", {}).get("default", {})
    has_default_config = all(k in default_volume_config for k in ["uid", "gid", "mode"])

    # Track directories to create
    dirs_to_create = []

    for service_name, service_config in template["services"].items():
        # Process volumes first
        if "volumes" in service_config:
            processed_volumes = []
            for volume in service_config["volumes"]:
                # Only process string volumes (short syntax)
                # Dict volumes (long syntax) are passed through as-is
                if isinstance(volume, str):
                    rendered_volume, uses_default = parse_volume_placeholder(volume, service_name, config)
                    processed_volumes.append(rendered_volume)

                    # If this volume uses the default template, track it for directory creation
                    if uses_default and has_default_config:
                        # Extract host path from rendered volume
                        host_path = rendered_volume.split(":")[0]
                        dirs_to_create.append({
                            "path": host_path,
                            "uid": default_volume_config["uid"],
                            "gid": default_volume_config["gid"],
                            "mode": str(default_volume_config["mode"]),
                        })
                else:
                    processed_volumes.append(volume)
            service_config["volumes"] = processed_volumes

        # Apply common container configuration (environment, networks, etc.)
        service_config = apply_common_container_config(service_config, common_container)

        # Process x-features first, then merge with labels
        all_labels = []

        # Process x-features if present
        if "x-features" in service_config:
            x_features_labels = process_x_features(
                service_config["x-features"], service_name, config
            )
            all_labels.extend(x_features_labels)
            # Remove x-features from final config (it's only for processing)
            del service_config["x-features"]

        # Process label templates from labels section
        if "labels" in service_config:
            template_labels = render_label_templates(
                service_config["labels"], service_name, config
            )
            all_labels.extend(template_labels)

        # Set combined labels if any were generated
        if all_labels:
            service_config["labels"] = all_labels

        # Build new config with proper field order
        new_config = {}

        # Add fields in desired order
        field_order = [
            "image", "container_name", "cap_add", "privileged", "volumes",
            "devices", "ports", "restart", "networks", "environment"
        ]

        # First, add fields that exist in the original config in order
        for field in field_order:
            if field in service_config:
                new_config[field] = service_config[field]
            elif field == "container_name":
                # Add container_name after image
                new_config["container_name"] = service_name
            elif field == "restart":
                # Add restart policy at the end
                new_config["restart"] = "unless-stopped"

        # Add any remaining fields that weren't in our order list
        for field, value in service_config.items():
            if field not in new_config:
                new_config[field] = value

        # Update the service config
        template["services"][service_name] = new_config

    # Merge common compose-level configuration (e.g., networks, volumes)
    common_compose = config.get("common", {}).get("compose", {})
    if common_compose:
        template = deep_merge(template, common_compose)

    return template, dirs_to_create


def preprocess_yaml(content: str) -> str:
    """
    Preprocess YAML content to quote @ placeholders.
    YAML treats @ as a reserved character, so we need to quote it.
    """
    lines = []
    for line in content.split("\n"):
        # Check if line contains an unquoted @ in a volume definition
        stripped = line.lstrip()
        if stripped.startswith("- @"):
            # Extract the indentation
            indent = line[: len(line) - len(stripped)]
            # Extract value and preserve comment
            value = stripped[2:]  # Remove "- "
            # Split on comment
            if "#" in value:
                value_part, comment_part = value.split("#", 1)
                value_part = value_part.rstrip()
                lines.append(f'{indent}- "{value_part}"  # {comment_part}')
            else:
                lines.append(f'{indent}- "{value}"')
        else:
            lines.append(line)
    return "\n".join(lines)


# ============================================================================
# TrueNAS Integration
# ============================================================================

def load_truenas_config() -> dict[str, Any]:
    """Load TrueNAS configuration from config file."""
    if not TRUENAS_CONFIG_FILE.exists():
        return {}

    with open(TRUENAS_CONFIG_FILE) as f:
        return yaml.safe_load(f) or {}


def save_truenas_config(config: dict[str, Any]):
    """Save TrueNAS configuration to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRUENAS_CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def get_truenas_client() -> Client:
    """Get a TrueNAS API client using stored credentials."""
    config = load_truenas_config()

    if not config.get("host"):
        console.print("[red]Error:[/red] TrueNAS host not configured. Run 'dalboplex login' first.")
        raise typer.Exit(1)

    if not config.get("api_key"):
        console.print("[red]Error:[/red] TrueNAS API key not configured. Run 'dalboplex login' first.")
        raise typer.Exit(1)

    host = config["host"]
    api_key = config["api_key"]

    # Ensure proper WebSocket URL format
    if not host.startswith(("ws://", "wss://")):
        # Always use wss:// (secure WebSocket) for API key authentication
        if host.startswith("http://"):
            host = host.replace("http://", "wss://")
        elif host.startswith("https://"):
            host = host.replace("https://", "wss://")
        else:
            host = f"wss://{host}"

    # Add /api/current path if not present
    if not host.endswith(("/websocket", "/api/current")):
        host = f"{host.rstrip('/')}/api/current"

    # Disable SSL verification for self-signed certificates (set in login command)
    client = Client(uri=host)
    client.call("auth.login_with_api_key", api_key)
    return client


@app.command()
def login(
    host: str = typer.Option(..., "--host", "-h", prompt=True, help="TrueNAS host (e.g., truenas.local or https://truenas.local)"),
    api_key: str = typer.Option(..., "--api-key", "-k", prompt=True, hide_input=True, help="TrueNAS API key"),
):
    """
    Configure TrueNAS connection credentials.

    This stores your TrueNAS host and API key for use with other commands.
    You can generate an API key in the TrueNAS UI under System Settings > API Keys.
    """
    # Test the connection
    console.print("[blue]Testing connection...[/blue]")

    try:
        # Normalize host URL
        test_host = host
        if not test_host.startswith(("ws://", "wss://")):
            # Always use wss:// (secure WebSocket) for API key authentication
            if test_host.startswith("http://"):
                test_host = test_host.replace("http://", "wss://")
            elif test_host.startswith("https://"):
                test_host = test_host.replace("https://", "wss://")
            else:
                test_host = f"wss://{test_host}"

        if not test_host.endswith(("/websocket", "/api/current")):
            test_host = f"{test_host.rstrip('/')}/api/current"

        with Client(uri=test_host) as c:
            # Authenticate with API key
            console.print(f"[dim]Authenticating...[/dim]")
            auth_result = c.call("auth.login_with_api_key", api_key)
            console.print(f"[dim]Auth result: {auth_result}[/dim]")

            # Test connection by getting system info
            console.print(f"[dim]Getting system info...[/dim]")
            info = c.call("system.info")
            hostname = info.get("hostname", "Unknown")
            version = info.get("version", "Unknown")

            console.print(f"[green]✓[/green] Connected to {hostname} (TrueNAS {version})")

    except Exception as e:
        console.print(f"[red]✗ Connection failed:[/red] {e}")
        raise typer.Exit(1)

    # Save configuration
    config = {
        "host": host,
        "api_key": api_key,
    }

    save_truenas_config(config)

    console.print(f"[green]✓[/green] Credentials saved to {TRUENAS_CONFIG_FILE}")


@app.command()
def status():
    """
    List all apps running on TrueNAS and their status.

    Shows the status of all custom apps installed on your TrueNAS instance.
    """
    try:
        with get_truenas_client() as client:
            # Get system info
            info = client.call("system.info")
            hostname = info.get("hostname", "Unknown")

            console.print(f"\n[bold]TrueNAS Host:[/bold] {hostname}\n")

            # Get all apps
            apps = client.call("app.query")

            if not apps:
                console.print("[yellow]No apps found.[/yellow]")
                return

            # Create a table
            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Name", style="dim", width=20)
            table.add_column("State", width=15)
            table.add_column("Version", width=15)
            table.add_column("Active Workloads", justify="right", width=15)

            for app in apps:
                name = app.get("name", "Unknown")
                state = app.get("state", "unknown")
                version = app.get("version", "N/A")

                # Get active workload count
                active_workloads = 0
                if app.get("active_workloads"):
                    active_workloads = len(app["active_workloads"])

                # Color code the state
                state_lower = state.lower()
                if state_lower == "running":
                    state_display = f"[green]{state}[/green]"
                elif state_lower == "stopped":
                    state_display = f"[red]{state}[/red]"
                elif state_lower == "deploying":
                    state_display = f"[yellow]{state}[/yellow]"
                else:
                    state_display = f"[dim]{state}[/dim]"

                table.add_row(
                    name,
                    state_display,
                    version,
                    str(active_workloads)
                )

            console.print(table)
            console.print(f"\n[dim]Total apps: {len(apps)}[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _update_single_app(
    app_name: str,
    config: Path,
    apps_dir: Path,
    force: bool,
    dry_run: bool,
    state: dict,
    state_path: Path,
) -> bool:
    """
    Update a single TrueNAS app. Returns True if successful, False otherwise.
    """
    if not dry_run:
        console.print(f"[blue]Updating app:[/blue] {app_name}")

    # Check if compose file exists
    compose_file = apps_dir / f"{app_name}.yml"
    if not compose_file.exists():
        console.print(f"[red]Error:[/red] {app_name}.yml not found in {apps_dir}")
        return False

    # Render the docker-compose file
    try:
        # Load and render the template
        with open(compose_file) as f:
            content = f.read()
            preprocessed = preprocess_yaml(content)
            template = yaml.safe_load(preprocessed)

        render_config = load_config(config)
        rendered, dirs_to_ensure = render_compose(template, render_config, app_name)

        # Save rendered file to apps/.state/rendered/<app_name>.yml
        rendered_dir = apps_dir / ".state" / "rendered"
        rendered_dir.mkdir(parents=True, exist_ok=True)

        output_file = rendered_dir / f"{app_name}.yml"
        with open(output_file, "w") as f:
            yaml.dump(
                rendered,
                f,
                Dumper=NoAliasDumper,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=1000,
            )

        # Convert rendered config to YAML string for TrueNAS
        rendered_yaml = yaml.dump(rendered, Dumper=NoAliasDumper, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)

        # Replace secrets from .secrets.yml
        secrets_path = apps_dir / ".secrets.yml"
        rendered_yaml = replace_secrets(rendered_yaml, secrets_path)

        # Check for installed version and show diff
        installed_dir = apps_dir / ".state" / "installed"
        installed_file = installed_dir / f"{app_name}.yml"

        if installed_file.exists():
            with open(installed_file) as f:
                installed_yaml = f.read()

            # Show diff between installed and newly rendered
            if dry_run:
                console.print(f"[blue]Comparing with installed version[/blue]")
                show_diff(installed_yaml, rendered_yaml, f"installed/{app_name}.yml", f"rendered/{app_name}.yml")
                return True
            else:
                # Show diff before updating (unless they're identical)
                if installed_yaml != rendered_yaml:
                    show_diff(installed_yaml, rendered_yaml, f"installed/{app_name}.yml", f"rendered/{app_name}.yml")
        else:
            # No installed version exists
            if dry_run:
                from rich.syntax import Syntax
                console.print("[yellow]No installed version found - showing rendered config:[/yellow]\n")
                syntax = Syntax(rendered_yaml, "yaml", theme="monokai", line_numbers=False)
                console.print(syntax)
                return True
            else:
                console.print("[dim]No previous installation found[/dim]")

        # Compute hash of the rendered configuration
        new_hash = compute_config_hash(rendered_yaml)

        # Check if configuration has changed (unless force flag is set)
        if not force and app_name in state and state[app_name].get("hash") == new_hash:
            console.print(f"[yellow]⊘[/yellow] No changes detected (use --force to update anyway)")
            return True

        if force and app_name in state and state[app_name].get("hash") == new_hash:
            console.print(f"[yellow]⚠[/yellow] Forcing update (no changes detected)")

        with get_truenas_client() as client:
            # Check if app exists
            apps = client.call("app.query", [["name", "=", app_name]])
            app_exists = len(apps) > 0

            # Ensure directories exist with correct permissions (collected during rendering)
            if dirs_to_ensure:
                ensure_directories(client, dirs_to_ensure)

            if not app_exists:
                # Create new app
                try:
                    create_data = {
                        "custom_app": True,
                        "app_name": app_name,
                        "version": "1.0.0",
                        "train": "stable",
                        "custom_compose_config_string": rendered_yaml,
                    }
                    result = client.call("app.create", create_data)
                    console.print(f"[green]✓[/green] Created '{app_name}'")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to create app: {e}")
                    return False
            else:
                # Update existing app
                try:
                    update_data = {
                        "custom_compose_config_string": rendered_yaml,
                    }
                    result = client.call("app.update", app_name, update_data)
                    console.print(f"[green]✓[/green] Updated '{app_name}'")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to update app: {e}")
                    return False

            # Save the new hash to state file only after successful deployment
            if app_name not in state:
                state[app_name] = {}
            state[app_name]["hash"] = new_hash
            save_state(state_path, state)

            # Save the rendered file with secrets to installed directory
            installed_dir.mkdir(parents=True, exist_ok=True)
            with open(installed_file, "w") as f:
                f.write(rendered_yaml)
            console.print(f"[dim]Saved installed config to {installed_file}[/dim]")

        return True

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] File not found: {e}")
        return False
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return False


@app.command()
def update(
    app_name: str = typer.Argument(None, help="Name of the application to update (omit when using --all)"),
    config: Path = typer.Option("apps/.config.yml", "--config", "-c", help="Render configuration file"),
    apps_dir: Path = typer.Option("apps", "--apps-dir", help="Directory containing app definitions"),
    force: bool = typer.Option(False, "--force", "-f", help="Force update even if configuration hasn't changed"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Render and display the configuration without updating"),
    all_apps: bool = typer.Option(False, "--all", "-a", help="Update all apps"),
):
    """
    Update a TrueNAS app by rendering and deploying its docker-compose configuration.

    This command will:
    1. Find the <app_name>.yml in apps/
    2. Render it with the configured templates
    3. Update the app in TrueNAS with the rendered configuration

    Use --dry-run to preview the rendered configuration without making changes.
    Use --all to update all apps in the apps directory.
    """
    # Validate arguments
    if all_apps and app_name:
        console.print("[red]Error:[/red] Cannot specify both app_name and --all")
        raise typer.Exit(1)

    if not all_apps and not app_name:
        console.print("[red]Error:[/red] Must specify either app_name or --all")
        raise typer.Exit(1)

    # Load state file
    state_path = apps_dir / ".state" / "state.yml"
    state = load_state(state_path)

    if all_apps:
        # Find all .yml files in apps directory
        compose_files = sorted(apps_dir.glob("*.yml"))
        # Filter out config and secrets files
        app_files = [
            f for f in compose_files
            if f.name not in [".config.yml", ".secrets.yml"]
        ]

        if not app_files:
            console.print(f"[yellow]No app files found in {apps_dir}[/yellow]")
            raise typer.Exit(0)

        # Extract app names
        app_names = [f.stem for f in app_files]

        console.print(f"[blue]Updating {len(app_names)} apps:[/blue] {', '.join(app_names)}\n")

        # Track results
        successful = []
        failed = []

        for app_name in app_names:
            try:
                success = _update_single_app(
                    app_name=app_name,
                    config=config,
                    apps_dir=apps_dir,
                    force=force,
                    dry_run=dry_run,
                    state=state,
                    state_path=state_path,
                )
                if success:
                    successful.append(app_name)
                else:
                    failed.append(app_name)
            except Exception as e:
                console.print(f"[red]Error updating {app_name}:[/red] {e}")
                failed.append(app_name)

            # Add spacing between apps
            if app_name != app_names[-1]:
                console.print()

        # Print summary
        console.print()
        console.print("[blue]═" * 60 + "[/blue]")
        console.print(f"[green]✓[/green] Successfully updated: {len(successful)}/{len(app_names)}")
        if failed:
            console.print(f"[red]✗[/red] Failed: {len(failed)}/{len(app_names)} ({', '.join(failed)})")
            raise typer.Exit(1)
    else:
        # Update single app
        success = _update_single_app(
            app_name=app_name,
            config=config,
            apps_dir=apps_dir,
            force=force,
            dry_run=dry_run,
            state=state,
            state_path=state_path,
        )
        if not success:
            raise typer.Exit(1)


def _render_single_file(
    input_file: Path,
    output_file: Optional[Path],
    render_config: dict,
    redacted: bool,
) -> Path:
    """Helper function to render a single compose file."""
    # Determine output file
    if output_file is None:
        # Default to apps/.state/rendered/{app_name}.yml
        app_name = input_file.stem
        output_dir = input_file.parent / ".state" / "rendered"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{app_name}.yml"
    else:
        app_name = input_file.stem

    # Load files with preprocessing
    with open(input_file) as f:
        content = f.read()
        preprocessed = preprocess_yaml(content)
        template = yaml.safe_load(preprocessed)

    # Render template (ignore dirs_to_ensure for standalone render command)
    rendered, _ = render_compose(template, render_config, app_name)

    # Convert to YAML string
    rendered_yaml = yaml.dump(
        rendered,
        Dumper=NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=1000,  # Prevent line wrapping
    )

    # Replace secrets from .secrets.yml
    secrets_path = input_file.parent / ".secrets.yml"
    rendered_yaml = replace_secrets(rendered_yaml, secrets_path, redacted=redacted)

    # Write output with proper formatting
    with open(output_file, "w") as f:
        f.write(rendered_yaml)

    return output_file


@app.command()
def render(
    input_file: Optional[Path] = typer.Argument(None, help="Input docker-compose template file"),
    output_file: Optional[Path] = typer.Argument(None, help="Output file (default: apps/.state/rendered/{app_name}.yml)"),
    config: Path = typer.Option("apps/.config.yml", "--config", "-c", help="Render configuration file"),
    redacted: bool = typer.Option(False, "--redacted", help="Replace all secrets with '<REDACTED>' instead of actual values"),
    all_apps: bool = typer.Option(False, "--all", help="Render all app compose files in apps/ directory"),
):
    """
    Render a docker-compose template with boilerplate and path replacements.

    Processes volume placeholders like @config, @data, @docker and merges
    environment variables from the config file.
    """
    render_config = load_config(config)

    if all_apps:
        # Find all .yml files in apps/ directory (excluding subdirectories and hidden files)
        apps_dir = Path("apps")
        if not apps_dir.exists():
            typer.echo("Error: apps/ directory not found", err=True)
            raise typer.Exit(1)

        compose_files = sorted([f for f in apps_dir.glob("*.yml") if not f.name.startswith(".")])
        if not compose_files:
            typer.echo("No .yml files found in apps/ directory", err=True)
            raise typer.Exit(1)

        typer.echo(f"Rendering {len(compose_files)} compose files...")
        for compose_file in compose_files:
            output = _render_single_file(compose_file, None, render_config, redacted)
            typer.echo(f"  {compose_file} -> {output}")

        typer.echo(f"\nSuccessfully rendered {len(compose_files)} files")
    else:
        if input_file is None:
            typer.echo("Error: input_file is required when --all is not specified", err=True)
            raise typer.Exit(1)

        output = _render_single_file(input_file, output_file, render_config, redacted)
        typer.echo(f"Rendered {input_file} -> {output}")


if __name__ == "__main__":
    app()
