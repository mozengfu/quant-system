#!/bin/bash
# 量化系统 - 从本地上传到服务器
# 用途：本地修改后推送到服务器
# 注意：不覆盖 .env 和 data/*.json（服务器本地数据）

DATE=$(date '+%Y-%m-%d %H:%M:%S')
LOG="/Users/mozengfu/workspace/quant-system/logs/sync.log"

echo "[$DATE] 开始上传到服务器..." >> $LOG

# 上传入口文件
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  /Users/mozengfu/workspace/quant-system/app.py \
  root@8.148.158.153:/opt/quant-system/app.py 2>> $LOG

# 上传 quant_app 包（路由、工具、服务）
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r /Users/mozengfu/workspace/quant-system/quant_app/. \
  root@8.148.158.153:/opt/quant-system/quant_app/ 2>> $LOG

# 上传根目录 Python 脚本（ML、行情、策略）
for f in /Users/mozengfu/workspace/quant-system/*.py; do
  [ -f "$f" ] && scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
    "$f" root@8.148.158.153:/opt/quant-system/ 2>> $LOG
done

# 上传 scripts（定时任务、回测、工具）
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r /Users/mozengfu/workspace/quant-system/scripts/. \
  root@8.148.158.153:/opt/quant-system/scripts/ 2>> $LOG

# 上传 templates
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r /Users/mozengfu/workspace/quant-system/templates/. \
  root@8.148.158.153:/opt/quant-system/templates/ 2>> $LOG

# 上传 frontend/dist
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no   -r /Users/mozengfu/workspace/quant-system/frontend/dist/.   root@8.148.158.153:/opt/quant-system/frontend/dist/ 2>> $LOG

# 上传 static
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  -r /Users/mozengfu/workspace/quant-system/static/. \
  root@8.148.158.153:/opt/quant-system/static/ 2>> $LOG

# 上传 requirements.txt（可选，服务器已装依赖）
scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  /Users/mozengfu/workspace/quant-system/requirements.txt \
  root@8.148.158.153:/opt/quant-system/requirements.txt 2>> $LOG

# 重启服务器服务
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no \
  root@8.148.158.153 "pkill -f 'python3 app.py'; sleep 1; cd /opt/quant-system && nohup python3 app.py > logs/app.log 2>&1 &" 2>> $LOG

echo "[$DATE] 上传完成，服务已重启" >> $LOG
