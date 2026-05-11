#!/bin/bash
# 检查是否需要同步（比较文件时间戳）

LOCAL_APP="/Users/mozengfu/workspace/quant-system/app.py"
SERVER_PATH="root@8.148.158.153:/opt/quant-system/app.py"
LOG="/Users/mozengfu/workspace/quant-system/logs/sync.log"

# 获取服务器文件修改时间
SERVER_TIME=$(ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no $SERVER_PATH stat -c %Y 2>/dev/null)
LOCAL_TIME=$(stat -f %m "$LOCAL_APP" 2>/dev/null)

if [ -n "$SERVER_TIME" ] && [ "$SERVER_TIME" -gt "$LOCAL_TIME" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 服务器有新版本，开始同步" >> $LOG
    /Users/mozengfu/workspace/quant-system/sync-from-server.sh
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 无需同步" >> $LOG
fi
