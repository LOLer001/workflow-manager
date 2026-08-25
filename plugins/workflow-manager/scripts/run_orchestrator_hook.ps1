$ErrorActionPreference = "SilentlyContinue"
$env:PYTHONDONTWRITEBYTECODE = "1"

$hookScript = "$($env:PLUGIN_ROOT)\scripts\orchestrator_hook.py"
if (-not (Test-Path -LiteralPath $hookScript -PathType Leaf)) {
    exit 0
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    $env:WORKFLOW_MANAGER_RUNNER_KIND = "windows_py"
    & $launcher.Source -3 -B $hookScript
    exit 0
}

$launcher = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    $env:WORKFLOW_MANAGER_RUNNER_KIND = "windows_python"
    & $launcher.Source -B $hookScript
    exit 0
}

exit 0
