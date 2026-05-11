#!/bin/bash
# 量化系统一键部署脚本
# 在阿里云服务器上执行: bash setup.sh

set -e

echo "=========================================="
echo "🚀 智能量化系统部署脚本"
echo "=========================================="

# 配置
INSTALL_DIR="/opt/quant-system"
PORT=5001

echo "📦 步骤1: 更新系统"
apt-get update -qq

echo "🐍 步骤2: 安装Python和依赖"
apt-get install -y -qq python3 python3-pip curl unzip

echo "📁 步骤3: 创建安装目录"
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

echo "📥 步骤4: 下载代码"
# 从GitHub或您的Mac下载
# 假设代码已经上传到某个位置

# 创建基本目录结构
mkdir -p templates static data logs scripts

echo "📝 步骤5: 创建requirements.txt"
cat > requirements.txt << 'EOF'
fastapi==0.115.0
uvicorn[standard]==0.30.0
jinja2==3.1.4
python-multipart==0.0.9
EOF

echo "📦 步骤6: 安装Python依赖"
pip3 install -q -r requirements.txt
pip3 install -q tushare pandas

echo "⚠️ 步骤7: 请上传代码文件"
echo ""
echo "请在本地Mac上执行以下命令上传代码："
echo ""
echo "scp /Users/mozengfu/workspace/quant-system/app.py root@8.148.158.153:$INSTALL_DIR/"
echo "scp -r /Users/mozengfu/workspace/quant-system/templates root@8.148.158.153:$INSTALL_DIR/"
echo "scp -r /Users/mozengfu/workspace/quant-system/static root@8.148.158.153:$INSTALL_DIR/"
echo "scp -r /Users/mozengfu/workspace/quant-system/data root@8.148.158.153:$INSTALL_DIR/"
echo ""
echo "上传完成后，按回车键继续..."
read

echo "🚀 步骤8: 启动服务"
cd $INSTALL_DIR
pkill -f "python3 app.py" 2>/dev/null || true
nohup python3 app.py > logs/app.log 2>&1 &
sleep 3

echo "🔍 步骤9: 检查服务状态"
if lsof -i :$PORT | grep LISTEN; then
    echo "✅ 服务启动成功！"
    echo ""
    echo "=========================================="
    echo "🎉 部署完成！"
    echo "🌐 访问地址: http://8.148.158.153:$PORT/"
    echo "=========================================="
else
    echo "❌ 服务启动失败，请检查日志:"
    tail -20 logs/app.log
fi

echo ""
echo "📋 常用命令："
echo "  查看日志: tail -f $INSTALL_DIR/logs/app.log"
echo "  重启服务: pkill -f 'python3 app.py' && cd $INSTALL_DIR && nohup python3 app.py > logs/app.log 2>&1 &"
echo "  停止服务: pkill -f 'python3 app.py'"
