#!/bin/sh
set -eu

install_prefix=${PREFIX:-"$HOME/.local"}
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="$install_prefix/bin"

mkdir -p "$destination"
install -m 755 "$source_dir/schedule-core" "$destination/schedule-core"
install -m 644 "$source_dir/schedule_supervisor.py" "$destination/schedule_supervisor.py"
install -m 644 "$source_dir/schedule_update.py" "$destination/schedule_update.py"
install -m 755 "$source_dir/schedule" "$destination/schedule"

echo "Installed schedule to $destination/schedule"
case ":$PATH:" in
    *":$destination:"*) ;;
    *)
        echo "Add it to Fish's PATH with:"
        echo "  fish_add_path $destination"
        ;;
esac
