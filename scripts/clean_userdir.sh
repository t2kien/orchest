#!/bin/bash

# This script restores the userdir to the state in the git repository
# (of the latest local commit).

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

echo "[Cleaning up userdir]: ..."

# Removes the userdir and then restores the structure imposed on the
# userdir by the git repository. Restoring all defaults such as
# .gitignore files.
sudo rm -rf $DIR/../userdir
git checkout $DIR/../userdir

echo "[Cleaning up userdir]: success"

echo "Setting the right permissions on the userdir..."
chmod g+s $DIR/../userdir
