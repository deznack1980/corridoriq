# Intended production path for the public CorridorIQ portal.
# Sets CORRIDORIQ_ENV=production explicitly so an omitted/unknown environment
# cannot silently run with development security behavior.
#
# Does not enable billing, configure a real email provider, write secrets,
# or change Cloudflare/DNS. Proxy secret and other production values must
# already be present in the process environment. The Python process then
# fail-closes if production identity config is incomplete.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$env:CORRIDORIQ_ENV = "production"
$env:PYTHONPATH = $Root

if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m pipeline.api.server
} else {
  python -m pipeline.api.server
}
