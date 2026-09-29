# mist-exporter

A [Prometheus](https://prometheus.io/) exporter for [Juniper Mist](https://www.mist.com/) cloud-managed
Wi-Fi and wired infrastructure. Exposes AP, switch, and client metrics for scraping by Prometheus / VictoriaMetrics.

## Metrics

The exporter collects, per Mist organization:

- **Access Point** status, uptime, client counts, radio/interface stats
- **Switch** status, port state, PoE, temperature, and wired client counts
- **Wireless client** signal, throughput, and idle time
- **Wired client** counts per switch port

## Configuration

The exporter is configured entirely via environment variables:

| Variable          | Required | Default                        | Description                                   |
|-------------------|----------|---------------------------------|------------------------------------------------|
| `MIST_TOKEN`      | yes      | —                                | Mist API token                                  |
| `MIST_ORG_ID`     | yes      | —                                | Mist organization ID                            |
| `MIST_API_BASE`   | no       | `https://api.mist.com/api/v1`  | Mist API base URL (region-specific clouds)      |
| `EXPORTER_PORT`   | no       | `9100`                          | Port the exporter listens on                    |
| `SCRAPE_INTERVAL` | no       | `60`                            | Idle loop interval in seconds (metrics are collected on each Prometheus scrape) |
| `LOG_LEVEL`       | no       | `INFO`                          | Python logging level                            |

## Running with Docker

```bash
docker run -d \
  -e MIST_TOKEN=your-token \
  -e MIST_ORG_ID=your-org-id \
  -p 9100:9100 \
  nirvanawgw/mist-exporter:latest
```

Then scrape `http://<host>:9100/metrics` from Prometheus.

## Running locally

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --no-dev
MIST_TOKEN=your-token MIST_ORG_ID=your-org-id uv run python exporter.py
```

## Development

```bash
uv sync --all-extras
uv run python -m py_compile exporter.py
```

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/); this is enforced
on pull requests via [gitlint](https://jorisroovers.com/gitlint/). Releases are automated with
[python-semantic-release](https://python-semantic-release.readthedocs.io/): every merge to `main` determines
the next version from commit history, tags the release, generates a changelog, publishes a GitHub Release,
and builds/pushes a multi-arch Docker image to Docker Hub.

## License

[MIT](LICENSE)
