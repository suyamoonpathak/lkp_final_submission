#!/bin/bash
# Compile all C helpers used by the C3 and C4 benchmarks.
set -euo pipefail
cd "$(dirname "$0")"

gcc -O2 -Wall -Wextra -o xattr_fsync_helper xattr_fsync_helper.c
gcc -O2 -Wall -Wextra -o fallocate_range_helper fallocate_range_helper.c
gcc -O2 -Wall -Wextra -o xattr_block_fsync_helper xattr_block_fsync_helper.c
gcc -O2 -Wall -Wextra -o concurrent_fsync_helper concurrent_fsync_helper.c -lpthread

echo "Built:"
ls -lh xattr_fsync_helper fallocate_range_helper xattr_block_fsync_helper concurrent_fsync_helper
