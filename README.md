# Docker-Snapshot

**Docker-Snapshot** is a Python utility that audits your running Docker containers and reconstructs their full `docker run` commands into human-readable shell scripts. It bridges the gap between "live" infrastructure and documentation, creating a single source of truth for your containerized environment.

## Purpose

Docker containers are often launched via ad-hoc command-line entries, and the original configuration is lost. Docker-Snapshot interrogates the Docker daemon to extract the complete runtime state of each container and emits reproducible shell scripts that can recreate them—with optional mass updates to labels and environment variables.

## Features

- **Container Discovery & Selection**: Query all running containers or filter by name pattern (case-insensitive).
- **Deep Infrastructure Auditing**: Extracts:
  - Images and tags
  - Network mode (custom networks preserved; default/bridge can be overridden)
  - Port mappings (with TCP/UDP support; duplicates removed)
  - Bind mounts and named volumes (with read-write status)
  - Restart policies
  - Capabilities (non-standard only; filters out defaults like NET_RAW, CHOWN, etc.)
  - Sysctls
  - Device mappings
  - Environment variables (system-injected ones filtered out)
  - Labels (standard ones filtered out; can be extended)
- **Clean Configuration**: Automatically excludes noise:
  - System env vars: `PATH`, `HOSTNAME`, `TERM`, `HOME`, `PWD`, and `DOCKER_*` prefixed
  - Standard labels: `org.opencontainers.*`, `maintainer`, `build_version`
- **Extensible Metadata**: Add/update labels and environment variables via CLI with `{{name}}` placeholder support for both keys and values.
- **Per-Container Scripts (Default)**: Generates individual executable shell scripts organized in a directory, or combine into a single script.
- **Security**: All values are shell-quoted to prevent breakage from spaces or special characters.
- **Idempotency**: Additions (labels, env, restart policy, network) only apply when not already present on the container.

## Installation

### Requirements
- Python 3.6+
- Docker daemon running and accessible
- Python `docker` library

### Setup

```bash
# Install the docker Python package
pip install docker

# Clone or download docker_snapshot.py to your workspace
cd /path/to/your/workspace
chmod +x docker_snapshot.py
```

## Usage

### Basic Syntax

```bash
python3 docker_snapshot.py [OPTIONS] [PATTERNS...]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `PATTERNS` | Optional space-separated container name substrings (case-insensitive). If omitted, all running containers are processed. |

### Options

| Option | Description |
|--------|-------------|
| `-h, --help` | Show help message and exit. |
| `-o, --output PATH` | Write all containers to a single script at PATH. Mutually exclusive with `--per-container-dir`. |
| `--per-container-dir DIR` | Directory for per-container scripts (default: `recreate_containers.d`). Individual files are named `<container_name>.sh`. |
| `--include-cmd` | Include the container's entrypoint command (Cmd) in the recreated run line. Omitted by default. |
| `--add-label KEY=VALUE` | Add a label to containers if that label key (after `{{name}}` substitution) is not already present. Supports `{{name}}` in both key and value. Repeatable. |
| `--add-env KEY=VALUE` | Add an environment variable to containers if that variable is not already present. Supports `{{name}}` in value. Repeatable. |
| `--add-restart POLICY` | Apply a restart policy to containers that have none set. Examples: `unless-stopped`, `on-failure:3`. |
| `--add-network NETWORK` | Apply a network to containers using the default/bridge network. Only affects default networks; custom networks are preserved. Examples: `home`, `docker_default`. |

## Examples

### 1. Default: Generate Per-Container Scripts

```bash
python3 docker_snapshot.py
```

Generates individual scripts in `recreate_containers.d/` for each running container:
- `recreate_containers.d/openhab.sh`
- `recreate_containers.d/influxdb.sh`
- etc.

If scripts already exist, you'll be prompted to overwrite or skip.

### 2. Filter by Container Name

```bash
python3 docker_snapshot.py openhab influx
```

Only processes containers with "openhab" or "influx" in their names.

### 3. Add Labels and Environment Variables

```bash
python3 docker_snapshot.py \
  --add-label watchtower.enable=true \
  --add-env TZ=Europe/Amsterdam
```

Adds the label and env var to all containers (if not already present).

### 4. Use {{name}} Placeholder in Labels

```bash
python3 docker_snapshot.py \
  --add-label homepage.name='{{name}}' \
  --add-label homepage.icon='{{name}}.png'
```

For a container named `openhab`:
- `homepage.name=openhab`
- `homepage.icon=openhab.png`

### 5. Complex Traefik Configuration with Backticks

```bash
python3 docker_snapshot.py \
  --add-label 'traefik.http.routers.{{name}}.entrypoints=websecure' \
  --add-label 'traefik.http.routers.{{name}}.rule=Host(`{{name}}.verpaalen.com`)'
```

For a container named `vaultwarden`:
```dockerfile
--label traefik.http.routers.vaultwarden.entrypoints=websecure
--label 'traefik.http.routers.vaultwarden.rule=Host(`vaultwarden.verpaalen.com`)'
```

(Use single quotes to protect backticks from shell interpretation.)

### 6. Set Default Network and Restart Policy

```bash
python3 docker_snapshot.py \
  --add-network home \
  --add-restart unless-stopped
```

Applies a network and restart policy to containers missing them.

### 7. Generate Single Combined Script

```bash
python3 docker_snapshot.py -o recreate_containers.sh
```

Outputs all container run commands into a single executable script.

### 8. Include Container Commands

```bash
python3 docker_snapshot.py --include-cmd
```

Adds the container's original `Cmd` to the reconstructed run line (default: omitted).

### 9. Process Only openhab with Full Configuration

```bash
python3 docker_snapshot.py openhab \
  --add-label com.centurylinklabs.watchtower.enable=true \
  --add-label traefik.enable=true \
  --add-env TZ=Europe/Amsterdam \
  --add-restart unless-stopped \
  --add-network home
```

## Output Format

Each generated script contains:

```bash
#!/bin/bash

# Container: <name>
docker run \
  --name <name> \
  --network <network> \
  --restart <policy> \
  -p <ports> \
  -v <mounts> \
  --device <devices> \
  --cap-add <capabilities> \
  --sysctl <sysctls> \
  -e <environment_vars> \
  --label <labels> \
  <image:tag> \
  [<cmd_args>]
```

- Arguments are multiline for readability.
- All values are shell-quoted to handle spaces and special characters.
- Duplicate port mappings are removed.
- Standard labels and env vars are filtered out.
- Non-standard capabilities only.

## Filtered Content

To keep output clean and focused, the tool automatically excludes:

### Environment Variables
- `PATH`, `HOSTNAME`, `TERM`, `HOME`, `PWD`
- Any prefixed with `DOCKER_`

### Labels
- Prefix: `org.opencontainers.*`
- Keys: `maintainer`, `build_version`

### Capabilities
- Default Linux capabilities (NET_RAW, CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, MKNOD, NET_BIND_SERVICE, SETFCAP, SETGID, SETPCAP, SETUID, SYS_CHROOT)
- Only non-standard capabilities are included.

## Idempotency

All additions (labels, env vars, restart policy, network) are idempotent:
- Labels are added only if the rendered key (after `{{name}}` substitution) does not already exist.
- Environment variables are added only if the key is not already present.
- Restart policy is applied only if the container has none.
- Network is applied only if the container uses default/bridge.

## Docker Socket Access

The script requires access to the Docker daemon, typically via `/var/run/docker.sock`. Ensure your user is in the `docker` group or has appropriate permissions:

```bash
# Add current user to docker group (requires sudo)
sudo usermod -aG docker $USER

# Then log out and back in, or:
newgrp docker
```

## Troubleshooting

### "Error connecting to Docker daemon"
- Ensure Docker is running: `docker ps`
- Check permissions on `/var/run/docker.sock`
- Add your user to the docker group (see above)

### No containers found
- Ensure containers are running: `docker ps`
- Check your filter pattern; filtering is case-insensitive but must match part of the container name

### Shell escaping issues with special characters
- Use single quotes for CLI arguments to prevent shell interpretation:
  ```bash
  --add-label 'traefik.http.routers.{{name}}.rule=Host(`{{name}}.example.com`)'
  ```

## Advanced Examples

### Mass-label containers for monitoring

```bash
python3 docker_snapshot.py \
  --add-label monitoring.enabled=true \
  --add-label monitoring.interval=30s
```

### Prepare migration scripts

Generate scripts for all containers and review before migrating to another host:

```bash
python3 docker_snapshot.py
# Review generated scripts in recreate_containers.d/
# Copy to new host and execute
scp -r recreate_containers.d/ user@newhost:/tmp/
```

### Document current state with metadata

```bash
python3 docker_snapshot.py \
  --add-label snapshot.date='2026-01-06' \
  --add-label snapshot.host='production'
```

## Performance Notes

- Large numbers of containers (100+) may take a few seconds as the script inspects each one.
- Network and mount operations are performed locally without remote calls.

## License

Copyright © 2026 Marcel Verpaalen

## Contributing

Feel free to extend or modify the script for your use case. Common enhancements:
- Support for `--cap-drop`
- Environment variable filtering rules
- Output to Compose YAML format
- Integration with CI/CD pipelines

---

For bugs, feature requests, or questions, review the script's inline documentation and the `--help` output.
