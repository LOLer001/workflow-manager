#!/bin/sh
set -eu

source_file="$PLUGIN_ROOT/scripts/orchestrator_hook.py"

run_direct() {
    exec env PYTHONDONTWRITEBYTECODE=1 python3 -B "$source_file"
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

# SHA-256 content addressing prevents stale execution even when an update
# preserves mtime, and lets us verify the cached bytes before every execution.
cache_key="$(sha256sum "$source_file" 2>/dev/null | awk '{print $1}')" || run_direct
case "$cache_key" in
    *[!0-9a-f]*) run_direct ;;
esac
[ "${#cache_key}" -eq 64 ] || run_direct
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
cached_digest=""
if [ -f "$cached_file" ]; then
    cached_digest="$(sha256sum "$cached_file" 2>/dev/null | awk '{print $1}')" || cached_digest=""
fi
if [ "$cached_digest" != "$cache_key" ]; then
    temporary="$cache_dir/.orchestrator_hook.py.$$"
    trap 'rm -f "$temporary"' EXIT HUP INT TERM
    cp "$source_file" "$temporary" 2>/dev/null || run_direct
    temporary_digest="$(sha256sum "$temporary" 2>/dev/null | awk '{print $1}')" || run_direct
    [ "$temporary_digest" = "$cache_key" ] || run_direct
    chmod 600 "$temporary" 2>/dev/null || run_direct
    mv "$temporary" "$cached_file" 2>/dev/null || run_direct
    trap - EXIT HUP INT TERM
fi
cached_owner="$(ls -nd "$cached_file" 2>/dev/null | awk '{print $3}')" || run_direct
[ "$cached_owner" = "$user_id" ] || run_direct
chmod 600 "$cached_file" 2>/dev/null || run_direct

# The private runtime cache is content addressed, so every other well-formed key
# is obsolete once the current file is verified. Keep unknown entries untouched.
for old_cache_dir in "$cache_root"/*; do
    [ "$old_cache_dir" = "$cache_dir" ] && continue
    [ -d "$old_cache_dir" ] && [ ! -L "$old_cache_dir" ] || continue
    old_cache_name=${old_cache_dir##*/}
    case "$old_cache_name" in
        *[!0-9a-f]*) continue ;;
    esac
    [ "${#old_cache_name}" -eq 64 ] || continue
    old_cache_owner="$(ls -nd "$old_cache_dir" 2>/dev/null | awk '{print $3}')" || continue
    [ "$old_cache_owner" = "$user_id" ] || continue
    rm -rf -- "$old_cache_dir" 2>/dev/null || true
done

exec env PYTHONDONTWRITEBYTECODE=1 python3 -B "$cached_file"
