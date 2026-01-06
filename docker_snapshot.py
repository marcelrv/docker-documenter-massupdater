#!/usr/bin/env python3
"""
Docker-Snapshot: Audit running Docker containers and reconstruct docker run commands.

Copyright (c) 2026 Marcel Verpaalen
Author: Marcel Verpaalen

PURPOSE:
    Bridge the gap between "live" infrastructure and documentation by auditing running
    containers and reconstructing their full docker run commands into human-readable,
    multiline shell scripts. This tool creates a "source of truth" repository for your
    existing Docker environment.
    It also allows for mass update of labels and environment variables, useful for
    adding monitoring or reverse proxy configurations.

CORE FEATURES:
    - Container Discovery & Selection: Query the Docker daemon; filter by name pattern
      or process all running containers.
    - Deep Infrastructure Auditing: Extract images, networking, ports, mounts/volumes
      (with read-write status), restart policies, capabilities, sysctls, and devices.
    - Clean Configuration: Filter out system-injected environment variables and standard
      labels (org.opencontainers.*, maintainer, build_version) to reduce noise.
    - Extensible Metadata: Add/update labels and environment variables via CLI args,
      with {{name}} placeholder support.
    - Per-Container Scripts (Default): Generate individual shell scripts under a directory,
      or combine into a single script with --output.
    - Security: All values are shell-quoted to prevent breakage from spaces or special chars.

USAGE:
    python3 docker_snapshot.py [OPTIONS] [PATTERNS...]

ARGUMENTS:
    PATTERNS                Optional container name substrings (case-insensitive).
                           Processes all running containers if omitted.

OPTIONS:
    -h, --help             Show this help message and exit.
    -o, --output PATH      Write all containers to a single script PATH.
    --per-container-dir D  Directory for per-container scripts (default: recreate_containers.d).
    --include-cmd          Include the container's command (Cmd) in recreated run lines.
    --add-label KEY=VALUE  Add label if missing; {{name}} in VALUE gets replaced with
                           container name. Repeatable.
    --add-env KEY=VALUE    Add environment variable if missing; {{name}} in VALUE gets
                           replaced with container name. Repeatable.
    --add-restart POLICY   Restart policy for containers with none set. Examples:
                           'unless-stopped', 'on-failure:3'.
    --add-network NET      Network to apply when container uses default/bridge. Examples:
                           'home', 'docker_default'.

EXAMPLES:
    # Default: generate per-container scripts in recreate_containers.d/
    python3 docker_snapshot.py

    # Filter containers by name and add labels/env
    python3 docker_snapshot.py openhab --add-label watchtower.enable=true \\
      --add-env TZ=Europe/Amsterdam

    # Generate single combined script with all containers
    python3 docker_snapshot.py -o recreate_containers.sh

    # Add traefik labels with {{name}} substitution in both key and value
    python3 docker_snapshot.py --add-label 'traefik.http.routers.{{name}}.entrypoints=websecure' \\
      --add-label 'traefik.http.routers.{{name}}.rule=Host(\\`{{name}}.example.com\\`)'

    # Set default network and restart policy for all containers
    python3 docker_snapshot.py --add-network home --add-restart unless-stopped

NOTES:
    - Filtered system env vars: PATH, HOSTNAME, TERM, HOME, PWD, DOCKER_*
    - Filtered standard labels: org.opencontainers.*, maintainer, build_version
    - Non-standard capabilities only (skips default Linux caps like NET_RAW, CHOWN, etc.)
    - Duplicate port mappings are automatically deduplicated.
    - Scripts are made executable (chmod 755) and include a #!/bin/bash header.
"""

import argparse
import os
import shlex
import sys
from typing import Dict, Iterable, List, Optional, Tuple

import docker
from docker.errors import DockerException

# Environment variables that are commonly injected by Docker and should be ignored
SYSTEM_ENV_KEYS = {"PATH", "HOSTNAME", "TERM", "HOME", "PWD"}
IGNORE_LABEL_PREFIXES = ("org.opencontainers",)
IGNORE_LABEL_KEYS = {"maintainer", "build_version"}
DEFAULT_LINUX_CAPS = {
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "NET_RAW",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_CHROOT",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect running containers and emit a recreate_containers.sh script "
            "with equivalent docker run commands."
        )
    )
    parser.add_argument(
        "patterns",
        nargs="*",
        help="Optional name substrings to filter containers (case-insensitive).",
    )
    parser.add_argument(
        "--add-label",
        dest="add_labels",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Label to add if missing; supports {{name}} placeholder in VALUE. "
            "May be passed multiple times."
        ),
    )
    parser.add_argument(
        "--add-env",
        dest="add_envs",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Environment variable to add if missing; supports {{name}} placeholder "
            "in VALUE. May be passed multiple times."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-o",
        "--output",
        dest="output",
        help="Write all containers to a single script path.",
    )
    group.add_argument(
        "--per-container-dir",
        dest="per_container_dir",
        default="recreate_containers.d",
        help="Directory for per-container scripts (default: recreate_containers.d)",
    )
    parser.add_argument(
        "--include-cmd",
        action="store_true",
        help="Include the container's command (Cmd) in the recreated run line.",
    )
    parser.add_argument(
        "--add-restart",
        dest="add_restart",
        help=(
            "Restart policy to apply when the container has none set. "
            "Example: 'unless-stopped' or 'on-failure:3'."
        ),
    )
    parser.add_argument(
        "--add-network",
        dest="add_network",
        help=(
            "Network to apply when the container uses the default/bridge network. "
            "Example: 'home'."
        ),
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Skip existing files without prompting; only create new ones.",
    )
    if len(sys.argv) == 1:
        parser.print_help()

    return parser.parse_args()


def connect_client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except DockerException as exc:  # pragma: no cover - defensive
        print(f"Error connecting to Docker daemon: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_kv_args(raw_items: Iterable[str], kind: str) -> List[Tuple[str, str]]:
    parsed: List[Tuple[str, str]] = []
    for raw in raw_items:
        if "=" not in raw:
            raise ValueError(f"Invalid {kind} '{raw}', expected KEY=VALUE")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid {kind} '{raw}', key is empty")
        parsed.append((key, value))
    return parsed


def parse_label_args(label_args: Iterable[str]) -> List[Tuple[str, str]]:
    return parse_kv_args(label_args, "label")


def parse_env_args(env_args: Iterable[str]) -> List[Tuple[str, str]]:
    return parse_kv_args(env_args, "environment variable")


def filter_env_vars(env_list: Iterable[str]) -> List[str]:
    filtered: List[str] = []
    for item in env_list or []:
        if "=" not in item:
            continue
        key, _ = item.split("=", 1)
        if key in SYSTEM_ENV_KEYS or key.startswith("DOCKER_"):
            continue
        filtered.append(item)
    return filtered


def filter_labels(labels: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value
        for key, value in (labels or {}).items()
        if key not in IGNORE_LABEL_KEYS and not key.startswith(IGNORE_LABEL_PREFIXES)
    }


def format_restart_policy(policy: Dict) -> str:
    name = policy.get("Name") or ""
    if not name:
        return ""
    if name == "on-failure":
        retry = policy.get("MaximumRetryCount")
        if retry:
            return f"on-failure:{retry}"
    return name


def collect_ports(port_settings: Dict[str, List[Dict]]) -> List[str]:
    port_args: List[str] = []
    seen: set = set()
    for container_port, bindings in (port_settings or {}).items():
        if not bindings:
            continue  # skip internal-only ports
        for binding in bindings:
            host_port = binding.get("HostPort")
            if not host_port:
                continue
            host_ip = binding.get("HostIp")
            if host_ip and host_ip not in {"0.0.0.0", "::", ""}:
                host = f"{host_ip}:{host_port}"
            else:
                host = host_port
            spec = f"{host}:{container_port}"
            arg = f"-p {shlex.quote(spec)}"
            if arg in seen:
                continue
            seen.add(arg)
            port_args.append(arg)
    return port_args


def collect_mounts(mounts: List[Dict]) -> List[str]:
    mount_args: List[str] = []
    for mount in mounts or []:
        destination = mount.get("Destination")
        if not destination:
            continue
        mount_type = mount.get("Type")
        ro_suffix = ":ro" if mount.get("RW") is False else ""
        if mount_type == "bind":
            source = mount.get("Source")
            if not source:
                continue
            spec_raw = f"{source}:{destination}{ro_suffix}"
            mount_args.append(f"-v {shlex.quote(spec_raw)}")
        elif mount_type == "volume":
            name = mount.get("Name") or mount.get("Source")
            if not name:
                continue
            spec_raw = f"{name}:{destination}{ro_suffix}"
            mount_args.append(f"-v {shlex.quote(spec_raw)}")
    return mount_args


def collect_capabilities(host_cfg: Dict) -> List[str]:
    def _norm(cap: str) -> str:
        cap_up = cap.upper()
        return cap_up[4:] if cap_up.startswith("CAP_") else cap_up

    caps = host_cfg.get("CapAdd") or []
    args: List[str] = []
    for cap in caps:
        if _norm(cap) in DEFAULT_LINUX_CAPS:
            continue
        args.append(f"--cap-add {shlex.quote(cap)}")
    return args


def collect_sysctls(host_cfg: Dict) -> List[str]:
    sysctls = host_cfg.get("Sysctls") or {}
    args: List[str] = []
    for key, value in sysctls.items():
        args.append(f"--sysctl {shlex.quote(f'{key}={value}')}")
    return args


def collect_devices(host_cfg: Dict) -> List[str]:
    devices = host_cfg.get("Devices") or []
    args: List[str] = []
    for device in devices:
        path_on_host = device.get("PathOnHost")
        path_in_container = device.get("PathInContainer")
        if not path_on_host or not path_in_container:
            continue
        cgroup_perms = device.get("CgroupPermissions") or "rwm"
        spec = f"{path_on_host}:{path_in_container}:{cgroup_perms}"
        args.append(f"--device {shlex.quote(spec)}")
    return args


def merge_labels(existing: Dict[str, str], additions: List[Tuple[str, str]], name: str) -> Dict[str, str]:
    merged = dict(existing or {})
    for key, raw_value in additions:
        rendered_key = key.replace("{{name}}", name)
        if rendered_key in merged:
            continue
        value = raw_value.replace("{{name}}", name)
        merged[rendered_key] = value
    return merged


def merge_envs(existing_envs: List[str], additions: List[Tuple[str, str]], name: str) -> List[str]:
    env_map: Dict[str, str] = {}
    for item in existing_envs:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        env_map.setdefault(key, value)

    for key, raw_value in additions:
        if key in env_map:
            continue
        value = raw_value.replace("{{name}}", name)
        env_map[key] = value

    return [f"{k}={v}" for k, v in env_map.items()]


def sanitize_filename(name: str) -> str:
    trimmed = name.strip("/") or "container"
    safe = "".join(ch if (ch.isalnum() or ch in {"-", "_", "."}) else "_" for ch in trimmed)
    return safe


def should_overwrite(path: str, overwrite_all: bool) -> Tuple[bool, bool]:
    if not os.path.exists(path):
        return True, overwrite_all
    if overwrite_all:
        return True, overwrite_all

    while True:
        response = input(f"{path} exists. Overwrite? [y/N/a] ").strip().lower()
        if response in {"y", "yes"}:
            return True, overwrite_all
        if response in {"a", "all"}:
            return True, True
        if response in {"", "n", "no"}:
            return False, overwrite_all


def format_command(
    container,
    add_label_pairs: List[Tuple[str, str]],
    add_env_pairs: List[Tuple[str, str]],
    add_restart: str,
    add_network: Optional[str],
    include_cmd: bool,
) -> str:
    attrs = container.attrs
    name = container.name
    cfg = attrs.get("Config", {})
    host_cfg = attrs.get("HostConfig", {})
    net_settings = attrs.get("NetworkSettings", {})

    args: List[str] = [f"--name {shlex.quote(name)}"]

    network_mode = host_cfg.get("NetworkMode")
    if network_mode and network_mode not in {"bridge", "default"}:
        args.append(f"--network {shlex.quote(network_mode)}")
    elif add_network:
        args.append(f"--network {shlex.quote(add_network)}")

    restart_value = format_restart_policy(host_cfg.get("RestartPolicy", {})) or add_restart
    if restart_value:
        args.append(f"--restart {shlex.quote(restart_value)}")

    args.extend(collect_ports(net_settings.get("Ports")))
    args.extend(collect_mounts(attrs.get("Mounts")))
    args.extend(collect_devices(host_cfg))
    args.extend(collect_capabilities(host_cfg))
    args.extend(collect_sysctls(host_cfg))

    env_vars = merge_envs(filter_env_vars(cfg.get("Env", [])), add_env_pairs, name)
    for env in env_vars:
        args.append(f"-e {shlex.quote(env)}")

    labels = merge_labels(filter_labels(cfg.get("Labels") or {}), add_label_pairs, name)
    for key, value in labels.items():
        args.append(f"--label {shlex.quote(f'{key}={value}')}")

    image = cfg.get("Image") or (container.image.tags[0] if container.image.tags else container.image.short_id)
    args.append(shlex.quote(image))

    if include_cmd:
        entrypoint = cfg.get("Entrypoint") or []
        cmd = cfg.get("Cmd") or []
        
        if isinstance(entrypoint, str):
            entrypoint = [entrypoint]
        if isinstance(cmd, str):
            cmd = [cmd]
        
        # If Entrypoint is set, Cmd contains only parameters; otherwise, Cmd's first element is the command
        if entrypoint:
            # Entrypoint is the command, all of Cmd is parameters
            for part in cmd:
                args.append(shlex.quote(part))
        else:
            # Cmd's first element is the command, rest are parameters
            for part in cmd[1:]:
                args.append(shlex.quote(part))

    formatted = " \\\n".join(["docker run"] + [f"  {arg}" for arg in args])
    return formatted


def render_container_block(
    container,
    add_label_pairs: List[Tuple[str, str]],
    add_env_pairs: List[Tuple[str, str]],
    add_restart: str,
    add_network: Optional[str],
    include_cmd: bool,
) -> str:
    cmd = format_command(container, add_label_pairs, add_env_pairs, add_restart, add_network, include_cmd)
    return f"# Container: {container.name}\n{cmd}\n"


def write_output_combined(path: str, blocks: List[str]) -> int:
    should_write, _ = should_overwrite(path, overwrite_all=False)
    if not should_write:
        print(f"Skipped writing {path}", file=sys.stderr)
        return 0

    script = ["#!/bin/bash", ""]
    script.extend(blocks)
    content = "\n".join(script).rstrip() + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)
    return 1


def write_output_per_container(directory: str, blocks: List[Tuple[str, str]], no_overwrite: bool = False) -> int:
    os.makedirs(directory, exist_ok=True)
    written = 0
    overwrite_all = False

    for name, block in blocks:
        filename = f"{sanitize_filename(name)}.sh"
        full_path = os.path.join(directory, filename)
        if no_overwrite and os.path.exists(full_path):
            continue
        should_write, overwrite_all = should_overwrite(full_path, overwrite_all)
        if not should_write:
            continue

        content = "#!/bin/bash\n\n" + block.rstrip() + "\n"
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(full_path, 0o755)
        written += 1

    return written


def select_containers(containers: List, patterns: List[str]) -> List:
    if not patterns:
        return containers
    lowered = [p.lower() for p in patterns]
    selected = []
    for c in containers:
        if any(pat in c.name.lower() for pat in lowered):
            selected.append(c)
    return selected


def main() -> int:
    args = parse_args()
    try:
        label_pairs = parse_label_args(args.add_labels)
        env_pairs = parse_env_args(args.add_envs)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    client = connect_client()
    try:
        running = client.containers.list()
    except DockerException as exc:
        print(f"Error listing containers: {exc}", file=sys.stderr)
        return 1

    target_containers = select_containers(running, args.patterns)
    if not target_containers:
        print("No matching running containers found.", file=sys.stderr)
        return 0

    blocks = [
        (
            c.name,
            render_container_block(
                c,
                label_pairs,
                env_pairs,
                args.add_restart if not format_restart_policy(c.attrs.get("HostConfig", {}).get("RestartPolicy", {})) else "",
                args.add_network if (c.attrs.get("HostConfig", {}).get("NetworkMode") in {None, "", "bridge", "default"}) else None,
                args.include_cmd,
            ),
        )
        for c in target_containers
    ]

    if args.output:
        written = write_output_combined(args.output, [block for _, block in blocks])
        if written:
            print(f"Wrote {len(blocks)} container definitions to {args.output}")
    else:
        written = write_output_per_container(args.per_container_dir, blocks, args.no_overwrite)
        print(
            f"Wrote {written}/{len(blocks)} container scripts to {args.per_container_dir}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
