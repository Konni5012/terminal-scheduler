#!/bin/sh
set -eu

install_prefix=${PREFIX:-"$HOME/.local"}
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="$install_prefix/bin"

mkdir -p "$destination"
install -m 755 "$source_dir/schedule" "$destination/schedule"

echo "Installed schedule to $destination/schedule"
case ":$PATH:" in
    *":$destination:"*) ;;
    *)
        echo "Add it to Fish's PATH with:"
        echo "  fish_add_path $destination"
        ;;
esac
