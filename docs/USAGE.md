# Dalboplex Usage Guide

This guide covers installation, deployment, and day-to-day management of the Dalboplex infrastructure.

## Getting Started

### Prerequisites

- TrueNAS Scale 24.10+ (or any Docker host)
- Python 3.11+
- Domain with DNS API access (for Let's Encrypt)

### Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd dalboplex
   ```

2. **Configure secrets**:
   ```bash
   cp apps/.secrets.yml.example apps/.secrets.yml
   # Edit apps/.secrets.yml with your actual API keys
   ```

3. **Configure TrueNAS connection**:
   ```bash
   ./dalboplex.py login \
     --host https://truenas.local \
     --api-key <your-api-key>
   ```

4. **Render compose files**:
   ```bash
   # Render a single file
   ./dalboplex.py render apps/core.yml

   # Render all files
   ./dalboplex.py render --all
   ```

5. **Deploy to TrueNAS**:
   ```bash
   # Deploy a specific app
   ./dalboplex.py deploy apps/core.yml

   # Check status
   ./dalboplex.py status
   ```

## Management

### Common Operations

#### Render Templates
```bash
# Render single file with secrets
./dalboplex.py render apps/web.yml

# Render all files
./dalboplex.py render --all

# Render with redacted secrets
./dalboplex.py render apps/web.yml --redacted
```

#### Deploy to TrueNAS
```bash
# Deploy an app
./dalboplex.py update apps/plex.yml

# Deploy with dry-run to preview changes
./dalboplex.py update apps/plex.yml --dry-run

# Force deployment even without changes
./dalboplex.py update apps/plex.yml --force

# Check status of all apps
./dalboplex.py status
```

### Configuration Files

| File | Purpose |
|------|---------|
| `apps/.config.yml` | Common settings, volumes, label templates |
| `apps/.secrets.yml` | API keys and tokens (gitignored) |
| `apps/.state/` | Deployment state and rendered configs |
| `~/.config/dalboplex/truenas.yml` | TrueNAS credentials |

### Template System

Compose files use Jinja2 templates and custom `x-features`:

```yaml
services:
  radarr:
    x-features:
      - domain: movies              # Generates Traefik labels
      - homepage:                   # Generates Homepage labels
          group: Media
          description: Movie manager
      - widget:                     # Generates Homepage widget
          type: radarr
          key: $RADARR_API_KEY
```

### Volume Path Resolution

The `.config.yml` defines path templates:

```yaml
volume_templates:
  "@config": /mnt/nvme/apps/{container}/config
  "@data": /mnt/nvme/apps/{container}/data
  "@media": /mnt/hdd/media
  "@downloads": /mnt/scratch/downloads
```

Used in compose files:
```yaml
volumes:
  - "@config:/config"              # → /mnt/nvme/apps/radarr/config:/config
  - "@media:/media"                # → /mnt/hdd/media:/media
```

## Advanced Topics

### Custom Label Templates

The `.config.yml` file defines reusable label templates that get expanded during rendering:

```yaml
label_templates:
  domain:
    args: [subdomain]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.{container}.rule=Host(`{subdomain}.dalboplex.ch`)"
      - "traefik.http.routers.{container}.middlewares=oauth2-proxy"
```

### Secret Management

Secrets are stored in `apps/.secrets.yml` and referenced in compose files using `$VARIABLE_NAME` syntax:

```yaml
environment:
  - RADARR_API_KEY=$RADARR_API_KEY
```

During rendering, these are replaced with actual values.

### State Management

The `.state/` directory tracks:
- **state.yml**: Deployment metadata and configuration hashes
- **rendered/**: Rendered compose files without secrets
- **installed/**: Last deployed configuration with secrets

### Troubleshooting

#### Connection Issues

If TrueNAS connection fails:
```bash
# Test connection
./dalboplex.py login --host https://truenas.local --api-key <key>

# Check stored credentials
cat ~/.config/dalboplex/truenas.yml
```

#### Render Errors

If template rendering fails:
```bash
# Check configuration syntax
cat apps/.config.yml

# Verify secrets file exists
ls apps/.secrets.yml
```

#### Deployment Failures

If deployment fails:
```bash
# Check current status
./dalboplex.py status

# View detailed error in TrueNAS UI:
# Apps > Discover Apps > Custom App > View Logs
```
