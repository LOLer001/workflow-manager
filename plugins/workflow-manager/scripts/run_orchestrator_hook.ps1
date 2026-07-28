$ErrorActionPreference = "SilentlyContinue"

$hookScript = "$($env:PLUGIN_ROOT)\scripts\orchestrator_hook.py"
if (-not (Test-Path -LiteralPath $hookScript -PathType Leaf)) {
    exit 0
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    & $launcher.Source -3 $hookScript
    exit 0
}

$launcher = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    & $launcher.Source $hookScript
    exit 0
}

exit 0
