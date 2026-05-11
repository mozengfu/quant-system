#!/bin/bash
# 量化系统 - 从服务器同步到本地
# 用途：服务器 → 本地同步，服务器有问题时本地可恢复

DATE=$(date '+%Y-%m-%d %H:%M:%S')
LOG="/Users/mozengfu/workspace/quant-system/logs/sync.log"

echo "[$DATE] 开始同步..." >> $LOG

# 同步 app.py
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  root@8.148.158.153:/opt/quant-system/app.py \
  /Users/mozengfu/workspace/quant-system/app.py 2>> $LOG

# 同步 templates
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r root@8.148.158.153:/opt/quant-system/templates/. \
  /Users/mozengfu/workspace/quant-system/templates/ 2>> $LOG

# 同步 static
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r root@8.148.158.153:/opt/quant-system/static/. \
  /Users/mozengfu/workspace/quant-system/static/ 2>> $LOG

echo "[$DATE] 同步完成" >> $LOG
