$ErrorActionPreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"

$root = $env:PLUGIN_ROOT
$runner = $null
try {
    if (-not [String]::IsNullOrWhiteSpace($root)) {
        $candidate = [IO.Path]::Combine($root, "scripts", "run_orchestrator_hook.ps1")
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $runner = $candidate
        }
    }
} catch {
    $runner = $null
}

if ($null -eq $runner) {
    if ($env:TOKEN_FRUGAL_DEBUG -eq "1") {
        [Console]::Error.WriteLine("workflow_manager_hook: runner_missing")
    }
    exit 0
}

& $runner
exit 0
