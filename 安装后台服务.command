#!/bin/zsh
cd "${0:A:h}"
./.venv/bin/python scripts/manage.py install
read -k 1 '?按任意键关闭…'
