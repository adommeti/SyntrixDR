# Self-hosted CI runner

`ci.yml` targets `runs-on: [self-hosted, linux, syntrixdr-linux]` (runner `ghlinux01-syntrixdr`, Linux x64).

## One-time host preparation

```bash
sudo bash infra/runner/setup-runner.sh <runner-service-user>     # the account the runner service runs as
# then restart the runner service (from the runner's install directory) so the docker group membership applies
cd <runner-dir> && sudo ./svc.sh stop && sudo ./svc.sh start
sudo -u <runner-service-user> docker info >/dev/null && echo "docker ok"
```

What the workflow assumes about the host (all provided by the script):

- Docker Engine + compose plugin, and the runner user in the `docker` group — needed for job `services:` (pgvector Postgres, Redis),
  testcontainers in pytest, and the e2e compose stack.
- No `sudo` in workflows. Tools install to `$GITHUB_WORKSPACE/../_tools/bin` — i.e. `<runner-dir>/_work/SyntrixDR/_tools/bin`
  (semgrep, bandit, pip-audit, checkov via `uv tool`; syft, grype, gitleaks binaries) and are reused across runs; Playwright
  browsers go to `../_tools/pw-browsers`; Chromium's system libraries come from the script.
- Host ports: service containers publish on **55432** (Postgres) and **56379** (Redis) so a developer stack on the same host never
  collides; the e2e job runs the repo compose file under project name `syntrixdr-ci` and tears it down with `-v`.
- Outbound HTTPS to github.com, pypi.org, registry.npmjs.org, astral.sh, ghcr.io/docker.io/mcr.microsoft.com (images) and the
  scanner download hosts.

## Operational notes

- One runner ⇒ jobs execute sequentially; `concurrency` cancels superseded runs of the same ref.
- The workspace persists between runs; every job checks out with `clean: true`. If a run leaves containers behind:
  `docker ps -a --filter label=com.docker.compose.project=syntrixdr-ci -q | xargs -r docker rm -f`.
- Keep the runner on a private network; the repository is private and pull requests from forks are not accepted.
- Disk hygiene (monthly): `docker system prune -af --volumes` when no job is running; clear `../_tools` to force fresh scanners.
