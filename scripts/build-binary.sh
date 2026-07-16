#!/bin/sh
set -eu
python3 -m venv .build-venv
. .build-venv/bin/activate
pip install -r requirements-build.txt
pyinstaller --clean --noconfirm tvheadend-manager.spec
echo "构建完成：dist/tvheadend-manager"
