$ErrorActionPreference = "SilentlyContinue"

$root = $env:PLUGIN_ROOT
if ([String]::IsNullOrWhiteSpace($root)) {
    exit 0
}

$selectedRoot = $root
$runner = Join-Path $root "scripts\run_orchestrator_hook.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    $runner = $null
    $latest = [DateTime]::MinValue
    $parent = Split-Path -Parent $root
    if (Test-Path -LiteralPath $parent -PathType Container) {
        foreach ($directory in [IO.Directory]::EnumerateDirectories($parent)) {
            $candidate = Join-Path $directory "scripts\run_orchestrator_hook.ps1"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $modified = [IO.File]::GetLastWriteTimeUtc($candidate)
                if ($modified -gt $latest) {
                    $latest = $modified
                    $runner = $candidate
                }
            }
        }
    }
    if ($null -ne $runner) {
        $selectedRoot = Split-Path -Parent (Split-Path -Parent $runner)
    }
}

if ($null -ne $runner) {
    $env:PLUGIN_ROOT = $selectedRoot
    & $runner
}

exit 0
