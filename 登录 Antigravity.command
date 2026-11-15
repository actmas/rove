#!/bin/zsh
cd "${0:A:h}"
"$HOME/.local/bin/agy"
read -k 1 '?按任意键关闭…'
