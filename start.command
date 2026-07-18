#!/bin/bash
cd "$(dirname "$0")"

# conda環境 "labelme" があれば優先して使う（無ければ system の python3 にフォールバック）
for CONDA_BASE in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/opt/anaconda3" "$HOME/opt/miniconda3" "/opt/anaconda3" "/opt/miniconda3"; do
    if [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
        source "$CONDA_BASE/etc/profile.d/conda.sh"
        if conda env list | grep -q "^labelme "; then
            conda activate labelme
        fi
        break
    fi
done

python3 main.py
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "アプリの起動中にエラーが発生しました（終了コード: $STATUS）。"
    read -p "Enterキーを押すと閉じます..."
fi
