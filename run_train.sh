#!/bin/bash
cd /Users/mozengfu/workspace/quant-system
python3 -u ml_train_v11_0.py > logs/train_v11_1.log 2>&1
echo "DONE" >> logs/train_v11_1.log
