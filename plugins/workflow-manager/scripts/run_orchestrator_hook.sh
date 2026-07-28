#!/bin/sh
set -eu

source_file="$PLUGIN_ROOT/scripts/orchestrator_hook.py"

run_direct() {
    exec python3 "$source_file"
}

[ -f "$source_file" ] || exit 0
user_id="$(id -u 2>/dev/null)" || run_direct
case "$user_id" in
    ''|*[!0-9]*) run_direct ;;
esac

runtime_root="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
cache_root="$runtime_root/codex-workflow-manager-$user_id"
umask 077

if [ -L "$cache_root" ] || { [ -e "$cache_root" ] && [ ! -d "$cache_root" ]; }; then
    run_direct
fi
if [ ! -d "$cache_root" ]; then
    mkdir "$cache_root" 2>/dev/null || run_direct
fi
cache_owner="$(ls -nd "$cache_root" 2>/dev/null | awk '{print $3}')" || run_direct
[ "$cache_owner" = "$user_id" ] || run_direct
chmod 700 "$cache_root" 2>/dev/null || run_direct

# Content addressing prevents stale execution even when an update preserves mtime.
cache_key="$(cksum "$source_file" 2>/dev/null | awk '{print $1 "-" $2}')" || run_direct
[ -n "$cache_key" ] || run_direct
cache_dir="$cache_root/$cache_key"
cached_file="$cache_dir/orchestrator_hook.py"

if [ -L "$cache_dir" ] || { [ -e "$cache_dir" ] && [ ! -d "$cache_dir" ]; }; then
    run_direct
fi
if [ ! -d "$cache_dir" ]; then
    mkdir "$cache_dir" 2>/dev/null || run_direct
fi
cache_dir_owner="$(ls -nd "$cache_dir" 2>/dev/null | awk '{print $3}')" || run_direct
[ "$cache_dir_owner" = "$user_id" ] || run_direct
chmod 700 "$cache_dir" 2>/dev/null || run_direct

if [ -L "$cached_file" ]; then
    run_direct
fi
if [ ! -f "$cached_file" ]; then
    temporary="$cache_dir/.orchestrator_hook.py.$$"
    trap 'rm -f "$temporary"' EXIT HUP INT TERM
    cp "$source_file" "$temporary" 2>/dev/null || run_direct
    chmod 600 "$temporary" 2>/dev/null || run_direct
    mv "$temporary" "$cached_file" 2>/dev/null || run_direct
    trap - EXIT HUP INT TERM
fi
cached_owner="$(ls -nd "$cached_file" 2>/dev/null | awk '{print $3}')" || run_direct
[ "$cached_owner" = "$user_id" ] || run_direct
chmod 600 "$cached_file" 2>/dev/null || run_direct

exec python3 "$cached_file"
