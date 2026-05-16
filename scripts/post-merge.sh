#!/bin/bash
set -e

pip install -r requirements.txt -q

git push https://${GITHUB_TOKEN}@github.com/jianran/object-souls.git HEAD:main
