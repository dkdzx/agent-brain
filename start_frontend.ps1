param(
    [string]$RuntimeRoot = (Join-Path $PSScriptRoot 'runtime'),
    [string]$HostAddress = '127.0.0.1',
    [int]$Port = 8766
)

$env:AGENT_BRAIN_RUNTIME_ROOT = $RuntimeRoot
python (Join-Path $PSScriptRoot 'src\workgroup_status_frontend.py') `
    --runtime-root $RuntimeRoot `
    --host $HostAddress `
    --port $Port
