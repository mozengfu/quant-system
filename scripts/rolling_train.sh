#!/bin/bash
# V11.0 滚动训练脚本（每周运行一次）
# 用最新数据重训模型，保持模型对当前市场的适应性
# 安装到 crontab：0 3 * * 6

QUANT_DIR=/Users/mozengfu/workspace/quant-system
PYTHON3=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
LOG=$QUANT_DIR/logs/rolling_train.log

echo "===== V11.0 滚动训练开始 $(date) =====" >> $LOG 2>&1

cd $QUANT_DIR

# 用最新数据重训 V11.0（不限制 max_date，使用全部可用数据）
$PYTHON3 ml_train_v11_0.py --output data/ml_stock_model_v11_0.pkl >> $LOG 2>&1

# 训练成功后才覆盖 OOS 备份
if [ $? -eq 0 ]; then
    cp data/ml_stock_model_v11_0.pkl data/ml_stock_model_v11_0_oos.pkl
    echo "训练成功，已部署" >> $LOG 2>&1
else
    echo "训练失败，保留旧模型" >> $LOG 2>&1
fi

echo "===== V11.0 滚动训练结束 $(date) =====" >> $LOG 2>&1
