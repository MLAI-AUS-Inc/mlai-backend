#!/usr/bin/env bash
set -euo pipefail

# Run on the origin as root. Only this owned vhost is changed; Nginx reloads
# its workers gracefully and retains the previous config if a reload fails.
operation="${1:-}"
target="${2:-}"
config="${MLAI_API_NGINX_CONFIG_PATH:-/etc/nginx/conf.d/mlai-backend-api.conf}"
template="${MLAI_API_NGINX_TEMPLATE_PATH:-$(dirname "$0")/nginx-api.conf.template}"
nginx_bin="${MLAI_API_NGINX_BIN:-nginx}"
listen_port="${MLAI_API_NGINX_LISTEN_PORT:-80}"
real_ip_config="${MLAI_API_REAL_IP_CONFIG_PATH:-/etc/nginx/cloudflare-real-ip.conf}"

case "$target" in
    web) port="${MLAI_API_WEB_PORT:-8001}" ;;
    candidate) port="${MLAI_API_CANDIDATE_PORT:-8002}" ;;
    "") port= ;;
    *) echo "Unknown web target: $target" >&2; exit 2 ;;
esac
ports=("$listen_port")
[ -z "$port" ] || ports+=("$port")
for number in "${ports[@]}"; do
    if ! [[ "$number" =~ ^[0-9]+$ ]] || [ "$number" -lt 1 ] || [ "$number" -gt 65535 ]; then
        echo "Invalid Nginx or upstream port" >&2
        exit 2
    fi
done

case "$operation" in
    validate|switch)
        [ -n "$port" ] || { echo "Target required" >&2; exit 2; }
        ;;
    remove)
        [ -z "$target" ] || { echo "remove takes no target" >&2; exit 2; }
        ;;
    *) echo "Usage: $0 validate|switch [web|candidate], or remove" >&2; exit 2 ;;
esac

if [ -e "$config" ] && ! grep -q '^# managed-mlai-backend-api target=' "$config"; then
    echo "Refusing to change an unmanaged Nginx vhost: $config" >&2
    exit 1
fi

render() {
    sed -e "s/@TARGET@/$target/g" -e "s/@PORT@/$port/g" \
        -e "s/@LISTEN_PORT@/$listen_port/g" \
        -e "s|@REAL_IP_CONFIG@|$real_ip_config|g" "$template"
}

if [ "$operation" = validate ]; then
    # A shadow master config checks syntax without adding a live port-80
    # listener while the old Docker container still owns that port.
    rendered="$(mktemp /tmp/mlai-api-vhost.XXXXXX)"
    shadow="$(mktemp /tmp/mlai-api-master.XXXXXX)"
    trap 'rm -f "$rendered" "$shadow"' EXIT
    render > "$rendered"
    printf 'pid "%s.pid";\nerror_log stderr notice;\nevents { worker_connections 32; }\nhttp { include "%s"; }\n' \
        "$shadow" "$rendered" > "$shadow"
    "$nginx_bin" -t -c "$shadow"
    exit
fi

backup="$(mktemp "${config}.previous.XXXXXX")"
temporary="$(mktemp "${config}.next.XXXXXX")"
had_previous=0
if [ -e "$config" ]; then
    cp -p "$config" "$backup"
    had_previous=1
fi
restore_previous() {
    if [ "$had_previous" = 1 ]; then
        cp -p "$backup" "$config"
    else
        rm -f "$config"
    fi
    "$nginx_bin" -t >/dev/null 2>&1 && "$nginx_bin" -s reload >/dev/null 2>&1 || true
}
cleanup() { rm -f "$backup" "$temporary"; }
trap cleanup EXIT

if [ "$operation" = remove ]; then
    rm -f "$config"
else
    render > "$temporary"
    chmod 644 "$temporary"
    mv "$temporary" "$config"
fi

if ! "$nginx_bin" -t; then
    restore_previous
    exit 1
fi
if ! "$nginx_bin" -s reload; then
    restore_previous
    exit 1
fi
